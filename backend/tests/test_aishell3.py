"""AISHELL-3 导入器的单元测试。"""

import wave
from pathlib import Path

from app.importers.aishell3 import Aishell3Importer


def _wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(b"\x00\x00" * 44100)


def test_scan_uses_even_tokens(tmp_path: Path) -> None:
    root = tmp_path
    (root / "train").mkdir()
    _wav(root / "train" / "wav" / "SSB0005" / "SSB00050001.wav")
    (root / "train" / "content.txt").write_text(
        "SSB00050001.wav\t广 guang3 州 zhou1 天 tian1 气 qi4\n", encoding="utf-8"
    )
    (root / "spk-info.txt").write_text("SSB0005\tB\tfemale\tnorth\n", encoding="utf-8")

    importer = Aishell3Importer()
    assert importer.detect(root)
    items = list(importer.scan(root, limit=10))
    assert len(items) == 1
    assert items[0].ref_text == "广州天气"
    assert items[0].speaker == "SSB0005"
    assert items[0].audio_profile == "mono_mix"
    assert items[0].buckets["gender"] == "F"
    assert items[0].speakers["SSB0005"]["gender"] == "F"
    assert items[0].split == "train"
