import pytest

from app.services import git_ops, scoring


def test_exact_match_normalizes_case_space_and_punctuation():
    assert scoring.exact_match("  hà   nội. ", "Hà Nội").passed
    assert scoring.exact_match("Suy nghĩ...\nHà Nội", "Hà Nội").passed  # dòng cuối khớp
    assert not scoring.exact_match("Huế", "Hà Nội").passed


def test_unit_test_runs_code_from_markdown_block():
    ok = scoring.unit_test("```python\ndef f(x):\n    return x * 2\n```", "assert f(2) == 4")
    bad = scoring.unit_test("def f(x):\n    return x", "assert f(2) == 4")
    assert ok.passed and not bad.passed


def test_parse_judge_and_threshold():
    score, reason = scoring.parse_judge('Đây là kết quả: {"score": 7, "reason": "ổn"}')
    assert score == 7 and reason == "ổn"
    s = scoring.judge_score(7, "ổn", threshold=6)
    assert s.passed and s.raw == "7/10" and s.value == pytest.approx(0.7)
    with pytest.raises(ValueError):
        scoring.parse_judge("không có json")


@pytest.mark.parametrize("url, repo, host", [
    ("https://github.com/acme/bench-nlg.git", "acme/bench-nlg", "GitHub"),
    ("git@gitlab.com:acme/vn-qa.git", "acme/vn-qa", "GitLab"),
    ("https://git.example.com/team/x", "team/x", "Git"),
])
def test_describe_repo(url, repo, host):
    assert git_ops.describe(url) == (repo, host)
