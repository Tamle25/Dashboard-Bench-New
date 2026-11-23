"""Kiểm tra và đồng bộ repo bench."""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal
from ..models import Bench, Source
from ..timeutil import now
from . import git_ops, harbor
from .manifest import BenchSpec, ManifestError, check_files, parse_manifest, safe_join
from .secrets import secrets, source_secret_name


@dataclass
class Check:
    label: str
    detail: str
    ok: bool


@dataclass
class Inspection:
    ok: bool
    sha: str = ""
    benches: list[tuple[BenchSpec, int]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)

    @property
    def error(self) -> str:
        failed = [c for c in self.checks if not c.ok]
        return failed[0].detail if failed else ""


def inspect(url: str, ref: str, manifest_path: str, auth: str, secret: str | None, workdir: Path) -> Inspection:
    """Lấy repo về `workdir`, đọc manifest và kiểm tra. Không ghi gì vào DB."""
    insp = Inspection(ok=False)

    # 1. Truy cập repo
    try:
        if git_ops.is_local(url):
            if not settings.allow_local_sources:
                raise git_ops.GitError("Không cho phép nguồn là thư mục cục bộ (bật BENCH_ALLOW_LOCAL_SOURCES=true khi phát triển).")
            root = Path(url).expanduser().resolve()
            insp.sha = git_ops.local_sha(root)
            insp.checks.append(Check("Truy cập thư mục", f"{root} @ {insp.sha[:7]}", True))
        elif not git_ops.is_remote(url):
            raise git_ops.GitError("URL không hợp lệ. Dùng https://máy-chủ/tổ-chức/repo.git hoặc git@máy-chủ:tổ-chức/repo.git")
        else:
            insp.sha = git_ops.fetch_ref(workdir, url, ref, auth, secret)
            root = workdir
            insp.checks.append(Check("Truy cập repo", f"{ref} @ {insp.sha[:7]}", True))
    except git_ops.GitError as e:
        insp.checks.append(Check("Truy cập repo", str(e), False))
        return insp

    # 2. Tìm manifest (không có thì thử nhận diện repo dạng Harbor / Terminal-Bench)
    try:
        mpath = safe_join(root, manifest_path)
        if mpath.is_file():
            text = mpath.read_text(encoding="utf-8")
            insp.checks.append(Check(f"Tìm thấy {manifest_path}", "", True))
        else:
            text = harbor.synthesize_manifest(root)
            if text is None:
                raise ManifestError(f"không có file {manifest_path} ở commit này")
            dirs = ", ".join(f"{d} ({n})" for d, n in harbor.detect_harbor_dirs(root))
            insp.checks.append(Check(f"Không có {manifest_path}", f"tự nhận diện task Harbor: {dirs}", True))
    except (ManifestError, OSError, UnicodeDecodeError) as e:
        insp.checks.append(Check(f"Tìm thấy {manifest_path}", str(e), False))
        return insp

    # 3. Kiểm tra nội dung (chạy bước prepare nếu bộ bench có khai báo)
    try:
        parsed = parse_manifest(text)
    except ManifestError as e:
        insp.checks.append(Check("Manifest hợp lệ", str(e), False))
        return insp
    prep_warnings: list[str] = []
    ready = []
    for spec in parsed.benches:
        try:
            harbor.run_prepare(root, spec.prepare_list)
            ready.append(spec)
        except harbor.HarborError as e:
            prep_warnings.append(f'Bỏ qua bộ bench "{spec.id}": {e}')
    valid, file_warnings = check_files(root, ready)
    insp.benches = valid
    insp.warnings = parsed.warnings + prep_warnings + file_warnings
    if not valid:
        insp.checks.append(Check("Manifest hợp lệ", "không có bộ bench nào dùng được. " + " ".join(insp.warnings), False))
        return insp
    detail = f"phát hiện {len(valid)} bộ bench"
    if insp.warnings:
        detail += f", {len(insp.warnings)} cảnh báo"
    insp.checks.append(Check("Manifest hợp lệ", detail, True))
    insp.ok = True
    return insp


