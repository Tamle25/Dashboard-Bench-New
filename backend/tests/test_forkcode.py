"""Phần không cần Harbor của agent ForkCode (backend/harbor_plugins/forkcode_client.py): bản chụp DB,
chọn credential, đọc secret, và vòng lặp điều khiển `<binary> serve` chạy với một server giả."""

import json
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "harbor_plugins"))
import forkcode_client as vc  # noqa: E402


@pytest.fixture(autouse=True)
def _default_brand(monkeypatch):
    monkeypatch.delenv("FORKCODE_BRAND", raising=False)
    monkeypatch.delenv("VC_BRAND", raising=False)


def _make_db(path: Path, creds: list[tuple], wal: bool = True) -> None:
    con = sqlite3.connect(path)
    if wal:
        con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE credential (id text PRIMARY KEY, integration_id text, label text NOT NULL, value text NOT NULL,"
                " connector_id text, method_id text, active integer, time_created integer NOT NULL, time_updated integer NOT NULL)")
    con.execute("CREATE TABLE session_v2 (id text)")
    con.execute("CREATE TABLE session_message (id text)")
    con.execute("CREATE TABLE kv (key text)")
    con.execute("CREATE TABLE migration (id text)")
    for cid, integ, active in creds:
        value = json.dumps({"type": "oauth", "access": "", "refresh": "", "expires": 1795267345103})
        con.execute("INSERT INTO credential VALUES (?, ?, 'me', ?, NULL, NULL, ?, 1, 1)", (cid, integ, value, active))
    con.executemany("INSERT INTO session_v2 VALUES (?)", [("ses_1",), ("ses_2",)])
    con.execute("INSERT INTO session_message VALUES ('msg_1')")
    con.execute("INSERT INTO kv VALUES ('core/x/ses_1')")
    con.execute("INSERT INTO migration VALUES ('m1')")
    con.commit()
    con.close()


def test_find_credential_and_snapshot(tmp_path):
    old = tmp_path / "forkcode-staging.db"
    _make_db(old, [("cred_old", "forkcode-api", 1)])
    time.sleep(0.05)
    db = tmp_path / "forkcode-stag.db"
    _make_db(db, [("cred_other", "other", 1), ("cred_a", "forkcode-api", 1), ("cred_b", "forkcode-api", 0)])

    path, cred = vc.find_credential(tmp_path, "forkcode-api")
    assert path == db and cred["id"] == "cred_a"
    assert vc.token_expiry_ms(cred) == 1795267345103
    # Không khớp integration thì lấy credential active bất kỳ (DB mới nhất trước)
    assert vc.find_credential(tmp_path, "nope")[1]["id"] in {"cred_other", "cred_a"}

    out = tmp_path / "snap" / "forkcode.db"
    out.parent.mkdir()
    vc.snapshot_db(db, out, "cred_a")
    con = sqlite3.connect(out)
    assert [r[0] for r in con.execute("SELECT id FROM credential")] == ["cred_a"]
    assert con.execute("SELECT count(*) FROM session_v2").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM session_message").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM kv").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM migration").fetchone()[0] == 1
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    con.close()
    assert sorted(p.name for p in out.parent.iterdir()) == ["forkcode.db"]  # không còn -wal/-shm
    # DB gốc giữ nguyên
    assert sqlite3.connect(db).execute("SELECT count(*) FROM session_v2").fetchone()[0] == 2


def test_brand_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FORKCODE_BRAND", "AcmeCode")
    assert vc.brand() == "acmecode" and vc.env_prefix() == "ACMECODE"
    assert vc.default_data_dir().name == "acmecode"
    assert vc.keyring_attrs() == {"xdg:schema": "io.github.acmecode", "service": "acmecode"}
    _make_db(tmp_path / "acmecode-stag.db", [("cred_z", "acme-api", 1)])
    _make_db(tmp_path / "forkcode.db", [("cred_y", "acme-api", 1)])
    assert vc.find_credential(tmp_path, "acme-api")[1]["id"] == "cred_z"


def test_find_credential_errors(tmp_path):
    with pytest.raises(FileNotFoundError):
        vc.find_credential(tmp_path, None)
    _make_db(tmp_path / "forkcode-stag.db", [("cred_x", "forkcode-api", 0)])
    with pytest.raises(LookupError):
        vc.find_credential(tmp_path, None)


