"""Harbor CLI giả cho test: ghi result.json đúng định dạng TrialResult của Harbor.

Hành vi theo agent (-a): oracle → reward 1, nop → reward 0, slow → ngủ lâu (để test dừng).
Task có tên chứa "infra-broken" → lỗi môi trường, không có reward.
"""

import argparse
import json
import sys
import time
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

if "--version" in sys.argv:
    print("0.0.0-fake")
    sys.exit(0)

if sys.argv[1:3] == ["agent", "list"]:  # dùng để kiểm tra tên agent người dùng nhập
    print(json.dumps(["oracle", "nop", "slow", "terminus-2", "claude-code", "codex"]))
    sys.exit(0)

ap = argparse.ArgumentParser()
ap.add_argument("cmd")
ap.add_argument("-p")
ap.add_argument("-o")
ap.add_argument("--job-name")
ap.add_argument("-e")
ap.add_argument("-a")
ap.add_argument("-m")
args, _rest = ap.parse_known_args()

task = Path(args.p)
name = tomllib.loads((task / "task.toml").read_text(encoding="utf-8")).get("task", {}).get("name", task.name)
trial = Path(args.o) / args.job_name / f"{task.name}__fake01"
(trial / "verifier").mkdir(parents=True, exist_ok=True)
(trial / "argv.json").write_text(json.dumps(sys.argv[1:]), encoding="utf-8")

if args.a == "slow":
    time.sleep(120)

t0 = datetime.now(timezone.utc)
t1 = t0 + timedelta(seconds=75)
result = {
    "task_name": name, "trial_name": trial.name, "trial_uri": trial.as_uri(),
    "agent_info": {"name": args.a, "version": "fake"},
    "agent_result": {"n_input_tokens": 1000, "n_output_tokens": 321, "cost_usd": 0.0123},
    "started_at": t0.isoformat(), "finished_at": t1.isoformat(),
}
if "infra-broken" in task.name:
    result.update({"verifier_result": None, "reward_valid": False, "exception_info": {
        "exception_type": "EnvironmentBuildError", "exception_message": "docker build failed", "exception_traceback": "", "occurred_at": t1.isoformat()}})
else:
    reward = 1.0 if args.a == "oracle" else 0.0
    (trial / "verifier" / "test-stdout.txt").write_text("1 passed" if reward else "1 failed: expected slug", encoding="utf-8")
    result.update({"verifier_result": {"rewards": {"reward": reward}}, "reward_valid": True, "exception_info": None})

(trial / "result.json").write_text(json.dumps(result), encoding="utf-8")
(Path(args.o) / args.job_name / "result.json").write_text(json.dumps({"id": "job"}), encoding="utf-8")
