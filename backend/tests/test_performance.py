"""性能统计量测试（M5 §5.7）。"""

from app.scoring.performance import summarize_latency, throughput


def test_latency_summary_uses_median_and_nearest_rank_p95() -> None:
    assert summarize_latency([50, 10, 20, 30, 40]) == (30.0, 50.0)
    assert summarize_latency([9]) == (9.0, 9.0)
    assert summarize_latency([]) == (None, None)


def test_throughput_uses_aggregate_audio_and_wall_time() -> None:
    assert throughput([100, 300], [50, 150]) == 2.0
    assert throughput([100, 0], [0, 20]) is None
