"""确定性 bootstrap 置信区间（M5 §5.8）。"""

import random
from collections.abc import Callable, Sequence


def _bootstrap_ci[T](
    samples: Sequence[T],
    statistic: Callable[[Sequence[T]], float | None],
    *,
    n_resample: int = 1000,
    seed: int = 42,
) -> tuple[float | None, float | None]:
    """按样本有放回重采样，返回 percentile 95% CI。"""
    if not samples or n_resample < 1:
        return None, None

    rng = random.Random(seed)  # noqa: S311 - 固定 seed 的统计重采样不用于安全用途
    size = len(samples)
    estimates: list[float] = []
    for _ in range(n_resample):
        resampled = [samples[rng.randrange(size)] for _ in range(size)]
        estimate = statistic(resampled)
        if estimate is not None:
            estimates.append(estimate)

    if not estimates:
        return None, None
    estimates.sort()
    low_index = int((len(estimates) - 1) * 0.025)
    high_index = int((len(estimates) - 1) * 0.975)
    return estimates[low_index], estimates[high_index]


def ratio_ci(
    samples: Sequence[tuple[int, int]], *, n_resample: int = 1000, seed: int = 42
) -> tuple[float | None, float | None]:
    """比率指标的 utterance bootstrap CI，例如 CER 与 ITN 准确率。"""

    def statistic(rows: Sequence[tuple[int, int]]) -> float | None:
        numerator = sum(row[0] for row in rows)
        denominator = sum(row[1] for row in rows)
        return numerator / denominator if denominator else None

    return _bootstrap_ci(samples, statistic, n_resample=n_resample, seed=seed)


def mean_ci(
    samples: Sequence[float], *, n_resample: int = 1000, seed: int = 42
) -> tuple[float | None, float | None]:
    """逐样本平均指标的 utterance bootstrap CI。"""

    def statistic(rows: Sequence[float]) -> float | None:
        return sum(rows) / len(rows) if rows else None

    return _bootstrap_ci(samples, statistic, n_resample=n_resample, seed=seed)


def f1_ci(
    samples: Sequence[tuple[int, int, int]], *, n_resample: int = 1000, seed: int = 42
) -> tuple[float | None, float | None]:
    """标点 micro F1 的 utterance bootstrap CI。"""

    def statistic(rows: Sequence[tuple[int, int, int]]) -> float | None:
        tp = sum(row[0] for row in rows)
        fp = sum(row[1] for row in rows)
        fn = sum(row[2] for row in rows)
        denominator = 2 * tp + fp + fn
        return 2 * tp / denominator if denominator else None

    return _bootstrap_ci(samples, statistic, n_resample=n_resample, seed=seed)
