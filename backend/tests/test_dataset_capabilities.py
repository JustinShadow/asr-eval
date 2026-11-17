"""M1 subset 能力分析单元测试。"""

from app.domain.datasets import (
    _aggregate_capabilities,
    _compute_capabilities,
    _speech_overlap_durations,
)
from app.importers.base import Segment


def _segment(speaker: str, start_ms: int, end_ms: int) -> Segment:
    return {
        "speaker": speaker,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "text": "测试",
    }


def test_capabilities_measure_segments_speakers_itn_and_overlap() -> None:
    """按样本分母统计分段、说话人、ITN 覆盖率与区间重叠比例。"""
    capabilities = _compute_capabilities(
        durations=[1000, 1000],
        refs=["你好。", "世界。"],
        speakers={"speaker-a", "speaker-b"},
        segments_by_item=[
            [_segment("speaker-a", 0, 500), _segment("speaker-b", 250, 750)],
            [],
        ],
        itn_items=1,
        buckets=[{}, {}],
        langs={"zh"},
        dataset_type="alimeeting",
    )

    assert capabilities["itn_ratio"] == 0.5
    assert capabilities["segment_ratio"] == 0.5
    assert capabilities["speaker_ratio"] == 0.5
    assert capabilities["speaker_count"] == 2
    assert capabilities["overlap_ratio"] == 0.25


def test_overlap_uses_unique_speaker_interval_union() -> None:
    """同一说话人的重复标注不误算为多人重叠，端点相接不形成重叠。"""
    overlap, speaker_time = _speech_overlap_durations(
        [[_segment("speaker-a", 0, 500), _segment("speaker-a", 250, 500)]]
    )
    assert overlap == 0
    assert speaker_time == 750

    overlap, speaker_time = _speech_overlap_durations(
        [[_segment("speaker-a", 0, 250), _segment("speaker-b", 250, 500)]]
    )
    assert overlap == 0
    assert speaker_time == 500


def test_apy_and_magicdata_never_claim_punctuation_support() -> None:
    """按 M1 §7.1，APY 与 MagicData 标点能力被数据集规则强制关闭。"""
    apy = _compute_capabilities(
        durations=[1000],
        refs=["你好。"],
        speakers=set(),
        segments_by_item=[[]],
        itn_items=0,
        buckets=[{}],
        langs={"yue"},
        dataset_type="apy",
    )
    assert apy["punct_sentence_ratio"] == 1.0
    assert apy["punct_supported"] is False

    aggregate = _aggregate_capabilities({"phone": apy}, dataset_type="apy")
    assert aggregate["punct_supported"] is False
