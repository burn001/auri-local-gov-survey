import logging
from fastapi import APIRouter, Request, HTTPException
from datetime import datetime, timezone
from uuid import uuid4
from models import (
    ResponseSubmit,
    ResponseRecord,
    ParticipantUpdate,
    SelfRegisterRequest,
    CommentCreateRequest,
    CommentUpdateRequest,
    COMMENT_STATUSES,
)
from services.db import get_db
from services.email_service import render_completion, send_email
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
    - 토큰은 random uuid (이메일 기반 HMAC 아님). 동일 email로 재등록 시 기존 토큰 반환(이어 작성용).
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

    now = datetime.utcnow()
    ip = request.client.host if request.client else ""
    ua = request.headers.get("user-agent", "")

    db = get_db()
    # 동일 이메일이 이미 등록되어 있으면 그 토큰 사용 (이어 작성용)
    existing = await db.participants.find_one({"email": email})

    # 신규 자가등록은 마감 시 차단 (이미 등록된 사람의 정보 갱신은 허용 — 연구진/기등록자)
    if not existing and await _is_survey_closed(db):
        raise HTTPException(
            410,
            f"설문이 마감되었습니다. (목표 {SURVEY_LIMIT}부 도달) 참여해 주셔서 감사합니다.",
        )

    # 사례품 동의 시 입력한 reward_name이 곧 응답자 이름. 미동의 시에는 익명 응답으로 빈 문자열.
    name = (body.reward_name or "").strip() if body.consent_reward else ""

    base_fields = {
        "email": email,
        "name": name if name else (existing.get("name", "") if existing else ""),
        "org": body.org.strip(),
        "category": body.category,
        "dept": (body.dept or "").strip(),
        "team": (body.team or "").strip(),
        "position": (body.position or "").strip(),
        "rank": (body.rank or "").strip(),
        "duty": (body.duty or "").strip(),
        "phone": "",  # 자가등록 흐름은 사무실 번호 안 받음
        "consent_pi": True,
        "consent_pi_at": now,
        "consent_reward": bool(body.consent_reward),
        "consent_reward_at": now if body.consent_reward else None,
        "reward_name": body.reward_name.strip() if body.consent_reward else "",
        "reward_phone": body.reward_phone.strip() if body.consent_reward else "",
        "register_ip": ip,
        "register_ua": ua,
        "register_updated_at": now,
    }

    if existing:
        token = existing["token"]
        base_fields["source"] = existing.get("source", "imported")
        await db.participants.update_one({"token": token}, {"$set": base_fields})
        status = "updated"
    else:
        token = uuid.uuid4().hex[:16]
        base_fields.update({
            "token": token,
            "source": "self",
            "created_at": now,
        })
        await db.participants.insert_one(base_fields)
        status = "created"

    return {
        "status": status,
        "token": token,
        "survey_url": f"{s.SURVEY_BASE_URL}/?token={token}",
    }


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
    return {"status": "created", "token": body.token}


@router.get("/responses/{token}")
async def get_response(token: str):
    db = get_db()
    doc = await db.responses.find_one({"token": token}, {"_id": 0})
    if not doc:
        return {"token": token, "responses": None}
    return doc
