"""MagicData 导入器的单元测试。"""

import wave
from pathlib import Path

from app.importers.magicdata import MagicDataImporter, infer_dialect


def test_scan_preserves_segments_and_annotation_markers(tmp_path: Path) -> None:
    root = tmp_path / "MagicData-湘语"
    (root / "TXT").mkdir(parents=True)
    (root / "WAV").mkdir()
    with wave.open(str(root / "WAV" / "s1.wav"), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 1600)
    (root / "TXT" / "s1.txt").write_text(
        "[0.0,1.0]\tG0001\tfemale\t你好。\n"
        "[1.0,2.0]\tG0000\tnone\t[ENS]\n"
        "[2.0,3.0]\tG0002\tmale\t再见。[+]\n",
        encoding="utf-8",
    )
    (root / "SPKINFO.txt").write_text(
        "CHANNEL\tSPEAKER_ID\tGENDER\tAGE\tREGION,CITY\tDEVICE\n"
        "C0\tG0001\tF\t32\tShanghai\tPhone\n",
        encoding="utf-8",
    )

    importer = MagicDataImporter()
    assert importer.detect(root)
    items = list(importer.scan(root, limit=10))
    assert len(items) == 1
    assert items[0].ref_text == "你好。再见。"
    assert items[0].buckets["language"] == "xiang"
    assert items[0].buckets["dialect"] == "湘语"
    assert items[0].lang == "xiang"
    assert items[0].segments[1]["skip"] is True
    assert items[0].segments[2]["overlap"] is True
    assert items[0].segments[2]["speaker"] == "G0002"
    assert items[0].speakers["G0001"]["age"] == "32"


def test_infer_dialect_normalizes_magicdata_names() -> None:
    """方言标签与语言代码统一，未知目录保留原名。"""
    assert infer_dialect("MagicData-沪语（吴语）") == ("wuu", "沪语")
    assert infer_dialect("MagicData-湘语") == ("xiang", "湘语")
    assert infer_dialect("MagicData-赣语") == ("gan", "赣语")
    assert infer_dialect("Other-Dataset") == ("zh", "Other-Dataset")