def probe(url: str, ref: str, manifest_path: str, auth: str, secret: str | None) -> Inspection:
    """Kiểm tra nhanh cho nút 'Kiểm tra kết nối': chỉ tải cấu trúc thư mục (partial clone), không tải
    nội dung file — vài giây kể cả với repo hàng trăm MB. Bước prepare chỉ chạy khi đồng bộ thật."""
    if not git_ops.is_remote(url):
        # Thư mục cục bộ (dev) đọc trực tiếp, không cần tải; URL sai cũng đi nhánh này để báo lỗi.
        return inspect(url, ref, manifest_path, auth, secret, git_ops.repo_cache_dir(settings.cache_dir, url))
    workdir = git_ops.repo_cache_dir(settings.cache_dir / "probe", url)
    insp = Inspection(ok=False)
    try:
        insp.sha = git_ops.fetch_tree_only(workdir, url, ref, auth, secret)
        paths = git_ops.list_paths(workdir)
    except git_ops.GitError as e:
        insp.checks.append(Check("Truy cập repo", str(e), False))
        return insp
    insp.checks.append(Check("Truy cập repo", f"{ref} @ {insp.sha[:7]} · {len(paths):,} file".replace(",", "."), True))

    try:
        if manifest_path in paths:
            text = git_ops.show_file(workdir, url, manifest_path, auth, secret)
            insp.checks.append(Check(f"Tìm thấy {manifest_path}", "", True))
        else:
            dirs = harbor.detect_harbor_dirs_from_paths(paths)
            text = harbor.synthesize_manifest(dirs=dirs)
            if text is None:
                raise ManifestError(f"không có file {manifest_path} ở commit này")
            insp.checks.append(Check(f"Không có {manifest_path}", "tự nhận diện task Harbor: " + ", ".join(f"{d} ({n})" for d, n in dirs), True))
        parsed = parse_manifest(text)
    except (ManifestError, git_ops.GitError) as e:
        insp.checks.append(Check("Manifest hợp lệ", str(e), False))
        return insp

    warnings = list(parsed.warnings)
    for spec in parsed.benches:
        data = spec.data.strip("/")
        missing = [p for p in (spec.scoring.rubric, (spec.scoring.scorer or "").split(":", 1)[0]) if p and p not in paths]
        if missing:
            warnings.append(f'Bỏ qua bộ bench "{spec.id}": không có file {", ".join(missing)}.')
        elif spec.scoring.type == "harbor":
            if spec.prepare_list:
                insp.benches.append((spec, 0))  # thư mục task được sinh khi đồng bộ
                continue
            n = harbor.count_tasks_from_paths(paths, data, spec.exclude)
            if n:
                insp.benches.append((spec, n))
            else:
                warnings.append(f'Bỏ qua bộ bench "{spec.id}": thư mục {data} không chứa task nào có task.toml.')
        elif data in paths:
            try:
                size = sum(1 for line in git_ops.show_file(workdir, url, data, auth, secret).splitlines() if line.strip())
                insp.benches.append((spec, size))
            except git_ops.GitError as e:
                warnings.append(f'Bỏ qua bộ bench "{spec.id}": {e}')
        else:
            warnings.append(f'Bỏ qua bộ bench "{spec.id}": không có file dữ liệu {data}.')
    insp.warnings = warnings
    if not insp.benches:
        insp.checks.append(Check("Manifest hợp lệ", "không có bộ bench nào dùng được. " + " ".join(warnings), False))
        return insp
    detail = f"phát hiện {len(insp.benches)} bộ bench" + (f", {len(warnings)} cảnh báo" if warnings else "")
    insp.checks.append(Check("Manifest hợp lệ", detail, True))
    insp.ok = True
    return insp


