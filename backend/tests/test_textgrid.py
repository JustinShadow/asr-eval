"""TextGrid 解析的单元测试（不依赖真实数据集）。"""

from pathlib import Path

from app.importers.textgrid import parse_textgrid

SAMPLE = """File type = "ooTextFile"
Object class = "TextGrid"

xmin = 0
xmax = 12.5
tiers? <exists>
size = 2
item []:
\titem [1]:
\t\tclass = "IntervalTier"
\t\tname = "N_SPK8005"
\t\txmin = 0
\t\txmax = 12.5
\t\tintervals: size = 2
\t\tintervals [1]:
\t\t\txmin = 0.5
\t\t\txmax = 2.0
\t\t\ttext = "你好。"
\t\tintervals [2]:
\t\t\txmin = 3.0
\t\t\txmax = 4.5
\t\t\ttext = "再见。"
\titem [2]:
\t\tclass = "IntervalTier"
\t\tname = "N_SPK8006"
\t\txmin = 0
\t\txmax = 12.5
\t\tintervals: size = 1
\t\tintervals [1]:
\t\t\txmin = 5.0
\t\t\txmax = 6.0
\t\t\ttext = ""
"""


def test_parse_two_tiers(tmp_path: Path) -> None:
    path = tmp_path / "sample.TextGrid"
    path.write_text(SAMPLE, encoding="utf-8")
    tiers = parse_textgrid(path)
    assert [tier.name for tier in tiers] == ["N_SPK8005", "N_SPK8006"]
    assert len(tiers[0].intervals) == 2
    assert tiers[0].intervals[0].text == "你好。"
    assert tiers[0].intervals[1].start_s == 3.0
    assert tiers[1].intervals[0].text == ""


def test_read_text_encoding_fallback(tmp_path) -> None:
    """read_text：UTF-8 失败回退 GB18030；两者都失败 → E_ENCODING（M1 §8）。"""
    import pytest

    from app.importers.base import EncodingError, read_text

    utf8 = tmp_path / "utf8.txt"
    utf8.write_text("你好世界", encoding="utf-8")
    assert read_text(utf8) == "你好世界"

    gb = tmp_path / "gb.txt"
    gb.write_bytes("你好世界".encode("gb18030"))
    assert read_text(gb) == "你好世界"

    bad = tmp_path / "bad.bin"
    bad.write_bytes(b"\xff\xfe\xfa\x81")
    with pytest.raises(EncodingError):
        read_text(bad)
