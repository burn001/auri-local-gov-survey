"""
자가등록 후 응답 미제출자에게 reminder 메일을 일괄 발송한다.

대상 정의:
    `participants.source == 'self'` AND token 이 `responses.submitted_at != null`인
    토큰 집합에 포함되지 않은 사람.

필터 (`--filter`):
    all          미제출 자가등록자 전원 (default)
    never_visited  last_seen_at 없음 — 등록 직후 응답 페이지 진입 흔적 없음
    drop_off       last_seen_at - created_at >= --gap-minutes (default 5분) — 진입했다가 이탈

본문:
    `--body-file <path>` 로 HTML 본문 지정 가능. {{name}}, {{org}}, {{survey_url}}
    치환 지원 (admin custom-send와 동일 컨벤션). 미지정 시 backend invite 템플릿
    (`templates/survey_invite.html`) 재사용.

동작:
    각 발송마다 email_logs row insert. type='reminder', admin_name='reminder CLI',
    batch_id='reminder-YYYYMMDDTHHMMSSZ'. 실패한 row는 status='failed'로 기록되어
    추후 `retry_failed_emails.py`로 재시도 가능.

사용 예:
    # 미리보기
    python scripts/reminder_unsubmitted.py --dry-run

    # 도중 이탈자만, 1.5초 throttle
    python scripts/reminder_unsubmitted.py --filter drop_off --throttle 1.5

    # 본문 파일 + 제목 지정 (한도 회복 후 일괄)
    python scripts/reminder_unsubmitted.py \\
        --subject "[AURI 청사관리실태조사] 응답 미제출자 안내" \\
        --body-file /scripts/templates/reminder_v1.html
"""
import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from pymongo import MongoClient
from services.email_service import send_email, render_email
from config import get_settings


DEFAULT_SUBJECT = "[AURI 청사관리실태조사] 설문 응답 안내 — 미제출 알림"


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--filter", choices=["all", "never_visited", "drop_off"], default="all")
    ap.add_argument("--gap-minutes", type=int, default=5,
                    help="drop_off 판정 — last_seen_at이 created_at보다 이 시간 이상 늦을 때")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--throttle", type=float, default=0.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--subject", default=DEFAULT_SUBJECT)
    ap.add_argument("--body-file", help="HTML 본문 파일 경로 (미지정 시 invite 템플릿 재사용)")
    ap.add_argument("--uri", default=os.getenv("MONGODB_URI", "mongodb://localhost:27017"))
    ap.add_argument("--db", default=os.getenv("MONGODB_DB", "auri_local_gov"))
    return ap.parse_args()


def select_targets(db, filter_kind: str, gap_minutes: int) -> list[dict]:
    submitted_tokens = db.responses.distinct("token", {"submitted_at": {"$ne": None}})
    base_q = {
        "source": "self",
        "token": {"$nin": submitted_tokens},
    }
    cursor = db.participants.find(
        base_q,
        {
            "_id": 0, "token": 1, "email": 1, "name": 1, "org": 1, "category": 1,
            "created_at": 1, "last_seen_at": 1, "reward_name": 1,
        },
    )
    targets = list(cursor)

    if filter_kind == "never_visited":
        targets = [p for p in targets if not p.get("last_seen_at")]
    elif filter_kind == "drop_off":
        kept = []
        threshold_ms = gap_minutes * 60 * 1000
        for p in targets:
            seen = p.get("last_seen_at")
            created = p.get("created_at")
            if not seen or not created:
                continue
            if (seen - created).total_seconds() * 1000 >= threshold_ms:
                kept.append(p)
        targets = kept
    return targets


def render_body(participant: dict, base_url: str, body_template: str | None) -> str:
    name = participant.get("name") or participant.get("reward_name") or "응답자"
    org = participant.get("org", "")
    survey_url = f"{(base_url or '').rstrip('/')}/?token={participant['token']}"

    if body_template is not None:
        return (body_template
                .replace("{{name}}", name)
                .replace("{{org}}", org)
                .replace("{{survey_url}}", survey_url))
    return render_email(name, org, survey_url)


def main():
    args = parse_args()
    s = get_settings()
    if not s.GMAIL_USER or not s.GMAIL_APP_PASSWORD:
        print("[abort] GMAIL_USER / GMAIL_APP_PASSWORD 미설정 — backend .env 확인", file=sys.stderr)
        sys.exit(2)

    body_template = None
    if args.body_file:
        p = Path(args.body_file)
        if not p.exists():
            print(f"[abort] body file 없음: {p}", file=sys.stderr)
            sys.exit(2)
        body_template = p.read_text(encoding="utf-8")
        print(f"[body]  custom template ({len(body_template)}자) — {p}")
    else:
        print("[body]  invite 템플릿 재사용")

    client = MongoClient(args.uri)
    db = client[args.db]
    targets = select_targets(db, args.filter, args.gap_minutes)
    if args.limit:
        targets = targets[: args.limit]

    print(f"[filter] {args.filter}" + (f" (gap >= {args.gap_minutes}min)" if args.filter == "drop_off" else ""))
    print(f"[match]  {len(targets)}명")
    print(f"[mode]   {'DRY-RUN' if args.dry_run else 'SEND'}  base_url={s.SURVEY_BASE_URL}")
    print(f"[subject] {args.subject}")
    print()

    sent = failed = 0
    batch_id = "reminder-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    for i, p in enumerate(targets, 1):
        head = f"[{i:>3}/{len(targets)}] {p.get('category', '-'):<8} {(p.get('org') or '-')[:14]:<14} {p['email']}"
        if args.dry_run:
            seen = p.get("last_seen_at")
            mark = f"seen={seen.isoformat()[:16]}" if seen else "seen=NEVER"
            print(f"{head}  DRY  {mark}")
            continue
        html = render_body(p, s.SURVEY_BASE_URL, body_template)
        log_doc = {
            "batch_id": batch_id,
            "token": p["token"],
            "email": p["email"],
            "name": p.get("name", ""),
            "org": p.get("org", ""),
            "category": p.get("category", ""),
            "type": "reminder",
            "subject": args.subject,
            "admin_email": "system",
            "admin_name": "reminder CLI",
            "filter_kind": args.filter,
            "sent_at": datetime.now(timezone.utc),
        }
        try:
            send_email(p["email"], args.subject, html)
            log_doc.update({"status": "sent", "error": ""})
            sent += 1
            print(f"{head}  SENT")
        except Exception as e:
            err = str(e)
            log_doc.update({"status": "failed", "error": err})
            failed += 1
            print(f"{head}  FAIL: {err[:120]}")
        try:
            db.email_logs.insert_one(log_doc)
        except Exception as ex:
            print(f"  email_logs insert 실패: {ex}", file=sys.stderr)
        if args.throttle > 0:
            time.sleep(args.throttle)

    print(f"\n[done] sent={sent} failed={failed}  batch_id={batch_id}")
    if failed:
        print("       실패 항목은 본 CLI를 다시 실행해 재시도 (이미 응답 제출한 사람은 자동으로 대상에서 제외됨)")
    client.close()


if __name__ == "__main__":
    main()
