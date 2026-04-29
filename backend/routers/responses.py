import logging
from fastapi import APIRouter, Request, HTTPException
from datetime import datetime, timezone
from uuid import uuid4
from models import (
    ResponseSubmit,
    ResponseRecord,
    ParticipantUpdate,
    SelfRegisterRequest,
    RecoverRequest,
    CommentCreateRequest,
    CommentUpdateRequest,
    COMMENT_STATUSES,
)
from services.db import get_db
from services.email_service import render_completion, render_email, send_email, send_email_multi
from config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["responses"])

ALLOWED_SELF_CATEGORIES = {"광역자치단체", "기초자치단체"}

# 완료 응답 limit — 연구진(category=연구진) 제외, self+imported 합산
# 도달 시 신규 자가등록·신규 제출·기등록자 제출·리뷰 접근 모두 차단
SURVEY_LIMIT = 250


async def _get_completed_count(db) -> int:
    """category=연구진을 제외한 submitted 응답 수."""
    reviewer_tokens = [
        p["token"]
        async for p in db.participants.find(
            {"category": "연구진"}, {"token": 1, "_id": 0}
        )
    ]
    return await db.responses.count_documents({
        "submitted_at": {"$ne": None},
        "token": {"$nin": reviewer_tokens},
    })


async def _is_survey_closed(db) -> bool:
    return (await _get_completed_count(db)) >= SURVEY_LIMIT


# 50부 단위 마일스톤 보고 메일 — 연구진(김영현·윤호선)에게 진행 현황 통보
MILESTONES = [50, 100, 150, 200, 250]
MILESTONE_TO = ["yhkim@auri.re.kr", "hsyoon@auri.re.kr"]
MILESTONE_CC = ["blaster@auri.re.kr"]


async def _gather_milestone_stats(db) -> dict:
    """카테고리별 응답 수, 사례품 동의자 수 (연구진 제외)."""
    pipeline_cat = [
        {"$match": {"submitted_at": {"$ne": None}}},
        {"$lookup": {
            "from": "participants",
            "localField": "token",
            "foreignField": "token",
            "as": "p",
        }},
        {"$unwind": "$p"},
        {"$match": {"p.category": {"$ne": "연구진"}}},
        {"$group": {"_id": "$p.category", "count": {"$sum": 1}}},
    ]
    by_cat = {}
    async for doc in db.responses.aggregate(pipeline_cat):
        by_cat[doc["_id"]] = doc["count"]

    pipeline_reward = [
        {"$match": {"submitted_at": {"$ne": None}}},
        {"$lookup": {
            "from": "participants",
            "localField": "token",
            "foreignField": "token",
            "as": "p",
        }},
        {"$unwind": "$p"},
        {"$match": {"p.category": {"$ne": "연구진"}, "p.consent_reward": True}},
        {"$count": "n"},
    ]
    reward = 0
    async for doc in db.responses.aggregate(pipeline_reward):
        reward = doc["n"]

    return {
        "metro_responded": by_cat.get("광역자치단체", 0),
        "metro_total": await db.participants.count_documents({"category": "광역자치단체"}),
        "base_responded": by_cat.get("기초자치단체", 0),
        "base_total": await db.participants.count_documents({"category": "기초자치단체"}),
        "reward_consenters": reward,
    }