def test_read_secret_from_file(tmp_path):
    f = tmp_path / "cred.json"
    # PowerShell hay ghi BOM; secret có thể là chuỗi JSON hoặc object
    f.write_text(json.dumps({"id": "cred_a", "secret": {"access": "A", "refresh": "R"}}), encoding="utf-8-sig")
    assert json.loads(vc.read_secret("cred_a", str(f))) == {"access": "A", "refresh": "R"}
    with pytest.raises(ValueError, match="trích xuất lại"):
        vc.read_secret("cred_new", str(f))
    f.write_text(json.dumps({"id": "cred_a", "secret": "not-json"}), encoding="utf-8")
    with pytest.raises(ValueError, match="access"):
        vc.read_secret("cred_a", str(f))


def test_strip_refresh_keeps_access_only():
    out = json.loads(vc.strip_refresh(json.dumps({"access": "A", "refresh": "R"})))
    assert out == {"access": "A", "refresh": ""}


def test_host_service_refresh_without_service(tmp_path):
    assert vc.host_service_refresh(tmp_path) is False  # không có service-*.json


class FakeForkcode:
    """Server giả theo đúng các route forkcode_client dùng. Agent "làm bài" xong sau khi yêu cầu
    quyền của nó được duyệt."""

    def __init__(self, mcp_status="connected", outcome="succeeded", first_error=None, first_outcome="failed", form=False):
        self.calls, self.mcp_status, self.outcome = [], mcp_status, outcome
        self.form, self.form_deleted = form, False
        # first_error: lượt đầu kết thúc (`first_outcome`) với lỗi này; lượt sau ("continue") ra `outcome`
        self.first_error, self.first_outcome, self.prompts = first_error, first_outcome, []
        self.session, self.prompt, self.approved, self.mcp = None, None, False, {}
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj=None):
                body = json.dumps(obj).encode() if obj is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n)) if n else None

            def do_GET(self):
                fake.calls.append(("GET", self.path, self.headers.get("Authorization")))
                fake.route(self, "GET", None)

            def do_POST(self):
                fake.route(self, "POST", self._body())

            def do_PUT(self):
                fake.route(self, "PUT", self._body())

            def do_DELETE(self):
                fake.route(self, "DELETE", None)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def route(self, h, method, body):
        path = h.path.split("?")[0]
        sid = "ses_test"
        if path == "/api/info":
            return h._send(200, {"version": "fake"})
        if method == "PUT" and path.startswith("/api/experimental/mcp/"):
            self.mcp[path.rsplit("/", 1)[1]] = body["config"]
            return h._send(204)
        if path == "/api/mcp":
            return h._send(200, {"data": [{"name": n, "status": {"status": self.mcp_status, "error": "http://x is not an allowed MCP destination"}} for n in self.mcp]})
        if method == "POST" and path == "/api/session":
            self.session = body
            return h._send(200, {"data": {"id": sid}})
        if method == "POST" and path == f"/api/session/{sid}/prompt":
            self.prompts.append(body["text"])
            self.prompt = self.prompt or body["text"]
            return h._send(200, {"data": {"id": "msg_u"}})
        if path == "/api/permission/request":
            pending = [] if self.approved or not self.prompt else [{"id": "per_1", "sessionID": sid, "action": "edit", "resources": ["hello.txt"]}]
            return h._send(200, {"data": pending})
        if method == "POST" and path == f"/api/session/{sid}/permission/per_1/reply":
            assert body == {"decision": "once"}
            self.approved = True
            return h._send(204)
        if method == "DELETE" and path == f"/api/session/{sid}/form/frm_1":
            self.form_deleted = True
            return h._send(204)
        if path == f"/api/session/{sid}/form":
            show = self.form and self.prompt and not self.form_deleted
            return h._send(200, {"data": [{"id": "frm_1", "fields": [{"question": "A or B?"}]}] if show else []})
        if path == f"/api/session/{sid}/task-approval":
            return h._send(200, {"data": None})
        if path == "/api/session/active":
            return h._send(200, {"data": {} if self.approved else {sid: {}}})
        if path == f"/api/session/{sid}":
            info = {"id": sid, "time": {"created": 1, "updated": 1}, "tokens": {"input": 10, "output": 2, "reasoning": 1, "cache": {"read": 5, "write": 0}}, "cost": 0}
            if self.approved:
                failing = self.first_error is not None and len(self.prompts) < 2
                info.update(outcome=self.first_outcome if failing else self.outcome)
                info["time"]["idle"] = time.time() * 1000
            return h._send(200, {"data": info})
        if path == f"/api/session/{sid}/message":
            msgs = [{"id": "msg_u", "type": "user", "text": self.prompt or ""}]
            if self.approved and self.first_error is not None:
                msgs.append({"id": "msg_e", "type": "assistant", "time": {"completed": 2}, "content": [], "error": self.first_error})
                if len(self.prompts) < 2:
                    msgs.append({"id": "msg_i0", "type": "idle", "outcome": self.first_outcome})
                    return h._send(200, {"data": msgs, "cursor": {}})
                msgs.append({"id": "msg_u2", "type": "user", "text": self.prompts[1]})
            if self.approved:
                msgs += [
                    {"id": "msg_a", "type": "assistant", "time": {"completed": 2}, "finish": "stop", "tokens": {"input": 10, "output": 2},
                     "content": [{"type": "tool", "name": "write", "state": {"status": "completed", "input": {"path": "hello.txt"}, "content": [{"type": "text", "text": "Created"}]}},
                                 {"type": "text", "text": "Done."}]},
                    {"id": "msg_i", "type": "idle", "outcome": self.outcome},
                ]
            return h._send(200, {"data": msgs, "cursor": {}})
        return h._send(404, {"_tag": "NotFound"})

    def close(self):
        self.server.shutdown()


