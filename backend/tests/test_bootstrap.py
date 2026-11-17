"""Bootstrap CI 的确定性与边界测试（M5 §5.8）。"""

from app.scoring.bootstrap import f1_ci, mean_ci, ratio_ci


def test_ratio_bootstrap_is_reproducible_and_contains_estimate() -> None:
    samples = [(1, 10), (2, 10), (0, 10), (3, 10)]
    first = ratio_ci(samples, n_resample=500, seed=17)
    second = ratio_ci(samples, n_resample=500, seed=17)
    estimate = sum(numerator for numerator, _denominator in samples) / sum(
        denominator for _numerator, denominator in samples
    )
    assert first == second
    assert first[0] is not None and first[0] <= estimate
    assert first[1] is not None and first[1] >= estimate


def test_f1_bootstrap_is_reproducible() -> None:
    samples = [(3, 1, 1), (4, 0, 2), (1, 2, 0)]
    first = f1_ci(samples, n_resample=500, seed=23)
    second = f1_ci(samples, n_resample=500, seed=23)
    assert first == second
    assert first[0] is not None and first[1] is not None
    assert first[0] <= first[1]


def test_mean_bootstrap_is_reproducible() -> None:
    samples = [0.0, 0.2, 0.4, 0.8]
    first = mean_ci(samples, n_resample=500, seed=31)
    assert first == mean_ci(samples, n_resample=500, seed=31)
    assert first[0] is not None and first[1] is not None
    assert first[0] <= sum(samples) / len(samples) <= first[1]


def test_bootstrap_without_scoreable_denominators_returns_no_interval() -> None:
    assert ratio_ci([], n_resample=100) == (None, None)
    assert ratio_ci([(0, 0)], n_resample=100) == (None, None)
    assert f1_ci([(0, 0, 0)], n_resample=100) == (None, None)