def _render_milestone_html(milestone: int, completed: int, stats: dict, admin_url: str) -> str:
    pct = round(completed / SURVEY_LIMIT * 100, 1)
    metro_pct = round(stats["metro_responded"] / max(stats["metro_total"], 1) * 100, 1)
    base_pct = round(stats["base_responded"] / max(stats["base_total"], 1) * 100, 1)
    closing_block = ""
    if milestone >= SURVEY_LIMIT:
        closing_block = (
            '<table width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 24px">'
            '<tr><td style="background:#fff4e6;border-left:4px solid #d97706;padding:18px 22px;border-radius:6px;font-size:13px;color:#5b3a14;line-height:1.8">'
            '<strong>📌 응답 모집 마감</strong><br>'
            f'목표 {SURVEY_LIMIT}부에 도달하여 신규 응답 접수가 자동 차단되었습니다. '
            '신규 진입·이어작성·리뷰 페이지 접근 모두 마감 안내 화면으로 전환됩니다 (연구진 토큰 예외).'
            '</td></tr></table>'
        )
    return f"""
    <div style="font-family:'Noto Sans KR',sans-serif;font-size:14px;line-height:1.7;color:#222;max-width:640px;margin:0 auto;padding:24px">
      <h2 style="font-size:18px;margin:0 0 8px">청사관리 실태조사 응답 {milestone}부 도달 안내</h2>
      <p style="color:#666;font-size:13px;margin:0 0 24px">자동 발송 — AURI 청사관리실태조사 시스템</p>

      <table width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 20px">
        <tr><td style="background:#f0f4f8;border-left:4px solid #2c2c2c;padding:18px 22px;border-radius:6px">
          <strong style="font-size:15px">유효 완료 응답: {completed}부 / {SURVEY_LIMIT}부 ({pct}%)</strong>
          <div style="color:#666;font-size:12px;margin-top:6px">연구진 응답 제외 · 자가등록(self) + 사전 import 합산</div>
        </td></tr>
      </table>

      {closing_block}

      <table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #e0e0e0;border-radius:6px;margin:0 0 24px">
        <tr><td style="padding:18px 22px">
          <strong style="display:block;margin:0 0 12px;font-size:14px">카테고리별 응답 현황</strong>
          <table width="100%" cellpadding="6" cellspacing="0" style="font-size:13px">
            <tr><td style="color:#666">광역자치단체</td>
                <td align="right"><strong>{stats['metro_responded']}</strong> / {stats['metro_total']}명 ({metro_pct}%)</td></tr>
            <tr><td style="color:#666">기초자치단체</td>
                <td align="right"><strong>{stats['base_responded']}</strong> / {stats['base_total']}명 ({base_pct}%)</td></tr>
            <tr><td style="color:#666;border-top:1px solid #eee;padding-top:10px">사례품 동의 응답자</td>
                <td align="right" style="border-top:1px solid #eee;padding-top:10px"><strong>{stats['reward_consenters']}</strong>명</td></tr>
          </table>
        </td></tr>
      </table>

      <p style="font-size:13px;color:#444;line-height:1.8;margin:0 0 16px">
        상세 내역은 <a href="{admin_url}" style="color:#2c2c2c">관리자 페이지</a>에서 확인하실 수 있습니다.
        (각자 보관하신 관리자 토큰을 사용해 주십시오.)
      </p>

      <p style="font-size:12px;color:#999;margin:0">
        ― AURI 청사관리실태조사 시스템 (자동 발송 · 회신 불가)
      </p>
    </div>
    """


async def _send_milestone_emails_if_needed(db) -> None:
    """제출 후 호출. 50/100/150/200/250 마일스톤 도달 + 미발송이면 자동 발송.
    이미 발송된 마일스톤은 email_logs(type=milestone, status=sent)로 식별하여 스킵.
    실패해도 응답 처리는 영향받지 않음."""
    s = get_settings()
    if not s.GMAIL_USER or not s.GMAIL_APP_PASSWORD:
        return

    completed = await _get_completed_count(db)

    pending = []
    for m in MILESTONES:
        if completed < m:
            break
        already = await db.email_logs.find_one(
            {"type": "milestone", "milestone": m, "status": "sent"}
        )
        if not already:
            pending.append(m)

    if not pending:
        return

    stats = await _gather_milestone_stats(db)
    admin_url = f"{s.SURVEY_BASE_URL}/admin/"

    for m in pending:
        subject = f"[AURI 청사관리실태조사] 응답 {m}부 도달 — 진행 현황 보고"
        log_doc = {
            "type": "milestone",
            "milestone": m,
            "completed_at_send": completed,
            "to": MILESTONE_TO,
            "cc": MILESTONE_CC,
            "subject": subject,
            "admin_email": "system",
            "admin_name": "마일스톤 자동 발송",
            "sent_at": datetime.now(timezone.utc),
        }
        try:
            html = _render_milestone_html(m, completed, stats, admin_url)
            send_email_multi(MILESTONE_TO, MILESTONE_CC, subject=subject, html_body=html)
            log_doc.update({"status": "sent", "error": ""})
            await db.email_logs.insert_one(log_doc)
        except Exception as e:
            err = str(e)
            logger.warning(f"Milestone {m}부 send failed: {err}")
            log_doc.update({"status": "failed", "error": err})
            try:
                await db.email_logs.insert_one(log_doc)
            except Exception:
                pass


