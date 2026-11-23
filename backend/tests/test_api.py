"""Luồng đầu-cuối: kết nối nguồn (thư mục mẫu) → thêm mô hình mock → chạy → xem kết quả."""

import hashlib
import hmac
import subprocess
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


@pytest.fixture(scope="module")
def seeded(client):
    test = client.post("/api/sources/test", json={"url": str(EXAMPLE_DIR), "auth": "public"}).json()
    assert test["ok"], test
    assert [c["ok"] for c in test["checks"]] == [True, True, True]
    assert len(test["warnings"]) == 1

    src = client.post("/api/sources", json={"url": str(EXAMPLE_DIR), "auth": "public", "sync_mode": "manual"})
    assert src.status_code == 201, src.text
    sid = src.json()["id"]
    wait_for(lambda: next(s for s in client.get("/api/sources").json() if s["id"] == sid)["status"] in ("ok", "warn"))

    for body in (
        {"id": "m-strong", "role": "contestant", "kind": "mock", "base_url": "mock://0.9"},
        {"id": "m-weak", "role": "contestant", "kind": "mock", "base_url": "mock://0.4"},
        {"id": "m-judge", "role": "judge", "kind": "mock", "base_url": "mock://1"},
    ):
        r = client.post("/api/models", json=body)
        assert r.status_code == 201 and r.json()["status"] == "ok", r.text
    return {b["key"]: b for b in client.get("/api/benches").json()}


def run_to_end(client, body):
    r = client.post("/api/runs", json=body)
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    return wait_for(lambda: (lambda x: x if x["status"] not in ("queued", "running") else None)(client.get(f"/api/runs/{rid}").json()))


def test_exact_match_run(client, seeded):
    run = run_to_end(client, {"bench_id": seeded["vnqa-mini"]["id"], "models": ["m-strong", "m-weak"], "samples": "all", "concurrency": 4})
    assert run["status"] == "done", run
    scores = {e["model"]: e["score"] for e in run["entries"]}
    assert scores["m-strong"] > scores["m-weak"]
    assert any("Lấy bộ bench" in line["msg"] for line in run["logs"])

    entry = run["entries"][0]["id"]
    detail = client.get(f"/api/results/{entry}").json()
    assert detail["categories"] and detail["comparison"]
    samples = client.get(f"/api/results/{entry}/samples", params={"filter": "fail"}).json()
    assert samples["counts"]["all"] == 10


def test_judge_run_requires_judge(client, seeded):
    bench = seeded["sum-mini"]["id"]
    r = client.post("/api/runs", json={"bench_id": bench, "models": ["m-strong"]})
    assert r.status_code == 422
    run = run_to_end(client, {"bench_id": bench, "models": ["m-strong"], "judge_id": "m-judge", "samples": "all"})
    assert run["status"] == "done" and run["judge"] == "m-judge"


def test_unit_test_run(client, seeded):
    run = run_to_end(client, {"bench_id": seeded["code-mini"]["id"], "models": ["m-strong"], "samples": "all"})
    assert run["status"] == "done"


def test_results_and_stats(client, seeded):
    rows = client.get("/api/results").json()
    assert rows and all(r["status"] == "done" for r in rows)
    stats = client.get("/api/stats").json()
    assert stats["benches"] == 3 and stats["sources_warn"] == 1 and stats["active_run"] is None


def test_stream_replays_finished_run(client, seeded):
    rid = client.get("/api/results").json()[0]["run_id"]
    with client.stream("GET", f"/api/runs/{rid}/stream") as resp:
        body = "".join(resp.iter_text())
    assert "event: progress" in body and "event: log" in body and "event: end" in body


def test_git_source_pins_commit(client, tmp_path):
    repo = tmp_path / "bench-git"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    for f in ("bench.yaml", "data/vnqa.jsonl", "data/sum.jsonl", "data/code.jsonl", "rubrics/sum_v1.md"):
        (repo / f).parent.mkdir(parents=True, exist_ok=True)
        (repo / f).write_bytes((EXAMPLE_DIR / f).read_bytes())
    git = ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()

    # file:// là URL Git hợp lệ → đi qua đúng đường fetch/worktree như repo từ xa
    url = "file://" + repo.as_posix() if repo.as_posix().startswith("/") else "file:///" + repo.as_posix()
    from app.services import git_ops
    assert not git_ops.is_remote(url)  # file:// không được coi là remote qua API công khai
    insp_dir = tmp_path / "cache"
    got = git_ops.fetch_ref(insp_dir, url, "main", "public", None)
    assert got == sha
    dest = tmp_path / "wt"
    git_ops.checkout_pinned(insp_dir, url, sha, "public", None, dest)
    assert (dest / "bench.yaml").is_file()
    git_ops.remove_worktree(insp_dir, dest)


def test_stale_syncing_status_does_not_brick_source(client, seeded):
    """Máy chủ tắt giữa lúc đồng bộ để lại status "syncing" trong DB. Nguồn vẫn phải nạp lại được."""
    from app.db import SessionLocal
    from app.models import Source
    from app.services.sources import is_syncing, recover_orphan_syncs

    sid = client.get("/api/sources").json()[0]["id"]
    with SessionLocal() as db:  # giả lập lượt đồng bộ bị bỏ dở
        db.get(Source, sid).status = "syncing"
        db.commit()
    assert not is_syncing(sid)  # không có lượt nào chạy thật trong tiến trình

    # Nút "Đồng bộ" phải chạy được dù status còn kẹt, không bị khoá vĩnh viễn.
    assert client.post(f"/api/sources/{sid}/sync").status_code == 202
    wait_for(lambda: next(s for s in client.get("/api/sources").json() if s["id"] == sid)["status"] in ("ok", "warn"))

    # Khởi động lại máy chủ cũng tự đưa nguồn kẹt về "error" kèm hướng dẫn thử lại.
    with SessionLocal() as db:
        db.get(Source, sid).status = "syncing"
        db.commit()
    recover_orphan_syncs()
    s = next(s for s in client.get("/api/sources").json() if s["id"] == sid)
    assert s["status"] == "error" and "Đồng bộ" in s["message"]
    client.post(f"/api/sources/{sid}/sync")
    wait_for(lambda: next(s for s in client.get("/api/sources").json() if s["id"] == sid)["status"] in ("ok", "warn"))


def test_webhook_signature(client, seeded):
    sid = client.get("/api/sources").json()[0]["id"]
    body = b'{"ref": "refs/heads/main"}'
    bad = client.post(f"/api/webhooks/git/{sid}", content=body, headers={"X-Hub-Signature-256": "sha256=deadbeef"})
    assert bad.status_code == 401
    sig = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    ok = client.post(f"/api/webhooks/git/{sid}", content=body, headers={"X-Hub-Signature-256": sig})
    assert ok.status_code == 202
