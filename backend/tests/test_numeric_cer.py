"""数字相关 CER 近似读数单测（M5 §5.4）。"""

from app.scoring.numeric import numeric_cer, numeric_reference_length


def test_numeric_cer_counts_only_edits_aligned_to_numeric_characters() -> None:
    """普通文字的替换不应进入数字相关错误率。"""
    result = numeric_cer("2024年会议正常", "2025年会议异常")

    assert result.value == 0.25
    assert (result.s, result.d, result.i, result.n) == (1, 0, 0, 4)


def test_numeric_cer_counts_insertions_and_deletions_near_numeric_spans() -> None:
    """数字插入和删除需参与误差分子，分母仍来自参考数字字符。"""
    inserted = numeric_cer("2024年", "20240年")
    deleted = numeric_cer("2024年", "204年")

    assert inserted.value == 0.25
    assert inserted.i == 1
    assert deleted.value == 0.25
    assert deleted.d == 1


def test_numeric_cer_marks_written_and_spoken_number_form_difference() -> None:
    """该近似读数保留 CER 的数词格式差异，不能替代有金标的 ITN 准确率。"""
    result = numeric_cer("二零一四年", "2014年")

    assert result.value == 1.0
    assert result.edits == 4


def test_numeric_cer_uses_profile_normalization_and_handles_no_numeric_reference() -> None:
    """标签剥离与繁简转换应遵循 CER profile，缺数字参考记为不可评。"""
    normalized = numeric_cer(
        "２０１４[NOISE]年",
        "2014年",
        drop_tags=("[NOISE]",),
    )
    missing = numeric_cer("会议顺利结束", "会议顺利结束")

    assert normalized.value == 0.0
    assert normalized.n == 4
    assert numeric_reference_length("２０２４年") == 4
    assert missing.value is None
    assert missing.n == 0