async def _send_completion_email(participant: dict, token: str) -> None:
    """응답 제출 직후 자동 발송. 실패해도 응답 처리는 영향받지 않음."""
    s = get_settings()
    if not s.GMAIL_USER or not s.GMAIL_APP_PASSWORD:
        return
    if not participant.get("email"):
        return
    db = get_db()
    review_url = f"{s.SURVEY_BASE_URL}/?token={token}&review=1"
    subject = "[AURI 청사관리실태조사] 응답 완료 안내 — 내 응답 확인 링크"
    html = render_completion(
        participant.get("name", ""),
        participant.get("org", ""),
        review_url,
    )
    now = datetime.now(timezone.utc)
    log_doc = {
        "batch_id": "auto-completion",
        "token": token,
        "email": participant["email"],
        "name": participant.get("name", ""),
        "org": participant.get("org", ""),
        "category": participant.get("category", ""),
        "type": "completion",
        "subject": subject,
        "admin_email": "system",
        "admin_name": "자동 발송",
        "sent_at": now,
    }
    try:
        send_email(participant["email"], subject, html)
        log_doc.update({"status": "sent", "error": ""})
        await db.email_logs.insert_one(log_doc)
    except Exception as e:
        err = str(e)
        logger.warning(f"Completion email failed for {participant['email']}: {err}")
        log_doc.update({"status": "failed", "error": err})
        try:
            await db.email_logs.insert_one(log_doc)
        except Exception:
            pass


async def _require_reviewer(token: str) -> dict:
    """token이 유효한 연구진 참가자인지 확인하고 participant doc 반환."""
    db = get_db()
    p = await db.participants.find_one({"token": token}, {"_id": 0})
    if not p:
        raise HTTPException(404, "유효하지 않은 토큰입니다.")
    if p.get("category") != "연구진":
        raise HTTPException(403, "연구진 전용 기능입니다.")
    return p


def _serialize_comment(doc: dict) -> dict:
    """ObjectId 제거 + datetime ISO 변환."""
    out = {k: v for k, v in doc.items() if k != "_id"}
    for k in ("created_at", "updated_at", "status_changed_at"):
        v = out.get(k)
        if isinstance(v, datetime):
            out[k] = v.isoformat()
    return out


async def _require_reviewer(token: str) -> dict:
    """token이 유효한 연구진 참가자인지 확인하고 participant doc 반환."""
    db = get_db()
    p = await db.participants.find_one({"token": token}, {"_id": 0})
    if not p:
        raise HTTPException(404, "유효하지 않은 토큰입니다.")
    if p.get("category") != "연구진":
        raise HTTPException(403, "연구진 전용 기능입니다.")
    return p


def _serialize_comment(doc: dict) -> dict:
    """ObjectId 제거 + datetime ISO 변환."""
    out = {k: v for k, v in doc.items() if k != "_id"}
    for k in ("created_at", "updated_at", "status_changed_at"):
        v = out.get(k)
        if isinstance(v, datetime):
            out[k] = v.isoformat()
    return out


@router.get("/survey/status")
async def survey_status():
    """공개 — 설문 진행 상황 (마감 여부·완료 수·limit). 인트로 마감 배너용."""
    db = get_db()
    completed = await _get_completed_count(db)
    return {
        "completed": completed,
        "limit": SURVEY_LIMIT,
        "is_closed": completed >= SURVEY_LIMIT,
    }


