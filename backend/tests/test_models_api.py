"""Vòng đời mô hình (thêm · sửa · xoá) và các kiểm tra chéo khi tạo lượt chạy."""

import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from conftest import EXAMPLE_DIR


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def wait_for(fn, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("hết thời gian chờ")


def _ready(client, sid):
    s = next(s for s in client.get("/api/sources").json() if s["id"] == sid)
    return s if s["status"] in ("ok", "warn") else None


@pytest.fixture(scope="module")
def benches(client):
    # Cả bộ test dùng chung một DB, nên nguồn mẫu có thể đã được file test khác kết nối —
    # và có thể đang dở một lượt đồng bộ do file đó để lại. Nhận cả hai trường hợp.
    src = client.post("/api/sources", json={"url": str(EXAMPLE_DIR), "auth": "public", "sync_mode": "manual"})
    assert src.status_code in (201, 409), src.text
    if src.status_code == 201:
        sid = src.json()["id"]
    else:
        sid = next(s["id"] for s in client.get("/api/sources").json() if s["url"].lower() == str(EXAMPLE_DIR).lower())
        if not _ready(client, sid):
            client.post(f"/api/sources/{sid}/sync")
    wait_for(lambda: _ready(client, sid), timeout=40)
    return {b["key"]: b for b in client.get("/api/benches").json() if b["source"].startswith("bench-demo@")}


# --------------------------------------------------------------- thêm mô hình

def test_create_validates_each_kind(client):
    bad_url = client.post("/api/models", json={"id": "x-openai", "kind": "openai", "base_url": "gpu:8000/v1"})
    assert bad_url.status_code == 422 and "Base URL" in bad_url.json()["detail"]

    no_agent = client.post("/api/models", json={"id": "x-harbor", "kind": "harbor"})
    assert no_agent.status_code == 422 and "Agent Harbor" in no_agent.json()["detail"]

    # mock: base_url trống được điền mặc định, agent thừa bị bỏ (form giữ lại khi đổi kiểu)
    m = client.post("/api/models", json={"id": "x-mock", "kind": "mock", "base_url": "", "agent": "claude-code"})
    assert m.status_code == 201, m.text
    assert m.json()["base_url"] == "mock://0.7" and m.json()["agent"] is None

    assert client.post("/api/models", json={"id": "x-mock", "kind": "mock"}).status_code == 409


def test_harbor_model_checks_agent_name(client):
    typo = client.post("/api/models", json={"id": "x-agent-typo", "kind": "harbor", "agent": "terminus2"})
    assert typo.status_code == 201  # vẫn lưu để người dùng sửa được
    assert typo.json()["status"] == "down"
    assert "không có agent 'terminus2'" in typo.json()["last_error"]
    assert "terminus-2" in typo.json()["last_error"]  # gợi ý tên đúng

    good = client.post("/api/models", json={"id": "x-agent-ok", "kind": "harbor", "agent": "oracle"})
    assert good.status_code == 201 and good.json()["status"] == "ok", good.text

    # Agent tuỳ chỉnh (đường dẫn import / shorthand ACP) không có trong danh sách: không chặn.
    acp = client.post("/api/models", json={"id": "x-agent-acp", "kind": "harbor", "agent": "acp:opencode@1.3.9"})
    assert acp.status_code == 201 and acp.json()["status"] == "ok", acp.text


def test_claude_subscription_oauth(client, monkeypatch):
    # Chỉ claude-code dùng được gói đăng ký
    bad = client.post("/api/models", json={"id": "x-oauth-oracle", "kind": "harbor", "agent": "oracle", "auth": "oauth"})
    assert bad.status_code == 422 and "claude-code" in bad.json()["detail"]
    bad = client.post("/api/models", json={"id": "x-oauth-openai", "kind": "openai", "base_url": "http://h/v1", "auth": "oauth"})
    assert bad.status_code == 422
    bad = client.post("/api/models", json={"id": "x-oauth-gpt", "kind": "harbor", "agent": "claude-code", "auth": "oauth",
                                           "model_name": "openai/gpt-5"})
    assert bad.status_code == 422 and "Anthropic" in bad.json()["detail"]

    # Tên bí mật để trống = CLAUDE_CODE_OAUTH_TOKEN; chưa đặt token thì kiểm tra báo rõ
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    m = client.post("/api/models", json={"id": "x-oauth", "kind": "harbor", "agent": "claude-code", "auth": "oauth",
                                         "model_name": "anthropic/claude-haiku-5-5"})
    assert m.status_code == 201, m.text
    body = m.json()
    assert body["auth"] == "oauth" and body["secret_name"] == "CLAUDE_CODE_OAUTH_TOKEN"
    if not body["secret_present"]:  # máy dev có thể đã đặt token trong backend/.env
        assert body["status"] == "down" and "setup-token" in body["last_error"]

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-test")
    assert client.post("/api/models/x-oauth/ping").json()["status"] == "ok"

    # Đổi agent mà không gửi auth: OAuth tự tắt thay vì báo lỗi
    r = client.patch("/api/models/x-oauth", json={"agent": "oracle"})
    assert r.status_code == 200 and r.json()["auth"] == "api_key"


# ---------------------------------------------------------------- sửa mô hình

def test_patch_fixes_a_mistyped_model(client):
    """Lý do PATCH tồn tại: gõ nhầm Base URL hay tên bí mật thì phải sửa được, không thì
    mô hình kẹt vĩnh viễn ở trạng thái down."""
    client.post("/api/models", json={"id": "x-typo", "kind": "openai", "base_url": "http://127.0.0.1:9", "secret_name": "OPEN_API_KEY"})

    r = client.patch("/api/models/x-typo", json={"base_url": "mock://0.9", "kind": "mock", "note": "đã sửa"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "mock" and body["base_url"] == "mock://0.9" and body["note"] == "đã sửa"
    assert body["status"] == "ok"
    # đổi sang mock thì các trường của kiểu cũ phải được dọn
    assert body["secret_name"] is None and body["agent"] is None

    # ô để trống = xoá giá trị
    r = client.patch("/api/models/x-typo", json={"kind": "openai", "base_url": "http://127.0.0.1:9/v1", "secret_name": "MY_KEY"})
    assert r.status_code == 200 and r.json()["secret_name"] == "MY_KEY"
    assert client.patch("/api/models/x-typo", json={"secret_name": ""}).json()["secret_name"] is None

    assert client.patch("/api/models/x-typo", json={"base_url": "127.0.0.1"}).status_code == 422
    assert client.patch("/api/models/x-typo", json={"kind": "harbor", "agent": ""}).status_code == 422
    assert client.patch("/api/models/khong-ton-tai", json={"note": "x"}).status_code == 404


# ---------------------------------------------------------------- xoá mô hình

def test_delete_keeps_history_but_asks_for_force(client, benches):
    client.post("/api/models", json={"id": "x-tam", "kind": "mock", "base_url": "mock://0.9"})
    assert client.delete("/api/models/x-tam").status_code == 204
    assert all(m["id"] != "x-tam" for m in client.get("/api/models").json())

    client.post("/api/models", json={"id": "x-da-chay", "kind": "mock", "base_url": "mock://0.9"})
    run = client.post("/api/runs", json={"bench_id": benches["vnqa-mini"]["id"], "models": ["x-da-chay"], "samples": 2})
    assert run.status_code == 201, run.text
    rid = run.json()["id"]
    wait_for(lambda: client.get(f"/api/runs/{rid}").json()["status"] not in ("queued", "running"), timeout=60)

    assert next(m for m in client.get("/api/models").json() if m["id"] == "x-da-chay")["used"] is True
    conflict = client.delete("/api/models/x-da-chay")
    assert conflict.status_code == 409 and "force" in conflict.json()["detail"]

    assert client.delete("/api/models/x-da-chay?force=true").status_code == 204
    # Kết quả cũ không bị kéo theo: bảng kết quả chỉ lưu tên mô hình dạng chuỗi.
    assert any(r["model"] == "x-da-chay" for r in client.get("/api/results").json())


def test_cannot_edit_or_delete_model_mid_run(client, benches):
    client.post("/api/models", json={"id": "x-dang-chay", "kind": "mock", "base_url": "mock://0.9"})
    run = client.post("/api/runs", json={"bench_id": benches["vnqa-mini"]["id"], "models": ["x-dang-chay"], "samples": "all", "concurrency": 1})
    assert run.status_code == 201, run.text
    try:
        assert client.patch("/api/models/x-dang-chay", json={"base_url": "mock://0.1"}).status_code == 409
        assert client.delete("/api/models/x-dang-chay?force=true").status_code == 409
    finally:
        rid = run.json()["id"]
        client.post(f"/api/runs/{rid}/cancel")
        wait_for(lambda: client.get(f"/api/runs/{rid}").json()["status"] not in ("queued", "running"), timeout=60)


# ------------------------------------------------- kiểm tra chéo khi tạo lượt chạy

def test_harbor_agent_cannot_be_a_judge(client, benches):
    """Giám khảo được gọi qua Chat Completions. Nếu không chặn ở đây, lượt chạy tiêu hết
    lượt gọi thí sinh rồi mới hỏng ở từng mẫu một."""
    client.post("/api/models", json={"id": "x-judge-harbor", "kind": "harbor", "agent": "oracle", "role": "judge"})
    client.post("/api/models", json={"id": "x-thi-sinh", "kind": "mock", "base_url": "mock://0.9"})
    r = client.post("/api/runs", json={
        "bench_id": benches["sum-mini"]["id"], "models": ["x-thi-sinh"], "judge_id": "x-judge-harbor", "samples": 2})
    assert r.status_code == 422 and "agent Harbor" in r.json()["detail"]


def test_entry_without_any_scorable_sample_fails_instead_of_scoring_zero(client, benches):
    """Giám khảo hỏng → mọi mẫu 'cần xem lại'. Điểm phải để trống và trạng thái failed:
    nếu gán 0 điểm, bảng xếp hạng hiểu nhầm là mô hình trả lời sai hết."""
    client.post("/api/models", json={"id": "x-judge-chet", "kind": "openai", "base_url": "http://127.0.0.1:9/v1", "role": "judge"})
    client.post("/api/models", json={"id": "x-ts", "kind": "mock", "base_url": "mock://0.9"})
    r = client.post("/api/runs", json={
        "bench_id": benches["sum-mini"]["id"], "models": ["x-ts"], "judge_id": "x-judge-chet", "samples": 2})
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    run = wait_for(lambda: (lambda x: x if x["status"] not in ("queued", "running") else None)(client.get(f"/api/runs/{rid}").json()), timeout=90)

    entry = run["entries"][0]
    assert entry["status"] == "failed" and entry["score"] is None, entry
    assert entry["review"] == entry["total"] == 2
    assert all(row["model"] != "x-ts" or row["score"] is None for row in client.get("/api/results").json())
    assert all(row["model"] != "x-ts" for row in client.get(f"/api/leaderboard?bench_id={benches['sum-mini']['id']}").json())


# -------------------------------------------------------------------- nguồn bench

def test_duplicate_source_detected_after_trimming_ref(client, benches):
    """ref được strip trước khi lưu, nên cũng phải strip trước khi so trùng — nếu không
    '  main  ' tạo nguồn thứ hai y hệt và bộ bench hiện hai lần ở màn Chạy mới."""
    before = len(client.get("/api/sources").json())
    r = client.post("/api/sources", json={"url": str(EXAMPLE_DIR), "ref": "  main  ", "auth": "public"})
    assert r.status_code == 409, r.text
    assert len(client.get("/api/sources").json()) == before


def test_switching_source_to_public_clears_stored_secret(client, benches):
    from app.services.secrets import secrets, source_secret_name

    sid = client.get("/api/sources").json()[0]["id"]
    assert client.patch(f"/api/sources/{sid}", json={"auth": "token", "secret": "ghp_bimat"}).status_code == 200
    assert secrets.has(source_secret_name(sid))
    assert client.patch(f"/api/sources/{sid}", json={"auth": "public"}).status_code == 200
    assert not secrets.has(source_secret_name(sid))
