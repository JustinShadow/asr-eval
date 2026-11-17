"""真机探测 CLI 的参数边界与向量输出保护测试。"""

import argparse
import sys
from pathlib import Path
from typing import Any

import pytest

TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(TOOLS_DIR))

from bench_stream import (  # noqa: E402
    AttemptResult,
    WaveResult,
    _concurrency_levels,
    _level_passed,
    _parse_device,
    _percentile,
    _summarize_level,
)
from probe_etsstt import _parse_endpoint, _safe_matches  # noqa: E402


def test_probe_endpoint_accepts_expected_websocket_url() -> None:
    """设备地址限制为 WS 且只允许实时转写协议路径。"""
    assert _parse_endpoint("ws://192.0.2.10:18701/v1/audio/transcription/realtime") == (
        "192.0.2.10",
        18701,
    )
    assert _parse_endpoint("ws://192.0.2.10") == ("192.0.2.10", 80)


@pytest.mark.parametrize(
    "url",
    (
        "wss://192.0.2.10/v1/audio/transcription/realtime",
        "ws://192.0.2.10:0/v1/audio/transcription/realtime",
        "ws://192.0.2.10:70000/v1/audio/transcription/realtime",
        "ws://192.0.2.10/other",
        "ws://[broken/v1/audio/transcription/realtime",
    ),
)
def test_probe_endpoint_rejects_invalid_addresses(url: str) -> None:
    """无效 scheme、端口和路径不会进入设备请求。"""
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_endpoint(url)


def test_probe_match_summary_drops_embedding_material() -> None:
    """诊断摘要只保留 matched_id 与 confidence，不落声纹向量。"""
    source: dict[str, Any] = {
        "0": {
            "voiceprint": {"embedding": [0.25] * 192},
            "match_info": {
                "matched_id": "A",
                "confidence": 0.91,
                "merged_embedding": [0.5] * 192,
            },
        },
        "1": {"match_info": {"matched_id": "B", "confidence": True}},
    }

    assert _safe_matches(source) == [{"cluster_id": "0", "matched_id": "A", "confidence": 0.91}]


def test_benchmark_address_and_percentile_validation() -> None:
    """并发基准拒绝错误端口，并对少量重复样本稳定插值。"""
    assert _parse_device("192.0.2.10:30025") == ("192.0.2.10", 30025)
    assert _percentile([], 0.95) is None
    assert _percentile([0.0, 10.0], 0.95) == 9.5
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_device("192.0.2.10:70000")


def test_benchmark_concurrency_levels_supports_a_single_target_level() -> None:
    """可将真机长稳态验证限定在指定并发档，避免额外压测低档位。"""
    assert _concurrency_levels(3, 3) == [3]
    assert _concurrency_levels(1, 3) == [1, 2, 3]


@pytest.mark.parametrize(("minimum", "maximum"), ((0, 3), (3, 2)))
def test_benchmark_concurrency_levels_rejects_invalid_ranges(minimum: int, maximum: int) -> None:
    """并发范围必须从正整数起步且最小档不大于最大档。"""
    with pytest.raises(argparse.ArgumentTypeError):
        _concurrency_levels(minimum, maximum)


def test_benchmark_curve_uses_device_rtf_and_success_rate() -> None:
    """档位汇总排除预热样本，按重复连接计算成功率与 median / P95。"""
    warmup = WaveResult(
        wall_ms=1000,
        audio_s_per_s=1.0,
        attempts=[AttemptResult(True, 1000, 500, 200, 0.2, 3, 2, None)],
    )
    repeats = [
        WaveResult(
            wall_ms=1000,
            audio_s_per_s=1.0,
            attempts=[AttemptResult(True, 900, 450, 180, 0.2, 3, 2, None)],
        ),
        WaveResult(
            wall_ms=1200,
            audio_s_per_s=0.0,
            attempts=[AttemptResult(False, 1200, None, None, None, 0, 0, "TimeoutError")],
        ),
    ]

    result = _summarize_level(1, warmup, repeats)

    assert result["warmup_successes"] == 1
    assert result["successes"] == 1
    assert result["sessions"] == 2
    assert result["success_rate"] == 0.5
    assert result["warmup_success_rate"] == 1.0
    assert result["rtf_dev_median"] == 0.2
    assert result["ttft_ms_median"] == 450
    assert result["warmup_attempts"] == [
        {
            "ok": True,
            "wall_ms": 1000,
            "ttft_ms": 500,
            "finalize_ms": 200,
            "rtf_dev": 0.2,
            "partial_count": 3,
            "segment_count": 2,
            "error": None,
        }
    ]
    assert not _level_passed(result)