@router.get("/survey/{token}")
async def verify_token(token: str):
    db = get_db()
    participant = await db.participants.find_one({"token": token}, {"_id": 0})
    if not participant:
        raise HTTPException(404, "유효하지 않은 설문 링크입니다.")

    # 마감 후에는 신규 진입·이어작성·리뷰 모두 차단 (연구진 토큰은 예외)
    if participant.get("category") != "연구진" and await _is_survey_closed(db):
        raise HTTPException(
            410,
            f"설문이 마감되었습니다. (목표 {SURVEY_LIMIT}부 도달) 참여해 주셔서 감사합니다.",
        )

    existing = await db.responses.find_one({"token": token}, {"_id": 0})
    has_submitted = bool(existing and existing.get("submitted_at"))
    return {
        "token": participant["token"],
        "name": participant.get("name", ""),
        "email": participant.get("email", ""),
        "org": participant.get("org", ""),
        "category": participant.get("category", ""),
        "field": participant.get("field", ""),
        "phone": participant.get("phone", ""),
        "dept": participant.get("dept", ""),
        "team": participant.get("team", ""),
        "position": participant.get("position", ""),
        "rank": participant.get("rank", ""),
        "duty": participant.get("duty", ""),
        "source": participant.get("source", "imported"),
        "consent_pi": bool(participant.get("consent_pi", False)),
        "consent_reward": bool(participant.get("consent_reward", False)),
        "reward_name": participant.get("reward_name", ""),
        "reward_phone": participant.get("reward_phone", ""),
        "has_responded": has_submitted,
        "responses": existing.get("responses") if has_submitted else None,
        "comments": existing.get("comments") if existing else None,
        "submitted_at": existing.get("submitted_at").isoformat() if has_submitted else None,
        "updated_at": existing.get("updated_at").isoformat() if existing and existing.get("updated_at") else None,
    }


# ── 공개 자가등록 (No Auth) ──
import re
import uuid

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


