"""评分指标覆盖率状态（M5 §5.9）。"""


def coverage_state(scoreable: int, planned: int) -> tuple[str, str | None]:
    """按计划样本数返回 ok、partial 或 na 与对应原因码。"""
    ratio = scoreable / planned if planned else 0.0
    if scoreable == 0:
        return "na", "no_scoreable_cases"
    if ratio >= 0.95:
        return "ok", None
    return "partial", "low_coverage"
