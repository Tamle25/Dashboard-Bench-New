"""Chạy bộ bench dạng Harbor / Terminal-Bench.

Mỗi task là một thư mục:
    task.toml        cấu hình (timeout, tài nguyên, metadata.category…)
    instruction.md   đề bài cho agent
    environment/     Dockerfile môi trường agent làm việc
    tests/           verifier, ghi reward vào /logs/verifier/reward.{txt,json}
    solution/        lời giải mẫu (agent "oracle" dùng để kiểm tra task)

Backend gọi `harbor run -p <task> -a <agent> …` cho từng task (một tiến trình / task),
rồi đọc <jobs_dir>/<job>/<trial>/result.json để lấy reward.
"""

import fnmatch
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..config import settings
from ..models import ModelEndpoint
from .secrets import secrets

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "jobs"}
# Khi tự nhận diện (repo không có bench.yaml): bỏ các thư mục công cụ / fixture / mẫu,
# vd. scripts/checks/test-tasks, leaderboard/…/ci, vn-bench/layer-b/_template của terminal-bench.
NOISE_DIRS = {"scripts", "tools", "leaderboard", "docs", "examples", "fixtures", "templates", "test-tasks", "build"}
MIN_AUTO_TASKS = 2
INSTRUCTION_MAX = 6000


def _skip_part(name: str) -> bool:
    return name in SKIP_DIRS or name in NOISE_DIRS or name.startswith((".", "_"))


class HarborError(Exception):
    pass


# ---------------------------------------------------------------- nhận diện task

@dataclass
class TaskInfo:
    name: str  # tên thư mục
    path: Path
    category: str
    instruction: str


def is_excluded(name: str, exclude: list[str] | None) -> bool:
    """Tên task khớp một mục trong `exclude`: tên chính xác hoặc mẫu glob (vd. `*-ask_human`)."""
    return any(name == pat or fnmatch.fnmatchcase(name, pat) for pat in exclude or [])


def list_tasks(task_root: Path, exclude: list[str] | None = None) -> list[TaskInfo]:
    out: list[TaskInfo] = []
    for d in sorted(p for p in task_root.iterdir() if p.is_dir()):
        toml = d / "task.toml"
        if is_excluded(d.name, exclude) or d.name.startswith(("_", ".")) or not toml.is_file():
            continue
        category = ""
        try:
            meta = tomllib.loads(toml.read_text(encoding="utf-8")).get("metadata", {})
            category = str(meta.get("category") or "")
        except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError):
            pass
        instr = ""
        if (d / "instruction.md").is_file():
            instr = (d / "instruction.md").read_text(encoding="utf-8", errors="replace")[:INSTRUCTION_MAX]
        out.append(TaskInfo(d.name, d, category, instr))
    return out


def detect_harbor_dirs(root: Path, max_depth: int = 4) -> list[tuple[str, int]]:
    """Tìm các thư mục chứa task Harbor (thư mục con có task.toml). Trả về [(đường dẫn tương đối, số task)]."""
    found: list[tuple[str, int]] = []
    root = root.resolve()
    for cur, dirs, files in os.walk(root):
        rel = Path(cur).relative_to(root)
        depth = 0 if str(rel) == "." else len(rel.parts)
        if "task.toml" in files:  # bản thân là một task: không đi sâu hơn
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if not _skip_part(d)]
        n = sum(1 for d in dirs if (Path(cur) / d / "task.toml").is_file())
        if n >= MIN_AUTO_TASKS and str(rel) != ".":
            found.append((rel.as_posix(), n))
        if depth >= max_depth:
            dirs[:] = []
    return found


def detect_harbor_dirs_from_paths(paths: set[str], max_depth: int = 4) -> list[tuple[str, int]]:
    """Như detect_harbor_dirs nhưng chỉ dựa trên danh sách đường dẫn file (từ `git ls-tree`)."""
    from collections import Counter

    task_dirs = {p.rsplit("/", 1)[0] for p in paths if p.endswith("/task.toml")}
    parents: Counter[str] = Counter()
    for td in task_dirs:
        if "/" not in td:
            continue
        parent, name = td.rsplit("/", 1)
        parts = parent.split("/")
        if len(parts) > max_depth or parent in task_dirs or _skip_part(name) or any(_skip_part(p) for p in parts):
            continue
        if any("/".join(parts[:i]) in task_dirs for i in range(1, len(parts))):
            continue  # nằm bên trong một task khác
        parents[parent] += 1
    return sorted((d, n) for d, n in parents.items() if n >= MIN_AUTO_TASKS)


