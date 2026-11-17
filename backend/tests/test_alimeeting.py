"""AliMeeting near 导入器的单元测试。"""

import wave
from pathlib import Path

from app.importers.alimeeting import AlimeetingImporter, _segments_from_tiers
from app.importers.textgrid import Interval, Tier

GRID = """File type = "ooTextFile"
Object class = "TextGrid"
xmin = 0
xmax = 10
tiers? <exists>
size = 1
item []:
\titem [1]:
\t\tclass = "IntervalTier"
\t\tname = "c1"
\t\txmin = 0
\t\txmax = 10
\t\tintervals: size = 2
\t\tintervals [1]:
\t\t\txmin = 0.5
\t\t\txmax = 1.5
\t\t\ttext = "你好。"
\t\tintervals [2]:
\t\t\txmin = 2.0
\t\t\txmax = 3.0
\t\t\ttext = "再见。"
"""


def test_scan_near(tmp_path: Path) -> None:
    near = tmp_path / "Test_Ali_near"
    (near / "audio_dir").mkdir(parents=True)
    (near / "textgrid_dir").mkdir()
    audio = near / "audio_dir" / "R8002_M8002_N_SPK8005.wav"
    with wave.open(str(audio), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 1600)
    (near / "textgrid_dir" / "R8002_M8002_N_SPK8005.TextGrid").write_text(GRID, encoding="utf-8")

    importer = AlimeetingImporter()
    assert importer.detect(tmp_path)
    items = list(importer.scan(tmp_path, limit=10))
    assert len(items) == 1
    assert items[0].ref_text == "你好。再见。"
    assert items[0].speaker == "SPK8005"
    assert items[0].subset == "near"


def test_scan_far_keeps_source_and_uses_lazy_channel_profile(tmp_path: Path) -> None:
    far = tmp_path / "Test_Ali_far"
    (far / "audio_dir").mkdir(parents=True)
    (far / "textgrid_dir").mkdir()
    audio = far / "audio_dir" / "R8002_M8002_MS802.wav"
    with wave.open(str(audio), "wb") as handle:
        handle.setnchannels(8)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 1600 * 8)
    (far / "textgrid_dir" / "R8002_M8002.TextGrid").write_text(
        GRID.replace('name = "c1"', 'name = "S1"'), encoding="utf-8"
    )

    item = next(AlimeetingImporter().scan(tmp_path, limit=10))

    assert item.audio == audio
    assert item.audio_profile == "mono_ch0"
    assert item.speakers == {"S1": {}}
    assert item.segments[0]["speaker"] == "S1"


def test_far_segments_mark_cross_speaker_overlap() -> None:
    tiers = [
        Tier("S1", [Interval(0.0, 2.0, "你好")]),
        Tier("S2", [Interval(1.0, 3.0, "早上好")]),
    ]

    segments = _segments_from_tiers(tiers, default_speaker="unknown")

    assert [segment["overlap"] for segment in segments] == [True, True]
