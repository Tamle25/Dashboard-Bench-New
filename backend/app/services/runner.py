"""Thực thi lượt chạy.

Phiên bản khung này chạy ngay trong tiến trình API bằng asyncio (đủ cho một máy).
Khi cần nhiều worker: tách `RunManager._execute` sang hàng đợi (Arq/Celery/RQ) và thay
pub/sub trong bộ nhớ bằng Redis pub/sub; API và giao diện giữ nguyên.
"""

import asyncio
import json
import random
from collections import Counter, defaultdict
from collections.abc import Callable
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal
from ..models import ModelEndpoint, Run, RunEntry, RunLog, SampleResult, Source
from ..timeutil import iso, now
from . import git_ops, harbor, llm, scoring
from .manifest import safe_join
from .secrets import secrets, source_secret_name


ACTIVE = ("queued", "running")


class SampleFailed(Exception):
    pass


def short_sha(sha: str | None) -> str:
    return (sha or "—")[:7]


def log_dict(row: RunLog) -> dict:
    return {"id": row.id, "t": iso(row.ts), "lvl": row.level, "msg": row.message}


def run_snapshot(run: Run) -> dict:
    bench = run.bench
    src = bench.source
    return {
        "id": run.id,
        "status": run.status,
        "bench": {"id": bench.id, "name": bench.name, "scoring": scoring.SCORING_LABEL.get(bench.scoring_type, bench.scoring_type), "judge_required": bench.judge_required},
        "source": f"{src.repo}@{short_sha(run.source_sha)}",
        "judge": run.judge_id,
        "threshold": run.threshold,
        "params": run.params,
        "warns": run.warns,
        "errs": run.errs,
        "error": run.error,
        "created_at": iso(run.created_at),
        "started_at": iso(run.started_at),
        "finished_at": iso(run.finished_at),
        "entries": [
            {"id": e.id, "model": e.model_id, "total": e.total, "done": e.done, "passed": e.passed, "review": e.review, "score": e.score, "status": e.status, "error": e.error}
            for e in run.entries
        ],
    }


def load_samples(path: Path) -> list[dict]:
    samples = []
    with path.open(encoding="utf-8") as f:
        for i, line in enumerate(f, start=1):
            if not line.strip():
                continue
            item = json.loads(line)
            item.setdefault("id", str(i))
            item["id"] = str(item["id"])
            samples.append(item)
    return samples


def to_messages(inp) -> list[dict]:
    if isinstance(inp, list):
        return inp
    return [{"role": "user", "content": str(inp)}]


def prompt_text(inp) -> str:
    if isinstance(inp, list):
        return "\n".join(f"[{m.get('role', '?')}] {m.get('content', '')}" for m in inp)
    return str(inp)


def prepare_workspace(src: Source, sha: str | None, run_id: int) -> tuple[Path, Callable[[], None]]:
    """Trả về thư mục chứa đúng commit đã ghim của repo bench, kèm hàm dọn dẹp."""
    if git_ops.is_local(src.url):
        return Path(src.url).expanduser().resolve(), lambda: None
    if not sha:
        raise RuntimeError("Nguồn bench chưa đồng bộ thành công lần nào.")
    repo_dir = git_ops.repo_cache_dir(settings.cache_dir, src.url)
    dest = (settings.worktree_dir / f"run-{run_id}").resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    git_ops.checkout_pinned(repo_dir, src.url, sha, src.auth, secrets.get(source_secret_name(src.id)), dest)
    return dest, lambda: git_ops.remove_worktree(repo_dir, dest)


def remove_stale_worktrees(paths: list[Path]) -> None:
    for p in paths:
        if p.is_dir():
            git_ops.remove_tree(p)
    if settings.cache_dir.is_dir():
        for repo in (d for d in settings.cache_dir.iterdir() if d.is_dir()):
            git_ops.prune_worktrees(repo)


