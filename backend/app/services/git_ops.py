"""Thao tác Git qua lệnh `git` (cần cài git trên máy chạy backend).

- Không bao giờ nhúng token vào URL: token đi qua header http.extraHeader chỉ cho lần gọi
  đó, nên không bị ghi vào .git/config hay lộ trong thông báo lỗi.
- Deploy key được ghi ra file tạm và xoá ngay sau lệnh.
- Mỗi repo cache có một khoá riêng để fetch/worktree không giẫm lên nhau.
"""

import base64
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class GitError(Exception):
    pass


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def repo_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def clear_stale_locks(repo_dir: Path) -> list[str]:
    """Xoá file *.lock do một lệnh git bị ngắt giữa chừng để lại (timeout, máy chủ khởi động lại…).

    Chỉ gọi khi đang giữ `repo_lock(repo_dir)`: backend là tiến trình duy nhất chạy git trong thư mục
    cache này, nên mọi file .lock còn lại lúc đó chắc chắn là rác.
    """
    git_dir = repo_dir / ".git"
    removed: list[str] = []
    if not git_dir.is_dir():
        return removed
    candidates = [git_dir / n for n in ("index.lock", "shallow.lock", "HEAD.lock", "config.lock", "packed-refs.lock", "FETCH_HEAD.lock")]
    candidates += list((git_dir / "refs").rglob("*.lock")) if (git_dir / "refs").is_dir() else []
    for f in candidates:
        if f.is_file():
            try:
                f.unlink()
                removed.append(f.relative_to(repo_dir).as_posix())
            except OSError:
                pass
    return removed


def is_remote(url: str) -> bool:
    return bool(re.match(r"^(https?://|ssh://|git@)", url.strip()))


def is_local(url: str) -> bool:
    return not is_remote(url) and Path(url).expanduser().is_dir()


def describe(url: str) -> tuple[str, str]:
    """Trả về (tên repo dạng org/repo, máy chủ)."""
    u = url.strip()
    if is_local(u):
        return Path(u).resolve().name, "Thư mục cục bộ"
    host = "GitLab" if "gitlab" in u.lower() else "GitHub" if "github" in u.lower() else "Git"
    repo = re.sub(r"\.git/?$", "", u)
    repo = re.sub(r"^git@[^:]+:", "", repo)
    repo = re.sub(r"^(https?|ssh)://[^/]+/", "", repo)
    return repo, host


def _friendly(stderr: str, ref: str | None, returncode: int | None = None, stdout: str = "", args: list[str] | None = None) -> str:
    s = stderr.lower()
    if any(k in s for k in ("authentication failed", "could not read username", "403", "401", "permission denied", "access denied")):
        return "Xác thực thất bại: token/khoá hết hạn hoặc không có quyền đọc repo."
    if "couldn't find remote ref" in s or "not our ref" in s:
        return f"Không tìm thấy nhánh/tag/commit '{ref}' trên repo."
    if "repository not found" in s or ("not found" in s and "repository" in s):
        return "Không tìm thấy repo hoặc không có quyền truy cập."
    if "could not resolve host" in s:
        return "Không phân giải được tên máy chủ Git."
    if ".lock" in s and "exists" in s:
        return "Cache repo đang bị khoá bởi một lệnh git trước đó. Thử đồng bộ lại."
    lines = [ln.strip() for ln in stderr.strip().splitlines() if ln.strip()]
    if lines:
        return lines[-1][:300]
    # stderr rỗng = git không kịp báo lỗi, thường là bị hệ điều hành dừng giữa chừng.
    # Giữ lại mã thoát + lệnh để còn truy được, thay vì nuốt thành "Lệnh git thất bại".
    out = [ln.strip() for ln in (stdout or "").strip().splitlines() if ln.strip()]
    what = " ".join(args[:2]) if args else "git"
    detail = f": {out[-1][:150]}" if out else " và không in lỗi (có thể bị dừng giữa chừng)"
    return f"Lệnh `git {what}` thoát mã {returncode}{detail}"[:300]