def count_tasks_from_paths(paths: set[str], data: str, exclude: list[str] | None = None) -> int:
    prefix = data.strip("/") + "/"
    names = {p[len(prefix):].split("/", 1)[0] for p in paths if p.startswith(prefix) and p.endswith("/task.toml") and p[len(prefix):].count("/") == 1}
    return len({n for n in names if not is_excluded(n, exclude) and not n.startswith(("_", "."))})


def synthesize_manifest(root: Path | None = None, dirs: list[tuple[str, int]] | None = None) -> str | None:
    """Sinh manifest YAML tạm cho repo Harbor chưa có bench.yaml."""
    import yaml

    if dirs is None:
        dirs = detect_harbor_dirs(root)
    if not dirs:
        return None
    benches = []
    for rel, _n in dirs:
        bid = re.sub(r"[^a-z0-9]+", "-", rel.lower()).strip("-")[:64] or "tasks"
        benches.append({"id": bid, "name": rel, "task": f"Task Harbor trong {rel}/", "data": rel, "scoring": {"type": "harbor"}})
    return yaml.safe_dump({"version": 1, "benches": benches}, allow_unicode=True, sort_keys=False)


# ---------------------------------------------------------------- bước prepare

def run_prepare(root: Path, commands: list[str]) -> None:
    """Chạy lệnh chuẩn bị của manifest trong thư mục repo.

    CẢNH BÁO: đây là thực thi code từ repo bench. Chỉ kết nối repo bạn tin tưởng.
    "python" ở đầu lệnh được thay bằng trình thông dịch của backend.
    """
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    for cmd in commands:
        args = shlex.split(cmd, posix=True)
        if not args:
            continue
        if args[0] in ("python", "python3"):
            args[0] = sys.executable
        try:
            p = subprocess.run(args, cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=settings.prepare_timeout_s)
        except FileNotFoundError as e:
            raise HarborError(f"Bước prepare: không tìm thấy lệnh '{args[0]}'") from e
        except subprocess.TimeoutExpired as e:
            raise HarborError(f"Bước prepare quá {settings.prepare_timeout_s}s: {cmd}") from e
        if p.returncode != 0:
            tail = (p.stderr or p.stdout or "").strip().splitlines()[-3:]
            raise HarborError(f"Bước prepare thất bại ({cmd}): {' | '.join(tail)[:400]}")


# ---------------------------------------------------------------- kiểm tra môi trường

