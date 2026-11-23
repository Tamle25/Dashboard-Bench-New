"""Tạo dữ liệu demo: nguồn bench là thư mục examples/bench-demo + ba mô hình mock.

    cd backend
    python -m app.seed

Cần BENCH_ALLOW_LOCAL_SOURCES=true (đã có trong .env.example).
"""

import asyncio
from pathlib import Path

from sqlalchemy import select

from .config import settings
from .db import SessionLocal, init_db
from .models import ModelEndpoint, Source
from .services.sources import sync_source

DEMO_DIR = Path(__file__).resolve().parents[2] / "examples" / "bench-demo"

MOCK_MODELS = [
    ModelEndpoint(id="mock-strong", note="mock, đúng ~85%", role="contestant", kind="mock", base_url="mock://0.85", status="ok"),
    ModelEndpoint(id="mock-weak", note="mock, đúng ~55%", role="contestant", kind="mock", base_url="mock://0.55", status="ok"),
    ModelEndpoint(id="mock-judge", note="giám khảo mock", role="judge", kind="mock", base_url="mock://1", status="ok"),
]


async def main() -> None:
    if not settings.allow_local_sources:
        raise SystemExit("Đặt BENCH_ALLOW_LOCAL_SOURCES=true (xem backend/.env.example) rồi chạy lại.")
    init_db()
    with SessionLocal() as db:
        for m in MOCK_MODELS:
            if db.get(ModelEndpoint, m.id) is None:
                db.add(m)
        src = db.scalar(select(Source).where(Source.url == str(DEMO_DIR)))
        if src is None:
            src = Source(repo="bench-demo", url=str(DEMO_DIR), host="Thư mục cục bộ", ref="main", manifest_path="bench.yaml", auth="public", sync_mode="manual")
            db.add(src)
        db.commit()
        sid = src.id
    await sync_source(sid)
    with SessionLocal() as db:
        src = db.get(Source, sid)
        print(f"Nguồn {src.repo}: {src.status} — {len([b for b in src.benches if b.active])} bộ bench. {src.message or ''}")
        print("Mô hình: " + ", ".join(m.id for m in MOCK_MODELS))


if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # console Windows mặc định cp1252
    asyncio.run(main())