def _run_args(tmp_path, port, mcp=None):
    (tmp_path / "instr.md").write_text("Create hello.txt", encoding="utf-8")
    argv = ["run", "--port", str(port), "--model", "my-provider/my-model", "--variant", "high",
            "--workdir", "/app", "--instruction-file", str(tmp_path / "instr.md"), "--usage-file", str(tmp_path / "usage.json")]
    if mcp is not None:
        (tmp_path / "mcp.json").write_text(json.dumps(mcp), encoding="utf-8")
        argv += ["--mcp-file", str(tmp_path / "mcp.json")]
    return argv


def test_client_run_approves_permission_and_reports_usage(tmp_path, monkeypatch, capsys):
    fake = FakeForkcode()
    monkeypatch.setenv("VC_SERVER_PASSWORD", "pw")
    try:
        mcp = [{"name": "sql-tools", "config": {"type": "remote", "url": "http://sql-tools:8000/mcp", "oauth": False}}]
        assert vc.main(_run_args(tmp_path, fake.port, mcp)) == 0
    finally:
        fake.close()
    assert fake.calls[0][2] == "Basic b3BlbmNvZGU6cHc="  # base64("opencode:pw")
    assert fake.session["model"] == {"id": "my-model", "providerID": "my-provider", "variant": "high"}
    assert fake.session["location"] == {"directory": "/app"} and fake.prompt == "Create hello.txt"
    assert fake.mcp == {"sql-tools": mcp[0]["config"]} and fake.approved
    usage = json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))
    assert usage["outcome"] == "succeeded" and usage["tokens"]["cache"]["read"] == 5
    out = capsys.readouterr().out
    assert "[permission] edit" in out and "[tool write]" in out and "Done." in out and "outcome=succeeded" in out
    assert out.count("=== user") == 1  # tin nhắn không bị in lặp


def test_client_run_continues_after_transient_provider_error(tmp_path, monkeypatch, capsys):
    """run-42 bun-sourcemap-leak: provider trả 504, agent dừng session với outcome=failed."""
    err = {"type": "provider.internal", "message": "The upstream server is timing out", "status": 504}
    fake = FakeForkcode(first_error=err)
    monkeypatch.setenv("VC_SERVER_PASSWORD", "pw")
    try:
        assert vc.main(_run_args(tmp_path, fake.port) + ["--retry-delay", "0"]) == 0
    finally:
        fake.close()
    assert fake.prompts == ["Create hello.txt", "continue"]
    assert json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))["transient_retries"] == 1
    assert "gửi 'continue' lần 1/3" in capsys.readouterr().out


def test_client_run_continues_after_interrupted_step(tmp_path, monkeypatch):
    """run-42 pretrain-shard-corruption: tool đang chạy bị ngắt, session ra outcome=interrupted."""
    fake = FakeForkcode(first_error={"type": "aborted", "message": "Step interrupted"}, first_outcome="interrupted")
    monkeypatch.setenv("VC_SERVER_PASSWORD", "pw")
    try:
        assert vc.main(_run_args(tmp_path, fake.port) + ["--retry-delay", "0"]) == 0
    finally:
        fake.close()
    assert fake.prompts == ["Create hello.txt", "continue"]


