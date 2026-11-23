"""Xuất báo cáo tiến độ của một repo bench ra một thư mục: CSV + Markdown + Dashboard HTML.

Chỉ ĐỌC bench.db (mở ở chế độ read-only), không đụng tới run đang chạy.

    python scripts/export_report.py --source-id 2 --repo D:/Terminal-Bench/terminal-bench-lct \
        --out ../reports/terminal-bench-2026-11-22 --notes ../reports/nhan_xet_run42.json

Các file sinh ra:
    01_so_task.md            số task của mỗi bộ
    01_danh_sach_task.csv    từng task của mỗi bộ, có/không chạy, lý do loại, tài nguyên
    02_tien_do_chay.md/.csv  tiến độ mỗi bộ: đã chạy bao nhiêu / tổng
    02_chi_tiet_sample.csv   kết quả từng task của lần chạy chính
    03_bao_cao_dashboard.html  báo cáo tự chứa, mở bằng trình duyệt, không cần chạy Dashboard
    README.md
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sqlite3
import subprocess
import tomllib
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from pathlib import Path

VN = timezone(timedelta(hours=7))
BACKEND = Path(__file__).resolve().parent.parent

# Lý do loại task không suy ra được từ task.toml (lấy theo chú thích trong bench.yaml).
EXCLUDE_REASON = {
    "atrx-vep-crispr": "Bỏ khỏi baseline theo README của vn-bench",
    "batched-eval-parity": "Verifier cần chặn mạng; Docker Desktop trên WSL2 không chặn được nên Harbor từ chối chấm",
    "lake-temp-glm": "Verifier cần chặn mạng; Docker Desktop trên WSL2 không chặn được nên Harbor từ chối chấm",
}

RESULT_LABEL = {
    "pass": "Đạt", "fail": "Không đạt", "crash": "Agent dừng do lỗi", "timeout": "Hết giờ",
    "review": "Không chấm được", "running": "Đang chạy", "pending": "Chờ chạy",
}


# ------------------------------------------------------------------ tiện ích

def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def vn(dt: datetime | None, fmt: str = "%H:%M %d/%m/%Y") -> str:
    return dt.astimezone(VN).strftime(fmt) if dt else ""


def hm(seconds: float) -> str:
    m = int(round(seconds / 60))
    h, m = divmod(m, 60)
    return f"{h} giờ {m:02d} phút" if h else f"{m} phút"


def write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    # utf-8-sig để Excel mở đúng tiếng Việt
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def md_table(header: list[str], rows: list[list], align: str = "") -> str:
    al = align or "l" * len(header)
    sep = ["---:" if a == "r" else ":---:" if a == "c" else "---" for a in al]
    out = ["| " + " | ".join(header) + " |", "| " + " | ".join(sep) + " |"]
    out += ["| " + " | ".join(str(c).replace("|", "\\|") for c in r) + " |" for r in rows]
    return "\n".join(out)


def is_excluded(name: str, exclude: list[str]) -> bool:
    return any(name == p or fnmatchcase(name, p) for p in exclude)


def error_type(note: str) -> str:
    m = re.match(r"(?:Không có reward hợp lệ: )?([A-Za-z]+(?:Error|Exception))", note or "")
    return m.group(1) if m else ""


def docker_limits() -> tuple[int | None, float | None]:
    try:
        out = subprocess.run(["docker", "info", "--format", "{{.NCPU}} {{.MemTotal}}"],
                             capture_output=True, text=True, timeout=20).stdout.split()
        return int(out[0]), int(out[1]) / 2**30
    except Exception:
        return None, None


# ------------------------------------------------------------------ đọc dữ liệu

def read_task(d: Path) -> dict:
    try:
        cfg = tomllib.loads((d / "task.toml").read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError):
        cfg = {}
    meta, env, agent = cfg.get("metadata", {}), cfg.get("environment", {}), cfg.get("agent", {})
    tags = meta.get("tags") or []
    diff = next((t.split(":", 1)[1] for t in tags if isinstance(t, str) and t.startswith("difficulty:")), "")
    return {
        "category": str(meta.get("category") or ""),
        "subcategory": str(meta.get("subcategory") or ""),
        "difficulty": str(meta.get("difficulty") or diff),
        "cpus": env.get("cpus"),
        "memory_gb": round(env["memory_mb"] / 1024, 1) if env.get("memory_mb") else None,
        "gpus": env.get("gpus") or 0,
        "agent_timeout_min": round(agent["timeout_sec"] / 60) if agent.get("timeout_sec") else None,
        "expert_hours": meta.get("expert_time_estimate_hours"),
    }


def load(db_path: Path, source_id: int, repo: Path, notes: dict, only: list[str] | None = None) -> dict:
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    q = lambda s, *a: con.execute(s, a).fetchall()

    src = q("select * from sources where id=?", source_id)[0]
    host_cpus, host_mem = docker_limits()
    now = datetime.now(timezone.utc)

    benches = []
    for b in q("select * from benches where source_id=? and active=1 order by id", source_id):
        if only and b["key"] not in only:
            continue
        opts = json.loads(b["options"] or "{}")
        exclude = opts.get("exclude") or []
        root = repo / b["data_path"]
        tasks = []
        if root.is_dir():
            for d in sorted(p for p in root.iterdir() if p.is_dir() and (p / "task.toml").is_file() and not p.name.startswith(("_", "."))):
                info = read_task(d)
                excl = is_excluded(d.name, exclude)
                reason = ""
                if excl:
                    reason = f"Cần GPU (gpus = {info['gpus']}), máy không có GPU H100" if info["gpus"] else EXCLUDE_REASON.get(d.name, "Loại trong bench.yaml")
                warn = []
                if not excl and host_cpus and info["cpus"] and info["cpus"] > host_cpus:
                    warn.append(f"cần {info['cpus']} CPU > {host_cpus} của Docker")
                if not excl and host_mem and info["memory_gb"] and info["memory_gb"] > host_mem:
                    warn.append(f"cần {info['memory_gb']:g} GB RAM > {host_mem:.1f} GB của Docker")
                tasks.append({"name": d.name, **info, "included": not excl, "reason": reason, "warn": "; ".join(warn)})

        # Lần chạy chính = lần mới nhất chạy cả bộ (samples = "all"). Các lần chạy ít task = chạy thử.
        runs = q("select * from runs where bench_id=? order by id desc", b["id"])
        main = next((r for r in runs if json.loads(r["params"] or "{}").get("samples") == "all"), None)
        smoke = []
        for r in runs:
            if main and r["id"] == main["id"]:
                continue
            for e in q("select * from run_entries where run_id=? and done>0", r["id"]):
                smoke.append(f"run #{r['id']} {e['model_id']}: {e['passed']}/{e['done']} đạt")
        # Lịch sử của phiên bản cũ cùng thư mục (bộ đã đổi khoá trong manifest, vd. trước khi có bench.yaml).
        old = q("select b.size, r.id, e.model_id, e.done, e.passed from benches b join runs r on r.bench_id=b.id "
                "join run_entries e on e.run_id=r.id where b.source_id=? and b.active=0 and b.data_path=? and e.done>0 "
                "order by e.done desc, r.id desc limit 1", source_id, b["data_path"])
        for o in old:
            smoke.append(f"bản cũ {o['size']} task đã chạy {o['done']}/{o['size']} (run #{o['id']} {o['model_id']}, {o['passed']} đạt)")

        entries = []
        if main:
            started = parse_ts(main["started_at"])
            finished = parse_ts(main["finished_at"])
            logs = q("select ts, message from run_logs where run_id=? order by id", main["id"])
            for e in q("select * from run_entries where run_id=? order by id", main["id"]):
                samples = {s["sample_id"]: dict(s) for s in q("select * from sample_results where entry_id=? order by id", e["id"])}
                begun = {}
                for lg in logs:
                    m = re.search(rf"{re.escape(e['model_id'])} · task (\S+) · bắt đầu", lg["message"])
                    if m:
                        begun[m.group(1)] = parse_ts(lg["ts"])
                rows = []
                for t in [t for t in tasks if t["included"]]:
                    s = samples.get(t["name"])
                    n = notes.get(t["name"], {}) if isinstance(notes.get(t["name"]), dict) else {}
                    if s:
                        note = s["note"] or ""
                        if s["passed"]:
                            k = "pass"
                        elif s["needs_review"]:
                            k = "review"
                        elif note.startswith("AgentTimeoutError"):
                            k = "timeout"
                        elif note.startswith("NonZeroAgentExitCodeError"):
                            k = "crash"
                        else:
                            k = "fail"
                        rows.append({
                            "task": t["name"], "category": t["category"], "status": "Xong", "k": k,
                            "reward": "" if s["needs_review"] else s["value"], "raw": s["raw_score"],
                            "minutes": round(s["latency_ms"] / 60000, 1), "tokens": s["tokens"],
                            "error": error_type(note), "group": n.get("nhom", "" if k == "pass" else "Model"),
                            "cause": n.get("nguyen_nhan", ""), "rerun": bool(n.get("chay_lai")),
                            "started": vn(begun.get(t["name"])),
                        })
                    else:
                        running = t["name"] in begun and main["status"] == "running"
                        k = "running" if running else "pending"
                        rows.append({
                            "task": t["name"], "category": t["category"], "status": RESULT_LABEL[k], "k": k,
                            "reward": "", "raw": "", "minutes": round((now - begun[t["name"]]).total_seconds() / 60, 1) if running else "",
                            "tokens": "", "error": "", "group": "", "cause": "", "rerun": False,
                            "started": vn(begun.get(t["name"])),
                        })
                end = finished or now
                elapsed = (end - started).total_seconds() if started else 0
                entries.append({
                    "model": e["model_id"], "total": e["total"], "done": e["done"], "passed": e["passed"],
                    "review": e["review"], "status": e["status"], "rows": rows,
                    "elapsed_s": elapsed, "tokens": sum(r["tokens"] or 0 for r in rows if r["status"] == "Xong"),
                })
            main_info = {
                "id": main["id"], "status": main["status"], "sha": (main["source_sha"] or "")[:7],
                "params": json.loads(main["params"] or "{}"),
                "started": vn(started), "finished": vn(finished), "entries": entries,
            }
        else:
            main_info = None

        benches.append({
            "key": b["key"], "name": b["name"], "desc": b["task"], "data": b["data_path"],
            "exclude": exclude, "prepare": opts.get("prepare") or [], "tasks": tasks,
            "n_all": len(tasks), "n_run": sum(t["included"] for t in tasks),
            "main": main_info, "smoke": smoke,
        })

    agent = {r["id"]: dict(r) for r in q("select id, agent, model_name from model_endpoints")}
    return {
        "repo": src["repo"], "ref": src["ref"], "sha": (src["sha"] or "")[:7],
        "generated": vn(now), "generated_iso": now.isoformat(), "host_cpus": host_cpus,
        "host_mem": round(host_mem, 1) if host_mem else None, "benches": benches, "models": agent,
    }


# ------------------------------------------------------------------ tiến độ

RUN_STATUS = {"running": "Đang chạy", "done": "Xong", "failed": "Lỗi", "stopped": "Đã dừng", "queued": "Chờ"}


def progress_rows(data: dict) -> list[dict]:
    out = []
    for b in data["benches"]:
        m = b["main"]
        if not m:
            out.append({"bench": b["key"], "name": b["name"], "model": "", "run": "", "done": 0, "total": b["n_run"],
                        "passed": 0, "running": 0, "pending": b["n_run"], "status": "Chưa chạy cả bộ",
                        "note": ("Chạy thử: " + "; ".join(b["smoke"])) if b["smoke"] else "Chưa chạy"})
            continue
        for e in m["entries"]:
            c = Counter(r["k"] for r in e["rows"])
            done = sum(1 for r in e["rows"] if r["status"] == "Xong")
            note = f"Bắt đầu {m['started']}"
            if m["status"] == "running" and done:
                rate = done / e["elapsed_s"]
                eta = datetime.now(VN) + timedelta(seconds=(len(e["rows"]) - done) / rate)
                note += f"; đã chạy {hm(e['elapsed_s'])}; ước tính xong khoảng {eta.strftime('%Hh %d/%m')}"
            elif m["finished"]:
                note += f"; kết thúc {m['finished']}"
            out.append({"bench": b["key"], "name": b["name"], "model": e["model"], "run": f"#{m['id']}",
                        "done": done, "total": len(e["rows"]), "passed": c["pass"], "running": c["running"],
                        "pending": c["pending"], "status": RUN_STATUS.get(m["status"], m["status"]), "note": note})
    return out


def group_note(rows: list[dict]) -> str:
    known = [f"`{r['task']}` ({r['cause']})" for r in rows if r["cause"]]
    rest = sum(1 for r in rows if not r["cause"])
    if rest:
        known.append(f"{rest} task agent chạy xong nhưng lời giải sai hoặc chưa đủ" if known else "Lời giải sai hoặc chưa đủ")
    return "; ".join(known)


# ------------------------------------------------------------------ ghi file

def write_all(data: dict, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    B = data["benches"]
    total_run = sum(b["n_run"] for b in B)

    # 01_so_task.md
    lines = [f"# Số task của mỗi bộ bench", "",
             f"Repo `{data['repo']}`, nhánh `{data['ref']}`, commit `{data['sha']}`. Số liệu lấy lúc {data['generated']} (giờ Việt Nam).", "",
             md_table(["Bộ bench", "Tên", "Thư mục", "Task trong thư mục", "Bị loại", "Task chạy"],
                      [[f"`{b['key']}`", b["name"], f"`{b['data']}`", b["n_all"], b["n_all"] - b["n_run"], f"**{b['n_run']}**"] for b in B]
                      + [["**Tổng**", "", "", sum(b["n_all"] for b in B), sum(b["n_all"] - b["n_run"] for b in B), f"**{total_run}**"]],
                      "lllrrr"), "", "## Task bị loại", ""]
    reasons = defaultdict(list)
    for b in B:
        for t in b["tasks"]:
            if not t["included"]:
                reasons[(t["name"], t["reason"])].append(b["key"])
    lines.append(md_table(["Task", "Lý do", "Loại khỏi bộ"], [[f"`{n}`", r, ", ".join(ks)] for (n, r), ks in sorted(reasons.items())]))
    warns = defaultdict(list)
    for b in B:
        for t in b["tasks"]:
            if t["warn"]:
                warns[(t["name"], t["warn"])].append(b["key"])
    if warns:
        lines += ["", f"## Task vượt tài nguyên của máy chạy (Docker: {data['host_cpus']} CPU, {data['host_mem']} GB RAM)", "",
                  "Các task này vẫn nằm trong bộ nhưng có thể lỗi vì thiếu tài nguyên.", "",
                  md_table(["Task", "Yêu cầu", "Có trong bộ"], [[f"`{n}`", w, ", ".join(ks)] for (n, w), ks in sorted(warns.items())])]
    lines += ["", "Danh sách đầy đủ từng task: `01_danh_sach_task.csv`."]
    (out / "01_so_task.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 01_danh_sach_task.csv
    write_csv(out / "01_danh_sach_task.csv",
              ["bo_bench", "ten_bo", "task", "linh_vuc", "linh_vuc_con", "do_kho", "cpus", "ram_gb", "gpus",
               "gioi_han_agent_phut", "uoc_tinh_chuyen_gia_gio", "duoc_chay", "ly_do_loai", "canh_bao_tai_nguyen"],
              [[b["key"], b["name"], t["name"], t["category"], t["subcategory"], t["difficulty"], t["cpus"], t["memory_gb"], t["gpus"],
                t["agent_timeout_min"], t["expert_hours"], "có" if t["included"] else "không", t["reason"], t["warn"]]
               for b in B for t in b["tasks"]])

    # 02_tien_do_chay.csv / .md
    prog = progress_rows(data)
    write_csv(out / "02_tien_do_chay.csv",
              ["bo_bench", "ten_bo", "thi_sinh", "lan_chay", "da_chay", "tong", "phan_tram", "dat", "dang_chay", "cho_chay", "trang_thai", "ghi_chu"],
              [[p["bench"], p["name"], p["model"], p["run"], p["done"], p["total"], round(100 * p["done"] / p["total"], 1) if p["total"] else 0,
                p["passed"], p["running"], p["pending"], p["status"], p["note"]] for p in prog])

    lines = ["# Tiến độ chạy của mỗi bộ bench", "", f"Số liệu lấy lúc {data['generated']} (giờ Việt Nam).", "",
             md_table(["Bộ bench", "Thí sinh", "Lần chạy", "Đã chạy / Tổng", "%", "Đạt", "Trạng thái", "Ghi chú"],
                      [[f"`{p['bench']}`", p["model"] or "", p["run"], f"**{p['done']}/{p['total']}**",
                        f"{100 * p['done'] / p['total']:.0f}%" if p["total"] else "0%", p["passed"], p["status"], p["note"]] for p in prog],
                      "lllcrrll"),
             ] + ([] if len(B) == 1 else ["", f"Tổng: **{sum(p['done'] for p in prog)}/{total_run}** task đã chạy."])
    for b in B:
        m = b["main"]
        if not m:
            continue
        for e in m["entries"]:
            rows = e["rows"]
            done = [r for r in rows if r["status"] == "Xong"]
            c = Counter(r["k"] for r in rows)
            ep = data["models"].get(e["model"], {})
            lines += ["", f"## Chi tiết `{b['key']}`: run #{m['id']}, {e['model']}", "",
                      f"- Thí sinh: `{e['model']}` (agent `{ep.get('agent') or ''}`, model `{ep.get('model_name') or ''}`)",
                      f"- Commit bench: `{m['sha']}`; chạy {m['params'].get('concurrency', 1)} task song song",
                      f"- Bắt đầu {m['started']}; đã chạy {hm(e['elapsed_s'])}; tổng token {e['tokens']:,}".replace(",", "."),
                      f"- Đã xong **{len(done)}/{len(rows)}**, đạt **{c['pass']}/{len(done)}**", "",
                      "### Kết quả các task đã xong", "",
                      md_table(["Kết quả", "Số task", "Task"],
                               [[RESULT_LABEL[k], c[k], ", ".join(f"`{r['task']}`" for r in rows if r["k"] == k) if k != "fail" else "Agent chạy xong nhưng verifier chấm 0"]
                                for k in ["pass", "fail", "crash", "timeout", "review"] if c[k]], "lrl")]
            groups = Counter(r["group"] for r in done if r["k"] != "pass")
            lines += ["", "### Task không đạt, theo nguyên nhân", "",
                      md_table(["Nhóm nguyên nhân", "Số task", "Ghi chú"],
                               [[g, n, group_note([r for r in done if r["k"] != "pass" and r["group"] == g])]
                                for g, n in groups.most_common()], "lrl")]
            rerun = [r for r in done if r["rerun"]]
            if rerun:
                lines += ["", f"**Cần chạy lại {len(rerun)} task** vì lỗi không do model: " + ", ".join(f"`{r['task']}`" for r in rerun) + "."]
            cats = defaultdict(lambda: [0, 0])
            for r in done:
                cats[r["category"]][0] += 1
                cats[r["category"]][1] += r["k"] == "pass"
            lines += ["", "### Theo lĩnh vực", "",
                      md_table(["Lĩnh vực", "Đã chạy", "Đạt"], [[k, v[0], v[1]] for k, v in sorted(cats.items(), key=lambda x: -x[1][0])], "lrr")]
            if c["running"]:
                lines += ["", "### Đang chạy", "",
                          md_table(["Task", "Bắt đầu", "Đã chạy"], [[f"`{r['task']}`", r["started"], hm(r["minutes"] * 60)] for r in rows if r["k"] == "running"], "llr")]
    lines += ["", "Kết quả từng task: `02_chi_tiet_sample.csv`."]
    (out / "02_tien_do_chay.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 02_chi_tiet_sample.csv
    write_csv(out / "02_chi_tiet_sample.csv",
              ["bo_bench", "lan_chay", "thi_sinh", "task", "linh_vuc", "trang_thai", "ket_qua", "reward", "thoi_gian_phut",
               "token", "bat_dau", "loai_loi", "nhom_nguyen_nhan", "nguyen_nhan", "nen_chay_lai"],
              [[b["key"], f"#{b['main']['id']}", e["model"], r["task"], r["category"], r["status"],
                RESULT_LABEL[r["k"]] if r["status"] == "Xong" else "", r["reward"], r["minutes"], r["tokens"], r["started"],
                r["error"], r["group"], r["cause"], "có" if r["rerun"] else ""]
               for b in B if b["main"] for e in b["main"]["entries"] for r in e["rows"]])

    # 03_bao_cao_dashboard.html
    (out / "03_bao_cao_dashboard.html").write_text(render_html(data, prog), encoding="utf-8")

    # README.md
    readme = f"""# Báo cáo tiến độ bench: {data['repo']}

