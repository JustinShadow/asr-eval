"""配对 bootstrap 与 BH 校正的单元测试（M6 §5.5 / D-M6-3）。"""

from app.domain.compare import _bh_adjust, _bootstrap_delta


def test_consistent_improvement_is_significant() -> None:
    # baseline 每条错 2 字、other 每条错 0 字 → Δ 恒为 0.2，CI 不跨 0
    pairs = [(2, 10, 0, 10) for _ in range(50)]
    result = _bootstrap_delta(pairs, n_resample=200)
    assert result["delta"] == 0.2
    assert result["ci_low"] == 0.2 and result["ci_high"] == 0.2
    assert 0 < result["p"] <= 0.01


def test_no_difference_has_wide_interval() -> None:
    pairs = [(1, 10, 1, 10) for _ in range(30)]
    result = _bootstrap_delta(pairs, n_resample=200)
    assert result["delta"] == 0.0
    assert result["ci_low"] == 0.0 and result["ci_high"] == 0.0
    assert result["p"] == 1.0


def test_mixed_effect_not_significant() -> None:
    pairs = [
        (0, 10, 3, 10),
        (5, 10, 0, 10),
        (1, 10, 4, 10),
        (4, 10, 2, 10),
    ] * 5
    result = _bootstrap_delta(pairs, n_resample=300)
    assert result["ci_low"] is not None and result["ci_high"] is not None
    assert result["ci_low"] <= result["delta"] <= result["ci_high"]


def test_empty_pairs() -> None:
    result = _bootstrap_delta([])
    assert result["delta"] is None and result["p"] is None


def test_bh_adjust_monotonic_and_bounded() -> None:
    adjusted = _bh_adjust([0.001, 0.02, 0.04, 0.5])
    assert all(0 <= value <= 1 for value in adjusted)
    assert adjusted[0] <= adjusted[1] <= adjusted[2] <= adjusted[3]
    assert adjusted[0] == 0.004  # 0.001 * 4 / 1


def test_bh_preserves_order() -> None:
    p_values = [0.5, 0.001, 0.02]
    adjusted = _bh_adjust(p_values)
    assert adjusted[1] < adjusted[2] < adjusted[0]


def test_run_level_ci_brackets_value() -> None:
    """单 Run CI（M5 §5.8）：区间应包含点估计，且完全正确时上下界相等。"""
    from app.domain.readout import _bootstrap_ci

    units = [("a", 1, 10, "lt_5s"), ("b", 2, 10, "lt_5s"), ("c", 0, 10, "5_15s")]
    value, low, high, n = _bootstrap_ci(units, n_resample=200)
    assert n == 3
    assert value is not None and low is not None and high is not None
    assert low <= value <= high

    perfect = [("a", 0, 10, "lt_5s"), ("b", 0, 10, "5_15s")]
    p_value, p_low, p_high, _ = _bootstrap_ci(perfect, n_resample=100)
    assert p_value == 0.0 and p_low == 0.0 and p_high == 0.0


def test_ci_empty_units() -> None:
    from app.domain.readout import _bootstrap_ci

    assert _bootstrap_ci([]) == (None, None, None, 0)