@router.post("/survey/register")
async def self_register(body: SelfRegisterRequest, request: Request):
    """공개 단일 링크에서 응답자가 직접 정보를 입력하고 토큰을 발급받는다.
    - email은 필수 (완료 안내 메일 발송용). 분석·식별 목적으로는 사용하지 않음.
    - 분류 정보(category/org/dept...)는 통계법 §33 처리되므로 동의 대상 아님.
    - 사례품 동의(consent_reward) 시에만 reward_name·reward_phone 수집.
    - 토큰은 random uuid. 신규 email은 새 토큰 발급.
    - imported 명단 & 미응답: 폼 입력값으로 정보 갱신 + source='self' 전환 + 기존 토큰 노출(smooth 진입).
      신원 사칭 방지를 위해 폼 입력값이 imported 정보를 덮어씀.
    - 이미 응답 완료: 차단 (재등록 의미 없음, /recover로 리뷰 링크).
    - 이미 self 등록: 차단 (분실 시 /recover).
    """
    s = get_settings()

    email = (body.email or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "올바른 이메일을 입력해 주십시오.")
    if not body.consent_pi:
        raise HTTPException(400, "이메일 수집·이용에 동의해 주셔야 참여하실 수 있습니다.")
    if body.category not in ALLOWED_SELF_CATEGORIES:
        raise HTTPException(400, "지자체 유형(광역/기초)을 선택해 주십시오.")
    if not (body.org or "").strip():
        raise HTTPException(400, "지자체명을 입력해 주십시오.")
    if body.consent_reward:
        if not body.reward_name.strip() or not body.reward_phone.strip():
            raise HTTPException(400, "사례품 수령자명과 휴대폰 번호를 입력해 주십시오.")

    db = get_db()
    existing = await db.participants.find_one({"email": email})

    # 응답 완료자·self 기등록자는 차단 — 분실 시 /recover.
    if existing:
        if existing.get("responded"):
            raise HTTPException(
                409,
                "이 이메일로 이미 응답을 제출하셨습니다. 응답 확인·수정은 '토큰 재발송'을 요청해 메일의 리뷰 링크로 접속해 주십시오.",
            )
        if existing.get("source") == "self":
            raise HTTPException(
                409,
                "이 이메일로 이미 등록되어 있습니다. 처음 등록 시 받으신 메일의 링크로 접속하시거나, 메일을 못 받으셨다면 '토큰 재발송'을 요청해 주십시오.",
            )

    if await _is_survey_closed(db):
        raise HTTPException(
            410,
            f"설문이 마감되었습니다. (목표 {SURVEY_LIMIT}부 도달) 참여해 주셔서 감사합니다.",
        )

    now = datetime.utcnow()
    ip = request.client.host if request.client else ""
    ua = request.headers.get("user-agent", "")

    # 사례품 동의 시 입력한 reward_name이 곧 응답자 이름. 미동의 시에는 익명 응답으로 빈 문자열.
    name = (body.reward_name or "").strip() if body.consent_reward else ""

    # imported 명단 & 미응답 → 폼 입력값으로 정보 갱신 후 기존 토큰 노출 (smooth 진입).
    if existing:
        token = existing["token"]
        # 변경 전 스냅샷을 participants_backup에 저장 (update_participant와 동일 패턴).
        last_backup = await db.participants_backup.find_one(
            {"token": token}, sort=[("version", -1)]
        )
        next_version = (last_backup.get("version", 0) + 1) if last_backup else 1
        snapshot = {k: v for k, v in existing.items() if k != "_id"}
        await db.participants_backup.insert_one({
            "token": token,
            "version": next_version,
            "backed_up_at": now,
            "ip": ip,
            "user_agent": ua,
            "snapshot": snapshot,
            "source_action": "self_register_promote",
        })

        update_fields = {
            "name": name,
            "org": body.org.strip(),
            "category": body.category,
            "dept": (body.dept or "").strip(),
            "team": (body.team or "").strip(),
            "position": (body.position or "").strip(),
            "rank": (body.rank or "").strip(),
            "duty": (body.duty or "").strip(),
            "source": "self",
            "consent_pi": True,
            "consent_pi_at": now,
            "consent_reward": bool(body.consent_reward),
            "consent_reward_at": now if body.consent_reward else None,
            "reward_name": body.reward_name.strip() if body.consent_reward else "",
            "reward_phone": body.reward_phone.strip() if body.consent_reward else "",
            "register_ip": ip,
            "register_ua": ua,
            "register_updated_at": now,
            "self_registered_at": now,
            "updated_at": now,
        }
        await db.participants.update_one({"token": token}, {"$set": update_fields})

        return {
            "status": "promoted",
            "token": token,
            "survey_url": f"{s.SURVEY_BASE_URL}/?token={token}",
        }

    token = uuid.uuid4().hex[:16]
    doc = {
        "token": token,
        "email": email,
        "name": name,
        "org": body.org.strip(),
        "category": body.category,
        "dept": (body.dept or "").strip(),
        "team": (body.team or "").strip(),
        "position": (body.position or "").strip(),
        "rank": (body.rank or "").strip(),
        "duty": (body.duty or "").strip(),
        "phone": "",
        "source": "self",
        "consent_pi": True,
        "consent_pi_at": now,
        "consent_reward": bool(body.consent_reward),
        "consent_reward_at": now if body.consent_reward else None,
        "reward_name": body.reward_name.strip() if body.consent_reward else "",
        "reward_phone": body.reward_phone.strip() if body.consent_reward else "",
        "register_ip": ip,
        "register_ua": ua,
        "register_updated_at": now,
        "created_at": now,
    }
    await db.participants.insert_one(doc)

    return {
        "status": "created",
        "token": token,
        "survey_url": f"{s.SURVEY_BASE_URL}/?token={token}",
    }


