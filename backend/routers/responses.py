from fastapi import APIRouter, Request, HTTPException
from datetime import datetime
from models import ResponseSubmit, ResponseRecord, ParticipantUpdate
from services.db import get_db

router = APIRouter(prefix="/api", tags=["responses"])


@router.get("/survey/{token}")
async def verify_token(token: str):
    db = get_db()
    participant = await db.participants.find_one({"token": token}, {"_id": 0})
    if not participant:
        raise HTTPException(404, "유효하지 않은 설문 링크입니다.")

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
        "has_responded": has_submitted,
        "responses": existing.get("responses") if has_submitted else None,
        "comments": existing.get("comments") if existing else None,
        "submitted_at": existing.get("submitted_at").isoformat() if has_submitted else None,
        "updated_at": existing.get("updated_at").isoformat() if existing and existing.get("updated_at") else None,
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
        },
    }


@router.post("/responses")
async def submit_response(body: ResponseSubmit, request: Request):
    db = get_db()
    participant = await db.participants.find_one({"token": body.token})
    if not participant:
        raise HTTPException(404, "유효하지 않은 토큰입니다.")

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
    return {"status": "created", "token": body.token}


@router.get("/responses/{token}")
async def get_response(token: str):
    db = get_db()
    doc = await db.responses.find_one({"token": token}, {"_id": 0})
    if not doc:
        return {"token": token, "responses": None}
    return doc
