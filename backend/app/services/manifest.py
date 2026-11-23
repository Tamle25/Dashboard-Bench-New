"""Hợp đồng manifest (bench.yaml) mà mọi repo bench phải tuân theo.

version: 1
benches:
  - id: vnqa                      # [a-z0-9_-], duy nhất trong repo
    name: VN-QA
    task: Hỏi đáp kiến thức tiếng Việt
    data: data/vnqa.jsonl         # JSONL: {"id", "input", "reference", "category", "test"?}
    scoring:
      type: exact_match           # exact_match | unit_test | llm_judge | custom
      rubric: rubrics/x.md        # tuỳ chọn, dùng cho llm_judge
      scorer: scorers/s.py:score  # bắt buộc khi type = custom
    judge_required: false         # mặc định = (type == llm_judge)

Bộ bench sai schema bị bỏ qua kèm cảnh báo; các bộ khác trong cùng repo vẫn dùng được.

Bộ bench dạng Harbor / Terminal-Bench (mỗi task là một thư mục có task.toml):

  - id: vn-layer-a-vi
    name: VN Lớp A · có dấu
    data: vn-bench/layer-a/build/vi           # thư mục chứa các task
    prepare: python vn-bench/layer-a/build_variants.py --variants vi   # tuỳ chọn
    exclude: [atrx-vep-crispr, "*-draft"]     # tuỳ chọn; tên chính xác hoặc mẫu glob
    scoring:
      type: harbor                            # điểm = reward do verifier của task ghi ra

Repo không có manifest nhưng có thư mục chứa task Harbor sẽ được tự nhận diện
(xem `detect_harbor_dirs`).
"""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator


class ManifestError(Exception):
    pass


class ScoringSpec(BaseModel):
    type: Literal["exact_match", "unit_test", "llm_judge", "custom", "harbor"]
    rubric: str | None = None
    scorer: str | None = None

    @model_validator(mode="after")
    def _check(self):
        if self.type == "custom" and not self.scorer:
            raise ValueError("scoring.scorer là bắt buộc khi type = custom")
        return self


class BenchSpec(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=200)
    task: str = ""
    data: str
    scoring: ScoringSpec
    judge_required: bool | None = None
    # Lệnh chạy trong repo trước khi dùng bộ bench (vd. sinh task biến thể). Chuỗi hoặc danh sách lệnh.
    prepare: str | list[str] | None = None
    # Tên thư mục task bỏ qua (chỉ cho type = harbor). Nhận tên chính xác hoặc mẫu glob (`*-ask_human`).
    exclude: list[str] = []

    @property
    def needs_judge(self) -> bool:
        if self.scoring.type == "harbor":
            return False
        return self.judge_required if self.judge_required is not None else self.scoring.type == "llm_judge"

    @property
    def prepare_list(self) -> list[str]:
        if not self.prepare:
            return []
        return [self.prepare] if isinstance(self.prepare, str) else list(self.prepare)


class ParsedManifest(BaseModel):
    benches: list[BenchSpec]
    warnings: list[str]


def _fmt(e: ValidationError) -> str:
    parts = []
    for err in e.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "manifest"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)


def parse_manifest(text: str) -> ParsedManifest:
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ManifestError(f"YAML không hợp lệ: {e}") from e
    if not isinstance(doc, dict):
        raise ManifestError("Manifest phải là một object YAML.")
    if doc.get("version") != 1:
        raise ManifestError("Thiếu hoặc sai trường 'version' (hiện chỉ hỗ trợ version: 1).")
    raw = doc.get("benches")
    if not isinstance(raw, list) or not raw:
        raise ManifestError("Trường 'benches' phải là danh sách không rỗng.")

    benches: list[BenchSpec] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        label = item.get("id") if isinstance(item, dict) and item.get("id") else f"#{i + 1}"
        try:
            spec = BenchSpec.model_validate(item)
        except ValidationError as e:
            warnings.append(f'Bỏ qua bộ bench "{label}": {_fmt(e)}.')
            continue
        if spec.id in seen:
            warnings.append(f'Bỏ qua bộ bench "{spec.id}": trùng id.')
            continue
        seen.add(spec.id)
        benches.append(spec)
    return ParsedManifest(benches=benches, warnings=warnings)


def safe_join(root: Path, rel: str) -> Path:
    """Ghép đường dẫn trong repo, chặn thoát ra ngoài (../, đường dẫn tuyệt đối)."""
    root_r = root.resolve()
    p = (root_r / rel).resolve()
    if p != root_r and root_r not in p.parents:
        raise ManifestError(f"Đường dẫn '{rel}' nằm ngoài repo.")
    return p


def count_samples(path: Path) -> int:
    with path.open(encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def check_files(root: Path, specs: list[BenchSpec]) -> tuple[list[tuple[BenchSpec, int]], list[str]]:
    """Kiểm tra file dữ liệu / rubric / scorer có tồn tại. Trả về (bench hợp lệ + số mẫu, cảnh báo)."""
    valid: list[tuple[BenchSpec, int]] = []
    warnings: list[str] = []
    for spec in specs:
        try:
            if spec.scoring.type == "harbor":
                from .harbor import list_tasks  # tránh import vòng

                tdir = safe_join(root, spec.data)
                if not tdir.is_dir():
                    hint = " (thư mục này được sinh bởi bước prepare)" if spec.prepare else ""
                    raise ManifestError(f"không có thư mục task {spec.data}{hint}")
                size = len(list_tasks(tdir, spec.exclude))
                if size == 0:
                    raise ManifestError(f"thư mục {spec.data} không chứa task nào có task.toml")
                valid.append((spec, size))
                continue
            data = safe_join(root, spec.data)
            if not data.is_file():
                raise ManifestError(f"không có file dữ liệu {spec.data}")
            size = count_samples(data)
            if size == 0:
                raise ManifestError(f"file dữ liệu {spec.data} rỗng")
            if spec.scoring.rubric and not safe_join(root, spec.scoring.rubric).is_file():
                raise ManifestError(f"không có file rubric {spec.scoring.rubric}")
            if spec.scoring.scorer:
                scorer_file = spec.scoring.scorer.split(":", 1)[0]
                if not safe_join(root, scorer_file).is_file():
                    raise ManifestError(f"không có file scorer {scorer_file}")
        except (ManifestError, OSError, UnicodeDecodeError) as e:
            warnings.append(f'Bỏ qua bộ bench "{spec.id}": {e}.')
            continue
        valid.append((spec, size))
    return valid, warnings
