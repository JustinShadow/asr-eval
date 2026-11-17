"""AISHELL-1 导入器的目录识别与下钻测试。"""

from pathlib import Path

from app.importers.aishell1 import Aishell1Importer


def _make_dataset(parent: Path) -> Path:
    root = parent / "aishell1" / "data_aishell"
    (root / "transcript").mkdir(parents=True)
    audio_dir = root / "wav" / "test" / "S0001"
    audio_dir.mkdir(parents=True)
    (root / "transcript" / "aishell_transcript_v0.8.txt").write_text(
        "BAC009S0001W0001 你 好 世 界\n", encoding="utf-8"
    )
    (audio_dir / "BAC009S0001W0001.wav").write_bytes(b"wav")
    return root


def test_detect_accepts_data_aishell_parent(tmp_path: Path) -> None:
    """选择 aishell1/ 时自动识别其中的 data_aishell。"""
    _make_dataset(tmp_path)
    importer = Aishell1Importer()

    assert importer.detect(tmp_path / "aishell1") is True
    assert importer.detect(tmp_path / "aishell1" / "data_aishell") is True


def test_scan_descends_into_data_aishell(tmp_path: Path) -> None:
    """预览/导入扫描同样下钻，并保留原始音频路径。"""
    root = _make_dataset(tmp_path)
    items = list(Aishell1Importer().scan(tmp_path / "aishell1", 1))

    assert len(items) == 1
    assert items[0].item_key == "BAC009S0001W0001"
    assert items[0].ref_text == "你好世界"
    assert items[0].split == "test"
    assert items[0].audio == root / "wav" / "test" / "S0001" / "BAC009S0001W0001.wav"


def test_detect_rejects_unrelated_directory(tmp_path: Path) -> None:
    """没有 transcript/wav 的目录仍应拒绝。"""
    (tmp_path / "other").mkdir()
    assert Aishell1Importer().detect(tmp_path / "other") is False
