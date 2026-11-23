import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Bench, Run, Source
from ..services import git_ops
from ..services.scoring import SCORING_LABEL
from ..services.secrets import secrets, source_secret_name
from ..services.sources import is_syncing, probe, spawn, sync_source
from ..timeutil import iso

router = APIRouter(prefix="/api/sources", tags=["sources"])

Auth = Literal["public", "deploy_key", "token"]
SyncMode = Literal["webhook", "hourly", "manual"]


class SourceIn(BaseModel):
    url: str = Field(min_length=1, max_length=1000)
    ref: str = Field(default="main", min_length=1, max_length=200)
    manifest_path: str = Field(default="bench.yaml", min_length=1, max_length=300)
    auth: Auth = "public"
    secret: str | None = Field(default=None, description="Token hoặc deploy key. Chỉ ghi vào kho bí mật, không trả về.")
    sync_mode: SyncMode = "webhook"


class SourcePatch(BaseModel):
    ref: str | None = None
    manifest_path: str | None = None
    auth: Auth | None = None
    secret: str | None = None
    sync_mode: SyncMode | None = None


def source_out(s: Source) -> dict:
    benches = [b for b in s.benches if b.active]
    return {
        "id": s.id,
        "repo": s.repo,
        "url": s.url,
        "host": s.host,
        "ref": s.ref,
        "sha": s.sha,
        "manifest_path": s.manifest_path,
        "auth": s.auth,
        "has_secret": secrets.has(source_secret_name(s.id)),
        "sync_mode": s.sync_mode,
        "status": s.status,
        "message": s.message,
        "synced_at": iso(s.synced_at),
        "benches": [{"id": b.id, "key": b.key, "name": b.name, "scoring": SCORING_LABEL.get(b.scoring_type), "size": b.size} for b in benches],
    }


def _get(db: Session, source_id: int) -> Source:
    s = db.get(Source, source_id)
    if s is None:
        raise HTTPException(404, "Không tìm thấy nguồn bench")
    return s


@router.get("")
def list_sources(db: Session = Depends(get_db)):
    return [source_out(s) for s in db.scalars(select(Source).order_by(Source.id))]


@router.post("/test")
async def test_source(body: SourceIn):
    """Kiểm tra nhanh kết nối + manifest (chỉ tải cấu trúc thư mục), không ghi gì vào DB."""
    insp = await asyncio.to_thread(probe, body.url.strip(), body.ref.strip(), body.manifest_path.strip(), body.auth, body.secret)
    return {
        "ok": insp.ok,
        "sha": insp.sha or None,
        "checks": [{"label": c.label, "detail": c.detail, "ok": c.ok} for c in insp.checks],
        "benches": [
            {"key": spec.id, "name": spec.name, "size": size or None, "prepare": bool(spec.prepare_list), "harbor": spec.scoring.type == "harbor"}
            for spec, size in insp.benches
        ],
        "warnings": insp.warnings,
    }


# Các handler dưới đây là async để `spawn` chạy được trên event loop chính.
@router.post("", status_code=201)
async def create_source(body: SourceIn, db: Session = Depends(get_db)):
    url = body.url.strip()
    if not (git_ops.is_remote(url) or git_ops.is_local(url)):
        raise HTTPException(422, "URL repo không hợp lệ")
    repo, host = git_ops.describe(url)
    ref = body.ref.strip()
    # So sánh trên ref ĐÃ strip: bản lưu cũng là ref đã strip, nên nếu so bản thô thì "  main  "
    # lọt qua và tạo nguồn thứ hai y hệt — bộ bench sẽ hiện hai lần ở màn Chạy mới.
    if db.scalar(select(Source).where(Source.url == url, Source.ref == ref)):
        raise HTTPException(409, f"Repo {repo} ({ref}) đã được kết nối")
    s = Source(repo=repo, url=url, host=host, ref=ref, manifest_path=body.manifest_path.strip(), auth=body.auth, sync_mode=body.sync_mode)
    db.add(s)
    db.commit()
    if body.secret:
        secrets.set(source_secret_name(s.id), body.secret)
    spawn(sync_source(s.id))
    return source_out(s)


@router.patch("/{source_id}")
async def update_source(source_id: int, body: SourcePatch, db: Session = Depends(get_db)):
    s = _get(db, source_id)
    for field in ("ref", "manifest_path", "auth", "sync_mode"):
        val = getattr(body, field)
        if val is not None:
            setattr(s, field, val.strip() if isinstance(val, str) else val)
    db.commit()
    if s.auth == "public":
        # Chuyển sang công khai thì token/khoá cũ không còn lý do tồn tại: xoá khỏi kho bí mật.
        secrets.delete(source_secret_name(s.id))
    elif body.secret:
        secrets.set(source_secret_name(s.id), body.secret)
    spawn(sync_source(s.id))
    return source_out(s)


@router.post("/{source_id}/sync", status_code=202)
async def trigger_sync(source_id: int, db: Session = Depends(get_db)):
    s = _get(db, source_id)
    # Căn theo lượt đang chạy thật trong tiến trình, không theo cột status: status có thể còn
    # kẹt ở "syncing" từ lần máy chủ bị tắt giữa lúc đồng bộ.
    if not is_syncing(s.id):
        spawn(sync_source(s.id))
    return {"ok": True}


@router.delete("/{source_id}", status_code=204)
def delete_source(source_id: int, db: Session = Depends(get_db)):
    s = _get(db, source_id)
    used = db.scalar(select(Run.id).join(Bench, Run.bench_id == Bench.id).where(Bench.source_id == s.id).limit(1))
    if used:
        raise HTTPException(409, "Nguồn đã có lượt chạy; không thể xoá để giữ lịch sử. Hãy chuyển sang đồng bộ thủ công.")
    secrets.delete(source_secret_name(s.id))
    db.delete(s)
    db.commit()
