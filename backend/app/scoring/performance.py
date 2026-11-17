"""性能指标的稳健统计量（M5 §5.7）。"""

from collections.abc import Sequence
from statistics import median


def summarize_latency(values: Sequence[int | float]) -> tuple[float | None, float | None]:
    """返回中位数与 nearest-rank P95；空输入不补零。"""
    if not values:
        return None, None
    ordered = sorted(float(value) for value in values)
    p95_index = max(0, (95 * len(ordered) + 99) // 100 - 1)
    return float(median(ordered)), ordered[p95_index]


def throughput(audio_ms: Sequence[int], wall_ms: Sequence[int]) -> float | None:
    """按 Σ音频时长 / Σ墙上耗时计算吞吐倍率。"""
    pairs = [
        (audio, wall)
        for audio, wall in zip(audio_ms, wall_ms, strict=True)
        if audio > 0 and wall > 0
    ]
    total_audio = sum(audio for audio, _wall in pairs)
    total_wall = sum(wall for _audio, wall in pairs)
    return total_audio / total_wall if total_audio and total_wall else None
