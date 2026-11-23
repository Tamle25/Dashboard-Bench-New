import pytest

from app.services.manifest import ManifestError, check_files, parse_manifest, safe_join
from conftest import EXAMPLE_DIR


def test_example_manifest_skips_invalid_bench():
    parsed = parse_manifest((EXAMPLE_DIR / "bench.yaml").read_text(encoding="utf-8"))
    assert [b.id for b in parsed.benches] == ["vnqa-mini", "sum-mini", "code-mini"]
    assert len(parsed.warnings) == 1 and "broken-example" in parsed.warnings[0]
    assert parsed.benches[1].needs_judge and not parsed.benches[0].needs_judge


def test_check_files_counts_samples():
    parsed = parse_manifest((EXAMPLE_DIR / "bench.yaml").read_text(encoding="utf-8"))
    valid, warnings = check_files(EXAMPLE_DIR, parsed.benches)
    assert {spec.id: size for spec, size in valid} == {"vnqa-mini": 10, "sum-mini": 5, "code-mini": 3}
    assert warnings == []


@pytest.mark.parametrize("text, msg", [
    ("version: 2\nbenches: []", "version"),
    ("version: 1\nbenches: []", "benches"),
    ("- not a dict", "object"),
    ("version: 1\nbenches: [", "YAML"),
])
def test_invalid_manifests(text, msg):
    with pytest.raises(ManifestError, match=msg):
        parse_manifest(text)


def test_custom_requires_scorer_and_duplicate_ids():
    parsed = parse_manifest(
        "version: 1\nbenches:\n"
        "  - {id: a, name: A, data: d.jsonl, scoring: {type: custom}}\n"
        "  - {id: b, name: B, data: d.jsonl, scoring: {type: exact_match}}\n"
        "  - {id: b, name: B2, data: d.jsonl, scoring: {type: exact_match}}\n"
    )
    assert [b.id for b in parsed.benches] == ["b"]
    assert any("scorer" in w for w in parsed.warnings) and any("trùng id" in w for w in parsed.warnings)


def test_safe_join_blocks_traversal(tmp_path):
    with pytest.raises(ManifestError):
        safe_join(tmp_path, "../outside.jsonl")