Số liệu lấy lúc **{data['generated']}** (giờ Việt Nam) từ Bench Console (Dashboard-Bench), repo `{data['repo']}` commit `{data['sha']}`.

Phạm vi: {", ".join(f"`{b['key']}` ({b['name']}, {b['n_run']} task)" for b in B)}.

| File | Nội dung |
| --- | --- |
| `01_so_task.md` | Số task của mỗi bộ, task bị loại và lý do |
| `01_danh_sach_task.csv` | Từng task của mỗi bộ: lĩnh vực, tài nguyên yêu cầu, có được chạy không |
| `02_tien_do_chay.md` | Tiến độ mỗi bộ (đã chạy / tổng) và phân tích lần chạy đang diễn ra |
| `02_tien_do_chay.csv` | Bảng tiến độ dạng CSV |
| `02_chi_tiet_sample.csv` | Kết quả từng task của lần chạy chính: kết quả, thời gian, token, nguyên nhân lỗi |
| `03_bao_cao_dashboard.html` | Báo cáo Dashboard dạng HTML, mở trực tiếp bằng trình duyệt (không cần mạng) |

## Cách đọc

- **Task chạy** = task trong thư mục trừ các task bị loại trong `bench.yaml`.
- **Lần chạy chính** của một bộ = lần mới nhất chạy cả bộ. Lần chạy ít task được ghi là *chạy thử*.
- **Đạt** = verifier của task cho reward 1. **Không chấm được** = không có reward hợp lệ do lỗi hạ tầng.
- **Nhóm nguyên nhân** (Hạ tầng/API, Adapter, Model) được xác định bằng cách đọc log agent; task không có ghi chú riêng được tính là Model.
- File CSV lưu UTF-8 có BOM để Excel hiển thị đúng tiếng Việt.
- Báo cáo không chứa log thô, API key hay token.