@router.post("/survey/recover")
async def recover_token(body: RecoverRequest):
    """자가등록자가 토큰 링크를 분실한 경우, 등록 시 사용한 email로 토큰 링크를 재발송한다.

    - 응답에는 토큰을 노출하지 않는다 (메일 수신만이 본인 확인 메커니즘).
    - 등록 여부와 무관하게 동일한 응답을 반환해 email 정찰을 어렵게 한다.
    - 응답 미제출이면 '설문 시작 링크', 제출 완료면 '응답 확인·수정 링크'를 발송한다.
    """
    s = get_settings()
    email = (body.email or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "올바른 이메일을 입력해 주십시오.")

    db = get_db()
    participant = await db.participants.find_one({"email": email}, {"_id": 0})
    if not participant:
        return {"status": "sent"}

    token = participant["token"]
    name = participant.get("name") or participant.get("reward_name") or "응답자"
    org = participant.get("org", "")

    existing_resp = await db.responses.find_one({"token": token}, {"submitted_at": 1})
    has_submitted = bool(existing_resp and existing_resp.get("submitted_at"))

    base = (s.SURVEY_BASE_URL or "").rstrip("/")
    if has_submitted:
        link_url = f"{base}/?token={token}&review=1"
        subject = "[AURI 청사관리 실태조사] 응답 확인·수정 링크 재발송"
        html = render_completion(name, org, link_url)
    else:
        link_url = f"{base}/?token={token}"
        subject = "[AURI 청사관리 실태조사] 설문 참여 링크 재발송"
        html = render_email(name, org, link_url)

    log_doc = {
        "batch_id": "auto-recovery",
        "token": token,
        "email": email,
        "name": participant.get("name", ""),
        "org": org,
        "category": participant.get("category", ""),
        "type": "recovery",
        "subject": subject,
        "admin_email": "system",
        "admin_name": "자동 재발송",
        "sent_at": datetime.utcnow(),
    }
    try:
        send_email(email, subject, html)
        log_doc.update({"status": "sent", "error": ""})
    except Exception as e:
        err = str(e)
        logger.warning(f"Recovery email failed for {email}: {err}")
        log_doc.update({"status": "failed", "error": err})
    try:
        await db.email_logs.insert_one(log_doc)
    except Exception:
        pass

    return {"status": "sent"}


@router.patch("/survey/{token}/comments")
async def save_reviewer_comments(token: str, body: dict, request: Request):
    """연구진 전용 — 제출 전에 문항별 수정 요청 메모를 자동 저장한다.
    responses 문서를 upsert하되, 새로 만들 때는 submitted_at을 세팅하지 않아 '제출'과 구분한다.
    """
    db = get_db()
    participant = await db.participants.find_one({"token": token}, {"_id": 0})
    if not participant:
        raise HTTPException(404, "유효하지 않은 토큰입니다.")
    if participant.get("category") != "연구진":
        raise HTTPException(403, "연구진 전용 엔드포인트입니다.")

    comments = body.get("comments")
    if not isinstance(comments, dict):
        raise HTTPException(400, "comments 필드가 올바르지 않습니다.")

    now = datetime.utcnow()
    ip = request.client.host if request.client else ""
    ua = request.headers.get("user-agent", "")

    await db.responses.update_one(
        {"token": token},
        {
            "$set": {
                "comments": comments,
                "comments_updated_at": now,
                "ip": ip,
                "user_agent": ua,
            },
            "$setOnInsert": {
                "token": token,
                "survey_version": "v1",
                "responses": {},
                "submitted_at": None,
            },
        },
        upsert=True,
    )
    return {"status": "ok", "comments_count": len(comments), "updated_at": now.isoformat()}


# ── Review Comment Threads (연구진 + 관리자 공유) ──

@router.get("/survey/{token}/threads")
async def list_threads(token: str, survey_version: str = "v1"):
    """연구진 토큰으로 모든 코멘트 스레드를 조회한다.
    qid별로 그룹화하여 반환. 모든 작성자(다른 연구진 + 관리자)의 코멘트를 포함.
    """
    await _require_reviewer(token)
    db = get_db()
    cursor = db.review_comments.find(
        {"survey_version": survey_version},
        {"_id": 0},
    ).sort("created_at", 1)

    by_qid: dict[str, list[dict]] = {}
    async for doc in cursor:
        out = _serialize_comment(doc)
        by_qid.setdefault(out["qid"], []).append(out)
    return {"survey_version": survey_version, "threads": by_qid}


