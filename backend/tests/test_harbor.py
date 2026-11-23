"""Bộ bench dạng Harbor / Terminal-Bench, chạy với Harbor CLI giả (tests/fake_harbor.py)."""

import json
import textwrap
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import ModelEndpoint
from app.services import harbor


def make_task(root: Path, rel: str, category: str = "Software") -> None:
    d = root / rel
    (d / "tests").mkdir(parents=True)
    (d / "task.toml").write_text(f'[task]\nname = "test/{d.name}"\n\n[metadata]\ncategory = "{category}"\n', encoding="utf-8")
    (d / "instruction.md").write_text(f"Đề bài của {d.name}", encoding="utf-8")


@pytest.fixture(scope="module")
def harbor_repo(tmp_path_factory) -> Path:
    """Repo giống terminal-bench-lct: tasks/, vn-bench/layer-b/tasks/, layer-a cần bước build."""
    root = tmp_path_factory.mktemp("tb-repo")
    for t in ("t-alpha", "t-beta", "infra-broken", "atrx-vep-crispr"):
        make_task(root, f"tasks/{t}", "Science" if t == "t-beta" else "Software")
    make_task(root, "vn-bench/layer-b/tasks/vn-slugify")
    make_task(root, "vn-bench/layer-b/tasks/vn-date-normalize")
    # Nhiễu giống repo thật: mẫu task và fixture của công cụ kiểm tra
    make_task(root, "vn-bench/layer-b/_template")
    make_task(root, "scripts/checks/test-tasks/fixture-a")
    make_task(root, "scripts/checks/test-tasks/fixture-b")
    (root / "vn-bench/layer-a/translations/t-alpha").mkdir(parents=True)  # có meta.toml, không phải task
    (root / "vn-bench/layer-a/translations/t-alpha/meta.toml").write_text("status = 'reviewed'", encoding="utf-8")
    (root / "build_variants.py").write_text(textwrap.dedent("""
        import shutil, sys
        variant = sys.argv[sys.argv.index("--variants") + 1]
        shutil.copytree("tasks/t-alpha", f"vn-bench/layer-a/build/{variant}/t-alpha", dirs_exist_ok=True)
    """), encoding="utf-8")
    return root


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def wait_run(client, rid, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/runs/{rid}").json()
        if r["status"] not in ("queued", "running"):
            return r
        time.sleep(0.2)
    raise AssertionError("lượt chạy không kết thúc")


def test_detect_harbor_dirs(harbor_repo):
    found = dict(harbor.detect_harbor_dirs(harbor_repo))
    assert found == {"tasks": 4, "vn-bench/layer-b/tasks": 2}


def test_detect_from_paths_matches_filesystem(harbor_repo):
    paths = {p.relative_to(harbor_repo).as_posix() for p in harbor_repo.rglob("*") if p.is_file()}
    assert harbor.detect_harbor_dirs_from_paths(paths) == sorted(harbor.detect_harbor_dirs(harbor_repo))
    assert harbor.count_tasks_from_paths(paths, "tasks", ["atrx-vep-crispr"]) == 3


def test_exclude_accepts_glob_patterns(tmp_path):
    names = [f"hil-bench-sql_{i}-{mode}" for i in range(3) for mode in ("baseline", "full_info", "ask_human")]
    for n in names:
        (tmp_path / "harbor_sql" / n).mkdir(parents=True)
        (tmp_path / "harbor_sql" / n / "task.toml").write_text("[metadata]\n", encoding="utf-8")
    excl = ["*-ask_human", "*-full_info"]
    picked = [t.name for t in harbor.list_tasks(tmp_path / "harbor_sql", excl)]
    assert picked == [f"hil-bench-sql_{i}-baseline" for i in range(3)]
    paths = {f"harbor_sql/{n}/task.toml" for n in names}
    assert harbor.count_tasks_from_paths(paths, "harbor_sql", excl) == 3
    assert harbor.count_tasks_from_paths(paths, "harbor_sql", ["hil-bench-sql_0-baseline"]) == 8


def test_partial_clone_probe(tmp_path, harbor_repo):
    """fetch_tree_only + list_paths + show_file trên repo git thật (qua file://)."""
    import shutil
    import subprocess

    from app.services import git_ops

    repo = tmp_path / "remote"
    shutil.copytree(harbor_repo, repo)
    (repo / "bench.yaml").write_text("version: 1\nbenches: []\n", encoding="utf-8")
    git = ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "uploadpack.allowFilter", "true"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
    url = "file:///" + repo.as_posix().lstrip("/")
    work = tmp_path / "probe"
    sha = git_ops.fetch_tree_only(work, url, "main", "public", None)
    paths = git_ops.list_paths(work)
    assert len(sha) == 40 and "tasks/t-alpha/task.toml" in paths and "bench.yaml" in paths
    assert git_ops.show_file(work, url, "bench.yaml", "public", None).startswith("version: 1")
    assert not (work / "tasks").exists()  # không checkout nội dung

    # Lệnh git bị ngắt giữa chừng để lại file .lock: lần sau phải tự dọn, không kẹt mãi
    (work / ".git" / "shallow.lock").write_text("", encoding="utf-8")
    (work / ".git" / "index.lock").write_text("", encoding="utf-8")
    assert git_ops.fetch_tree_only(work, url, "main", "public", None) == sha
    assert not (work / ".git" / "shallow.lock").exists()


def test_read_trial_parses_reward_and_exception(tmp_path):
    trial = tmp_path / "job" / "t__1"
    (trial / "verifier").mkdir(parents=True)
    (trial / "verifier" / "test-stdout.txt").write_text("ok", encoding="utf-8")
    (trial / "result.json").write_text(json.dumps({
        "task_name": "x", "verifier_result": {"rewards": {"reward": 1}}, "reward_valid": True,
        "exception_info": {"exception_type": "AgentTimeoutError", "exception_message": "8h"},
        "agent_result": {"n_output_tokens": 50}, "started_at": "2026-11-17T01:00:00Z", "finished_at": "2026-11-17T01:02:05Z",
    }), encoding="utf-8")
    (tmp_path / "job" / "result.json").write_text("{}", encoding="utf-8")  # result của job: bỏ qua
    out = harbor.read_trial(tmp_path / "job")
    assert out.reward == 1.0 and out.valid and out.seconds == 125 and out.tokens == 50
    assert out.exception.startswith("AgentTimeoutError") and out.verifier_tail == "ok"


def test_build_command_for_openai_endpoint(monkeypatch):
    monkeypatch.setenv("MY_KEY", "sk-test")
    ep = ModelEndpoint(id="my-chat-v3", kind="openai", base_url="http://gpu:8000/v1", secret_name="MY_KEY", agent=None)
    cmd, env = harbor.build_command(ep, Path("/t/task"), Path("/jobs"), "job1")
    s = " ".join(cmd)
    assert "-a terminus-2" in s and "-m my-chat-v3" in s and "--base-url http://gpu:8000/v1" in s
    assert "--api-format openai_chat_completions" in s and "--api-key-name MY_KEY" in s
    assert env["MY_KEY"] == "sk-test"
    with pytest.raises(harbor.HarborError):
        harbor.build_command(ModelEndpoint(id="m", kind="mock", base_url="mock://1"), Path("/t"), Path("/j"), "j")


def test_build_command_for_claude_subscription(monkeypatch):
    """OAuth: token phải tới claude-code dưới đúng tên CLAUDE_CODE_OAUTH_TOKEN, không đi qua
    --api-key-name (Harbor sẽ coi nó là ANTHROPIC_API_KEY), và API key sẵn có bị gỡ để lượt
    chạy không âm thầm tính tiền theo API."""
    monkeypatch.setenv("MY_CLAUDE_TOKEN", "sk-ant-oat-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-should-not-leak")
    ep = ModelEndpoint(id="haiku-sub", kind="harbor", agent="claude-code", model_name="anthropic/claude-haiku-5-5",
                       secret_name="MY_CLAUDE_TOKEN", auth="oauth")
    cmd, env = harbor.build_command(ep, Path("/t/task"), Path("/jobs"), "job1")
    s = " ".join(cmd)
    assert "-a claude-code" in s and "-m anthropic/claude-haiku-5-5" in s and "--api-key-name" not in s
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat-test" and env["CLAUDE_FORCE_OAUTH"] == "1"
    assert "ANTHROPIC_API_KEY" not in env

    monkeypatch.delenv("MY_CLAUDE_TOKEN")
    with pytest.raises(harbor.HarborError, match="thiếu bí mật"):
        harbor.build_command(ep, Path("/t/task"), Path("/jobs"), "job1")


def test_build_command_with_apt_mirror(monkeypatch):
    """BENCH_APT_MIRROR: bọc agent bằng bench_apt_mirror và đưa module vào PYTHONPATH của Harbor."""
    import os

    monkeypatch.setenv("TOK", "sk-ant-oat-test")
    monkeypatch.setenv("PYTHONPATH", "X:/old")
    monkeypatch.setattr(harbor.settings, "apt_mirror", "http://mirror.bizflycloud.vn/ubuntu")
    ep = ModelEndpoint(id="opus-sub", kind="harbor", agent="claude-code", model_name="anthropic/claude-opus-4-7", secret_name="TOK", auth="oauth")
    cmd, env = harbor.build_command(ep, Path("/t/task"), Path("/jobs"), "job1")
    assert cmd[cmd.index("-a") + 1] == "bench_apt_mirror:claude-code"
    assert env["BENCH_APT_MIRROR"] == "http://mirror.bizflycloud.vn/ubuntu"
    assert env["PYTHONPATH"].split(os.pathsep) == [str(harbor.PLUGINS_DIR), "X:/old"]
    assert (harbor.PLUGINS_DIR / "bench_apt_mirror.py").is_file()

    # Agent dạng đường dẫn tuỳ chỉnh: giữ nguyên
    custom = ModelEndpoint(id="c", kind="harbor", agent="my.mod:Agent")
    cmd, _ = harbor.build_command(custom, Path("/t/task"), Path("/jobs"), "job1")
    assert cmd[cmd.index("-a") + 1] == "my.mod:Agent"

    monkeypatch.setattr(harbor.settings, "apt_mirror", "")
    cmd, env = harbor.build_command(ep, Path("/t/task"), Path("/jobs"), "job1")
    assert cmd[cmd.index("-a") + 1] == "claude-code" and "BENCH_APT_MIRROR" not in env


def test_build_command_for_custom_node_agent(monkeypatch, tmp_path):
    """Agent tuỳ chỉnh (`node_agent:NodeAgent`): harbor_plugins/ luôn ở PYTHONPATH, biến cấu hình
    trong .env được chuyển sang Harbor theo BENCH_HARBOR_PASS_ENV, và không truyền --api-key-name
    (Harbor từ chối agent không khai MODEL_CONNECTION nhận cờ đó)."""
    import os

    from app.services.secrets import SecretStore

    dotenv = tmp_path / ".env"
    dotenv.write_text("NODE_AGENT_CMD=agent run\nNODE_AGENT_PACKAGE=D:/pkg/agent-1.0.0.tgz\nCOMPANY_KEY=sk-co\nOTHER=x\n", encoding="utf-8")
    monkeypatch.setattr(harbor, "secrets", SecretStore(tmp_path / "secrets.json", dotenv))
    monkeypatch.setattr(harbor.settings, "apt_mirror", "")
    monkeypatch.setattr(harbor.settings, "harbor_pass_env", ["NODE_AGENT_*"])
    monkeypatch.delenv("PYTHONPATH", raising=False)
    ep = ModelEndpoint(id="company", kind="harbor", agent="node_agent:NodeAgent", model_name="company/m1", secret_name="COMPANY_KEY")
    cmd, env = harbor.build_command(ep, Path("/t/task"), Path("/jobs"), "job1")
    assert cmd[cmd.index("-a") + 1] == "node_agent:NodeAgent" and "-m" in cmd and "--api-key-name" not in cmd
    assert env["PYTHONPATH"].split(os.pathsep) == [str(harbor.PLUGINS_DIR)]
    assert env["NODE_AGENT_CMD"] == "agent run" and env["COMPANY_KEY"] == "sk-co" and "OTHER" not in env
    assert (harbor.PLUGINS_DIR / "node_agent.py").is_file()

    # Agent cài sẵn vẫn nhận --api-key-name như cũ
    codex = ModelEndpoint(id="cx", kind="harbor", agent="codex", secret_name="COMPANY_KEY")
    monkeypatch.setattr(harbor, "check_agent", lambda name: None)
    cmd, _ = harbor.build_command(codex, Path("/t/task"), Path("/jobs"), "job1")
    assert "--api-key-name" in cmd


def test_stop_removes_task_containers(tmp_path, monkeypatch):
    """Agent chạy trong container: dừng Harbor thôi thì agent vẫn chạy tiếp (run-27 tiêu
    thêm ~25 phút hạn mức sau khi đã bấm Dừng). Phải gỡ container theo project compose."""
    import subprocess
    from types import SimpleNamespace

    job = tmp_path / "e34-formal-crypto"
    (job / "formal-crypto__dHCKUK7").mkdir(parents=True)
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        out = ""
        if cmd[1:3] == ["container", "ls"] and cmd[-1].endswith("=formal-crypto__dhckuk7__env"):
            out = "abc123\n"
        if cmd[1:3] == ["network", "ls"]:
            out = "net1\n"
        return SimpleNamespace(returncode=0, stdout=out)

    monkeypatch.setattr(harbor.settings, "harbor_env", "docker")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert harbor.remove_job_containers(job) == ["formal-crypto__dhckuk7__env"]
    assert ["docker", "rm", "-f", "abc123"] in calls
    assert ["docker", "network", "rm", "net1"] in calls

    # Môi trường không phải docker: không đụng tới Docker
    calls.clear()
    monkeypatch.setattr(harbor.settings, "harbor_env", "fake")
    assert harbor.remove_job_containers(job) == [] and calls == []


def test_task_images_reads_toml_and_compose(tmp_path):
    t = tmp_path / "hil-bench-sql_3-ask_human"
    (t / "environment").mkdir(parents=True)
    (t / "task.toml").write_text('[environment]\ndocker_image = "reg/img@sha256:aaa"\n', encoding="utf-8")
    (t / "environment" / "docker-compose.yaml").write_text(textwrap.dedent("""
        services:
          main: {image: "reg/img@sha256:aaa"}
          ask-human: {image: "reg/img@sha256:bbb"}
          sidecar: {image: "${SIDECAR_IMAGE}"}
          built: {build: .}
    """), encoding="utf-8")
    assert harbor.task_images(t) == {"reg/img@sha256:aaa", "reg/img@sha256:bbb"}
    assert harbor.task_images(tmp_path / "missing") == set()


def test_cleanup_task_images(monkeypatch):
    """Gỡ image kéo sẵn + image compose build cho đúng trial; image không có trong máy thì bỏ qua."""
    from types import SimpleNamespace

    calls: list[tuple] = []
    sizes = {"reg/img@sha256:aaa": "1700000000", "sha256:built1": "500000000", "sha256:inuse": "100"}

    def fake_docker(*args, timeout=120):
        calls.append(args)
        if args[:2] == ("image", "ls"):
            return SimpleNamespace(returncode=0, stdout="sha256:built1\nsha256:other\nsha256:inuse\n")
        if args[:2] == ("image", "inspect") and args[3] != "{{.Size}}":
            return SimpleNamespace(returncode=0, stdout="sha256:built1 task-a__abc1234__env\nsha256:other otherproj\nsha256:inuse task-a__abc1234__verifier__trial\n")
        if args[:2] == ("image", "inspect"):
            ref = args[-1]
            return SimpleNamespace(returncode=0 if ref in sizes else 1, stdout=sizes.get(ref, ""))
        if args[:2] == ("image", "rm"):
            return SimpleNamespace(returncode=1 if args[-1] == "sha256:inuse" else 0, stdout="")
        raise AssertionError(args)

    monkeypatch.setattr(harbor.settings, "harbor_env", "docker")
    monkeypatch.setattr(harbor.settings, "harbor_cleanup_images", True)
    monkeypatch.setattr(harbor, "_docker", fake_docker)
    n, size = harbor.cleanup_task_images({"reg/img@sha256:aaa", "reg/img@sha256:notpulled"}, ["task-a__ABC1234"])
    assert (n, size) == (2, 2_200_000_000)
    rm = {a[-1] for a in calls if a[:2] == ("image", "rm")}
    assert rm == {"reg/img@sha256:aaa", "sha256:built1", "sha256:inuse"}  # sha256:other không thuộc trial này

    calls.clear()
    monkeypatch.setattr(harbor.settings, "harbor_cleanup_images", False)
    assert harbor.cleanup_task_images({"reg/img@sha256:aaa"}, ["task-a__abc1234"]) == (0, 0) and calls == []


def test_kill_orphan_compose(tmp_path, monkeypatch):
    """Harbor hết giờ khi đang kéo image thì bỏ lại `docker compose up` chạy ngầm (run-36:
    hai tiến trình kéo image thêm hơn 1 giờ sau khi task đã bị đánh lỗi)."""
    import os
    import subprocess
    from types import SimpleNamespace

    job = tmp_path / "e43-hil-bench-swe_40-full_info"
    (job / "hil-bench-swe_40-full_info__SagPdbq").mkdir(parents=True)
    listing = (
        "31668 docker compose --project-name hil-bench-swe_40-full_info__sagpdbq__env --project-directory X up --detach --wait\n"
        "2616 docker compose --project-name hil-bench-swe_94-full_info__y2mruxq__env up\n"
        "500 docker ps\n"
    )
    killed: list[str] = []

    def fake_run(cmd, **kw):
        if cmd[0] in ("powershell", "ps"):
            return SimpleNamespace(returncode=0, stdout=listing)
        if cmd[0] == "taskkill":
            killed.append(cmd[-1])
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(harbor.settings, "harbor_env", "docker")
    monkeypatch.setattr(subprocess, "run", fake_run)
    if os.name != "nt":
        monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append(str(pid)))
    assert harbor.kill_orphan_compose(job) == 1
    assert killed == ["31668"]  # không đụng compose của task khác

    monkeypatch.setattr(harbor.settings, "harbor_env", "fake")
    assert harbor.kill_orphan_compose(job) == 0