# - core.longpaths: repo bench lớn (vd. Terminal-Bench) có đường dẫn > 260 ký tự trên Windows.
# - core.autocrlf=false: giữ nguyên LF như trong repo. Nếu Git trên Windows đổi sang CRLF,
#   script .sh của task sẽ hỏng shebang khi chạy trong container Linux.
_GIT_BASE = ["git", "-c", "core.longpaths=true", "-c", "core.autocrlf=false"]


def repo_cache_dir(cache_root: Path, url: str) -> Path:
    """Thư mục cache theo URL, dùng chung cho 'Kiểm tra kết nối' và đồng bộ (repo lớn chỉ tải một lần)."""
    import hashlib

    return cache_root / hashlib.sha1(url.strip().encode()).hexdigest()[:16]


def _run(args: list[str], cwd: Path | None = None, env: dict | None = None, ref: str | None = None, timeout: int | None = None) -> str:
    if timeout is None:
        from ..config import settings

        timeout = settings.git_timeout_s
    # Trên Windows, Ctrl+C/Ctrl+Break gửi tới console đi tới MỌI tiến trình cùng nhóm, nên một
    # lệnh git dài (vd. `worktree add` checkout vài nghìn file) bị giết giữa chừng với mã 130 và
    # stderr rỗng. Tách nhóm tiến trình để git không nhận tín hiệu console của tiến trình cha.
    kw: dict = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
    try:
        p = subprocess.run([*_GIT_BASE, *args], cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, **kw)
    except FileNotFoundError as e:
        raise GitError("Không tìm thấy lệnh git trên máy chủ.") from e
    except subprocess.TimeoutExpired as e:
        raise GitError("Lệnh git quá thời gian chờ.") from e
    if p.returncode != 0:
        raise GitError(_friendly(p.stderr, ref, p.returncode, p.stdout, args))
    return p.stdout.strip()


@contextmanager
def auth_context(url: str, auth: str, secret: str | None) -> Iterator[tuple[list[str], dict]]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    pre: list[str] = []
    key_file: str | None = None
    try:
        if auth == "token":
            if not secret:
                raise GitError("Chưa có token cho repo này.")
            user = "oauth2" if "gitlab" in url.lower() else "x-access-token"
            basic = base64.b64encode(f"{user}:{secret}".encode()).decode()
            pre = ["-c", f"http.extraHeader=Authorization: Basic {basic}"]
        elif auth == "deploy_key":
            if not secret:
                raise GitError("Chưa có deploy key cho repo này.")
            fd, key_file = tempfile.mkstemp(prefix="bench-key-")
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(secret.strip() + "\n")
            os.chmod(key_file, 0o600)
            env["GIT_SSH_COMMAND"] = (
                f'ssh -i "{Path(key_file).as_posix()}" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new'
            )
        yield pre, env
    finally:
        if key_file:
            try:
                os.remove(key_file)
            except OSError:
                pass


def fetch_ref(repo_dir: Path, url: str, ref: str, auth: str, secret: str | None) -> str:
    """Lấy `ref` (nhánh, tag hoặc commit) về repo_dir và checkout. Trả về commit SHA."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    with repo_lock(repo_dir):
        clear_stale_locks(repo_dir)
        if not (repo_dir / ".git").exists():
            _run(["init", "-q"], cwd=repo_dir)
        remotes = _run(["remote"], cwd=repo_dir).split()
        _run(["remote", "set-url" if "origin" in remotes else "add", "origin", url], cwd=repo_dir)
        with auth_context(url, auth, secret) as (pre, env):
            _run([*pre, "fetch", "-q", "--depth", "1", "--force", "origin", ref], cwd=repo_dir, env=env, ref=ref)
        _run(["checkout", "-q", "--force", "FETCH_HEAD"], cwd=repo_dir)
        return _run(["rev-parse", "HEAD"], cwd=repo_dir)


def fetch_tree_only(repo_dir: Path, url: str, ref: str, auth: str, secret: str | None) -> str:
    """Partial clone: chỉ tải commit + cây thư mục, không tải nội dung file (vài giây kể cả repo hàng GB).
    Nội dung file được tải lười khi gọi `show_file`. Trả về commit SHA."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    with repo_lock(repo_dir):
        clear_stale_locks(repo_dir)
        if not (repo_dir / ".git").exists():
            _run(["init", "-q"], cwd=repo_dir)
        remotes = _run(["remote"], cwd=repo_dir).split()
        _run(["remote", "set-url" if "origin" in remotes else "add", "origin", url], cwd=repo_dir)
        _run(["config", "remote.origin.promisor", "true"], cwd=repo_dir)
        _run(["config", "remote.origin.partialclonefilter", "blob:none"], cwd=repo_dir)
        with auth_context(url, auth, secret) as (pre, env):
            _run([*pre, "fetch", "-q", "--depth", "1", "--filter=blob:none", "--force", "origin", ref], cwd=repo_dir, env=env, ref=ref, timeout=300)
        return _run(["rev-parse", "FETCH_HEAD"], cwd=repo_dir)