def apply_inspection(db: Session, src: Source, insp: Inspection) -> None:
    if not insp.ok:
        src.status = "error"
        src.message = insp.error
        return
    existing = {b.key: b for b in src.benches}
    for spec, size in insp.benches:
        b = existing.pop(spec.id, None)
        if b is None:
            b = Bench(source=src, key=spec.id)
            db.add(b)
        b.name = spec.name
        b.task = spec.task
        b.scoring_type = spec.scoring.type
        b.rubric_path = spec.scoring.rubric
        b.scorer = spec.scoring.scorer
        b.judge_required = spec.needs_judge
        b.data_path = spec.data
        b.size = size
        b.options = {"prepare": spec.prepare_list, "exclude": spec.exclude}
        b.active = True
    for b in existing.values():
        b.active = False
    src.sha = insp.sha
    src.status = "warn" if insp.warnings else "ok"
    src.message = " ".join(insp.warnings) or None
    src.synced_at = now()


_async_locks: dict[int, asyncio.Lock] = {}
_background: set[asyncio.Task] = set()
_in_flight: set[int] = set()


def is_syncing(source_id: int) -> bool:
    """Nguồn này có đang được đồng bộ trong tiến trình hiện tại hay không.

    Nguồn sự thật là tiến trình, không phải cột `status` trong DB: nếu máy chủ tắt hoặc
    khởi động lại giữa lúc đồng bộ, DB còn lại "syncing" mãi và nút Đồng bộ sẽ bị khoá
    vĩnh viễn — nguồn không bao giờ nạp lại được.
    """
    return source_id in _in_flight


def recover_orphan_syncs() -> None:
    """Đưa các nguồn bị kẹt ở "syncing"/"pending" về "error" khi máy chủ khởi động lại.

    Gọi lúc khởi động, trước khi có lượt đồng bộ nào được tạo, nên mọi nguồn còn ở hai
    trạng thái này chắc chắn là rác của tiến trình trước.
    """
    with SessionLocal() as db:
        for src in db.query(Source).filter(Source.status.in_(("syncing", "pending"))).all():
            src.status = "error"
            src.message = "Máy chủ khởi động lại khi đang đồng bộ. Bấm Đồng bộ để thử lại."
        db.commit()


def spawn(coro) -> asyncio.Task:
    """Chạy nền và giữ tham chiếu để task không bị thu gom rác giữa chừng."""
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


async def sync_source(source_id: int) -> None:
    lock = _async_locks.setdefault(source_id, asyncio.Lock())
    async with lock:
        _in_flight.add(source_id)
        try:
            with SessionLocal() as db:
                src = db.get(Source, source_id)
                if src is None:
                    return
                src.status = "syncing"
                db.commit()
                args = (src.url, src.ref, src.manifest_path, src.auth, secrets.get(source_secret_name(src.id)), git_ops.repo_cache_dir(settings.cache_dir, src.url))
            try:
                insp = await asyncio.to_thread(inspect, *args)
            except Exception as e:  # lỗi bất ngờ: vẫn phải đưa trạng thái về "error"
                insp = Inspection(ok=False, checks=[Check("Đồng bộ", f"Lỗi không mong đợi: {e}", False)])
            with SessionLocal() as db:
                src = db.get(Source, source_id)
                if src is None:
                    return
                apply_inspection(db, src, insp)
                db.commit()
        finally:
            _in_flight.discard(source_id)


async def hourly_sync_loop() -> None:
    """Đồng bộ các nguồn có sync_mode = hourly khi lần đồng bộ cuối đã quá 1 giờ."""
    from datetime import timedelta, timezone

    while True:
        await asyncio.sleep(settings.hourly_sync_check_s)
        cutoff = now() - timedelta(hours=1)
        with SessionLocal() as db:
            due = [
                s.id
                for s in db.query(Source).filter(Source.sync_mode == "hourly", Source.status != "syncing").all()
                if s.synced_at is None or s.synced_at.replace(tzinfo=s.synced_at.tzinfo or timezone.utc) < cutoff
            ]
        for sid in due:
            await sync_source(sid)
