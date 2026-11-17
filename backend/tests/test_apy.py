"""APY phone/mic 配对导入器的单元测试。"""

import wave
from pathlib import Path

from app.domain.datasets import select_importer
from app.domain.models import Dataset
from app.importers.apy import ApyImporter


def test_scan_pairs_phone_and_mic_subsets(tmp_path: Path) -> None:
    session = tmp_path / "data" / "category" / "2" / "001_0001"
    session.mkdir(parents=True)
    audio = session / "ast_phone.wav"
    with wave.open(str(audio), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 1600)
    mic_audio = session / "ast_mic.wav"
    with wave.open(str(mic_audio), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(b"\x00\x00\x00\x00" * 44100)
    (session / "ast_phone.txt").write_text(
        "0.0\t1.0\tO1\t你好。\n1.0\t2.0\tO2\t再见。\n", encoding="utf-8"
    )
    (session / "ast_phone.metadata").write_text(
        "SES\t001_0001\nACT\tCantonese\nACC\tGuangZhou;GuangZhou\n"
        "SCD\tO1,O2\nSEX\tO1_Male,O2_Female\nAGE\t40;45\nSAM\t16000\nNCH\t1\n",
        encoding="utf-8",
    )
    (session / "ast_mic.txt").write_text(
        "0.0\t1.0\tO1\t你好。\n1.0\t2.0\tO2\t再见。\n", encoding="utf-8"
    )
    (session / "ast_mic.metadata").write_text(
        "SES\t001_0001\nACT\tCantonese\nACC\tGuangZhou;GuangZhou\n"
        "SCD\tO1,O2\nSEX\tO1_Male,O2_Female\nAGE\t40;45\nSAM\t44100\nNCH\t2\n",
        encoding="utf-8",
    )

    importer = ApyImporter()
    assert importer.detect(tmp_path)
    items = list(importer.scan(tmp_path, limit=10))
    assert len(items) == 2
    phone, mic = items
    assert [item.subset for item in items] == ["phone", "mic"]
    assert phone.item_key != mic.item_key
    assert phone.pair_key == mic.pair_key == "001_0001/ast"
    assert phone.audio_profile == "as_is"
    assert mic.audio_profile == "mono_mix"
    assert phone.ref_text == mic.ref_text == "你好。再见。"
    assert phone.buckets["language"] == "yue"
    assert phone.buckets["dialect"] == "粤语"
    assert phone.lang == "yue"
    assert len(phone.segments) == 2
    assert phone.segments[1]["start_ms"] == 1000
    assert phone.speakers["O1"]["gender"] == "M"
    assert phone.speakers["O2"]["gender"] == "F"


def test_generic_lhd_filename_uses_metadata_mit_as_phone(tmp_path: Path) -> None:
    """APY 通用文件名（无 _phone 后缀）按 metadata MIT 识别为 phone。"""
    session = tmp_path / "APY200501001" / "data" / "2_154"
    session.mkdir(parents=True)
    audio = session / "2_154_001.wav"
    with wave.open(str(audio), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 1600)
    (session / "2_154_001.txt").write_text("0.0\t1.0\tO1\t你好。\n", encoding="utf-8")
    (session / "2_154_001.metadata").write_text(
        "SES\t2_154\nACT\tsichuan\nACC\tchongqing;chongqing\n"
        "SCD\tO1\nSEX\tO1_Female\nAGE\t21\nSAM\t16000\nNCH\t1\nMIT\tandroid\n",
        encoding="utf-8",
    )

    importer = ApyImporter()
    assert importer.detect(tmp_path)
    items = list(importer.scan(tmp_path, limit=10))

    assert len(items) == 1
    assert items[0].subset == "phone"
    assert items[0].item_key == "2_154/2_154_001/phone"
    assert items[0].audio_profile == "as_is"
    assert items[0].buckets["language"] == "sichuan"
    assert items[0].speakers["O1"]["gender"] == "F"


def test_datatang_source_type_uses_lhd_importer(tmp_path: Path) -> None:
    """M1 §6：DataTang LHD 使用与 APY 相同的 phone/mic 导入契约。"""
    audio_dir = tmp_path / "data" / "category" / "2" / "session"
    audio_dir.mkdir(parents=True)
    (audio_dir / "sample_phone.wav").touch()

    importer = select_importer(Dataset(name="DataTang", source_type="datatang"), tmp_path)

    assert isinstance(importer, ApyImporter)


def test_minnan_package_path_sets_dialect_even_when_act_is_location(tmp_path: Path) -> None:
    """APY 闽语包的 ACT 是地点；应从包目录名推断 minnan/闽南语。"""
    session = tmp_path / "APY210930001-闽语" / "APY210930001" / "data" / "category" / "G00005"
    session.mkdir(parents=True)
    audio = session / "foo.wav"
    with wave.open(str(audio), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 1600)
    (session / "foo.txt").write_text("0.0\t1.0\tO1\t汝好。\n", encoding="utf-8")
    (session / "foo.metadata").write_text(
        "SES\tG00005/foo\nACT\tO1_漳州龙文区,O2_漳州芗城区\n"
        "ACC\tO1_漳州龙文区,O2_漳州芗城区\n"
        "SCD\tO1,O2\nSEX\tO1_Male,O2_Male\nAGE\t30;31\n"
        "SAM\t16000\nNCH\t1\nMIT\tandroid\n",
        encoding="utf-8",
    )

    items = list(ApyImporter().scan(tmp_path / "APY210930001-闽语", limit=10))

    assert len(items) == 1
    assert items[0].buckets["language"] == "minnan"
    assert items[0].buckets["dialect"] == "闽南语"
    assert items[0].lang == "minnan"
