"""客家话 SRT/TXT 导入器测试（M1 §6、§8）。"""

from pathlib import Path

from app.importers.srt import SrtImporter, _parse_subtitle


def test_txt_with_srt_timecodes_keeps_timing_and_decodes_entities() -> None:
    """实际客家话素材的 .txt 使用 SRT 时码，按内容识别时间轴。"""
    text, segments = _parse_subtitle(
        "1\n00:00:00,866 --> 00:00:04,600\n<font>你好 &amp; 再见</font>\n",
        ".txt",
    )

    assert text == "你好 & 再见"
    assert segments == [
        {
            "speaker": "unknown",
            "start_ms": 866,
            "end_ms": 4600,
            "text": "你好 & 再见",
        }
    ]


def test_plain_txt_is_preserved_without_inventing_timestamps() -> None:
    text, segments = _parse_subtitle("第一行\n第二行\n", ".txt")

    assert text == "第一行\n第二行"
    assert segments == []


def test_scan_pairs_files_reports_missing_media_and_prefers_srt(tmp_path: Path) -> None:
    root = tmp_path / "素材"
    root.mkdir()
    (root / "有音频.MP4").touch()
    (root / "有音频.txt").write_text("纯文本版本", encoding="utf-8")
    (root / "有音频.SRT").write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nSRT版本\n", encoding="utf-8"
    )
    (root / "缺音频.txt").write_text("只有字幕", encoding="utf-8")

    importer = SrtImporter()
    items = list(importer.scan(root, limit=10))

    assert importer.detect(root)
    assert [item.item_key for item in items] == ["有音频", "缺音频"]
    assert items[0].ref_text == "SRT版本"
    assert items[0].segments[0]["start_ms"] == 1000
    assert items[1].issues[0].code == "E_AUDIO_MISSING"


def test_scan_limit_counts_items_with_pairing_issues(tmp_path: Path) -> None:
    root = tmp_path / "素材"
    root.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")
    (root / "b.mp4").touch()
    (root / "b.txt").write_text("b", encoding="utf-8")

    items = list(SrtImporter().scan(root, limit=1))

    assert len(items) == 1
    assert items[0].item_key == "a"
    assert {issue.code for issue in items[0].issues} == {"E_AUDIO_MISSING"}
