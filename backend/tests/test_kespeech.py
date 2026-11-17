"""KeSpeech 导入器的单元测试。"""

import wave
from pathlib import Path

from app.importers.kespeech import KespeechImporter


def _write_wav(path: Path, seconds: float = 0.1) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * int(16000 * seconds))


def test_scan_pairs_and_dialect(tmp_path: Path) -> None:
    dialect = tmp_path / "Beijing"
    dialect.mkdir()
    _write_wav(dialect / "1000234_abc.wav")
    (dialect / "1000234_abc.txt").write_text("今天天气不错", encoding="utf-8")

    importer = KespeechImporter()
    assert importer.detect(tmp_path)
    items = list(importer.scan(tmp_path, limit=10))
    assert len(items) == 1
    assert items[0].item_key == "1000234_abc"
    assert items[0].speaker == "1000234"
    assert items[0].buckets["dialect"] == "Beijing"
    # 时长由服务层 fingerprint_wav 填充，scan 只负责路径与文本
    assert items[0].duration_ms == 0


def test_scan_skips_text_without_audio(tmp_path: Path) -> None:
    dialect = tmp_path / "Ji-Lu"
    dialect.mkdir()
    (dialect / "a.txt").write_text("没有音频", encoding="utf-8")
    assert not KespeechImporter().detect(tmp_path)
