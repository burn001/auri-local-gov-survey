from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone
from uuid import uuid4
from services.db import get_db
from services.email_service import render_email, send_email
from services.admin_auth import verify_admin_token
from config import get_settings
import logging

logger = logging.getLogger(__name__)

ALLOWED_EMAIL_TYPES = {"invite", "reminder", "deadline", "custom"}

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/me")
async def whoami(admin: dict = Depends(verify_admin_token)):
    return admin


@router.get("/stats")
async def get_stats(admin: dict = Depends(verify_admin_token)):
    db = get_db()

    total_p = await db.participants.count_documents({})
    total_r = await db.responses.count_documents({})

    pipeline = [
        {"$lookup": {
            "from": "responses",
            "localField": "token",
            "foreignField": "token",
            "as": "resp",
        }},
        {"$group": {
            "_id": "$category",
            "participants": {"$sum": 1},
            "responded": {"$sum": {"$cond": [{"$gt": [{"$size": "$resp"}, 0]}, 1, 0]}},
        }},
        {"$sort": {"_id": 1}},
    ]
    cursor = db.participants.aggregate(pipeline)
    by_category = {}
    async for doc in cursor:
        cat = doc["_id"] or "미분류"
        by_category[cat] = {
            "participants": doc["participants"],
            "responded": doc["responded"],
        }

    return {
        "total_participants": total_p,
        "total_responses": total_r,
        "by_category": by_category,
    }


@router.get("/responses")
async def list_responses(
    admin: dict = Depends(verify_admin_token),
    skip: int = 0,
    limit: int = 100,
    category: Optional[str] = None,
):
    db = get_db()

    pipeline = [
        {"$lookup": {
            "from": "participants",
            "localField": "token",
            "foreignField": "token",
            "as": "participant",
        }},
        {"$unwind": {"path": "$participant", "preserveNullAndEmptyArrays": True}},
    ]
    if category:
        pipeline.append({"$match": {"participant.category": category}})
    pipeline += [
        {"$sort": {"submitted_at": -1}},
        {"$skip": skip},
        {"$limit": limit},
        {"$project": {
            "_id": 0,
            "token": 1,
            "survey_version": 1,
            "responses": 1,
            "submitted_at": 1,
            "updated_at": 1,
            "name": "$participant.name",
            "org": "$participant.org",
            "category": "$participant.category",
        }},
    ]
    cursor = db.responses.aggregate(pipeline)
    results = [doc async for doc in cursor]
    return {"count": len(results), "data": results}