@router.post("/survey/{token}/threads/{qid}")
async def create_comment(
    token: str,
    qid: str,
    body: CommentCreateRequest,
    survey_version: str = "v1",
):
    """연구진이 새 코멘트(또는 답글)를 작성한다."""
    p = await _require_reviewer(token)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "내용을 입력해 주십시오.")

    db = get_db()
    if body.parent_id:
        parent = await db.review_comments.find_one({"id": body.parent_id})
        if not parent:
            raise HTTPException(404, "원본 코멘트를 찾을 수 없습니다.")

    now = datetime.utcnow()
    doc = {
        "id": uuid4().hex,
        "survey_version": survey_version,
        "qid": qid,
        "author_role": "reviewer",
        "author_token": token,
        "author_name": p.get("name", ""),
        "author_email": p.get("email", ""),
        "author_org": p.get("org", ""),
        "text": text,
        "status": "open",
        "parent_id": body.parent_id,
        "created_at": now,
        "updated_at": None,
        "status_changed_at": None,
        "status_changed_by": "",
    }
    await db.review_comments.insert_one(doc)
    return {"status": "created", "comment": _serialize_comment(doc)}


@router.patch("/survey/{token}/threads/{qid}/{comment_id}")
async def update_own_comment(
    token: str,
    qid: str,
    comment_id: str,
    body: CommentUpdateRequest,
):
    """본인이 작성한 코멘트의 본문만 수정 가능. 상태 변경은 관리자 전용."""
    await _require_reviewer(token)
    if body.status is not None:
        raise HTTPException(403, "상태 변경은 관리자만 가능합니다.")
    text = (body.text or "").strip() if body.text is not None else None
    if not text:
        raise HTTPException(400, "내용을 입력해 주십시오.")

    db = get_db()
    target = await db.review_comments.find_one({"id": comment_id, "qid": qid})
    if not target:
        raise HTTPException(404, "코멘트를 찾을 수 없습니다.")
    if target.get("author_token") != token:
        raise HTTPException(403, "본인이 작성한 코멘트만 수정할 수 있습니다.")

    now = datetime.utcnow()
    await db.review_comments.update_one(
        {"id": comment_id},
        {"$set": {"text": text, "updated_at": now}},
    )
    updated = await db.review_comments.find_one({"id": comment_id}, {"_id": 0})
    return {"status": "updated", "comment": _serialize_comment(updated)}


@router.delete("/survey/{token}/threads/{qid}/{comment_id}")
async def delete_own_comment(token: str, qid: str, comment_id: str):
    """본인이 작성한 코멘트 삭제. 답글이 달린 경우에도 본문은 비우지만 entry는 유지."""
    await _require_reviewer(token)
    db = get_db()
    target = await db.review_comments.find_one({"id": comment_id, "qid": qid})
    if not target:
        raise HTTPException(404, "코멘트를 찾을 수 없습니다.")
    if target.get("author_token") != token:
        raise HTTPException(403, "본인이 작성한 코멘트만 삭제할 수 있습니다.")

    has_replies = await db.review_comments.count_documents({"parent_id": comment_id}) > 0
    if has_replies:
        await db.review_comments.update_one(
            {"id": comment_id},
            {"$set": {"text": "(작성자가 삭제한 코멘트)", "updated_at": datetime.utcnow()}},
        )
        return {"status": "soft_deleted"}

    await db.review_comments.delete_one({"id": comment_id})
    return {"status": "deleted"}