def preflight() -> str:
    """Kiểm tra Harbor CLI và môi trường chạy. Trả về phiên bản Harbor."""
    exe = settings.harbor_cmd[0]
    if not (shutil.which(exe) or Path(exe).is_file()):
        raise HarborError(f"Không tìm thấy lệnh '{exe}'. Cài bằng: uv tool install harbor (hoặc đặt BENCH_HARBOR_CMD).")
    try:
        v = subprocess.run([*settings.harbor_cmd, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        version = (v.stdout or v.stderr).strip().splitlines()[-1] if (v.stdout or v.stderr).strip() else "?"
    except (subprocess.TimeoutExpired, OSError) as e:
        raise HarborError(f"Không chạy được Harbor CLI: {e}") from e
    if settings.harbor_env == "docker":
        try:
            d = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True, timeout=30)
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            raise HarborError("Không gọi được Docker. Cài Docker Desktop, hoặc đặt BENCH_HARBOR_ENV=modal/daytona.") from e
        if d.returncode != 0:
            raise HarborError("Docker chưa chạy. Mở Docker Desktop rồi chạy lại.")
    return version


# Một dòng của bảng `harbor agent list` khi CLI không hỗ trợ --json. Viền bảng là ký tự
# box-drawing Unicode (│), không phải "|" ASCII — nhiều terminal hiển thị thành "|".
_AGENT_ROW = re.compile(r"^[|│]\s*([A-Za-z0-9][A-Za-z0-9._:@/-]*)\s*[|│]$")
_agents_cache: list[str] | None = None


def _run_agent_list(*args: str) -> str | None:
    try:
        p = subprocess.run([*settings.harbor_cmd, "agent", "list", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout if p.returncode == 0 else None


def list_agents(refresh: bool = False) -> list[str]:
    """Tên các agent Harbor cài sẵn (đọc `harbor agent list`, nhớ lại trong tiến trình).

    Trả về danh sách rỗng khi không gọi được CLI — khi đó bỏ qua bước kiểm tra tên thay vì
    chặn người dùng vì một lệnh phụ không chạy được.
    """
    global _agents_cache
    if _agents_cache is not None and not refresh:
        return _agents_cache
    names: list[str] = []
    out = _run_agent_list("--json")
    if out:
        try:
            data = json.loads(out)
            names = [str(x) for x in data] if isinstance(data, list) else []
        except json.JSONDecodeError:
            names = []
    if not names:  # Harbor cũ chưa có --json: đọc bảng đã render
        out = _run_agent_list()
        names = [m.group(1) for line in (out or "").splitlines() if (m := _AGENT_ROW.match(line.strip())) and m.group(1).lower() != "name"]
    if names:
        _agents_cache = names
    return names


def check_agent(name: str) -> None:
    """Báo lỗi nếu Harbor không có agent tên này.

    Bỏ qua agent dạng đường dẫn tuỳ chỉnh (`module.path:Class`) và shorthand ACP
    (`acp:opencode@1.3.9`): hai dạng này không có trong danh sách và chỉ Harbor mới giải được.
    """
    if not name or ":" in name or "/" in name or "." in name:
        return
    agents = list_agents()
    if not agents or name in agents:
        return
    # Gợi ý tên gần đúng: lỗi hay gặp nhất là sai dấu gạch ("terminus2" ↔ "terminus-2"),
    # nên so cả bản đã bỏ hết ký tự ngăn cách.
    flat = re.sub(r"[^a-z0-9]", "", name.lower())
    low = name.lower()
    near = [a for a in agents if re.sub(r"[^a-z0-9]", "", a.lower()) == flat]
    near += [a for a in agents if a not in near and (low in a.lower() or a.lower() in low)]
    hint = f" Có phải: {', '.join(near[:3])}?" if near else ""
    raise HarborError(f"Harbor không có agent '{name}'.{hint} Xem danh sách bằng: harbor agent list")


# ---------------------------------------------------------------- dựng lệnh

# Xác thực bằng gói Claude Pro/Max thay cho API key: chỉ agent claude-code hỗ trợ.
OAUTH_AGENT = "claude-code"
OAUTH_ENV = "CLAUDE_CODE_OAUTH_TOKEN"

# Module chạy trong Python của Harbor (không phải của backend), nạp qua PYTHONPATH.
PLUGINS_DIR = Path(__file__).resolve().parents[2] / "harbor_plugins"
APT_MIRROR_MODULE = "bench_apt_mirror"


def is_import_path(agent: str) -> bool:
    """Agent tuỳ chỉnh dạng `module.path:Class` (không tính shorthand ACP `acp:…`)."""
    return ":" in agent and not agent.startswith("acp:")


def _agent_arg(agent: str, env: dict) -> str:
    """Tên agent truyền cho `harbor run -a`. Khi đặt BENCH_APT_MIRROR thì bọc agent cài sẵn bằng
    bench_apt_mirror để apt trong container dùng mirror. Agent dạng đường dẫn tuỳ chỉnh giữ
    nguyên tên (agent trong harbor_plugins/ tự đọc BENCH_APT_MIRROR)."""
    mirror = settings.apt_mirror.strip()
    if mirror:
        env["BENCH_APT_MIRROR"] = mirror
    if not mirror or ":" in agent or "/" in agent or "." in agent:
        return agent
    return f"{APT_MIRROR_MODULE}:{agent}"


def _pass_env(env: dict) -> None:
    """Chuyển các bí mật khớp BENCH_HARBOR_PASS_ENV (tên hoặc mẫu glob, vd. `NODE_AGENT_*`) từ
    backend/.env / data/secrets.json sang môi trường của Harbor. Biến đã có trong môi trường
    của backend thì giữ nguyên."""
    for name in secrets.names():
        if name not in env and any(fnmatch.fnmatchcase(name, pat) for pat in settings.harbor_pass_env):
            value = secrets.get(name)
            if value:
                env[name] = value

def build_command(ep: ModelEndpoint, task_path: Path, jobs_dir: Path, job_name: str) -> tuple[list[str], dict]:
    cmd = [
        *settings.harbor_cmd, "run",
        "-p", str(task_path), "-o", str(jobs_dir), "--job-name", job_name,
        "-e", settings.harbor_env, "-n", "1", "-y", "-q",
    ]
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    # Agent trong harbor_plugins/ (bench_apt_mirror, node_agent…) nạp được bằng `-a module:Class`.
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(PLUGINS_DIR), env.get("PYTHONPATH", "")) if p)
    _pass_env(env)
    oauth = ep.auth == "oauth"
    if ep.kind == "harbor":
        if not ep.agent:
            raise HarborError(f"{ep.id}: chưa đặt agent Harbor")
        check_agent(ep.agent)
        cmd += ["-a", _agent_arg(ep.agent, env)]
        if ep.model_name:
            cmd += ["-m", ep.model_name]
        if oauth:
            # Gói Claude Pro/Max: claude-code đọc CLAUDE_CODE_OAUTH_TOKEN, còn CLAUDE_FORCE_OAUTH
            # bắt nó bỏ API key. KHÔNG truyền --api-key-name: Harbor sẽ đổ token vào
            # ANTHROPIC_API_KEY và xác thực hỏng. Gỡ luôn API key có sẵn trong môi trường để
            # lượt chạy không âm thầm tính tiền theo API.
            env.pop("ANTHROPIC_API_KEY", None)
            env.pop("ANTHROPIC_AUTH_TOKEN", None)
            env["CLAUDE_FORCE_OAUTH"] = "1"
        elif ep.secret_name and not is_import_path(ep.agent):
            # Nói rõ cho Harbor biến môi trường nào chứa API key, nếu không nó chỉ tìm theo
            # tên mặc định của agent (codex: OPENAI_API_KEY) và ô "Tên bí mật" sẽ vô nghĩa.
            # Agent tuỳ chỉnh thường không khai MODEL_CONNECTION, và Harbor từ chối hẳn
            # (`does not support inference connection overrides`) nếu nhận cờ này: chỉ đặt
            # biến môi trường bên dưới, agent tự đọc.
            cmd += ["--api-key-name", ep.secret_name]
    elif ep.kind == "openai":
        check_agent(ep.agent or "terminus-2")
        cmd += ["-a", _agent_arg(ep.agent or "terminus-2", env), "-m", ep.model_name or ep.id, "--base-url", ep.base_url, "--api-format", "openai_chat_completions"]
        if ep.secret_name:
            cmd += ["--api-key-name", ep.secret_name]
    else:
        raise HarborError(f"{ep.id}: kiểu '{ep.kind}' không chạy được bộ bench Harbor")
    if ep.secret_name:
        value = secrets.get(ep.secret_name)
        if not value:
            raise HarborError(f"{ep.id}: thiếu bí mật {ep.secret_name}")
        env[ep.secret_name] = value
        if oauth:
            env[OAUTH_ENV] = value  # tên bí mật có thể khác tên biến claude-code đọc
    elif oauth:
        raise HarborError(f"{ep.id}: thiếu token OAuth")
    cmd += settings.harbor_extra_args
    return cmd, env


def start_process(cmd: list[str], env: dict, log_path: Path) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    kw: dict = {}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    f = open(log_path, "wb")
    try:
        return subprocess.Popen(cmd, env=env, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **kw)
    finally:
        f.close()  # tiến trình con giữ handle riêng


def stop_process(proc: subprocess.Popen, grace_s: float = 30) -> None:
    """Dừng nhẹ (để Harbor kịp dọn container), quá hạn thì giết cả cây tiến trình."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(proc.pid, signal.SIGINT)
        proc.wait(timeout=grace_s)
        return
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _docker_ids(kind: str, project: str) -> list[str]:
    r = subprocess.run(
        ["docker", kind, "ls", "-aq" if kind == "container" else "-q", "--filter", f"label=com.docker.compose.project={project}"],
        capture_output=True, text=True, timeout=30,
    )
    return r.stdout.split() if r.returncode == 0 else []


def remove_job_containers(job_dir: Path) -> list[str]:
    """Gỡ container + network của mọi trial trong một job Harbor. Trả về các project đã dọn.

    Phải gọi sau khi dừng Harbor: agent (vd. claude-code) chạy BÊN TRONG container qua
    `docker exec`, nên giết tiến trình Harbor trên máy chủ không làm agent dừng — nó cứ chạy
    tiếp và tiêu token/hạn mức. Harbor tự dọn khi thoát êm, nhưng trên Windows CTRL_BREAK
    giết Python ngay lập tức (không thành KeyboardInterrupt), nên bước dọn của nó không chạy.

    Tên project compose của Harbor là `<thư mục trial>__env`, viết thường.
    """
    if settings.harbor_env != "docker" or not job_dir.is_dir():
        return []
    removed: list[str] = []
    for trial in (d for d in job_dir.iterdir() if d.is_dir()):
        project = f"{trial.name.lower()}__env"
        try:
            ids = _docker_ids("container", project)
            if ids:
                subprocess.run(["docker", "rm", "-f", *ids], capture_output=True, timeout=120)
                removed.append(project)
            nets = _docker_ids("network", project)
            if nets:
                subprocess.run(["docker", "network", "rm", *nets], capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            continue  # Docker không phản hồi: không chặn việc dừng lượt chạy
    return removed


# ---------------------------------------------------------------- dọn image sau task
#
# Harbor tự gỡ container + volume khi xong trial (`compose down --rmi local`), nhưng `--rmi local`
# KHÔNG gỡ image có tên: image kéo sẵn (`docker_image`, `image:` của service) và image tự build
# (`hb__<hash>`, bản cũ là `<trial>__env-main`). Chúng tích lại, mỗi cái 1–9 GB.

COMPOSE_FILES = ("docker-compose.yaml", "docker-compose.yml")


def task_images(task_path: Path) -> set[str]:
    """Image kéo sẵn mà task khai báo: `docker_image` trong task.toml và `image:` của các service
    trong environment/docker-compose.yaml (bỏ giá trị chứa biến ${…})."""
    import yaml

    refs: set[str] = set()
    try:
        env = tomllib.loads((task_path / "task.toml").read_text(encoding="utf-8")).get("environment") or {}
        if isinstance(env.get("docker_image"), str) and env["docker_image"].strip():
            refs.add(env["docker_image"].strip())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        pass
    for name in COMPOSE_FILES:
        f = task_path / "environment" / name
        if not f.is_file():
            continue
        try:
            doc = yaml.safe_load(f.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            continue
        services = doc.get("services") if isinstance(doc, dict) else None
        for svc in (services or {}).values() if isinstance(services, dict) else ():
            img = svc.get("image") if isinstance(svc, dict) else None
            if isinstance(img, str) and img.strip() and "$" not in img:
                refs.add(img.strip())
    return refs


def _docker(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def built_images(trial_names: list[str]) -> set[str]:
    """ID các image compose đã build cho những trial này.

    Compose gắn nhãn `com.docker.compose.project` = project lúc build, mà project của Harbor là
    `<trial>__env` / `<trial>__verifier__trial` (viết thường).
    """
    prefixes = tuple(f"{t.lower()}__" for t in trial_names)
    if not prefixes:
        return set()
    r = _docker("image", "ls", "-q", "--no-trunc", "--filter", "label=com.docker.compose.project")
    ids = sorted(set(r.stdout.split())) if r.returncode == 0 else []
    if not ids:
        return set()
    r = _docker("image", "inspect", "--format", '{{.Id}} {{index .Config.Labels "com.docker.compose.project"}}', *ids)
    out: set[str] = set()
    for line in r.stdout.splitlines():
        iid, _, project = line.strip().partition(" ")
        if project.startswith(prefixes):
            out.add(iid)
    return out


def remove_images(refs: set[str]) -> tuple[int, int]:
    """Gỡ các image (không dùng -f: image đang có container dùng thì Docker từ chối và ta bỏ qua).
    Trả về (số image đã gỡ, tổng dung lượng byte)."""
    removed = freed = 0
    for ref in sorted(refs):
        try:
            size = _docker("image", "inspect", "--format", "{{.Size}}", ref, timeout=60)
            if size.returncode != 0:
                continue  # không có trong máy (chưa kéo, hoặc đã gỡ)
            if _docker("image", "rm", ref, timeout=600).returncode == 0:
                removed += 1
                freed += int(size.stdout.strip() or 0)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            continue
    return removed, freed


def cleanup_task_images(prebuilt: set[str], trial_names: list[str]) -> tuple[int, int]:
    """Gỡ image kéo sẵn + image tự build của một task. Không làm gì nếu không chạy bằng Docker."""
    if settings.harbor_env != "docker" or not settings.harbor_cleanup_images:
        return 0, 0
    try:
        refs = set(prebuilt) | built_images(trial_names)
    except (OSError, subprocess.TimeoutExpired):
        refs = set(prebuilt)
    return remove_images(refs)


def trial_names(job_dir: Path) -> list[str]:
    return [d.name for d in job_dir.iterdir() if d.is_dir()] if job_dir.is_dir() else []


def fmt_bytes(n: int) -> str:
    return f"{n / 1e9:.1f} GB".replace(".", ",") if n >= 1e8 else f"{n / 1e6:.0f} MB"


def kill_orphan_compose(job_dir: Path) -> int:
    """Dừng các tiến trình `docker compose` Harbor bỏ lại cho các trial của job này. Trả về số đã dừng.

    Hết thời gian chờ (vd. EnvironmentStartTimeoutError khi đang kéo image) thì Harbor thoát mà
    không dừng `docker compose up` con: tiến trình đó tiếp tục kéo image ngầm hàng giờ (tốn băng
    thông, đầy ổ đĩa) và giữ thư mục worktree khiến không xoá được.
    """
    if settings.harbor_env != "docker":
        return 0
    marks = [f"--project-name {t.lower()}__" for t in trial_names(job_dir)]
    if not marks:
        return 0
    try:
        if os.name == "nt":
            ps = ("Get-CimInstance Win32_Process -Filter \"Name='docker.exe' or Name='docker-compose.exe'\" | "
                  "ForEach-Object { \"$($_.ProcessId) $($_.CommandLine)\" }")
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        else:
            r = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return 0
    killed = 0
    for line in r.stdout.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if not pid.isdigit() or "compose" not in cmd or not any(m in cmd for m in marks):
            continue
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/T", "/F", "/PID", pid], capture_output=True, timeout=30)
            else:
                os.kill(int(pid), signal.SIGKILL)
            killed += 1
        except (OSError, subprocess.TimeoutExpired):
            continue
    return killed


def tail(path: Path, n: int = 1500) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-n:].decode("utf-8", errors="replace").strip()


# ---------------------------------------------------------------- đọc kết quả

@dataclass
class TrialOutcome:
    reward: float | None
    valid: bool
    exception: str | None
    tokens: int
    cost_usd: float | None
    seconds: int
    trial_dir: Path
    verifier_tail: str


def _parse_time(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def read_trial(job_dir: Path) -> TrialOutcome | None:
    """Đọc result.json của trial đầu tiên trong job (mỗi job ở đây chỉ có một task, một lần thử)."""
    for rp in sorted(job_dir.glob("*/result.json")):
        try:
            d = json.loads(rp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if "task_name" not in d:
            continue
        rewards = (d.get("verifier_result") or {}).get("rewards") or {}
        reward: float | None = None
        if "reward" in rewards:
            reward = float(rewards["reward"])
        elif rewards:
            reward = sum(float(v) for v in rewards.values()) / len(rewards)
        exc = d.get("exception_info") or None
        exc_text = f"{exc.get('exception_type')}: {str(exc.get('exception_message', ''))[:300]}" if exc else None
        valid = bool(d.get("reward_valid", reward is not None))
        ar = d.get("agent_result") or {}
        t0, t1 = _parse_time(d.get("started_at")), _parse_time(d.get("finished_at"))
        trial_dir = rp.parent
        return TrialOutcome(
            reward=reward, valid=valid and reward is not None, exception=exc_text,
            tokens=int(ar.get("n_output_tokens") or 0), cost_usd=ar.get("cost_usd"),
            seconds=int((t1 - t0).total_seconds()) if t0 and t1 else 0,
            trial_dir=trial_dir, verifier_tail=tail(trial_dir / "verifier" / "test-stdout.txt"),
        )
    return None
