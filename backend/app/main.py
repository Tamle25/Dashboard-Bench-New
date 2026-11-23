import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .db import init_db
from .routers import models_api, results, runs, sources, webhooks
from .services.runner import manager
from .services.sources import hourly_sync_loop, recover_orphan_syncs


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    recover_orphan_syncs()
    await manager.startup()
    scheduler = asyncio.create_task(hourly_sync_loop())
    try:
        yield
    finally:
        scheduler.cancel()
        await manager.shutdown()


app = FastAPI(title="Bench Console API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"], allow_headers=["*"])

for r in (sources.router, webhooks.router, models_api.router, runs.router, results.router):
    app.include_router(r)


@app.get("/api/health")
def health():
    return {"ok": True}


# Phục vụ frontend đã build (npm run build) để triển khai chỉ một tiến trình.
DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        f = (DIST / path).resolve()
        if path and f.is_file() and DIST in f.parents:
            return FileResponse(f)
        return FileResponse(DIST / "index.html")