def test_client_run_resumes_after_dismissed_question(tmp_path, monkeypatch):
    """run-42 react-lead-form: agent gọi tool question, client huỷ form, lượt kết thúc interrupted."""
    fake = FakeForkcode(first_error={"type": "aborted", "message": "Step interrupted"}, first_outcome="interrupted", form=True)
    monkeypatch.setenv("VC_SERVER_PASSWORD", "pw")
    try:
        assert vc.main(_run_args(tmp_path, fake.port) + ["--retry-delay", "0"]) == 0
    finally:
        fake.close()
    assert fake.form_deleted and fake.prompts == ["Create hello.txt", vc.NO_USER_TEXT]
    usage = json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))
    assert usage["question_resumes"] == 1 and usage["transient_retries"] == 0


def test_client_run_does_not_continue_on_permanent_error(tmp_path, monkeypatch):
    fake = FakeForkcode(first_error={"type": "provider.bad_request", "message": "Invalid parameter", "status": 400})
    monkeypatch.setenv("VC_SERVER_PASSWORD", "pw")
    try:
        assert vc.main(_run_args(tmp_path, fake.port) + ["--retry-delay", "0"]) == 1
    finally:
        fake.close()
    assert fake.prompts == ["Create hello.txt"]


def test_transient_error_classification():
    u = {"type": "user", "text": "x"}
    a = lambda err: {"type": "assistant", "error": err}
    assert vc.transient_error([u, a({"status": 504, "message": "x"})])["status"] == 504
    assert vc.transient_error([u, a({"status": 503})]) is not None
    assert vc.transient_error([u, a({"message": "Request timed out"})]) is not None
    assert vc.transient_error([u, a({"status": 401, "message": "Unauthorized"})]) is not None  # token hết hạn
    assert vc.transient_error([u, a({"message": "Refresh token was rejected; sign in again"})]) is not None
    assert vc.transient_error([u, a({"status": 403, "message": "timeout"})]) is None
    assert vc.transient_error([u, a({"status": 400, "message": "bad"})]) is None
    # lỗi cũ trước tin nhắn user cuối không tính
    assert vc.transient_error([a({"status": 504}), u, {"type": "idle", "outcome": "failed"}]) is None


def test_client_run_failed_outcome_exits_nonzero(tmp_path, monkeypatch):
    fake = FakeForkcode(outcome="failed")
    monkeypatch.setenv("VC_SERVER_PASSWORD", "pw")
    try:
        assert vc.main(_run_args(tmp_path, fake.port)) == 1
    finally:
        fake.close()


def test_client_run_stops_when_mcp_refused(tmp_path, monkeypatch, capsys):
    fake = FakeForkcode(mcp_status="failed")
    monkeypatch.setenv("VC_SERVER_PASSWORD", "pw")
    try:
        mcp = [{"name": "sql-tools", "config": {"type": "remote", "url": "http://sql-tools:8000/mcp", "oauth": False}}]
        assert vc.main(_run_args(tmp_path, fake.port, mcp)) == 2
    finally:
        fake.close()
    assert fake.session is None  # không tạo session khi thiếu công cụ
    assert "chỉ cho MCP tới các đích trong allowlist dựng sẵn" in capsys.readouterr().err


def test_transcript_pages_without_order_after_first_page():
    """Session > 200 tin nhắn: server trả 400 nếu cursor đi kèm order (run-41 bun-sourcemap-leak)."""
    msgs = [{"id": f"msg_{i:03d}", "type": "user", "text": str(i)} for i in range(450)]

    class Api:
        def call(self, method, path, body=None, query=None, timeout=60):
            if query.get("cursor") and "order" in query:
                raise AssertionError("cursor cannot be combined with order")
            start = int(query["cursor"]) if query.get("cursor") else 0
            page = msgs[start:start + int(query["limit"])]
            return {"data": page, "cursor": {"next": str(start + len(page))}}

    assert [m["id"] for m in vc.Transcript(Api(), "ses_x").messages()] == [m["id"] for m in msgs]


def test_render_message_clips_long_tool_output():
    msg = {"type": "assistant", "content": [{"type": "tool", "name": "bash", "state": {"status": "error", "input": {"cmd": "x"}, "error": "boom", "content": [{"type": "text", "text": "y" * 5000}]}}]}
    text = vc.render_message(msg)
    assert "[tool bash]" in text and "[status error] boom" in text and "[+2000 ký tự]" in text
