"""
email_logs.status=='failed' 항목을 type별로 본문 재구성해 재발송한다.

운영 시나리오 (`_STATUS.md` 5/6 참조):
    Gmail SMTP 일일 500건 한도 초과 등으로 발송이 누락된 메일을 한도 회복 후 일괄 재시도.
    원본 row는 그대로 두고, 새 row를 동일 schema로 insert해 시간순 이력을 보존한다.

지원 type:
    completion / recovery / invite — participants에서 token 조회 후 본문 재생성.
    milestone / custom / reminder — 본문이 동적이거나 admin 입력값이라 자동 재구성 불가 → 스킵.

DB 직접 접근. backend 컨테이너 내부에서 motor 대신 pymongo로 동기 처리한다 (ssh 터널 + 로컬
실행도 가능). 운영 패턴은 `comments_cli.py`와 동일하게 docker run --env-file 형태.

사용 예:
    # 미리 보기 (dry-run) — 발송 시도 X, 대상만 출력
    python scripts/retry_failed_emails.py --dry-run

    # type 한정
    python scripts/retry_failed_emails.py --type completion

    # 기간 한정 (UTC ISO yyyy-mm-dd)
    python scripts/retry_failed_emails.py --since 2026-05-04 --until 2026-05-05

    # 한도 회피용 throttle (초 단위 sleep)
    python scripts/retry_failed_emails.py --throttle 1.5 --limit 50
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from pymongo import MongoClient
from services.email_service import send_email, render_completion, render_email
from config import get_settings


SUPPORTED_TYPES = {"completion", "recovery", "invite"}
SKIP_TYPES = {"milestone", "custom", "reminder"}


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", choices=sorted(SUPPORTED_TYPES) + ["all"], default="all")
    ap.add_argument("--since", help="UTC ISO yyyy-mm-dd (sent_at 시작)")
    ap.add_argument("--until", help="UTC ISO yyyy-mm-dd (sent_at 끝, exclusive)")
    ap.add_argument("--limit", type=int, default=0, help="0=무제한")
    ap.add_argument("--throttle", type=float, default=0.0, help="발송 사이 sleep (초)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--uri", default=os.getenv("MONGODB_URI", "mongodb://localhost:27017"))
    ap.add_argument("--db", default=os.getenv("MONGODB_DB", "auri_local_gov"))
    return ap.parse_args()


def build_query(args) -> dict:
    q = {"status": "failed"}
    if args.type != "all":
        q["type"] = args.type
    else:
        q["type"] = {"$in": list(SUPPORTED_TYPES)}
    if args.since or args.until:
        rng = {}
        if args.since:
            rng["$gte"] = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
        if args.until:
            rng["$lt"] = datetime.fromisoformat(args.until).replace(tzinfo=timezone.utc)
        q["sent_at"] = rng
    return q


def reconstruct(log: dict, db, base_url: str):
    """type별 본문 재구성. participant 정보·token으로 link/subject 재생성.
    반환: (subject, html, error_or_None). error 있으면 스킵.
    """
    token = log.get("token")
    if not token:
        return None, None, "token 없음"
    participant = db.participants.find_one({"token": token})
    if not participant:
        return None, None, f"participant 없음 (token={token})"

    name = (
        participant.get("name")
        or participant.get("reward_name")
        or "응답자"
    )
    org = participant.get("org", "")
    base = (base_url or "").rstrip("/")

    t = log.get("type")
    if t == "completion":
        review_url = f"{base}/?token={token}&review=1"
        return (
            "[AURI 청사관리실태조사] 응답 완료 안내 — 내 응답 확인 링크",
            render_completion(name, org, review_url),
            None,
        )
    if t == "recovery":
        # 원본 시점에 응답을 냈는지 여부에 따라 invite/review로 분기. 현재 시점 기준으로 재판단.
        existing_resp = db.responses.find_one({"token": token, "submitted_at": {"$ne": None}})
        if existing_resp:
            link_url = f"{base}/?token={token}&review=1"
            return (
                "[AURI 청사관리 실태조사] 응답 확인·수정 링크 재발송",
                render_completion(name, org, link_url),
                None,
            )
        link_url = f"{base}/?token={token}"
        return (
            "[AURI 청사관리 실태조사] 설문 참여 링크 재발송",
            render_email(name, org, link_url),
            None,
        )
    if t == "invite":
        survey_url = f"{base}/?token={token}"
        # 원본 subject 보존 — admin이 발송한 invite 제목을 유지하면 동일 스레드로 보임
        subject = log.get("subject") or "[AURI 청사관리 실태조사] 설문 참여 안내"
        return subject, render_email(name, org, survey_url), None
    return None, None, f"미지원 type: {t}"


def main():
    args = parse_args()
    s = get_settings()
    if not s.GMAIL_USER or not s.GMAIL_APP_PASSWORD:
        print("[abort] GMAIL_USER / GMAIL_APP_PASSWORD 미설정 — backend .env 확인", file=sys.stderr)
        sys.exit(2)

    client = MongoClient(args.uri)
    db = client[args.db]
    q = build_query(args)
    cursor = db.email_logs.find(q).sort("sent_at", 1)
    if args.limit:
        cursor = cursor.limit(args.limit)
    logs = list(cursor)

    print(f"[query] {json.dumps({**q, 'sent_at': str(q.get('sent_at', ''))}, ensure_ascii=False, default=str)}")
    print(f"[match] {len(logs)}건")
    print(f"[mode]  {'DRY-RUN' if args.dry_run else 'SEND'}  base_url={s.SURVEY_BASE_URL}")
    if args.type == "all":
        skipped = list(db.email_logs.find({"status": "failed", "type": {"$in": list(SKIP_TYPES)}}))
        if skipped:
            print(f"[note]  자동 재구성 불가 type 스킵: {len(skipped)}건 (milestone/custom/reminder — 수동 발송 권장)")

    sent = failed = skipped_n = 0
    batch_id = "retry-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for i, log in enumerate(logs, 1):
        subject, html, err = reconstruct(log, db, s.SURVEY_BASE_URL)
        head = f"[{i:>3}/{len(logs)}] {log.get('type', '-'):<10} {log.get('email', '-')}"
        if err:
            print(f"{head}  SKIP: {err}")
            skipped_n += 1
            continue
        if args.dry_run:
            print(f"{head}  DRY  subject='{subject}'")
            continue
        new_log = {
            "batch_id": batch_id,
            "token": log.get("token", ""),
            "email": log.get("email", ""),
            "name": log.get("name", ""),
            "org": log.get("org", ""),
            "category": log.get("category", ""),
            "type": log.get("type"),
            "subject": subject,
            "admin_email": "system",
            "admin_name": "retry CLI",
            "sent_at": datetime.now(timezone.utc),
            "retry_of": log.get("_id"),
            "retry_of_sent_at": log.get("sent_at"),
        }
        try:
            send_email(log["email"], subject, html)
            new_log.update({"status": "sent", "error": ""})
            sent += 1
            print(f"{head}  SENT")
        except Exception as e:
            err = str(e)
            new_log.update({"status": "failed", "error": err})
            failed += 1
            print(f"{head}  FAIL: {err[:120]}")
        try:
            db.email_logs.insert_one(new_log)
        except Exception as ex:
            print(f"  email_logs insert 실패: {ex}", file=sys.stderr)
        if args.throttle > 0:
            time.sleep(args.throttle)

    print(f"\n[done] sent={sent} failed={failed} skipped={skipped_n}  batch_id={batch_id}")
    client.close()


if __name__ == "__main__":
    main()
