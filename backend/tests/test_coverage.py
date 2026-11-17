"""评分覆盖率状态边界测试（M5 §5.9）。"""

from app.scoring.coverage import coverage_state


def test_coverage_status_uses_planned_case_threshold() -> None:
    assert coverage_state(94, 100) == ("partial", "low_coverage")
    assert coverage_state(95, 100) == ("ok", None)


def test_coverage_without_scoreable_or_planned_cases_is_na() -> None:
    assert coverage_state(0, 100) == ("na", "no_scoreable_cases")
    assert coverage_state(0, 0) == ("na", "no_scoreable_cases")
