"""CER 口径的单元测试。"""

import pytest

from app.scoring.cer import cer, normalize_for_cer


def test_normalize_removes_punctuation_and_space() -> None:
    assert normalize_for_cer("甚至出现，交易 几乎停滞。") == "甚至出现交易几乎停滞"


def test_profile_controls_traditional_conversion_and_fillers() -> None:
    assert (
        normalize_for_cer(
            "繁體，嗯 中文",
            traditional="to_simplified",
            drop_fillers=("嗯",),
        )
        == "繁体中文"
    )


def test_normalize_strips_sc_separator() -> None:
    # AliMeeting FIFO 的 <sc> 只用于排序，默认剥离（M5 §5.2）
    assert normalize_for_cer("第一句。<sc>第二句") == "第一句第二句"
    assert normalize_for_cer("第一句。<sc>第二句", strip_sc=False) == "第一句<sc>第二句"


def test_identical_text_is_zero() -> None:
    result = cer("甚至出现交易几乎停滞的情况", "甚至出现交易几乎停滞的情况")
    assert result.value == 0
    assert result.edits == 0
    assert result.n == 13


def test_one_substitution() -> None:
    result = cer("甚至出现交易几乎停滞的情况", "甚至出现交易几乎停止的情况")
    assert result.s == 1
    assert result.d == 0
    assert result.i == 0
    assert result.edits == 1
    assert result.value == pytest.approx(1 / 13)
    assert result.n == 13


def test_insertion_and_deletion_decomposed() -> None:
    result = cer("天气好", "天气很好")
    assert result.i == 1
    assert result.d == 0
    assert result.s == 0

    result2 = cer("天气很好", "天气好")
    assert result2.d == 1


def test_punctuation_does_not_count() -> None:
    assert cer("甚至出现交易几乎停滞的情况", "甚至出现交易，几乎停滞的情况。").value == 0


def test_profile_text_options_control_cer_normalization() -> None:
    result = cer(
        "A [NOISE] B",
        "a b",
        strip_punct=False,
        strip_space=False,
        lowercase=False,
        drop_tags=("[NOISE]",),
    )
    assert result.value > 0
    assert normalize_for_cer("A [NOISE] B", drop_tags=("[NOISE]",)) == "ab"


def test_empty_reference_raises() -> None:
    with pytest.raises(ValueError):
        cer("，。", "任何")
