"""Phần hỗ trợ cho agent ForkCode (xem forkcode_agent.py). Chỉ dùng thư viện chuẩn của Python.

Module có hai nửa:

- Phía host (Harbor import trên máy chạy benchmark): tìm DB của agent đã đăng nhập, chụp một
  bản DB sạch (bỏ lịch sử session) và đọc secret OAuth trong Windows Credential Manager.
- Trong container (file này được tải vào container rồi chạy bằng `python3`):
    python3 forkcode_client.py inject --secret-file F   nạp secret vào gnome-keyring
    python3 forkcode_client.py run ...                  điều khiển `<binary> serve` qua HTTP API

ForkCode (bản fork của opencode) không có lệnh chạy một lần: phải mở `<binary> serve` rồi tạo
session, gửi đề bài, duyệt quyền và chờ session có `outcome`.

Tên nội bộ của bản fork (thư mục dữ liệu, tên DB, mục keyring) lấy từ FORKCODE_BRAND (phía host)
hoặc VC_BRAND (trong container), mặc định `forkcode`.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_BRAND = "forkcode"


def brand() -> str:
    """Tên nội bộ của bản fork (chữ thường): thư mục dữ liệu, tên DB, mục keyring/Credential Manager."""
    return (os.environ.get("FORKCODE_BRAND") or os.environ.get("VC_BRAND") or DEFAULT_BRAND).strip().lower()


def env_prefix() -> str:
    """Tiền tố biến môi trường mà binary đọc (vd. <BRAND>_SERVER_PASSWORD)."""
    return brand().upper().replace("-", "_")


def keyring_attrs() -> dict:
    """Thuộc tính libsecret mà binary tra cứu. Thiếu `xdg:schema` thì binary không thấy secret."""
    return {"xdg:schema": f"io.github.{brand()}", "service": brand()}

# Bảng chứa nội dung làm việc của người dùng (session, tin nhắn, kv theo session...): xoá khỏi
# bản chụp để lịch sử trên máy không vào container của task. Giữ nguyên bảng schema/credential.
HISTORY_TABLES = (
    "session_v2", "session_message", "session_inbox", "session_pending", "kv", "event",
    "permission", "telemetry_outbox", "telemetry_feedback",
)


# ================================================================ phía host

def default_data_dir() -> Path:
    """Thư mục dữ liệu của agent trên máy host (cả Windows lẫn Linux đều theo kiểu XDG)."""
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / brand()


def find_credential(data_dir: Path, integration: str | None) -> tuple[Path, dict]:
    """DB mới nhất trong `data_dir` có credential đang active (ưu tiên đúng integration).

    Trả về (đường dẫn DB, dòng credential dạng dict). Tên DB theo channel của binary
    (<brand>-stag.db, <brand>-staging.db...) nên quét mọi `<brand>*.db`.
    """
    import sqlite3  # chỉ phía host; python3 trong container có thể thiếu module này

    b = brand()
    dbs = sorted(data_dir.glob(f"{b}*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not dbs:
        raise FileNotFoundError(
            f"Không thấy DB của agent ({b}*.db) trong {data_dir}. Đăng nhập agent trên máy này "
            "hoặc đặt FORKCODE_DATA_DIR / FORKCODE_BRAND."
        )
    fallback = None
    for db in dbs:
        con = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
        try:
            con.row_factory = sqlite3.Row
            rows = [dict(r) for r in con.execute("SELECT * FROM credential WHERE active = 1")]
        except sqlite3.Error:
            rows = []
        finally:
            con.close()
        for row in rows:
            if integration and row.get("integration_id") == integration:
                return db, row
            fallback = fallback or (db, row)
    if fallback:
        return fallback
    raise LookupError(f"Không có credential active nào trong {', '.join(p.name for p in dbs)}: hãy đăng nhập agent.")


def snapshot_db(src: Path, dest: Path, credential_id: str) -> None:
    """Chụp DB nhất quán (sqlite backup đọc cả WAL, an toàn khi agent đang chạy) rồi xoá lịch
    sử làm việc và mọi credential khác, ghi thành một file duy nhất (không -wal/-shm)."""
    import sqlite3

    source = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True)
    target = sqlite3.connect(dest)
    try:
        source.backup(target)
    finally:
        source.close()
    try:
        tables = {r[0] for r in target.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        for table in HISTORY_TABLES:
            if table in tables:
                target.execute(f'DELETE FROM "{table}"')
        target.execute("DELETE FROM credential WHERE id != ?", (credential_id,))
        target.commit()
        target.execute("PRAGMA journal_mode=DELETE")
        target.execute("VACUUM")
    finally:
        target.close()


def token_expiry_ms(credential: dict) -> int | None:
    """Hạn của access token (ms, theo cột `value` của credential), nếu đọc được."""
    try:
        return int(json.loads(credential.get("value") or "{}").get("expires"))
    except (TypeError, ValueError):
        return None


def _read_windows_credential(target: str) -> str:
    import ctypes
    from ctypes import wintypes as w

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", w.DWORD), ("Type", w.DWORD), ("TargetName", w.LPWSTR), ("Comment", w.LPWSTR),
            ("LastWritten", w.FILETIME), ("CredentialBlobSize", w.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", w.DWORD),
            ("AttributeCount", w.DWORD), ("Attributes", ctypes.c_void_p), ("TargetAlias", w.LPWSTR),
            ("UserName", w.LPWSTR),
        ]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.CredReadW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
    advapi.CredFree.argtypes = [ctypes.c_void_p]
    ptr = ctypes.POINTER(CREDENTIAL)()
    if not advapi.CredReadW(target, 1, 0, ctypes.byref(ptr)):  # 1 = CRED_TYPE_GENERIC
        raise LookupError(
            f"Không có mục '{target}' trong Windows Credential Manager (lỗi {ctypes.get_last_error()}). "
            "Đăng nhập lại agent trên máy này."
        )
    try:
        blob = bytes(ptr.contents.CredentialBlob[: ptr.contents.CredentialBlobSize])
    finally:
        advapi.CredFree(ptr)
    if len(blob) > 1 and blob[1] == 0:
        return blob.decode("utf-16-le")
    return blob.decode("utf-8")


def read_secret(credential_id: str, credential_file: str | None = None) -> str:
    """Secret OAuth của credential (chuỗi JSON {"access", "refresh"}).

    Secret không nằm trong DB mà trong keychain của hệ điều hành. Trên Windows đọc thẳng từ
    Credential Manager (target `<brand>:cred:<id>`), nên mỗi trial lấy token mới nhất. Máy khác
    dùng FORKCODE_CREDENTIAL_FILE: JSON {"id": ..., "secret": ...}.
    """
    if credential_file:
        data = json.loads(Path(credential_file).read_text(encoding="utf-8-sig"))
        if data.get("id") and data["id"] != credential_id:
            raise ValueError(
                f"{credential_file} là của credential {data['id']}, còn DB đang dùng {credential_id}: "
                "trích xuất lại file sau lần đăng nhập gần nhất."
            )
        secret = data.get("secret")
        if isinstance(secret, dict):
            secret = json.dumps(secret, separators=(",", ":"))
    elif os.name == "nt":
        secret = _read_windows_credential(f"{brand()}:cred:{credential_id}")
    else:
        raise RuntimeError("Ngoài Windows phải đặt FORKCODE_CREDENTIAL_FILE (JSON {id, secret}).")
    try:
        parsed = json.loads(secret or "")
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict) or not parsed.get("access"):
        raise ValueError("Secret của agent không phải JSON có trường 'access'.")
    return secret


def strip_refresh(secret: str) -> str:
    """Bỏ refresh token khỏi secret gửi vào container. Nếu agent trong container tự làm mới
    token và server xoay vòng refresh token, bản trên máy host sẽ mất hiệu lực (đăng xuất
    agent trên máy và mọi trial sau). Làm mới chỉ ở host, qua host_service_refresh()."""
    data = json.loads(secret)
    data["refresh"] = ""
    return json.dumps(data, separators=(",", ":"))


def host_service_refresh(state_dir: Path | None = None, timeout: float = 60) -> bool:
    """Nhờ service của agent đang chạy trên máy host đổi refresh token lấy access token mới.
    Service tự ghi token mới vào DB và Credential Manager.
    Trả về False nếu không có service nào đang chạy."""
    state = state_dir or Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / brand()
    for f in sorted(state.glob("service-*.json")):
        info = json.loads(f.read_text(encoding="utf-8"))
        if not info.get("url"):
            continue
        password = _read_windows_credential(f"{brand()}:service-password:{f.name}") if os.name == "nt" else ""
        token = base64.b64encode(f"{info.get('username') or 'opencode'}:{password}".encode()).decode()
        req = urllib.request.Request(info["url"].rstrip("/") + "/auth/sso/refresh", data=b"", method="POST",
                                     headers={"Authorization": f"Basic {token}"})
        try:
            urllib.request.urlopen(req, timeout=timeout).read()
        except (urllib.error.URLError, OSError):
            continue  # file service cũ, service đã tắt
        return True
    return False


# ================================================================ trong container: keyring

def _keyring_collection(conn):
    import secretstorage

    try:
        collection = secretstorage.get_default_collection(conn)
    except Exception:
        collections = list(secretstorage.get_all_collections(conn))
        if not collections:
            raise
        collection = next((c for c in collections if c.get_label() == "login"), collections[0])
    if collection.is_locked():
        collection.unlock()
    if collection.is_locked():
        raise RuntimeError("Keyring vẫn bị khoá: gnome-keyring-daemon chưa được --unlock.")
    return collection


def cmd_inject(args) -> int:
    path = Path(args.secret_file)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    import secretstorage

    conn = secretstorage.dbus_init()
    collection = _keyring_collection(conn)
    base = keyring_attrs()
    items = (
        (f"cred:{data['id']}", data["secret"]),
        (f"{brand()}:telemetry:active-credential", data["id"]),
    )
    for account, value in items:
        collection.create_item(brand(), {**base, "account": account}, value.encode("utf-8"), replace=True)
    found = list(secretstorage.search_items(conn, {**base, "account": f"cred:{data['id']}"}))
    if not found:
        print("forkcode: nạp secret vào keyring không thành công", file=sys.stderr)
        return 1
    print(f"forkcode: đã nạp credential {data['id']} vào keyring")
    return 0


# ================================================================ trong container: điều khiển server

class Api:
    def __init__(self, port: int, password: str):
        self.base = f"http://127.0.0.1:{port}"
        token = base64.b64encode(f"opencode:{password}".encode()).decode()
        self.headers = {"Authorization": f"Basic {token}", "Accept": "application/json"}

    def call(self, method: str, path: str, body=None, query: dict | None = None, timeout: float = 60):
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        data = None
        headers = dict(self.headers)
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else None


def _http_error_text(e: urllib.error.HTTPError) -> str:
    try:
        return f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:2000]}"
    except Exception:
        return f"HTTP {e.code}"


def _alive(pid: int | None) -> bool:
    if not pid:
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def wait_ready(api: Api, serve_pid: int | None, timeout: float = 120) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        if not _alive(serve_pid):
            raise RuntimeError("`serve` đã thoát trước khi sẵn sàng (xem serve log bên dưới)")
        try:
            return api.call("GET", "/api/info", timeout=5)
        except Exception as e:  # server chưa mở cổng
            last = e
        time.sleep(0.5)
    raise RuntimeError(f"`serve` không trả lời sau {timeout:.0f}s: {last}")


def register_mcp(api: Api, servers: list[dict], workdir: str, timeout: float = 90) -> None:
    """Đăng ký MCP server của task lúc chạy (file cấu hình của agent không nhận mục `mcp`:
    "only an administrator can configure"). Dừng hẳn nếu server nào không kết nối được: task
    thiếu công cụ thì điểm không còn ý nghĩa."""
    if not servers:
        return
    loc = {"location[directory]": workdir}
    for server in servers:
        name = urllib.parse.quote(server["name"], safe="")
        api.call("PUT", f"/api/experimental/mcp/{name}", {"config": server["config"]}, query=loc, timeout=120)
    wanted = {s["name"] for s in servers}
    deadline = time.time() + timeout
    while True:
        status = {m.get("name"): m.get("status") or {} for m in (api.call("GET", "/api/mcp", query=loc) or {}).get("data", [])}
        states = {n: (status.get(n) or {}).get("status") for n in wanted}
        if all(s == "connected" for s in states.values()):
            print(f"forkcode: MCP đã kết nối: {', '.join(sorted(wanted))}", flush=True)
            return
        failed = {n: status.get(n) for n, s in states.items() if s not in (None, "pending", "connecting", "connected")}
        if failed or time.time() > deadline:
            detail = "; ".join(f"{n}: {(st or {}).get('error') or (st or {}).get('status')}" for n, st in (failed or {n: status.get(n) for n in wanted}).items())
            hint = ""
            if "not an allowed MCP destination" in detail:
                hint = (" Build agent này chỉ cho MCP tới các đích trong allowlist dựng sẵn (bỏ qua"
                        f" {env_prefix()}_MCP_ALLOWED_ORIGINS), nên task cần MCP không chạy được với nó.")
            raise RuntimeError(f"MCP server không kết nối được: {detail}.{hint}")
        time.sleep(1)


def create_session(api: Api, provider: str, model: str, variant: str, workdir: str) -> dict:
    body = {
        "model": {"id": model, "providerID": provider, "variant": variant},
        "location": {"directory": workdir},
        # Server nhận ruleset này nhưng (bản đã thử) vẫn hỏi quyền khi sửa file: cơ chế chính là
        # answer_pending() tự duyệt; ruleset chỉ để bớt yêu cầu nếu bản sau áp dụng nó.
        "permissions": [{"action": "*", "resource": "*", "effect": "allow"}],
    }
    try:
        return api.call("POST", "/api/session", body)["data"]
    except urllib.error.HTTPError as e:
        if e.code != 400:
            raise
        print(f"forkcode: server không nhận ruleset quyền ({_http_error_text(e)}), tạo session không kèm", flush=True)
        body.pop("permissions")
        return api.call("POST", "/api/session", body)["data"]


def _clip(text, limit: int) -> str:
    text = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + f"... [+{len(text) - limit} ký tự]"


def render_message(msg: dict) -> str:
    kind = msg.get("type")
    if kind == "user":
        return f"\n=== user\n{msg.get('text', '')}"
    if kind == "assistant":
        out = ["\n=== assistant"]
        for part in msg.get("content") or []:
            ptype = part.get("type")
            if ptype == "text":
                out.append(part.get("text", ""))
            elif ptype == "reasoning":
                out.append("[reasoning] " + _clip(part.get("text", ""), 2000))
            elif ptype == "tool":
                state = part.get("state") or {}
                out.append(f"[tool {part.get('name')}] {_clip(state.get('input'), 1500)}")
                result = "\n".join(c.get("text", "") for c in state.get("content") or [] if isinstance(c, dict))
                if result:
                    out.append("  -> " + _clip(result, 3000).replace("\n", "\n     "))
                if state.get("status") not in (None, "completed"):
                    out.append(f"  [status {state.get('status')}] {_clip(state.get('error') or '', 1500)}")
            else:
                out.append(f"[{ptype}] {_clip(part, 500)}")
        if msg.get("error"):
            out.append(f"[error] {_clip(msg['error'], 2000)}")
        tokens = msg.get("tokens") or {}
        out.append(f"(finish={msg.get('finish')}, in={tokens.get('input')}, out={tokens.get('output')})")
        return "\n".join(out)
    if kind == "idle":
        return f"\n=== idle: outcome={msg.get('outcome')}"
    return f"\n=== {kind}: {_clip(msg, 500)}"


def _message_done(msg: dict) -> bool:
    if msg.get("type") == "assistant":
        return bool((msg.get("time") or {}).get("completed")) or bool(msg.get("error"))
    return True


class Transcript:
    """In dần các tin nhắn đã xong của session (stdout được tee vào /logs/agent/forkcode.txt)."""

    def __init__(self, api: Api, session_id: str):
        self.api, self.sid, self.printed = api, session_id, set()

    def messages(self) -> list[dict]:
        out, cursor = [], None
        for _ in range(1000):
            # Server từ chối cursor đi kèm order ("Cursor cannot be combined with order"): cursor
            # đã mang chiều duyệt của trang đầu.
            query = {"limit": "200", "cursor": cursor} if cursor else {"limit": "200", "order": "asc"}
            page = self.api.call("GET", f"/api/session/{self.sid}/message", query=query)
            data = (page or {}).get("data") or []
            out.extend(data)
            cursor = ((page or {}).get("cursor") or {}).get("next")
            if len(data) < 200 or not cursor:
                break
        return out

    def flush(self, final: bool = False) -> None:
        for msg in self.messages():
            mid = msg.get("id")
            if mid in self.printed:
                continue
            if not final and not _message_done(msg):
                break  # giữ thứ tự: tin đang chạy dở thì in ở lần sau
            print(render_message(msg), flush=True)
            self.printed.add(mid)


def answer_pending(api: Api, sid: str, seen: set) -> None:
    """Duyệt mọi yêu cầu quyền, huỷ form hỏi người dùng, chấp thuận tài liệu spec/plan:
    benchmark chạy không có người, agent không được đứng chờ."""
    try:
        requests = (api.call("GET", "/api/permission/request") or {}).get("data") or []
    except urllib.error.HTTPError:
        requests = (api.call("GET", f"/api/session/{sid}/permission") or {}).get("data") or []
    for req in requests:
        rid = req.get("id")
        if rid in seen:
            continue
        seen.add(rid)
        owner = req.get("sessionID") or sid
        print(f"[permission] {req.get('action')} {_clip(req.get('resources'), 300)} -> once", flush=True)
        try:
            api.call("POST", f"/api/session/{owner}/permission/{rid}/reply", {"decision": "once"})
        except urllib.error.HTTPError as e:
            print(f"[permission] trả lời lỗi: {_http_error_text(e)}", flush=True)

    for form in (api.call("GET", f"/api/session/{sid}/form") or {}).get("data") or []:
        fid = form.get("id")
        if fid in seen:
            continue
        seen.add(fid)
        seen.add(f"{FORM_DISMISSED}{fid}")
        print(f"[form] agent hỏi người dùng (không có người trả lời, huỷ): {_clip(form.get('fields'), 800)}", flush=True)
        try:
            api.call("DELETE", f"/api/session/{sid}/form/{fid}")
        except urllib.error.HTTPError as e:
            print(f"[form] huỷ lỗi: {_http_error_text(e)}", flush=True)

    approval = (api.call("GET", f"/api/session/{sid}/task-approval") or {}).get("data")
    if approval:
        key = f"approval:{approval.get('taskID')}:{approval.get('documentRevision')}:{approval.get('generation')}"
        if key not in seen:
            seen.add(key)
            print(f"[task-approval] chấp thuận {approval.get('document')} {approval.get('displayID')}", flush=True)
            api.call("POST", f"/api/session/{sid}/task-approval", approval)


CONTINUE_TEXT = "continue"
# Huỷ form (tool `question`) làm agent kết thúc lượt với outcome=interrupted ("The user dismissed
# this question" -> "Step interrupted"): báo agent là không có ai trả lời và cho làm tiếp.
FORM_DISMISSED = "form-dismissed:"
NO_USER_TEXT = ("No user is available to answer questions in this environment. Make the most reasonable "
                "decision yourself, briefly state the assumption, and continue the task without asking again.")
MAX_QUESTION_RESUMES = 5
_TRANSIENT_HINTS = ("timing out", "timed out", "timeout", "temporarily unavailable", "overloaded",
                    # Hết hạn access token: adapter đẩy token mới vào keyring sát hạn, lượt sau dùng được.
                    "sign in again", "token refresh", "not logged in", "unauthorized")


def transient_error(messages: list[dict]) -> dict | None:
    """Lỗi tạm thời ở lượt cuối — tức sau tin nhắn user cuối cùng: provider HTTP 5xx, 401 (token
    hết hạn, adapter sẽ đẩy token mới), hoặc timeout/lỗi xác thực không kèm mã. None nếu lượt đó
    thất bại vì lý do khác."""
    for msg in reversed(messages):
        if msg.get("type") == "user":
            return None
        err = msg.get("error") if msg.get("type") == "assistant" else None
        if isinstance(err, dict):
            status = err.get("status")
            text = str(err.get("message") or "").lower()
            if isinstance(status, int) and (500 <= status < 600 or status == 401):
                return err
            if status is None and any(h in text for h in _TRANSIENT_HINTS):
                return err
            # Bước bị ngắt từ bên ngoài agent (vd. agent dọn instance giữa lúc tool đang chạy) —
            # client chỉ tự ngắt khi hết giờ và khi đó không thử lại.
            if err.get("type") == "aborted":
                return err
            return None
    return None


def wait_outcome(api: Api, sid: str, sent_ms: float, transcript: "Transcript", seen: set, args, deadline) -> tuple:
    """Chờ tới khi lượt hiện tại của session kết thúc. Trả về (outcome, info của session)."""
    last_flush, info = 0.0, {}
    while True:
        if not _alive(args.serve_pid):
            print("forkcode: `serve` đã thoát giữa chừng", flush=True)
            return None, info
        try:
            answer_pending(api, sid, seen)
            info = api.call("GET", f"/api/session/{sid}")["data"]
            active = (api.call("GET", "/api/session/active") or {}).get("data") or {}
        except (urllib.error.URLError, OSError) as e:
            print(f"forkcode: lỗi gọi API, thử lại: {e}", flush=True)
            time.sleep(2)
            continue
        idle_ms = (info.get("time") or {}).get("idle") or 0
        if info.get("outcome") and idle_ms >= sent_ms and sid not in active:
            return info["outcome"], info
        if deadline and time.time() > deadline:
            print(f"forkcode: hết {args.timeout}s, ngắt session", flush=True)
            try:
                api.call("POST", f"/api/session/{sid}/interrupt", query={"resume": "false"})
            except Exception:
                pass
            return "timeout", info
        if time.time() - last_flush > 10:
            last_flush = time.time()
            try:
                transcript.flush()
            except Exception as e:  # transcript chỉ để xem; không được làm dừng agent
                print(f"forkcode: lỗi lấy transcript (bỏ qua): {e}", flush=True)
        time.sleep(1)


def cmd_run(args) -> int:
    api = Api(args.port, os.environ["VC_SERVER_PASSWORD"])
    info = wait_ready(api, args.serve_pid)
    print(f"forkcode: server {info.get('version')} sẵn sàng", flush=True)

    servers = json.loads(Path(args.mcp_file).read_text(encoding="utf-8")) if args.mcp_file else []
    register_mcp(api, servers, args.workdir)

    provider, _, model = args.model.partition("/")
    if not model:
        raise ValueError(f"Model phải có dạng provider/model, vd. my-provider/my-model (nhận: {args.model})")
    session = create_session(api, provider, model, args.variant, args.workdir)
    sid = session["id"]
    print(f"forkcode: session {sid} ({provider}/{model}#{args.variant}) tại {args.workdir}", flush=True)

    instruction = Path(args.instruction_file).read_text(encoding="utf-8")
    sent_ms = time.time() * 1000
    api.call("POST", f"/api/session/{sid}/prompt", {"text": instruction})

    transcript, seen = Transcript(api, sid), set()
    deadline = time.time() + args.timeout if args.timeout > 0 else None
    outcome, info = wait_outcome(api, sid, sent_ms, transcript, seen, args, deadline)

    # Provider lỗi tạm thời (vd. 504 "The upstream server is timing out") làm agent dừng
    # session với outcome=failed mà không tự thử lại: gửi "continue" vào CÙNG session để agent
    # làm tiếp từ chỗ dở, chờ lâu dần giữa các lần.
    retries, resumes, dismissed = 0, 0, 0
    while outcome in ("failed", "interrupted"):
        now_dismissed = sum(1 for k in seen if isinstance(k, str) and k.startswith(FORM_DISMISSED))
        if outcome == "interrupted" and now_dismissed > dismissed and resumes < MAX_QUESTION_RESUMES:
            dismissed, resumes = now_dismissed, resumes + 1
            print(f"forkcode: agent hỏi người dùng và bị huỷ; báo không có người trả lời, làm tiếp "
                  f"(lần {resumes}/{MAX_QUESTION_RESUMES})", flush=True)
            sent_ms = time.time() * 1000
            api.call("POST", f"/api/session/{sid}/prompt", {"text": NO_USER_TEXT})
            outcome, info = wait_outcome(api, sid, sent_ms, transcript, seen, args, deadline)
            continue
        dismissed = now_dismissed
        if retries >= args.transient_retries:
            break
        try:
            err = transient_error(transcript.messages())
        except Exception as e:
            print(f"forkcode: không đọc được lỗi của lượt cuối: {e}", flush=True)
            err = None
        if not err:
            break
        retries += 1
        delay = min(args.retry_delay * 2 ** (retries - 1), 600)
        print(f"forkcode: provider lỗi tạm thời ({err.get('status')} {_clip(err.get('message') or '', 200)}); "
              f"gửi '{CONTINUE_TEXT}' lần {retries}/{args.transient_retries} sau {delay:.0f}s", flush=True)
        time.sleep(delay)
        sent_ms = time.time() * 1000
        api.call("POST", f"/api/session/{sid}/prompt", {"text": CONTINUE_TEXT})
        outcome, info = wait_outcome(api, sid, sent_ms, transcript, seen, args, deadline)

    try:
        transcript.flush(final=True)
    except Exception as e:
        print(f"forkcode: không lấy được transcript: {e}", flush=True)

    usage = {"session_id": sid, "outcome": outcome, "tokens": info.get("tokens"), "cost": info.get("cost"),
             "transient_retries": retries, "question_resumes": resumes}
    if args.usage_file:
        Path(args.usage_file).write_text(json.dumps(usage), encoding="utf-8")
    print(f"\nforkcode: outcome={outcome} tokens={json.dumps(info.get('tokens'))}", flush=True)
    return 0 if outcome == "succeeded" else (124 if outcome == "timeout" else 1)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("inject")
    p.add_argument("--secret-file", required=True)
    p = sub.add_parser("run")
    p.add_argument("--port", type=int, default=4096)
    p.add_argument("--serve-pid", type=int)
    p.add_argument("--model", required=True)
    p.add_argument("--variant", default="default")
    p.add_argument("--workdir", required=True)
    p.add_argument("--instruction-file", required=True)
    p.add_argument("--mcp-file")
    p.add_argument("--usage-file")
    p.add_argument("--transient-retries", type=int, default=3)
    p.add_argument("--retry-delay", type=float, default=30)
    p.add_argument("--timeout", type=float, default=0)
    args = ap.parse_args(argv)
    try:
        return cmd_inject(args) if args.cmd == "inject" else cmd_run(args)
    except urllib.error.HTTPError as e:
        print(f"forkcode: {e.url}: {_http_error_text(e)}", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"forkcode: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
    return 2


if __name__ == "__main__":
    sys.exit(main())