@router.patch("/survey/{token}/participant")
async def update_participant(token: str, body: ParticipantUpdate, request: Request):
    db = get_db()
    current = await db.participants.find_one({"token": token})
    if not current:
        raise HTTPException(404, "유효하지 않은 토큰입니다.")

    update_fields = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if not update_fields:
        raise HTTPException(400, "수정할 필드가 없습니다.")

    if "email" in update_fields and update_fields["email"] != current.get("email"):
        clash = await db.participants.find_one({
            "email": update_fields["email"],
            "token": {"$ne": token},
        })
        if clash:
            raise HTTPException(409, "이미 사용 중인 이메일입니다.")

    now = datetime.utcnow()
    last_backup = await db.participants_backup.find_one(
        {"token": token}, sort=[("version", -1)]
    )
    next_version = (last_backup.get("version", 0) + 1) if last_backup else 1

    snapshot = {k: v for k, v in current.items() if k != "_id"}
    await db.participants_backup.insert_one({
        "token": token,
        "version": next_version,
        "backed_up_at": now,
        "ip": request.client.host if request.client else "",
        "user_agent": request.headers.get("user-agent", ""),
        "snapshot": snapshot,
    })

    update_fields["updated_at"] = now
    await db.participants.update_one({"token": token}, {"$set": update_fields})

    updated = await db.participants.find_one({"token": token}, {"_id": 0})
    return {
        "status": "updated",
        "backup_version": next_version,
        "participant": {
            "token": updated["token"],
            "name": updated.get("name", ""),
            "email": updated.get("email", ""),
            "org": updated.get("org", ""),
            "phone": updated.get("phone", ""),
            "category": updated.get("category", ""),
            "dept": updated.get("dept", ""),
            "team": updated.get("team", ""),
            "position": updated.get("position", ""),
            "rank": updated.get("rank", ""),
            "duty": updated.get("duty", ""),
            "reward_name": updated.get("reward_name", ""),
            "reward_phone": updated.get("reward_phone", ""),
            "consent_reward": bool(updated.get("consent_reward", False)),
        },
    }


@router.post("/responses")
async def submit_response(body: ResponseSubmit, request: Request):
    db = get_db()
    participant = await db.participants.find_one({"token": body.token})
    if not participant:
        raise HTTPException(404, "유효하지 않은 토큰입니다.")

    # 마감 후 신규 제출·기제출 수정 모두 차단 (연구진 토큰은 예외)
    if participant.get("category") != "연구진" and await _is_survey_closed(db):
        raise HTTPException(
            410,
            f"설문이 마감되었습니다. (목표 {SURVEY_LIMIT}부 도달) 참여해 주셔서 감사합니다.",
        )

    now = datetime.utcnow()
    ip = request.client.host if request.client else ""
    ua = request.headers.get("user-agent", "")

    comments = body.comments or {}

    existing = await db.responses.find_one({"token": body.token})
    if existing and existing.get("submitted_at"):
        # 이미 제출된 응답 — 수정으로 처리
        update_fields = {
            "responses": body.responses,
            "survey_version": body.survey_version,
            "updated_at": now,
            "ip": ip,
            "user_agent": ua,
        }
        if body.comments is not None:
            update_fields["comments"] = comments
        await db.responses.update_one(
            {"token": body.token},
            {"$set": update_fields},
        )
        return {"status": "updated", "token": body.token}

    if existing:
        # comment-only upsert 문서가 있었음 — submitted_at만 세팅해 '최초 제출'로 마크
        update_fields = {
            "responses": body.responses,
            "survey_version": body.survey_version,
            "submitted_at": now,
            "ip": ip,
            "user_agent": ua,
        }
        if body.comments is not None:
            update_fields["comments"] = comments
        await db.responses.update_one(
            {"token": body.token},
            {"$set": update_fields},
        )
        if participant.get("category") != "연구진":
            await _send_completion_email(participant, body.token)
            await _send_milestone_emails_if_needed(db)
        return {"status": "created", "token": body.token}

    record = ResponseRecord(
        token=body.token,
        survey_version=body.survey_version,
        responses=body.responses,
        comments=comments,
        submitted_at=now,
        ip=ip,
        user_agent=ua,
    )
    await db.responses.insert_one(record.model_dump())
    if participant.get("category") != "연구진":
        await _send_completion_email(participant, body.token)
        await _send_milestone_emails_if_needed(db)
    return {"status": "created", "token": body.token}


@router.get("/responses/{token}")
async def get_response(token: str):
    db = get_db()
    doc = await db.responses.find_one({"token": token}, {"_id": 0})
    if not doc:
        return {"token": token, "responses": None}
    return doc
