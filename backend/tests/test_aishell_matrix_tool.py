"""AISHELL-1 多设备矩阵工具的选样与 micro CER 汇总测试。"""

import argparse
import sys
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(TOOLS_DIR))

from bench_aishell_matrix import (  # noqa: E402
    CaseResult,
    Sample,
    Target,
    _case_result,
    _load_samples,
    _parse_target,
    _summarize_target,
)

from app.adapters.etsstt import TranscribeOutcome  # noqa: E402


def test_target_parser_requires_name_host_port_and_model() -> None:
    """目标参数明确绑定设备名、监听地址和设备模型键。"""
    assert _parse_target("600=127.0.0.1:18702,asr-b,stream") == Target(
        "600", "127.0.0.1", 18702, "asr-b", "stream"
    )
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_target("600=127.0.0.1:18702")
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_target("600=127.0.0.1:18702,asr-b,streaming")


def test_loader_selects_only_requested_split_without_copying_audio(tmp_path: Path) -> None:
    """选样从指定 split 的参考顺序读取，音频路径仍指向原目录。"""
    dataset_root = tmp_path / "data_aishell"
    transcript_dir = dataset_root / "transcript"
    test_audio_dir = dataset_root / "wav" / "test" / "S0001"
    train_audio_dir = dataset_root / "wav" / "train" / "S0001"
    transcript_dir.mkdir(parents=True)
    test_audio_dir.mkdir(parents=True)
    train_audio_dir.mkdir(parents=True)
    test_audio = test_audio_dir / "BAC009S0001W0001.wav"
    train_audio = train_audio_dir / "BAC009S0001W0002.wav"
    test_audio.write_bytes(b"test")
    train_audio.write_bytes(b"train")
    (transcript_dir / "aishell_transcript_v0.8.txt").write_text(
        "BAC009S0001W0002 训练文本\nBAC009S0001W0001 测试 文本\n", encoding="utf-8"
    )

    samples = _load_samples(dataset_root, "test", 1)

    assert samples == [Sample("BAC009S0001W0001", test_audio, "测试 文本")]
    assert samples[0].audio_path == test_audio


def test_case_scoring_uses_project_cer_and_device_rtf() -> None:
    """单条评分遵循项目归一口径，RTF 用 inference_ms 而非 usage.rtf。"""
    sample = Sample("item-1", Path("audio.wav"), "你好世界")
    outcome = TranscribeOutcome(
        ok=True,
        text="你好世介",
        usage={"inference_ms": 50, "audio_duration_ms": 1000, "rtf": 99.0},
        ttft_ms=120,
        finalize_ms=80,
    )

    result = _case_result(sample, outcome, wall_ms=1500, audio_duration_ms=1000)

    assert result.ok
    assert result.errors == 1
    assert result.reference_chars == 4
    assert result.cer == 0.25
    assert result.rtf_dev == 0.05
    assert not hasattr(result, "text")


def test_target_summary_uses_micro_cer_and_reports_failures_separately() -> None:
    """跨条目 CER 按总编辑数除以参考字符数，失败样本不伪作 CER。"""
    target = Target("test", "127.0.0.1", 18702, "test-model", "stream")
    cases = [
        CaseResult("a", True, 100, 1000, 10, 20, 0.1, 1, 4, 0.25, None),
        CaseResult("b", True, 100, 1000, 11, 21, 0.2, 1, 10, 0.1, None),
        CaseResult("c", False, 50, 1000, None, None, None, None, None, None, "init error"),
    ]

    summary = _summarize_target(target, cases)

    assert summary["samples"] == 3
    assert summary["successes"] == 2
    assert summary["success_rate"] == pytest.approx(2 / 3)
    assert summary["errors"] == 2
    assert summary["reference_chars"] == 14
    assert summary["cer"] == pytest.approx(2 / 14)
