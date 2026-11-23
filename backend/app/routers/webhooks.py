"""Webhook từ máy chủ Git: push lên nhánh đang theo dõi → đồng bộ lại nguồn.

GitHub: Payload URL = https://<máy-chủ>/api/webhooks/git/<source_id>, content type JSON,
        Secret = BENCH_WEBHOOK_SECRET (xác thực bằng X-Hub-Signature-256).
GitLab: URL như trên, Secret token = BENCH_WEBHOOK_SECRET (gửi trong X-Gitlab-Token).
"""

import hashlib
import hmac
import json

from fastapi import APIRouter, HTTPException, Request

from ..config import settings
from ..db import SessionLocal
from ..models import Source
from ..services.sources import spawn, sync_source

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


def _verify(request: Request, body: bytes) -> None:
    secret = settings.webhook_secret
    if not secret:
        raise HTTPException(503, "Chưa cấu hình BENCH_WEBHOOK_SECRET")
    gh = request.headers.get("X-Hub-Signature-256")
    gl = request.headers.get("X-Gitlab-Token")
    if gh:
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(gh, expected):
            return
    elif gl and hmac.compare_digest(gl, secret):
        return
    raise HTTPException(401, "Chữ ký webhook không hợp lệ")


@router.post("/git/{source_id}", status_code=202)
async def git_push(source_id: int, request: Request):
    body = await request.body()
    _verify(request, body)
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        payload = {}
    with SessionLocal() as db:
        src = db.get(Source, source_id)
        if src is None:
            raise HTTPException(404, "Không tìm thấy nguồn bench")
        pushed = str(payload.get("ref", ""))
        if pushed and pushed not in (src.ref, f"refs/heads/{src.ref}", f"refs/tags/{src.ref}"):
            return {"ok": True, "skipped": f"push lên {pushed}, nguồn đang theo dõi {src.ref}"}
    spawn(sync_source(source_id))
    return {"ok": True}