def test_remove_stale_worktrees(tmp_path, monkeypatch):
    from app.services import runner

    wt = tmp_path / "worktrees"
    (wt / "run-15" / "sub").mkdir(parents=True)
    (wt / "run-15" / "sub" / "f.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(runner.settings, "data_dir", tmp_path)
    runner.remove_stale_worktrees([wt / "run-15"])
    assert not (wt / "run-15").exists()


def test_auto_detect_source_without_manifest(client, harbor_repo):
    test = client.post("/api/sources/test", json={"url": str(harbor_repo)}).json()
    assert test["ok"], test
    assert test["checks"][1]["label"] == "Không có bench.yaml" and test["checks"][1]["ok"]
    assert {b["key"]: b["size"] for b in test["benches"]} == {"tasks": 4, "vn-bench-layer-b-tasks": 2}


def test_manifest_with_prepare_and_exclude(client, harbor_repo):
    (harbor_repo / "bench.yaml").write_text(textwrap.dedent("""
        version: 1
        benches:
          - id: en-baseline
            name: Terminal-Bench gốc
            data: tasks
            exclude: [atrx-vep-crispr]
            scoring: {type: harbor}
          - id: vn-layer-a-vi
            name: VN Lớp A · vi
            data: vn-bench/layer-a/build/vi
            prepare: python build_variants.py --variants vi
            scoring: {type: harbor}
          - id: vn-layer-a-broken
            name: Prepare lỗi
            data: vn-bench/layer-a/build/zz
            prepare: python -c "import sys; sys.exit('build hỏng')"
            scoring: {type: harbor}
    """), encoding="utf-8")
    try:
        test = client.post("/api/sources/test", json={"url": str(harbor_repo)}).json()
        assert {b["key"]: b["size"] for b in test["benches"]} == {"en-baseline": 3, "vn-layer-a-vi": 1}
        assert any("vn-layer-a-broken" in w and "prepare" in w for w in test["warnings"])
    finally:
        (harbor_repo / "bench.yaml").unlink()
        import shutil
        shutil.rmtree(harbor_repo / "vn-bench/layer-a/build", ignore_errors=True)


@pytest.fixture(scope="module")
def seeded(client, harbor_repo):
    r = client.post("/api/sources", json={"url": str(harbor_repo), "sync_mode": "manual"})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    deadline = time.time() + 20
    while time.time() < deadline:
        s = next(x for x in client.get("/api/sources").json() if x["id"] == sid)
        if s["status"] in ("ok", "warn", "error"):
            break
        time.sleep(0.1)
    assert s["status"] == "ok", s
    for body in (
        {"id": "h-oracle", "kind": "harbor", "agent": "oracle", "role": "contestant"},
        {"id": "h-nop", "kind": "harbor", "agent": "nop", "role": "contestant"},
        {"id": "h-slow", "kind": "harbor", "agent": "slow", "role": "contestant"},
    ):
        m = client.post("/api/models", json=body)
        assert m.status_code == 201 and m.json()["status"] == "ok", m.text
    return {b["key"]: b for b in client.get("/api/benches").json() if b["source"].startswith(harbor_repo.name)}


def test_harbor_run_scores_from_verifier(client, seeded):
    bench = seeded["tasks"]
    assert bench["scoring_type"] == "harbor" and not bench["judge_required"]
    r = client.post("/api/runs", json={"bench_id": bench["id"], "models": ["h-oracle", "h-nop"], "samples": "all", "concurrency": 2})
    assert r.status_code == 201, r.text
    run = wait_run(client, r.json()["id"])
    assert run["status"] == "done", run
    e = {x["model"]: x for x in run["entries"]}
    # infra-broken không có reward → "cần xem lại", không tính vào điểm
    assert e["h-oracle"]["score"] == 100.0 and e["h-oracle"]["review"] == 1 and e["h-oracle"]["passed"] == 3
    assert e["h-nop"]["score"] == 0.0
    msgs = [l["msg"] for l in run["logs"]]
    assert any("Harbor 0.0.0-fake" in m for m in msgs)
    assert any("task t-alpha · reward 1" in m for m in msgs)
    assert any("infra-broken" in m and "EnvironmentBuildError" in m for m in msgs)

    detail = client.get(f"/api/results/{e['h-oracle']['id']}").json()
    assert {c["name"] for c in detail["categories"]} == {"Software", "Science"}
    samples = client.get(f"/api/results/{e['h-oracle']['id']}/samples").json()["items"]
    alpha = next(s for s in samples if s["sample_id"] == "t-alpha")
    assert alpha["prompt"] == "Đề bài của t-alpha" and alpha["note"] == "1 passed" and alpha["tokens"] == 321


def test_mock_model_rejected_for_harbor_bench(client, seeded):
    client.post("/api/models", json={"id": "m-mock", "kind": "mock", "base_url": "mock://1"})
    r = client.post("/api/runs", json={"bench_id": seeded["tasks"]["id"], "models": ["m-mock"]})
    assert r.status_code == 422 and "Harbor" in r.json()["detail"]


def test_cancel_stops_harbor_process(client, seeded):
    r = client.post("/api/runs", json={"bench_id": seeded["vn-bench-layer-b-tasks"]["id"], "models": ["h-slow"], "samples": "all"})
    rid = r.json()["id"]
    time.sleep(1.5)
    t0 = time.time()
    assert client.post(f"/api/runs/{rid}/cancel").status_code == 200
    run = wait_run(client, rid, timeout=60)
    assert run["status"] == "stopped"
    assert time.time() - t0 < 45  # tiến trình con bị dừng, không chờ hết 120s