class RunManager:
    def __init__(self) -> None:
        self.tasks: dict[int, asyncio.Task] = {}
        self.subs: dict[int, set[asyncio.Queue]] = defaultdict(set)
        self.client: httpx.AsyncClient | None = None
        self._bg_cleanup: asyncio.Task | None = None

    # ---------- vòng đời ----------
    async def startup(self) -> None:
        self.client = httpx.AsyncClient()
        interrupted: list[int] = []
        with SessionLocal() as db:
            for run in db.scalars(select(Run).where(Run.status.in_(ACTIVE))):
                run.status = "failed"
                run.error = "Máy chủ khởi động lại khi lượt chạy đang diễn ra."
                run.finished_at = now()
                interrupted.append(run.id)
                for e in run.entries:
                    if e.status in ACTIVE:
                        e.status = "failed"
            db.commit()
        # Máy chủ chết giữa lượt chạy (crash, bị giết) thì không ai gỡ container của các task
        # đang dở, và agent bên trong vẫn chạy tiếp. Dọn ở đây.
        for run_id in interrupted:
            jobs_dir = settings.data_dir / "harbor-jobs" / f"run-{run_id}"
            if jobs_dir.is_dir():
                for job_dir in (d for d in jobs_dir.iterdir() if d.is_dir()):
                    await asyncio.to_thread(harbor.remove_job_containers, job_dir)
        # Worktree của lượt chạy bị ngắt (máy chủ tắt/khởi động lại giữa chừng) không ai xoá, mỗi
        # cái là một bản checkout đầy đủ của repo bench (vài trăm MB). Lúc này chưa lượt nào chạy,
        # nên chụp danh sách ngay rồi xoá ở nền: worktree của lượt mới tạo sau đó không bị đụng.
        stale = sorted(settings.worktree_dir.iterdir()) if settings.worktree_dir.is_dir() else []
        if stale:
            self._bg_cleanup = asyncio.create_task(asyncio.to_thread(remove_stale_worktrees, stale))

    async def shutdown(self) -> None:
        for t in list(self.tasks.values()):
            t.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        if self.client:
            await self.client.aclose()

    # ---------- pub/sub cho SSE ----------
    def subscribe(self, run_id: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=5000)
        self.subs[run_id].add(q)
        return q

    def unsubscribe(self, run_id: int, q: asyncio.Queue) -> None:
        self.subs[run_id].discard(q)
        if not self.subs[run_id]:
            self.subs.pop(run_id, None)

    def publish(self, run_id: int, kind: str, data: dict) -> None:
        for q in list(self.subs.get(run_id, ())):
            try:
                q.put_nowait({"type": kind, "data": data})
            except asyncio.QueueFull:
                pass  # client quá chậm: bỏ sự kiện, client sẽ tự đồng bộ lại khi kết nối lại

    def log(self, db: Session, run: Run, level: str, message: str) -> None:
        row = RunLog(run_id=run.id, level=level, message=message)
        db.add(row)
        db.commit()
        self.publish(run.id, "log", log_dict(row))

    def progress(self, run: Run) -> None:
        self.publish(run.id, "progress", run_snapshot(run))

    # ---------- điều khiển ----------
    def is_busy(self) -> bool:
        return any(not t.done() for t in self.tasks.values())

    def start(self, run_id: int) -> None:
        self.tasks[run_id] = asyncio.create_task(self._execute(run_id))

    def cancel(self, run_id: int) -> bool:
        t = self.tasks.get(run_id)
        if t and not t.done():
            t.cancel()
            return True
        return False

    # ---------- thực thi ----------
    async def _execute(self, run_id: int) -> None:
        db = SessionLocal()
        run = db.get(Run, run_id)
        cleanup: Callable[[], None] = lambda: None
        try:
            bench = run.bench
            src = bench.source
            p = run.params
            run.status = "running"
            run.started_at = now()
            for e in run.entries:
                e.status = "running"
            db.commit()
            self.progress(run)

            self.log(db, run, "INFO", f"Lấy bộ bench từ {src.repo}@{short_sha(run.source_sha)}")
            root, cleanup = await asyncio.to_thread(prepare_workspace, src, run.source_sha, run.id)
            if bench.scoring_type == "harbor":
                await self._run_harbor(db, run, bench, root)
            else:
                await self._run_chat(db, run, bench, root)

            for e in run.entries:
                if e.status == "failed":
                    continue
                rows = db.scalars(select(SampleResult).where(SampleResult.entry_id == e.id, SampleResult.needs_review.is_(False))).all()
                e.finished_at = now()
                if not rows:
                    # Không mẫu nào chấm được (vd. giám khảo lỗi, verifier Harbor không trả reward).
                    # Để điểm trống và trạng thái failed: nếu gán 0 điểm, bảng xếp hạng sẽ hiểu
                    # là mô hình trả lời sai hết, trong khi thực tế là lỗi cấu hình.
                    e.score = None
                    e.status = "failed"
                    e.error = e.error or f"Không chấm được mẫu nào trong {e.total} mẫu ({e.review} cần xem lại)."
                    self.log(db, run, "ERROR", f"{e.model_id} không có mẫu nào chấm được ({e.review}/{e.total} cần xem lại) → không tính điểm")
                    continue
                e.score = round(sum(r.value for r in rows) / len(rows) * 100, 1)
                e.status = "done"
                self.log(db, run, "DONE", f"{e.model_id} hoàn tất {e.done}/{e.total} mẫu, điểm {e.score:.1f}".replace(".", ","))
            run.status = "done" if any(e.status == "done" for e in run.entries) else "failed"
            secs = int((now() - run.started_at.replace(tzinfo=now().tzinfo)).total_seconds()) if run.started_at else 0
            self.log(db, run, "DONE", f"Lượt chạy R-{run.id} kết thúc sau {secs} giây · {run.warns} cảnh báo · {run.errs} lỗi")
        except asyncio.CancelledError:
            db.rollback()
            run.status = "stopped"
            for e in run.entries:
                if e.status in ACTIVE:
                    e.status = "stopped"
            self.log(db, run, "WARN", "Người dùng đã dừng lượt chạy. Kết quả dở dang được giữ trong log nhưng không tính điểm.")
        except Exception as exc:  # lỗi chuẩn bị (git, dữ liệu, scorer…)
            db.rollback()
            run.status = "failed"
            run.error = str(exc)
            for e in run.entries:
                if e.status in ACTIVE:
                    e.status = "failed"
            self.log(db, run, "ERROR", f"Lượt chạy thất bại: {exc}")
        finally:
            run.finished_at = now()
            db.commit()
            self.progress(run)
            self.publish(run.id, "end", {})
            try:
                await asyncio.to_thread(cleanup)
            except Exception:
                pass
            db.close()
            self.tasks.pop(run_id, None)

    def _pick(self, items: list, p: dict) -> list:
        n = len(items) if p["samples"] == "all" else min(int(p["samples"]), len(items))
        rng = random.Random(p["seed"])
        return [items[i] for i in sorted(rng.sample(range(len(items)), n))]

    # ---------- bộ bench hỏi–đáp: gọi thẳng API mô hình ----------
    async def _run_chat(self, db: Session, run: Run, bench, root: Path) -> None:
        p = run.params
        all_samples = load_samples(safe_join(root, bench.data_path))
        picked = self._pick(all_samples, p)
        n = len(picked)
        self.log(db, run, "INFO", f'Tải bộ bench "{bench.name}": dùng {n}/{len(all_samples)} mẫu, seed {p["seed"]}')

        endpoints = {e.model_id: db.get(ModelEndpoint, e.model_id) for e in run.entries}
        for e in run.entries:
            e.total = n
            self.log(db, run, "INFO", f"Khởi tạo thí sinh {e.model_id} (temperature {p['temperature']}, max_tokens {p['max_tokens']})")

        judge = db.get(ModelEndpoint, run.judge_id) if bench.judge_required else None
        rubric = scoring.DEFAULT_RUBRIC
        if bench.rubric_path:
            rubric = safe_join(root, bench.rubric_path).read_text(encoding="utf-8")
        scorer_fn = scoring.load_custom_scorer(root, bench.scorer) if bench.scoring_type == "custom" else None
        if judge:
            self.log(db, run, "INFO", f"Khởi tạo giám khảo {judge.id} · {bench.rubric_path or 'rubric mặc định'} · ngưỡng đạt ≥ {run.threshold:g}/10")
        else:
            self.log(db, run, "INFO", f"Chấm tự động: {scoring.SCORING_LABEL.get(bench.scoring_type)}")
        self.log(db, run, "INFO", f"Bắt đầu với {p['concurrency']} luồng song song")
        db.commit()
        self.progress(run)

        sem = asyncio.Semaphore(int(p["concurrency"]))
        stopped: set[int] = set()

        async def one(entry: RunEntry, sample: dict) -> None:
            async with sem:
                if entry.id in stopped:
                    return
                try:
                    await self._sample(db, run, entry, endpoints[entry.model_id], judge, bench.scoring_type, rubric, scorer_fn, sample)
                except SampleFailed as exc:
                    if entry.id not in stopped:
                        stopped.add(entry.id)
                        entry.status = "failed"
                        entry.error = str(exc)
                        run.errs += 1
                        self.log(db, run, "ERROR", f"{exc} → dừng thí sinh này")
                self.progress(run)

        await asyncio.gather(*(one(e, s) for e in run.entries for s in picked))

    # ---------- bộ bench Harbor / Terminal-Bench: mỗi task một tiến trình `harbor run` ----------
    async def _run_harbor(self, db: Session, run: Run, bench, root: Path) -> None:
        p = run.params
        opts = bench.options or {}
        version = await asyncio.to_thread(harbor.preflight)
        self.log(db, run, "INFO", f"Harbor {version} · môi trường {settings.harbor_env}")
        if settings.apt_mirror.strip():
            self.log(db, run, "INFO", f"apt trong container dùng mirror {settings.apt_mirror.strip()}")
        if opts.get("prepare"):
            self.log(db, run, "INFO", "Chạy bước prepare: " + " && ".join(opts["prepare"]))
            await asyncio.to_thread(harbor.run_prepare, root, opts["prepare"])

        all_tasks = harbor.list_tasks(safe_join(root, bench.data_path), opts.get("exclude"))
        if not all_tasks:
            raise RuntimeError(f"Không có task nào trong {bench.data_path}")
        picked = self._pick(all_tasks, p)
        self.log(db, run, "INFO", f'Bộ bench "{bench.name}": dùng {len(picked)}/{len(all_tasks)} task, seed {p["seed"]}')

        endpoints = {e.model_id: db.get(ModelEndpoint, e.model_id) for e in run.entries}
        for e in run.entries:
            e.total = len(picked)
            ep = endpoints[e.model_id]
            agent = ep.agent if ep.kind == "harbor" else (ep.agent or "terminus-2")
            model = ep.model_name or (ep.id if ep.kind == "openai" else "")
            self.log(db, run, "INFO", f"Thí sinh {ep.id}: agent {agent}" + (f", model {model}" if model else ""))
        self.log(db, run, "INFO", f"Chấm bằng verifier của từng task · {p['concurrency']} task song song · mỗi task chạy trong môi trường riêng")
        db.commit()
        self.progress(run)

        jobs_dir = (settings.data_dir / "harbor-jobs" / f"run-{run.id}").resolve()
        sem = asyncio.Semaphore(int(p["concurrency"]))

        # Gỡ image Docker của task ngay khi không còn thí sinh nào cần nó (image dùng chung giữa
        # các task, vd. ask-human của HiL-Bench, chờ tới lượt dùng cuối). Log/kết quả giữ nguyên.
        prebuilt = {t.name: harbor.task_images(t.path) for t in picked}
        task_left = Counter({t.name: len(run.entries) for t in picked})
        image_left = Counter(ref for t in picked for ref in prebuilt[t.name] for _ in run.entries)
        started: set[str] = set()
        cleaned: set[str] = set()

        def trials_of(task_name: str) -> list[str]:
            return [n for e in run.entries for n in harbor.trial_names(jobs_dir / f"e{e.id}-{task_name}")]

        async def release(task: harbor.TaskInfo) -> None:
            task_left[task.name] -= 1
            for ref in prebuilt[task.name]:
                image_left[ref] -= 1
            if task_left[task.name] > 0:
                return
            cleaned.add(task.name)
            refs = {r for r in prebuilt[task.name] if image_left[r] <= 0}
            n, size = await asyncio.to_thread(harbor.cleanup_task_images, refs, trials_of(task.name))
            if n:
                self.log(db, run, "INFO", f"Dọn task {task.name}: gỡ {n} image Docker ({harbor.fmt_bytes(size)})")

        async def one(entry: RunEntry, task: harbor.TaskInfo) -> None:
            async with sem:
                started.add(task.name)
                await self._harbor_task(db, run, entry, endpoints[entry.model_id], task, jobs_dir)
                self.progress(run)
                await release(task)  # vẫn giữ chỗ trong sem: task sau chưa kéo image khi đang gỡ

        try:
            await asyncio.gather(*(one(e, t) for e in run.entries for t in picked))
        finally:
            # Lượt chạy bị dừng / lỗi giữa chừng: gỡ image của các task đã bắt đầu mà chưa dọn.
            leftover = [n for n in started if n not in cleaned]
            if leftover:
                refs = set().union(*(prebuilt[n] for n in leftover))
                names = [t for n in leftover for t in trials_of(n)]
                await asyncio.shield(asyncio.to_thread(harbor.cleanup_task_images, refs, names))

    @staticmethod
    def _stop_harbor(proc, job_dir: Path) -> None:
        """Dừng Harbor rồi gỡ container của task: agent chạy trong container, không gỡ thì
        nó vẫn chạy tiếp sau khi người dùng bấm Dừng."""
        harbor.stop_process(proc)
        harbor.kill_orphan_compose(job_dir)
        harbor.remove_job_containers(job_dir)

    async def _harbor_task(self, db: Session, run: Run, entry: RunEntry, ep: ModelEndpoint, task, jobs_dir: Path) -> None:
        job = f"e{entry.id}-{task.name}"
        cmd, env = harbor.build_command(ep, task.path, jobs_dir, job)
        self.log(db, run, "INFO", f"{ep.id} · task {task.name} · bắt đầu")
        proc = harbor.start_process(cmd, env, jobs_dir / f"{job}.log")
        try:
            rc = await asyncio.to_thread(proc.wait)
        except asyncio.CancelledError:
            # shield: dọn dẹp phải chạy hết dù task bị huỷ thêm lần nữa (vd. tắt máy chủ).
            await asyncio.shield(asyncio.to_thread(self._stop_harbor, proc, jobs_dir / job))
            raise
        if await asyncio.to_thread(harbor.kill_orphan_compose, jobs_dir / job):
            await asyncio.to_thread(harbor.remove_job_containers, jobs_dir / job)
            run.warns += 1
            self.log(db, run, "WARN", f"{ep.id} · task {task.name} · Harbor để lại tiến trình docker compose đang chạy → đã dừng")
        outcome = harbor.read_trial(jobs_dir / job)

        if outcome is None or not outcome.valid:
            why = outcome.exception if outcome and outcome.exception else (
                f"harbor thoát mã {rc}: {harbor.tail(jobs_dir / f'{job}.log', 300).splitlines()[-1:] or ['(không có log)']}"
            )
            run.errs += 1
            entry.review += 1
            entry.done += 1
            db.add(SampleResult(
                entry_id=entry.id, sample_id=task.name, category=task.category, prompt=task.instruction,
                output=f"Trial: {outcome.trial_dir if outcome else jobs_dir / job}", reference="", value=0.0, raw_score="—",
                passed=False, note=f"Không có reward hợp lệ: {why}", needs_review=True, latency_ms=(outcome.seconds * 1000 if outcome else 0),
                tokens=outcome.tokens if outcome else 0,
            ))
            db.commit()
            self.log(db, run, "ERROR", f"{ep.id} · task {task.name} · không chấm được ({why}) → đánh dấu cần xem lại")
            return

        reward = outcome.reward or 0.0
        passed = reward >= 1.0 - 1e-9
        raw = f"{reward:g}"
        note = outcome.verifier_tail[-1200:] if outcome.verifier_tail else ""
        if outcome.exception:
            note = f"{outcome.exception}\n\n{note}".strip()
        cost = f" · ${outcome.cost_usd:.2f}" if outcome.cost_usd else ""
        db.add(SampleResult(
            entry_id=entry.id, sample_id=task.name, category=task.category, prompt=task.instruction,
            output=f"Trial: {outcome.trial_dir}{cost}", reference="", value=max(0.0, min(1.0, reward)), raw_score=raw,
            passed=passed, note=note, needs_review=False, latency_ms=outcome.seconds * 1000, tokens=outcome.tokens,
        ))
        entry.done += 1
        if passed:
            entry.passed += 1
        db.commit()
        level = "WARN" if outcome.exception else "INFO"
        if outcome.exception:
            run.warns += 1
        mins = f"{outcome.seconds // 60}p{outcome.seconds % 60:02d}s"
        extra = f" · {outcome.exception.split(':')[0]}" if outcome.exception else ""
        self.log(db, run, level, f"{ep.id} · task {task.name} · reward {raw} · {mins} · {outcome.tokens} tok{cost}{extra}")

    async def _call_with_retry(self, db: Session, run: Run, ep: ModelEndpoint, sid: str, messages: list[dict], **kw) -> llm.ChatResult:
        retries = max(1, settings.model_retries)
        for attempt in range(1, retries + 1):
            try:
                return await llm.chat(self.client, ep, messages, **kw)
            except llm.ModelCallError as e:
                if not e.retryable or attempt == retries:
                    raise SampleFailed(f"{ep.id} · mẫu #{sid} {e} sau {attempt} lần thử") from e
                run.warns += 1
                self.log(db, run, "WARN", f"{ep.id} · mẫu #{sid} {e}, thử lại {attempt}/{retries - 1}")
                await asyncio.sleep(min(8, 2 ** attempt))
        raise AssertionError("unreachable")

    async def _sample(self, db: Session, run: Run, entry: RunEntry, ep: ModelEndpoint, judge: ModelEndpoint | None,
                      scoring_type: str, rubric: str, scorer_fn, sample: dict) -> None:
        p = run.params
        sid = sample["id"]
        reference = str(sample.get("reference", ""))
        mock_reply = None
        if ep.kind == "mock":
            r = llm._rng(ep.id, p["seed"], sid)
            mock_reply = reference if r.random() < llm.mock_quality(ep) else "Tôi không chắc về câu trả lời."

        res = await self._call_with_retry(
            db, run, ep, sid, to_messages(sample["input"]),
            temperature=float(p["temperature"]), max_tokens=int(p["max_tokens"]), seed=int(p["seed"]), mock_reply=mock_reply,
        )

        if scoring_type == "exact_match":
            sc = scoring.exact_match(res.text, reference)
        elif scoring_type == "unit_test":
            sc = await asyncio.to_thread(scoring.unit_test, res.text, str(sample.get("test", "")))
        elif scoring_type == "custom":
            sc = scoring.custom(scorer_fn, sample, res.text)
        else:  # llm_judge
            sc = await self._judge(db, run, judge, rubric, sample, res.text)

        db.add(SampleResult(
            entry_id=entry.id, sample_id=sid, category=str(sample.get("category", "")), prompt=prompt_text(sample["input"]),
            output=res.text, reference=reference, value=sc.value, raw_score=sc.raw, passed=sc.passed, note=sc.note,
            needs_review=sc.needs_review, latency_ms=res.latency_ms, tokens=res.tokens,
        ))
        entry.done += 1
        if sc.passed:
            entry.passed += 1
        if sc.needs_review:
            entry.review += 1
        db.commit()

        if sc.needs_review:
            return  # đã ghi log ERROR ở bước chấm
        verdict = f"giám khảo {sc.raw}" if scoring_type == "llm_judge" else ("đúng" if sc.passed else "sai")
        secs = f"{res.latency_ms / 1000:.1f}".replace(".", ",")
        self.log(db, run, "INFO", f"{ep.id} · mẫu #{sid} · {res.tokens} tok · {secs}s · {verdict}")

    async def _judge(self, db: Session, run: Run, judge: ModelEndpoint, rubric: str, sample: dict, output: str) -> scoring.Score:
        sid = sample["id"]
        reference = str(sample.get("reference", ""))
        mock_reply = None
        if judge.kind == "mock":
            mock_reply = scoring.mock_judge_reply(output, reference, llm._rng(judge.id, sid, output).random())
        msgs = scoring.judge_messages(rubric, prompt_text(sample["input"]), reference, output)
        try:
            res = await self._call_with_retry(db, run, judge, sid, msgs, temperature=0.0, max_tokens=512, seed=int(run.params["seed"]), mock_reply=mock_reply)
            score10, reason = scoring.parse_judge(res.text)
        except (SampleFailed, ValueError, KeyError, json.JSONDecodeError) as e:
            run.errs += 1
            what = "giám khảo trả JSON không hợp lệ" if not isinstance(e, SampleFailed) else f"giám khảo lỗi ({e})"
            self.log(db, run, "ERROR", f"{judge.id} · mẫu #{sid} {what} → đánh dấu cần xem lại")
            return scoring.Score(0.0, "—", False, f"Không chấm được: {e}", needs_review=True)
        return scoring.judge_score(score10, reason, run.threshold)


manager = RunManager()