def list_paths(repo_dir: Path, rev: str = "FETCH_HEAD") -> set[str]:
    out = _run(["-c", "core.quotepath=false", "ls-tree", "-r", "--name-only", rev], cwd=repo_dir, timeout=120)
    return set(out.splitlines())


def show_file(repo_dir: Path, url: str, path: str, auth: str, secret: str | None, rev: str = "FETCH_HEAD") -> str:
    """Đọc một file ở commit `rev` (tải lười blob đó trong partial clone)."""
    with repo_lock(repo_dir), auth_context(url, auth, secret) as (pre, env):
        return _run([*pre, "show", f"{rev}:{path}"], cwd=repo_dir, env=env, timeout=300)


def local_sha(path: Path) -> str:
    try:
        return _run(["rev-parse", "HEAD"], cwd=path)
    except GitError:
        return "local"


def checkout_pinned(repo_dir: Path, url: str, sha: str, auth: str, secret: str | None, dest: Path) -> None:
    """Tạo worktree tách rời tại `dest` đúng commit `sha` (fetch thêm nếu cache đã trôi)."""
    with repo_lock(repo_dir):
        clear_stale_locks(repo_dir)
        try:
            _run(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo_dir)
        except GitError:
            with auth_context(url, auth, secret) as (pre, env):
                _run([*pre, "fetch", "-q", "--depth", "1", "origin", sha], cwd=repo_dir, env=env, ref=sha)
        if dest.exists():
            remove_tree(dest)
            if dest.exists():
                # remove_tree bỏ qua file xoá không được; nếu không chặn ở đây thì
                # `worktree add` sẽ báo "already exists", không nói được lý do thật.
                raise GitError(f"Không xoá hết thư mục làm việc cũ: {dest}. Xoá tay thư mục này rồi chạy lại.")
        _run(["worktree", "prune"], cwd=repo_dir)
        _run(["worktree", "add", "-q", "--detach", "--force", str(dest), sha], cwd=repo_dir)


def remove_worktree(repo_dir: Path, dest: Path) -> None:
    with repo_lock(repo_dir):
        try:
            _run(["worktree", "remove", "--force", str(dest)], cwd=repo_dir)
        except GitError:
            pass
        # `worktree remove` có thể bỏ sót thư mục rỗng mà Docker vừa mount (vd. environment/ của
        # task bị dừng giữa chừng). Xoá nốt phần còn lại.
        if dest.exists():
            remove_tree(dest)


def prune_worktrees(repo_dir: Path) -> None:
    """Quên các worktree mà thư mục đã bị xoá (không lỗi nếu repo_dir không phải repo git)."""
    if not (repo_dir / ".git").exists() and not (repo_dir / "HEAD").is_file():
        return  # không phải repo: git sẽ leo lên repo cha (vd. chính repo dashboard)
    with repo_lock(repo_dir):
        try:
            _run(["worktree", "prune"], cwd=repo_dir)
        except GitError:
            pass


def remove_tree(path: Path) -> None:
    """rmtree chịu được file chỉ-đọc của .git trên Windows."""

    def _onerror(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    shutil.rmtree(path, onerror=_onerror)
