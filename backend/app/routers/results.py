from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Bench, ModelEndpoint, Run, RunEntry, RunLog, SampleResult, Source
from ..services.runner import ACTIVE, short_sha
from ..services.scoring import SCORING_LABEL
from ..timeutil import iso

router = APIRouter(prefix="/api", tags=["results"])


def _entries(db: Session, bench_id: int | None = None) -> list[RunEntry]:
    q = select(RunEntry).join(Run).order_by(RunEntry.id.desc())
    if bench_id:
        q = q.where(Run.bench_id == bench_id)
    return list(db.scalars(q))


def _previous(db: Session, e: RunEntry) -> RunEntry | None:
    return db.scalar(
        select(RunEntry).join(Run)
        .where(Run.bench_id == e.run.bench_id, RunEntry.model_id == e.model_id, RunEntry.status == "done", RunEntry.id < e.id)
        .order_by(RunEntry.id.desc()).limit(1)
    )


def entry_row(db: Session, e: RunEntry) -> dict:
    run = e.run
    prev = _previous(db, e) if e.status == "done" else None
    return {
        "id": e.id,
        "run_id": run.id,
        "bench": {"id": run.bench.id, "name": run.bench.name},
        "model": e.model_id,
        "judge": run.judge_id,
        "score": e.score,
        "delta": round(e.score - prev.score, 1) if prev and e.score is not None and prev.score is not None else None,
        "status": e.status,
        "when": iso(e.finished_at or run.finished_at or run.created_at),
    }


@router.get("/results")
def list_results(bench_id: int | None = None, limit: int = Query(200, le=1000), db: Session = Depends(get_db)):
    return [entry_row(db, e) for e in _entries(db, bench_id)[:limit]]


def _leaderboard(db: Session, bench_id: int) -> list[dict]:
    best: dict[str, RunEntry] = {}
    for e in _entries(db, bench_id):
        if e.status == "done" and e.score is not None and (e.model_id not in best or e.score > best[e.model_id].score):
            best[e.model_id] = e
    rows = sorted(best.values(), key=lambda x: x.score, reverse=True)
    return [{"entry_id": e.id, "model": e.model_id, "score": e.score, "judge": e.run.judge_id, "when": iso(e.finished_at)} for e in rows]


@router.get("/leaderboard")
def leaderboard(bench_id: int, db: Session = Depends(get_db)):
    return _leaderboard(db, bench_id)


def _get_entry(db: Session, entry_id: int) -> RunEntry:
    e = db.get(RunEntry, entry_id)
    if e is None:
        raise HTTPException(404, "Không tìm thấy kết quả")
    return e


@router.get("/results/{entry_id}")
def result_detail(entry_id: int, db: Session = Depends(get_db)):
    e = _get_entry(db, entry_id)
    run, bench = e.run, e.run.bench
    prev = _previous(db, e)
    cats: dict[str, list[float]] = defaultdict(list)
    for r in db.scalars(select(SampleResult).where(SampleResult.entry_id == e.id, SampleResult.needs_review.is_(False))):
        cats[r.category or "Khác"].append(r.value)
    errors = []
    if e.status == "failed":
        q = select(RunLog).where(RunLog.run_id == run.id, RunLog.level.in_(("WARN", "ERROR")), RunLog.message.contains(e.model_id)).order_by(RunLog.id.desc()).limit(5)
        errors = [{"lvl": r.level, "msg": r.message} for r in reversed(db.scalars(q).all())]
    started, finished = run.started_at, e.finished_at or run.finished_at
    return {
        **entry_row(db, e),
        "bench": {"id": bench.id, "name": bench.name, "scoring": SCORING_LABEL.get(bench.scoring_type), "judge_required": bench.judge_required, "rubric_path": bench.rubric_path},
        "source": f"{bench.source.repo}@{short_sha(run.source_sha)}",
        "params": run.params,
        "threshold": run.threshold,
        "total": e.total, "done": e.done, "passed": e.passed, "review": e.review,
        "error": e.error or run.error,
        "error_logs": errors,
        "duration_s": int((finished - started).total_seconds()) if started and finished else None,
        "previous": {"id": prev.id, "score": prev.score} if prev else None,
        "categories": [{"name": k, "score": round(sum(v) / len(v) * 100, 1)} for k, v in sorted(cats.items())],
        "comparison": _leaderboard(db, bench.id),
    }


@router.get("/results/{entry_id}/samples")
def result_samples(entry_id: int, filter: str = "all", offset: int = 0, limit: int = Query(50, le=500), db: Session = Depends(get_db)):
    _get_entry(db, entry_id)
    base = select(SampleResult).where(SampleResult.entry_id == entry_id)
    counts = {
        "all": db.scalar(select(func.count()).select_from(base.subquery())),
        "pass": db.scalar(select(func.count()).select_from(base.where(SampleResult.passed.is_(True)).subquery())),
        "fail": db.scalar(select(func.count()).select_from(base.where(SampleResult.passed.is_(False)).subquery())),
    }
    if filter == "pass":
        base = base.where(SampleResult.passed.is_(True))
    elif filter == "fail":
        base = base.where(SampleResult.passed.is_(False))
    rows = db.scalars(base.order_by(SampleResult.id).offset(offset).limit(limit)).all()
    return {
        "counts": counts,
        "items": [
            {"id": r.id, "sample_id": r.sample_id, "category": r.category, "prompt": r.prompt, "output": r.output, "reference": r.reference,
             "raw_score": r.raw_score, "passed": r.passed, "note": r.note, "needs_review": r.needs_review, "latency_ms": r.latency_ms, "tokens": r.tokens}
            for r in rows
        ],
    }


@router.get("/results/{entry_id}/export")
def export_result(entry_id: int, db: Session = Depends(get_db)):
    detail = result_detail(entry_id, db)
    detail["samples"] = [
        {"sample_id": r.sample_id, "category": r.category, "prompt": r.prompt, "output": r.output, "reference": r.reference,
         "raw_score": r.raw_score, "passed": r.passed, "note": r.note, "needs_review": r.needs_review}
        for r in db.scalars(select(SampleResult).where(SampleResult.entry_id == entry_id).order_by(SampleResult.id))
    ]
    return JSONResponse(detail, headers={"Content-Disposition": f'attachment; filename="R-{entry_id}.json"'})


@router.get("/stats")
def stats(db: Session = Depends(get_db)):
    entries = db.scalars(select(RunEntry)).all()
    benches = db.scalars(select(Bench).where(Bench.active.is_(True))).all()
    sources = db.scalars(select(Source)).all()
    active = db.scalar(select(Run).where(Run.status.in_(ACTIVE)).order_by(Run.id.desc()).limit(1))
    pct = None
    if active:
        total = sum(e.total for e in active.entries) or 1
        pct = round(sum(e.done for e in active.entries) / total * 100)
    return {
        "entries": len(entries),
        "entries_done": sum(1 for e in entries if e.status == "done"),
        "entries_failed": sum(1 for e in entries if e.status == "failed"),
        "models_evaluated": len({e.model_id for e in entries}),
        "benches": len(benches),
        "benches_with_judge": sum(1 for b in benches if b.judge_required),
        "sources": len(sources),
        "sources_ok": sum(1 for s in sources if s.status in ("ok", "warn", "syncing")),
        "sources_error": sum(1 for s in sources if s.status == "error"),
        "sources_warn": sum(1 for s in sources if s.status == "warn"),
        "models_down": db.scalar(select(func.count()).select_from(ModelEndpoint).where(ModelEndpoint.status == "down")),
        "active_run": {"id": active.id, "pct": pct} if active else None,
    }
