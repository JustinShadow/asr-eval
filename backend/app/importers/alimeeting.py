"""AliMeeting 导入器（near + far）。

目录：
    Test_Ali_near/audio_dir/R8002_M8002_N_SPK8005.wav
    Test_Ali_near/textgrid_dir/R8002_M8002_N_SPK8005.TextGrid
    Test_Ali_far/audio_dir/R8002_M8002_MS802.wav          # 8 声道
    Test_Ali_far/textgrid_dir/R8002_M8002.TextGrid        # 多说话人 tier

near 是单人耳机麦（单 tier）；far 保留 8 声道原始资产，Runner/音频接口按
audio_profile=mono_ch0 懒取 ch0 并缓存变体，不修改源目录。
far 的 TextGrid 每个 tier 一个说话人（N_SPKxxxx），用于 Badcase 泳道与 DER。
"""

from collections.abc import Iterator
from pathlib import Path

from app.importers.base import RawItem, Segment, SpeakerInfo
from app.importers.textgrid import Tier, parse_textgrid


class AlimeetingImporter:
    """扫描 near 与 far；far 保留原始 8 声道，运行时按 profile 懒取 ch0。"""

    name = "alimeeting"
    version = "3"

    def detect(self, root: Path) -> bool:
        return _near_dir(root) is not None or _far_dir(root) is not None

    def scan(self, root: Path, limit: int) -> Iterator[RawItem]:
        produced = 0
        near = _near_dir(root)
        if near is not None:
            for raw in _scan_near(near):
                if produced >= limit:
                    return
                produced += 1
                yield raw
        far = _far_dir(root)
        if far is not None:
            for raw in _scan_far(far):
                if produced >= limit:
                    return
                produced += 1
                yield raw


def _scan_near(near: Path) -> Iterator[RawItem]:
    audio_dir = near / "audio_dir"
    grid_dir = near / "textgrid_dir"
    for audio in sorted(audio_dir.glob("*_N_SPK*.wav")):
        grid = grid_dir / f"{audio.stem}.TextGrid"
        if not grid.is_file():
            continue
        tiers = parse_textgrid(grid)
        segments = _segments_from_tiers(tiers, default_speaker=_near_speaker(audio))
        text = "".join(segment["text"] for segment in segments)
        if not text:
            continue
        speaker = _near_speaker(audio)
        yield RawItem(
            item_key=audio.stem,
            audio=audio,
            ref_text=text,
            split="test",
            speaker=speaker,
            duration_ms=0,
            sample_rate=0,
            channels=0,
            subset="near",
            audio_profile="as_is",
            segments=segments,
            speakers={_near_speaker(audio): SpeakerInfo()},
            pair_key=_session_id(audio.stem),
            buckets={"scene": "near", "speaker": speaker, "language": "zh"},
        )


def _scan_far(far: Path) -> Iterator[RawItem]:
    audio_dir = far / "audio_dir"
    grid_dir = far / "textgrid_dir"
    for audio in sorted(audio_dir.glob("*_MS*.wav")):
        session = _session_id(audio.stem)
        grid = grid_dir / f"{session}.TextGrid"
        if not grid.is_file():
            continue
        tiers = parse_textgrid(grid)
        segments = _segments_from_tiers(tiers, default_speaker="unknown")
        text = "".join(segment["text"] for segment in segments)
        if not text:
            continue
        speakers = sorted(
            {
                str(segment["speaker"])
                for segment in segments
                if segment["speaker"].casefold() != "unknown"
            }
        )
        yield RawItem(
            item_key=audio.stem,
            audio=audio,
            ref_text=text,
            split="test",
            speaker=speakers[0] if speakers else "unknown",
            duration_ms=0,
            sample_rate=0,
            channels=0,
            subset="far",
            audio_profile="mono_ch0",
            segments=segments,
            speakers={speaker_id: SpeakerInfo() for speaker_id in speakers},
            pair_key=session,
            buckets={
                "scene": "far",
                "language": "zh",
                "channel": "ch0",
                "n_speakers": len(speakers),
            },
        )


def _segments_from_tiers(
    tiers: list[Tier],
    *,
    default_speaker: str,
) -> list[Segment]:
    """把 tier 区间展平为按时间排序的分段；说话人取 tier 名（near 用文件名）。"""
    segments: list[Segment] = []
    for tier in tiers:
        speaker = (
            default_speaker
            if len(tiers) == 1 and default_speaker != "unknown"
            else (tier.name or default_speaker)
        )
        for interval in tier.intervals:
            if not interval.text.strip():
                continue
            segments.append(
                Segment(
                    speaker=speaker,
                    start_ms=round(interval.start_s * 1000),
                    end_ms=round(interval.end_s * 1000),
                    text=interval.text.strip(),
                )
            )
    segments.sort(key=lambda segment: (segment["start_ms"], segment["speaker"]))
    # 不同说话人的时间区间相交时标注重叠；DER 仍保留该区间，M1 §7.1。
    active: list[Segment] = []
    for segment in segments:
        active = [previous for previous in active if previous["end_ms"] > segment["start_ms"]]
        for previous in active:
            if previous["speaker"] != segment["speaker"]:
                previous["overlap"] = True
                segment["overlap"] = True
        active.append(segment)
    return segments


def _near_speaker(audio: Path) -> str:
    return audio.stem.split("_N_")[-1] if "_N_" in audio.stem else "unknown"


def _session_id(stem: str) -> str:
    """R8002_M8002_N_SPK8005 → R8002_M8002；R8002_M8002_MS802 → R8002_M8002。"""
    parts = stem.split("_")
    return "_".join(parts[:2]) if len(parts) >= 2 else stem


def _near_dir(root: Path) -> Path | None:
    """兼容传入 Test_Ali 或 Test_Ali_near。"""
    candidate = root / "Test_Ali_near"
    if candidate.is_dir():
        return candidate
    if (root / "audio_dir").is_dir() and any(root.glob("audio_dir/*_N_SPK*.wav")):
        return root
    return None


def _far_dir(root: Path) -> Path | None:
    candidate = root / "Test_Ali_far"
    if candidate.is_dir():
        return candidate
    if (root / "audio_dir").is_dir() and any(root.glob("audio_dir/*_MS*.wav")):
        return root
    return None
