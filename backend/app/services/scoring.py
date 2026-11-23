"""Các kiểu chấm điểm. Mỗi hàm trả về Score với value chuẩn hoá về 0..1."""

import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .manifest import safe_join

SCORING_LABEL = {
    "exact_match": "Khớp đáp án",
    "unit_test": "Chạy unit test",
    "llm_judge": "LLM chấm theo rubric",
    "custom": "Scorer tuỳ chỉnh",
    "harbor": "Harbor · verifier của task",
}

DEFAULT_RUBRIC = (
    "Chấm theo thang 1–10:\n"
    "- Đúng nội dung so với đáp án/tiêu chí tham chiếu (tối đa 5 điểm)\n"
    "- Đầy đủ các ý được yêu cầu (tối đa 3 điểm)\n"
    "- Diễn đạt rõ ràng, đúng giọng điệu (tối đa 2 điểm)"
)

JUDGE_SYSTEM = (
    "Bạn là giám khảo chấm bài cho mô hình ngôn ngữ. Chấm nghiêm túc theo rubric. "
    'Chỉ trả về DUY NHẤT một JSON: {"score": <số nguyên 1-10>, "reason": "<nhận xét ngắn bằng tiếng Việt>"}'
)


@dataclass
class Score:
    value: float
    raw: str
    passed: bool
    note: str = ""
    needs_review: bool = False


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFC", text or "").strip().lower()
    t = re.sub(r"\s+", " ", t)
    return t.strip(" .。!?;:\"'")


def exact_match(output: str, reference: str) -> Score:
    out, ref = normalize(output), normalize(reference)
    last_line = normalize((output or "").strip().splitlines()[-1]) if (output or "").strip() else ""
    ok = bool(ref) and (out == ref or last_line == ref)
    return Score(1.0 if ok else 0.0, "1/1" if ok else "0/1", ok, "" if ok else f"Kỳ vọng: {reference}")


_CODE_BLOCK = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.S)


def extract_code(output: str) -> str:
    m = _CODE_BLOCK.search(output or "")
    return m.group(1) if m else (output or "")


def unit_test(output: str, test_code: str, timeout_s: int = 10) -> Score:
    """Chạy code của thí sinh + test trong tiến trình Python riêng.

    CẢNH BÁO: đây KHÔNG phải sandbox. Khi chạy thật, hãy chạy worker trong container
    không có mạng, không có bí mật (hoặc thay bằng gVisor / Firecracker…).
    """
    if not test_code:
        return Score(0.0, "0/1", False, "Mẫu thiếu trường 'test'", needs_review=True)
    with tempfile.TemporaryDirectory(prefix="bench-ut-") as d:
        f = Path(d) / "solution_test.py"
        f.write_text(extract_code(output) + "\n\n" + test_code + "\n", encoding="utf-8")
        try:
            p = subprocess.run([sys.executable, "-I", str(f)], cwd=d, capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return Score(0.0, "0/1", False, f"Quá thời gian {timeout_s}s")
    ok = p.returncode == 0
    tail = (p.stderr or "").strip().splitlines()[-1:] if not ok else []
    return Score(1.0 if ok else 0.0, "1/1" if ok else "0/1", ok, tail[0][:300] if tail else "")


def load_custom_scorer(root: Path, spec: str) -> Callable[[dict, str], dict]:
    """spec = "scorers/score.py:score". Hàm nhận (sample, output) và trả về
    {"score": 0..1, "passed": bool, "note": str}.

    CẢNH BÁO: đây là thực thi code từ repo bench. Chỉ kết nối repo bạn tin tưởng.
    """
    file_part, _, func = spec.partition(":")
    path = safe_join(root, file_part)
    mod_spec = importlib.util.spec_from_file_location(f"bench_scorer_{abs(hash(path))}", path)
    if mod_spec is None or mod_spec.loader is None:
        raise RuntimeError(f"Không nạp được scorer {spec}")
    mod = importlib.util.module_from_spec(mod_spec)
    mod_spec.loader.exec_module(mod)
    fn = getattr(mod, func or "score", None)
    if not callable(fn):
        raise RuntimeError(f"Scorer {spec} không có hàm '{func or 'score'}'")
    return fn


def custom(fn: Callable[[dict, str], dict], sample: dict, output: str) -> Score:
    res = fn(sample, output) or {}
    value = float(res.get("score", 0))
    passed = bool(res.get("passed", value >= 0.5))
    return Score(max(0.0, min(1.0, value)), f"{value:.2f}", passed, str(res.get("note", "")))


def judge_messages(rubric: str, prompt: str, reference: str, output: str) -> list[dict]:
    user = (
        f"## Rubric\n{rubric}\n\n## Đề bài\n{prompt}\n\n"
        f"## Đáp án / tiêu chí tham chiếu\n{reference or '(không có)'}\n\n## Trả lời của thí sinh\n{output}"
    )
    return [{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": user}]


def parse_judge(text: str) -> tuple[float, str]:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("không có JSON")
    data = json.loads(m.group(0))
    score = float(data["score"])
    if not 0 <= score <= 10:
        raise ValueError("score ngoài khoảng 0–10")
    return score, str(data.get("reason", ""))


def judge_score(score10: float, reason: str, threshold: float) -> Score:
    s = round(float(score10), 1)
    raw = f"{int(s) if s.is_integer() else s}/10"
    return Score(score10 / 10, raw, score10 >= threshold, reason)


def mock_judge_reply(output: str, reference: str, jitter: float) -> str:
    """Giám khảo giả cho kind = mock: chấm dựa trên độ trùng từ với tham chiếu."""
    out, ref = set(normalize(output).split()), set(normalize(reference).split())
    overlap = len(out & ref) / max(1, len(ref))
    base = 3 + overlap * 6
    score = max(1, min(10, round(base + (jitter - 0.5) * 2)))
    reason = "Bám sát tiêu chí tham chiếu." if score >= 7 else "Thiếu ý hoặc sai so với tiêu chí tham chiếu."
    return json.dumps({"score": score, "reason": reason}, ensure_ascii=False)