@router.get("/export")
async def export_csv(admin: dict = Depends(verify_admin_token)):
    db = get_db()
    import csv, io, json as _json
    from fastapi.responses import StreamingResponse

    pipeline = [
        {"$lookup": {
            "from": "participants",
            "localField": "token",
            "foreignField": "token",
            "as": "p",
        }},
        {"$unwind": {"path": "$p", "preserveNullAndEmptyArrays": True}},
        {"$sort": {"submitted_at": 1}},
    ]
    cursor = db.responses.aggregate(pipeline)
    docs = [doc async for doc in cursor]

    if not docs:
        raise HTTPException(404, "응답 데이터가 없습니다.")

    all_keys = set()
    for d in docs:
        all_keys.update(d.get("responses", {}).keys())
    sorted_keys = sorted(all_keys)

    output = io.StringIO()
    writer = csv.writer(output)
    header = ["token", "name", "org", "category", "submitted_at", "updated_at"] + sorted_keys
    writer.writerow(header)

    for d in docs:
        p = d.get("p", {})
        resp = d.get("responses", {})
        row = [
            d.get("token", ""),
            p.get("name", ""),
            p.get("org", ""),
            p.get("category", ""),
            str(d.get("submitted_at", "")),
            str(d.get("updated_at", "")),
        ]
        for k in sorted_keys:
            v = resp.get(k, "")
            if isinstance(v, (list, dict)):
                row.append(_json.dumps(v, ensure_ascii=False))
            else:
                row.append(str(v) if v is not None else "")
        writer.writerow(row)

    output.seek(0)
    return StreamingResponse(
        iter(["\ufeff" + output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=survey_responses.csv"},
    )


@router.get("/participants")
async def list_participants(
    admin: dict = Depends(verify_admin_token),
    category: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
):
    db = get_db()
    match: dict = {}
    if category:
        match["category"] = category

    pipeline = [
        {"$match": match},
        {"$lookup": {
            "from": "responses",
            "localField": "token",
            "foreignField": "token",
            "as": "resp",
        }},
        {"$addFields": {
            "responded": {"$gt": [{"$size": "$resp"}, 0]},
            "response_submitted_at": {"$arrayElemAt": ["$resp.submitted_at", 0]},
            "response_updated_at": {"$arrayElemAt": ["$resp.updated_at", 0]},
        }},
        {"$project": {"resp": 0, "_id": 0}},
        {"$skip": skip},
        {"$limit": limit},
    ]
    cursor = db.participants.aggregate(pipeline)
    results = [doc async for doc in cursor]
    total = await db.participants.count_documents(match)
    return {"total": total, "count": len(results), "data": results}


# ── Email ──

class EmailSendRequest(BaseModel):
    tokens: list[str]
    subject: str = "지방자치단체 청사 관리 실태조사 참여 요청 (AURI)"
    type: str = "invite"  # invite | reminder | deadline | custom


@router.post("/email/preview", response_class=HTMLResponse)
async def email_preview(admin: dict = Depends(verify_admin_token)):
    s = get_settings()
    url = f"{s.SURVEY_BASE_URL}/?token=SAMPLE_TOKEN"
    return render_email("홍길동", "예시 지방자치단체", url)


@router.post("/email/send")
async def send_survey_emails(
    body: EmailSendRequest,
    admin: dict = Depends(verify_admin_token),
):
    s = get_settings()
    if not s.GMAIL_USER or not s.GMAIL_APP_PASSWORD:
        raise HTTPException(500, "Gmail 설정이 없습니다.")

    email_type = body.type if body.type in ALLOWED_EMAIL_TYPES else "custom"
    batch_id = uuid4().hex[:12]
    db = get_db()
    results = {
        "batch_id": batch_id,
        "type": email_type,
        "sent": 0,
        "failed": 0,
        "skipped": 0,
        "errors": [],
    }

    for token in body.tokens:
        p = await db.participants.find_one({"token": token})
        if not p:
            results["skipped"] += 1
            continue

        survey_url = f"{s.SURVEY_BASE_URL}/?token={token}"
        html = render_email(p.get("name", ""), p.get("org", ""), survey_url)
        now = datetime.now(timezone.utc)

        log_doc = {
            "batch_id": batch_id,
            "token": token,
            "email": p["email"],
            "name": p.get("name", ""),
            "org": p.get("org", ""),
            "category": p.get("category", ""),
            "type": email_type,
            "subject": body.subject,
            "admin_email": admin.get("email", ""),
            "admin_name": admin.get("name", ""),
            "sent_at": now,
        }

        try:
            send_email(p["email"], body.subject, html)
            log_doc.update({"status": "sent", "error": ""})
            await db.email_logs.insert_one(log_doc)
            await db.participants.update_one(
                {"token": token},
                [{"$set": {
                    "email_sent": True,
                    "email_sent_at": now,  # legacy compat
                    "email_last_sent_at": now,
                    "email_first_sent_at": {"$ifNull": ["$email_first_sent_at", now]},
                    "email_sent_count": {"$add": [{"$ifNull": ["$email_sent_count", 0]}, 1]},
                    "email_last_status": "sent",
                    "email_last_type": email_type,
                    "email_last_error": "",
                }}],
            )
            results["sent"] += 1
        except Exception as e:
            err_msg = str(e)
            logger.error(f"Email failed for {p['email']}: {err_msg}")
            log_doc.update({"status": "failed", "error": err_msg})
            await db.email_logs.insert_one(log_doc)
            await db.participants.update_one(
                {"token": token},
                {"$set": {
                    "email_last_status": "failed",
                    "email_last_attempt_at": now,
                    "email_last_type": email_type,
                    "email_last_error": err_msg,
                }},
            )
            results["failed"] += 1
            results["errors"].append({"token": token, "email": p["email"], "error": err_msg})

    return results


@router.get("/email/logs")
async def list_email_logs(
    admin: dict = Depends(verify_admin_token),
    token: Optional[str] = None,
    status: Optional[str] = None,
    type: Optional[str] = None,
    batch_id: Optional[str] = None,
    skip: int = 0,
    limit: int = 200,
):
    db = get_db()
    query: dict = {}
    if token:
        query["token"] = token
    if status:
        query["status"] = status
    if type:
        query["type"] = type
    if batch_id:
        query["batch_id"] = batch_id

    total = await db.email_logs.count_documents(query)
    cursor = (
        db.email_logs.find(query, {"_id": 0})
        .sort("sent_at", -1)
        .skip(skip)
        .limit(min(limit, 1000))
    )
    items = [doc async for doc in cursor]
    return {"total": total, "count": len(items), "data": items}


@router.get("/email/history")
async def email_history(
    admin: dict = Depends(verify_admin_token),
    category: Optional[str] = None,
):
    db = get_db()
    p_query: dict = {"email_sent": True}
    if category:
        p_query["category"] = category
    sent_count = await db.participants.count_documents(p_query)
    total = await db.participants.count_documents({"category": category} if category else {})

    log_total = await db.email_logs.count_documents({})
    log_sent = await db.email_logs.count_documents({"status": "sent"})
    log_failed = await db.email_logs.count_documents({"status": "failed"})

    by_type_cursor = db.email_logs.aggregate([
        {"$match": {"status": "sent"}},
        {"$group": {"_id": "$type", "count": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ])
    by_type = {doc["_id"] or "unknown": doc["count"] async for doc in by_type_cursor}

    return {
        "unique_recipients_sent": sent_count,
        "total_participants": total,
        "log_total": log_total,
        "log_sent": log_sent,
        "log_failed": log_failed,
        "by_type": by_type,
    }


# ── Admin Management (admins 관리) ──
# 관리자 추가/목록/활성화 토글 — 매우 단순하게. 초기 관리자는 seed 스크립트로 추가.

class AdminCreateRequest(BaseModel):
    email: str
    name: str = ""
    role: str = "manager"


@router.get("/admins")
async def list_admins(admin: dict = Depends(verify_admin_token)):
    db = get_db()
    cursor = db.admins.find({}, {"_id": 0}).sort("created_at", -1)
    items = [doc async for doc in cursor]
    return {"count": len(items), "data": items}


@router.post("/admins")
async def create_admin(body: AdminCreateRequest, admin: dict = Depends(verify_admin_token)):
    from services.token_service import generate_admin_token
    s = get_settings()
    db = get_db()
    email = body.email.lower().strip()
    token = generate_admin_token(email, s.ADMIN_TOKEN_SECRET)
    doc = {
        "email": email,
        "name": body.name,
        "role": body.role,
        "token": token,
        "active": True,
        "created_at": datetime.now(timezone.utc),
        "created_by": admin.get("email"),
    }
    await db.admins.update_one({"email": email}, {"$setOnInsert": doc}, upsert=True)
    saved = await db.admins.find_one({"email": email}, {"_id": 0})
    return {"status": "ok", "admin": saved, "admin_url": f"{s.ADMIN_BASE_URL}/?token={token}"}
