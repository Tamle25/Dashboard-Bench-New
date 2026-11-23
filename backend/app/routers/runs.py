import asyncio
import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import SessionLocal, get_db
from ..models import Bench, ModelEndpoint, Run, RunEntry, RunLog
from ..services.runner import ACTIVE, log_dict, manager, run_snapshot
from ..services.scoring import SCORING_LABEL

router = APIRouter(prefix="/api", tags=["runs"])

LOG_TAIL = 400


class RunIn(BaseModel):
    bench_id: int
    models: list[str] = Field(min_length=1, max_length=20)
    judge_id: str | None = None
    threshold: float = Field(default=6, ge=0, le=10)
    temperature: float = Field(default=0.2, ge=0, le=2)
    max_tokens: int = Field(default=1024, ge=1, le=32768)
    samples: int | Literal["all"] = 40
    concurrency: int = Field(default=8, ge=1, le=64)
    seed: int = 42


@router.get("/benches")
def list_benches(db: Session = Depends(get_db)):
    rows = db.scalars(select(Bench).where(Bench.active.is_(True)).order_by(Bench.id)).all()
    return [
        {
            "id": b.id, "key": b.key, "name": b.name, "task": b.task, "scoring_type": b.scoring_type,
            "scoring": SCORING_LABEL.get(b.scoring_type, b.scoring_type), "judge_required": b.judge_required,
            "rubric_path": b.rubric_path, "size": b.size, "source": f"{b.source.repo}@{(b.source.sha or '—')[:7]}",
            "data_path": b.data_path, "prepare": (b.options or {}).get("prepare") or [],
            "source_status": b.source.status,
        }
        for b in rows
    ]


@router.post("/runs", status_code=201)
async def create_run(body: RunIn, db: Session = Depends(get_db)):
    if manager.is_busy():
        raise HTTPException(409, "Đang có lượt chạy khác. Chờ xong hoặc dừng nó trước.")
    bench = db.get(Bench, body.bench_id)
    if bench is None or not bench.active:
        raise HTTPException(422, "Bộ bench không tồn tại hoặc đã bị gỡ khỏi manifest")
    if bench.source.status in ("error", "syncing", "pending"):
        raise HTTPException(409, f"Nguồn {bench.source.repo} chưa sẵn sàng ({bench.source.status})")
    models = list(dict.fromkeys(body.models))
    is_harbor = bench.scoring_type == "harbor"
    for mid in models:
        m = db.get(ModelEndpoint, mid)
        if m is None or m.role == "judge":
            raise HTTPException(422, f"{mid} không phải thí sinh hợp lệ")
        if is_harbor and m.kind not in ("openai", "harbor"):
            raise HTTPException(422, f"{mid} (kiểu {m.kind}) không chạy được bộ bench Harbor; dùng mô hình OpenAI-compatible hoặc agent Harbor")
        if not is_harbor and m.kind == "harbor":
            raise HTTPException(422, f"{mid} là agent Harbor, chỉ chạy được bộ bench dạng Harbor")
    judge_id = None
    if bench.judge_required:
        judge = db.get(ModelEndpoint, body.judge_id) if body.judge_id else None
        if judge is None or judge.role == "contestant":
            raise HTTPException(422, "Bộ bench này cần chọn một giám khảo hợp lệ")
        # Giám khảo được gọi qua Chat Completions để chấm theo rubric; agent Harbor không có
        # endpoint như vậy. Không chặn ở đây thì lượt chạy vẫn tiêu hết lượt gọi thí sinh rồi
        # mới hỏng ở từng mẫu một.
        if judge.kind not in ("openai", "mock"):
            raise HTTPException(422, f"{judge.id} là agent Harbor, không chấm được theo rubric; chọn giám khảo kiểu OpenAI-compatible")
        judge_id = judge.id
    params = body.model_dump(include={"temperature", "max_tokens", "samples", "concurrency", "seed"})
    run = Run(bench_id=bench.id, source_sha=bench.source.sha, judge_id=judge_id, threshold=body.threshold, params=params)
    run.entries = [RunEntry(model_id=m) for m in models]
    db.add(run)
    db.commit()
    manager.start(run.id)
    return run_snapshot(run)


@router.get("/runs/active")
def active_run(db: Session = Depends(get_db)):
    run = db.scalar(select(Run).where(Run.status.in_(ACTIVE)).order_by(Run.id.desc()).limit(1))
    return run_snapshot(run) if run else None


@router.get("/runs/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db)):
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(404, "Không tìm thấy lượt chạy")
    logs = db.scalars(select(RunLog).where(RunLog.run_id == run_id).order_by(RunLog.id.desc()).limit(LOG_TAIL)).all()
    return {**run_snapshot(run), "logs": [log_dict(r) for r in reversed(logs)]}


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: int):
    if not manager.cancel(run_id):
        raise HTTPException(409, "Lượt chạy không còn đang chạy")
    return {"ok": True}


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/runs/{run_id}/stream")
async def stream_run(run_id: int, request: Request):
    """Server-Sent Events: progress (ảnh chụp tiến độ), log (một dòng), end."""
    q = manager.subscribe(run_id)  # đăng ký trước khi đọc DB để không lỡ sự kiện
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        if run is None:
            manager.unsubscribe(run_id, q)
            raise HTTPException(404, "Không tìm thấy lượt chạy")
        snap = run_snapshot(run)
        logs = [log_dict(r) for r in reversed(db.scalars(select(RunLog).where(RunLog.run_id == run_id).order_by(RunLog.id.desc()).limit(LOG_TAIL)).all())]
        finished = run.status not in ACTIVE

    async def gen():
        try:
            yield _sse("progress", snap)
            for line in logs:
                yield _sse("log", line)
            if finished:
                yield _sse("end", {})
                return
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield _sse(ev["type"], ev["data"])
                if ev["type"] == "end":
                    break
        finally:
            manager.unsubscribe(run_id, q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
