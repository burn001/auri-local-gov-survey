"""
email_logs.status=='failed' 항목을 재발송한다.

우선순위:
    1. log['html_body']가 저장돼 있으면 동일 본문·제목으로 그대로 재발송 (모든 type 지원).
       milestone은 to/cc 리스트가 함께 저장되므로 send_email_multi로 처리.
    2. html_body가 없으면 (구버전 row) type 기반 본문 재구성으로 fallback.
       completion / recovery / invite는 token으로 본문 재구성 가능.
       milestone / custom / reminder는 fallback 불가 → SKIP.

원본 row는 그대로 두고, 새 row를 동일 schema로 insert해 시간순 이력을 보존한다.
새 row는 batch_id='retry-YYYYMMDDTHHMMSSZ' + retry_of=원본 _id.

사용 예:
    # 미리 보기 (dry-run)
    python scripts/retry_failed_emails.py --dry-run

    # 기간 한정 (UTC ISO yyyy-mm-dd)
    python scripts/retry_failed_emails.py --since 2026-05-04 --until 2026-05-05

    # 한도 회피용 throttle (초 단위 sleep)
    python scripts/retry_failed_emails.py --throttle 1.5 --limit 50

    # type 한정
    python scripts/retry_failed_emails.py --type reminder
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
from services.email_service import (
    send_email,
    send_email_multi,
    render_completion,
    render_email,
)
from config import get_settings


# token으로 본문 재구성 가능한 type (구버전 row html_body 부재 시 fallback)
RECONSTRUCTIBLE_TYPES = {"completion", "recovery", "invite"}
ALL_TYPES = {"completion", "recovery", "invite", "milestone", "custom", "reminder"}


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", choices=sorted(ALL_TYPES) + ["all"], default="all")
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
    if args.since or args.until:
        rng = {}
        if args.since:
            rng["$gte"] = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
        if args.until:
            rng["$lt"] = datetime.fromisoformat(args.until).replace(tzinfo=timezone.utc)
        q["sent_at"] = rng
    return q


def reconstruct_from_token(log: dict, db, base_url: str):
    """html_body 없는 구버전 row의 fallback. token으로 participants 조회 후 본문 재생성.
    completion / recovery / invite만 지원. 반환: (subject, html, error_or_None).
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
        return (
            "[AURI 청사관리실태조사] 응답 완료 안내 — 내 응답 확인 링크",
            render_completion(name, org, f"{base}/?token={token}&review=1"),
            None,
        )
    if t == "recovery":
        existing_resp = db.responses.find_one({"token": token, "submitted_at": {"$ne": None}})
        if existing_resp:
            return (
                "[AURI 청사관리 실태조사] 응답 확인·수정 링크 재발송",
                render_completion(name, org, f"{base}/?token={token}&review=1"),
                None,
            )
        return (
            "[AURI 청사관리 실태조사] 설문 참여 링크 재발송",
            render_email(name, org, f"{base}/?token={token}"),
            None,
        )
    if t == "invite":
        subject = log.get("subject") or "[AURI 청사관리 실태조사] 설문 참여 안내"
        return subject, render_email(name, org, f"{base}/?token={token}"), None
    return None, None, f"fallback 불가 type: {t}"


def send_for(log: dict, subject: str, html: str):
    """type별 발송 분기 — milestone은 send_email_multi, 나머지는 send_email."""
    if log.get("type") == "milestone":
        to_list = log.get("to") or []
        cc_list = log.get("cc") or []
        if not to_list:
            raise RuntimeError("milestone log에 to 필드 없음")
        send_email_multi(to_list, cc_list, subject=subject, html_body=html)
    else:
        if not log.get("email"):
            raise RuntimeError("email 필드 없음")
        send_email(log["email"], subject, html)


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
    print()

    sent = failed = skipped_n = 0
    batch_id = "retry-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for i, log in enumerate(logs, 1):
        target = log.get("email") or (", ".join(log.get("to", [])) if log.get("type") == "milestone" else "?")
        head = f"[{i:>3}/{len(logs)}] {log.get('type', '-'):<10} {target}"

        # 1) html_body 우선 사용
        html = log.get("html_body")
        subject = log.get("subject") or ""
        source = "stored"
        err = None
        if not html:
            # 2) fallback — token 기반 재구성
            if log.get("type") in RECONSTRUCTIBLE_TYPES:
                subject, html, err = reconstruct_from_token(log, db, s.SURVEY_BASE_URL)
                source = "reconstructed"
            else:
                err = f"html_body 없음 + 재구성 불가 ({log.get('type')})"

        if err or not html or not subject:
            print(f"{head}  SKIP: {err or 'subject/html 없음'}")
            skipped_n += 1
            continue
        if args.dry_run:
            print(f"{head}  DRY  src={source}  subject='{subject[:60]}'")
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
            "html_body": html,
            "admin_email": "system",
            "admin_name": "retry CLI",
            "sent_at": datetime.now(timezone.utc),
            "retry_of": log.get("_id"),
            "retry_of_sent_at": log.get("sent_at"),
            "retry_source": source,
        }
        if log.get("type") == "milestone":
            new_log["to"] = log.get("to", [])
            new_log["cc"] = log.get("cc", [])
            new_log["milestone"] = log.get("milestone")

        try:
            send_for(log, subject, html)
            new_log.update({"status": "sent", "error": ""})
            sent += 1
            print(f"{head}  SENT  src={source}")
        except Exception as e:
            err_msg = str(e)
            new_log.update({"status": "failed", "error": err_msg})
            failed += 1
            print(f"{head}  FAIL: {err_msg[:120]}")
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