## Tạo lại báo cáo

Chạy từ thư mục `backend` của Dashboard-Bench:

```
python scripts/export_report.py --source-id 2 --repo D:/Terminal-Bench/terminal-bench-lct --out ../reports/<tên-thư-mục> --notes ../reports/nhan_xet_run42.json{"".join(f" --bench {b['key']}" for b in B)}
```

Bỏ `--bench` để xuất tất cả các bộ của repo.
"""
    (out / "README.md").write_text(readme, encoding="utf-8")


# ------------------------------------------------------------------ HTML

def render_html(data: dict, prog: list[dict]) -> str:
    payload = {
        "repo": data["repo"], "sha": data["sha"], "generated": data["generated"],
        "host": {"cpus": data["host_cpus"], "mem": data["host_mem"]},
        "labels": RESULT_LABEL, "progress": prog,
        "benches": [{
            "key": b["key"], "name": b["name"], "data": b["data"], "n_all": b["n_all"], "n_run": b["n_run"],
            "tasks": [{k: t[k] for k in ("name", "category", "cpus", "memory_gb", "gpus", "included", "reason", "warn")} for t in b["tasks"]],
            "main": None if not b["main"] else {
                "id": b["main"]["id"], "status": b["main"]["status"], "started": b["main"]["started"], "sha": b["main"]["sha"],
                "concurrency": b["main"]["params"].get("concurrency", 1),
                "entries": [{
                    "model": e["model"], "agent": data["models"].get(e["model"], {}).get("agent") or "",
                    "model_name": data["models"].get(e["model"], {}).get("model_name") or "",
                    "elapsed": hm(e["elapsed_s"]), "tokens": e["tokens"], "rows": e["rows"],
                } for e in b["main"]["entries"]],
            },
        } for b in data["benches"]],
    }
    B = data["benches"]
    payload["title"] = f"Báo cáo tiến độ · {B[0]['name']}" if len(B) == 1 else "Báo cáo tiến độ bench"
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return HTML_TEMPLATE.replace("__DATA__", blob).replace("__TITLE__", html.escape(payload["title"]))


HTML_TEMPLATE = r"""<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{
  --bg:#f6f7f9;--panel:#fff;--text:#1b1f24;--muted:#5d6672;--line:#e1e4e8;--soft:#f0f2f5;--accent:#2457c5;
  --pass:#1a7f37;--fail:#cf222e;--crash:#bc4c00;--timeout:#8250df;--review:#9a6700;--running:#0969da;--pending:#a2aab3;
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --bg:#0e1116;--panel:#161b22;--text:#e6edf3;--muted:#9aa5b1;--line:#2d333b;--soft:#1f252d;--accent:#6ea8fe;
  --pass:#3fb950;--fail:#f85149;--crash:#f0883e;--timeout:#a371f7;--review:#d29922;--running:#58a6ff;--pending:#545d68;}}
:root[data-theme="dark"]{
  --bg:#0e1116;--panel:#161b22;--text:#e6edf3;--muted:#9aa5b1;--line:#2d333b;--soft:#1f252d;--accent:#6ea8fe;
  --pass:#3fb950;--fail:#f85149;--crash:#f0883e;--timeout:#a371f7;--review:#d29922;--running:#58a6ff;--pending:#545d68;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 "Segoe UI",system-ui,-apple-system,Roboto,Arial,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:28px 16px 60px}
header h1{font-size:24px;margin:0 0 4px}
.meta{color:var(--muted);font-size:13px}
.meta code{background:var(--soft);padding:1px 5px;border-radius:4px}
h2{font-size:18px;margin:34px 0 12px}
h3{font-size:15px;margin:22px 0 10px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-top:20px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.kpi .v{font-size:26px;font-weight:650;font-variant-numeric:tabular-nums}
.kpi .l{color:var(--muted);font-size:12.5px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:4px 0;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{padding:8px 12px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;text-transform:uppercase;letter-spacing:.03em;color:var(--muted);font-weight:600;white-space:nowrap}
tr:last-child td{border-bottom:0}
td.n,th.n{text-align:right}
td code{font-size:12.5px;white-space:nowrap}
.bar{height:8px;border-radius:4px;background:var(--soft);overflow:hidden;display:flex;min-width:120px}
.bar span{display:block;height:100%}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600;white-space:nowrap;border:1px solid currentColor}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;margin:10px 0 4px;font-size:12.5px;color:var(--muted)}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}
.stack{height:22px;border-radius:6px;display:flex;overflow:hidden;background:var(--soft)}
.stack span{display:block;height:100%}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media (max-width:760px){.grid2{grid-template-columns:1fr}}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0 10px}
.chips button{font:inherit;font-size:12.5px;border:1px solid var(--line);background:var(--panel);color:var(--text);padding:3px 10px;border-radius:999px;cursor:pointer}
.chips button[aria-pressed="true"]{background:var(--accent);border-color:var(--accent);color:#fff}
input[type=search]{font:inherit;padding:5px 10px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--text);width:240px;max-width:100%}
.muted{color:var(--muted)}
.note{background:var(--soft);border-left:3px solid var(--review);padding:10px 14px;border-radius:6px;margin:12px 0}
details{background:var(--panel);border:1px solid var(--line);border-radius:10px;margin-bottom:8px}
details summary{cursor:pointer;padding:10px 14px;font-weight:600}
details .card{border:0;border-top:1px solid var(--line);border-radius:0}
.theme{float:right;font:inherit;font-size:12px;border:1px solid var(--line);background:var(--panel);color:var(--muted);border-radius:6px;padding:3px 8px;cursor:pointer}
footer{margin-top:40px;color:var(--muted);font-size:12px}
</style>
</head>
<body>
<div class="wrap" id="app"></div>
<script id="data" type="application/json">__DATA__</script>
<script>
(function(){
const D = JSON.parse(document.getElementById('data').textContent);
const L = D.labels;
const COLORS = {pass:'var(--pass)',fail:'var(--fail)',crash:'var(--crash)',timeout:'var(--timeout)',review:'var(--review)',running:'var(--running)',pending:'var(--pending)'};
const ORDER = ['pass','fail','crash','timeout','review','running','pending'];
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmt = n => (n ?? '') === '' ? '' : Number(n).toLocaleString('vi-VN');
const pill = k => `<span class="pill" style="color:${COLORS[k]}">${esc(L[k])}</span>`;
const totalRun = D.benches.reduce((s,b)=>s+b.n_run,0);
const doneAll = D.progress.reduce((s,p)=>s+p.done,0);
const active = D.benches.find(b => b.main && b.main.status==='running') || D.benches.find(b=>b.main);

let h = `<header><button class="theme" id="theme">Đổi nền sáng/tối</button>
<h1>${esc(D.title)}</h1>
<div class="meta">Repo <code>${esc(D.repo)}</code> · commit <code>${esc(D.sha)}</code> · số liệu lúc ${esc(D.generated)} (giờ Việt Nam)</div></header>`;

const sumP = f => D.progress.reduce((s,p)=>s+p[f],0);
let kp = [];
if (D.benches.length > 1) kp.push([D.benches.length,'bộ bench']);
kp.push([totalRun,'task chạy được'], [`${doneAll}/${totalRun}`,'task đã chạy'], [`${sumP('passed')}/${doneAll}`,'task đạt (trong số đã chạy)'],
        [sumP('running'),'task đang chạy'], [sumP('pending'),'task chờ chạy']);
h += `<div class="kpis">${kp.map(([v,l])=>`<div class="kpi"><div class="v">${esc(v)}</div><div class="l">${esc(l)}</div></div>`).join('')}</div>`;

// 1. số task
h += `<h2>1. Số task của mỗi bộ</h2><div class="card"><table><thead><tr><th>Bộ bench</th><th>Tên</th><th>Thư mục</th><th class="n">Trong thư mục</th><th class="n">Bị loại</th><th class="n">Task chạy</th></tr></thead><tbody>`;
for (const b of D.benches) h += `<tr><td><code>${esc(b.key)}</code></td><td>${esc(b.name)}</td><td class="muted"><code>${esc(b.data)}</code></td><td class="n">${b.n_all}</td><td class="n">${b.n_all-b.n_run}</td><td class="n"><b>${b.n_run}</b></td></tr>`;
h += `<tr><td><b>Tổng</b></td><td></td><td></td><td class="n">${D.benches.reduce((s,b)=>s+b.n_all,0)}</td><td class="n">${D.benches.reduce((s,b)=>s+b.n_all-b.n_run,0)}</td><td class="n"><b>${totalRun}</b></td></tr></tbody></table></div>`;
const ex = {}; D.benches.forEach(b=>b.tasks.filter(t=>!t.included).forEach(t=>{(ex[t.name] ??= {reason:t.reason,b:[]}).b.push(b.key)}));
h += `<h3>Task bị loại</h3><div class="card"><table><thead><tr><th>Task</th><th>Lý do</th><th>Loại khỏi bộ</th></tr></thead><tbody>${Object.entries(ex).sort().map(([n,v])=>`<tr><td><code>${esc(n)}</code></td><td>${esc(v.reason)}</td><td class="muted">${esc(v.b.join(', '))}</td></tr>`).join('')}</tbody></table></div>`;

// 2. tiến độ
h += `<h2>2. Tiến độ chạy của mỗi bộ</h2><div class="card"><table><thead><tr><th>Bộ bench</th><th>Thí sinh</th><th>Lần chạy</th><th class="n">Đã chạy / Tổng</th><th>Tiến độ</th><th class="n">Đạt</th><th>Trạng thái</th><th>Ghi chú</th></tr></thead><tbody>`;
for (const p of D.progress){ const pct = p.total ? p.done/p.total*100 : 0; const pp = p.total ? p.passed/p.total*100 : 0;
  h += `<tr><td><code>${esc(p.bench)}</code></td><td>${esc(p.model)}</td><td>${esc(p.run)}</td><td class="n"><b>${p.done}/${p.total}</b></td>
  <td><div class="bar" title="${pct.toFixed(0)}%"><span style="width:${pp}%;background:var(--pass)"></span><span style="width:${pct-pp}%;background:var(--accent);opacity:.55"></span></div><div class="muted" style="font-size:12px">${pct.toFixed(0)}%</div></td>
  <td class="n">${p.passed}</td><td>${esc(p.status)}</td><td class="muted">${esc(p.note)}</td></tr>`; }
h += `</tbody></table></div><div class="legend"><span><i style="background:var(--pass)"></i>Đạt</span><span><i style="background:var(--accent);opacity:.55"></i>Đã chạy, không đạt</span><span><i style="background:var(--soft)"></i>Chưa chạy</span></div>`;

// 2.x chi tiết lần chạy chính
for (const b of D.benches){ if (!b.main) continue; for (const e of b.main.entries){
  const rows = e.rows, done = rows.filter(r=>r.status==='Xong'), c = {}; rows.forEach(r=>c[r.k]=(c[r.k]||0)+1);
  h += `<h2>Chi tiết <code>${esc(b.key)}</code> · run #${b.main.id} · ${esc(e.model)}</h2>
  <div class="meta">Agent <code>${esc(e.agent)}</code>, model <code>${esc(e.model_name)}</code> · bắt đầu ${esc(b.main.started)} · đã chạy ${esc(e.elapsed)} · ${b.main.concurrency} task song song · ${fmt(e.tokens)} token</div>
  <div style="margin-top:14px" class="stack">${ORDER.filter(k=>c[k]).map(k=>`<span title="${esc(L[k])}: ${c[k]}" style="width:${c[k]/rows.length*100}%;background:${COLORS[k]}"></span>`).join('')}</div>
  <div class="legend">${ORDER.filter(k=>c[k]).map(k=>`<span><i style="background:${COLORS[k]}"></i>${esc(L[k])}: <b>${c[k]}</b></span>`).join('')}</div>`;
  const fails = done.filter(r=>r.k!=='pass'), g = {}; fails.forEach(r=>(g[r.group] ??= []).push(r));
  const rerun = done.filter(r=>r.rerun);
  if (rerun.length) h += `<div class="note"><b>${rerun.length} task không đạt vì lỗi hạ tầng/API hoặc adapter, không phải do model</b>, sẽ được chạy lại: ${rerun.map(r=>`<code>${esc(r.task)}</code>`).join(', ')}. Tỉ lệ đạt hiện tại vì vậy thấp hơn năng lực thật của model.</div>`;
  const cats = {}; done.forEach(r=>{const x=(cats[r.category] ??= [0,0]); x[0]++; x[1]+= r.k==='pass'?1:0;});
  h += `<div class="grid2"><div><h3>Task không đạt, theo nguyên nhân</h3><div class="card"><table><thead><tr><th>Nhóm</th><th class="n">Số task</th><th>Task</th></tr></thead><tbody>${Object.entries(g).sort((a,b)=>b[1].length-a[1].length).map(([k,v])=>`<tr><td>${esc(k)}</td><td class="n">${v.length}</td><td class="muted">${v.filter(r=>r.cause).map(r=>esc(r.task)).join(', ') || 'Giải sai hoặc chưa đủ'}${v.some(r=>!r.cause)&&v.some(r=>r.cause)?` và ${v.filter(r=>!r.cause).length} task giải sai`:''}</td></tr>`).join('')}</tbody></table></div></div>
  <div><h3>Theo lĩnh vực</h3><div class="card"><table><thead><tr><th>Lĩnh vực</th><th class="n">Đã chạy</th><th class="n">Đạt</th></tr></thead><tbody>${Object.entries(cats).sort((a,b)=>b[1][0]-a[1][0]).map(([k,v])=>`<tr><td>${esc(k)}</td><td class="n">${v[0]}</td><td class="n">${v[1]}</td></tr>`).join('')}</tbody></table></div></div></div>`;
  const id = `t${b.main.id}_${e.model}`.replace(/\W/g,'_');
  h += `<h3>Kết quả từng task</h3><div class="chips" data-tbl="${id}"><button aria-pressed="true" data-k="">Tất cả (${rows.length})</button>${ORDER.filter(k=>c[k]).map(k=>`<button aria-pressed="false" data-k="${k}">${esc(L[k])} (${c[k]})</button>`).join('')}</div>
  <input type="search" placeholder="Tìm task…" data-tbl="${id}">
  <div class="card" style="margin-top:10px"><table id="${id}"><thead><tr><th>Task</th><th>Lĩnh vực</th><th>Kết quả</th><th class="n">Thời gian</th><th class="n">Token</th><th>Nhóm</th><th>Nguyên nhân / ghi chú</th></tr></thead><tbody>
  ${rows.map(r=>`<tr data-k="${r.k}" data-t="${esc(r.task)}"><td><code>${esc(r.task)}</code></td><td>${esc(r.category)}</td><td>${pill(r.k)}</td><td class="n">${r.minutes===''?'':Math.round(r.minutes)+' ph'}</td><td class="n">${fmt(r.tokens)}</td><td>${esc(r.group)}</td><td class="muted">${esc(r.cause || (r.error ? r.error : ''))}${r.k==='running'?'Bắt đầu '+esc(r.started):''}</td></tr>`).join('')}
  </tbody></table></div>`;
}}

// 3. danh sách task
h += `<h2>3. Danh sách task của từng bộ</h2>`;
for (const b of D.benches){
  h += `<details><summary>${esc(b.name)} · <code>${esc(b.key)}</code> · ${b.n_run}/${b.n_all} task chạy</summary><div class="card"><table><thead><tr><th>Task</th><th>Lĩnh vực</th><th class="n">CPU</th><th class="n">RAM (GB)</th><th class="n">GPU</th><th>Chạy</th><th>Ghi chú</th></tr></thead><tbody>
  ${b.tasks.map(t=>`<tr><td><code>${esc(t.name)}</code></td><td>${esc(t.category)}</td><td class="n">${t.cpus ?? ''}</td><td class="n">${t.memory_gb ?? ''}</td><td class="n">${t.gpus||''}</td><td>${t.included?'có':'<span style="color:var(--fail)">không</span>'}</td><td class="muted">${esc(t.reason || t.warn)}</td></tr>`).join('')}
  </tbody></table></div></details>`;
}
h += `<footer>Tạo bằng <code>backend/scripts/export_report.py</code> từ dữ liệu của Bench Console. Docker lúc xuất báo cáo: ${D.host.cpus ?? '?'} CPU, ${D.host.mem ?? '?'} GB RAM.</footer>`;
document.getElementById('app').innerHTML = h;

// lọc bảng
const state = {};
function apply(id){ const s = state[id] || {k:'',q:''}; document.querySelectorAll(`#${id} tbody tr`).forEach(tr=>{
  tr.style.display = (!s.k || tr.dataset.k===s.k) && (!s.q || tr.dataset.t.includes(s.q)) ? '' : 'none'; }); }
document.querySelectorAll('.chips').forEach(ch=>ch.addEventListener('click',ev=>{ const b = ev.target.closest('button'); if(!b) return;
  ch.querySelectorAll('button').forEach(x=>x.setAttribute('aria-pressed', x===b)); const id = ch.dataset.tbl;
  (state[id] ??= {k:'',q:''}).k = b.dataset.k; apply(id); }));
document.querySelectorAll('input[type=search]').forEach(inp=>inp.addEventListener('input',()=>{ const id = inp.dataset.tbl;
  (state[id] ??= {k:'',q:''}).q = inp.value.trim().toLowerCase(); apply(id); }));
document.getElementById('theme').addEventListener('click',()=>{ const r = document.documentElement;
  const dark = r.dataset.theme ? r.dataset.theme==='dark' : matchMedia('(prefers-color-scheme: dark)').matches;
  r.dataset.theme = dark ? 'light' : 'dark'; });
})();
</script>
</body>
</html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=BACKEND / "data" / "bench.db")
    ap.add_argument("--source-id", type=int, required=True)
    ap.add_argument("--repo", type=Path, required=True, help="bản clone của repo bench, đúng commit đã đồng bộ")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--notes", type=Path, help="JSON: task -> {nhom, nguyen_nhan, chay_lai}")
    ap.add_argument("--bench", action="append", help="chỉ xuất bộ này (khoá trong bench.yaml); lặp lại để chọn nhiều bộ")
    a = ap.parse_args()
    notes = json.loads(a.notes.read_text(encoding="utf-8")) if a.notes else {}
    data = load(a.db, a.source_id, a.repo, notes, a.bench)
    if not data["benches"]:
        raise SystemExit(f"Không có bộ nào khớp --bench {a.bench}")
    write_all(data, a.out)
    print(f"Đã ghi báo cáo vào {a.out.resolve()}")


if __name__ == "__main__":
    main()
