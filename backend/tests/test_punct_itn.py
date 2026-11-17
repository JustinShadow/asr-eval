"""标点 F1 与 ITN 准确率的单元测试（M5 §5.3/§5.4）。"""

from app.scoring.itn import aggregate as itn_aggregate
from app.scoring.itn import extract_entities, score_itn
from app.scoring.punct import aggregate as punct_aggregate
from app.scoring.punct import score_punctuation


def test_punctuation_perfect() -> None:
    result = score_punctuation("你好，世界。", "你好，世界。")
    assert result.tp == 2 and result.fp == 0 and result.fn == 0
    assert result.end_total == 1 and result.end_correct == 1
    summary = punct_aggregate([result])
    assert summary["f1"] == 1.0
    assert summary["sentence_end_acc"] == 1.0


def test_missing_final_punctuation_is_fn() -> None:
    result = score_punctuation("你好，世界。", "你好，世界")
    assert result.fn == 1
    assert result.tp == 1
    assert result.end_total == 1 and result.end_correct == 0


def test_extra_punctuation_is_fp() -> None:
    result = score_punctuation("你好世界。", "你好，世界。")
    assert result.fp == 1
    assert result.tp == 1


def test_punctuation_inserted_when_reference_has_none_is_false_positive() -> None:
    result = score_punctuation("你好世界", "你好，世界")
    assert result.fp == 1
    assert result.ref_punct == 0 and result.hyp_punct == 1


def test_wrong_type_counts_fp_and_fn() -> None:
    result = score_punctuation("你好，世界。", "你好。世界。")
    assert result.tp == 1  # 句末 。
    assert result.fp == 1  # 假设的逗号位置对但类型错
    assert result.fn == 1


def test_insertion_shift_keeps_alignment() -> None:
    """假设多一个字时，标点位置应随对齐映射，不误判。"""
    result = score_punctuation("你好，世界。", "你好啊，世界。")
    assert result.tp == 2 and result.fp == 0 and result.fn == 0


def test_english_sentence_end() -> None:
    result = score_punctuation("hello, world.", "hello world.")
    assert result.tp == 1  # 句号
    assert result.fn == 1  # 逗号缺失
    assert result.end_total == 1 and result.end_correct == 1


def test_numeric_decimal_and_thousands_separators_are_not_punctuation() -> None:
    result = score_punctuation("增长3.5%，数量1,000。", "增长3.5%，数量1,000。")
    assert result.tp == 2  # 逗号与句末句号
    assert result.fp == 0 and result.fn == 0


def test_punctuation_alignment_ignores_spaces_and_drops_profile_tags() -> None:
    result = score_punctuation(
        "hello , [NOISE] world .",
        "HELLO, world.",
        drop_tags=("[NOISE]",),
    )
    assert result.tp == 2
    assert result.fp == result.fn == 0


def test_punctuation_applies_profile_script_and_filler_options() -> None:
    result = score_punctuation(
        "繁體，嗯中文。",
        "繁体，中文。",
        traditional="to_simplified",
        drop_fillers=("嗯",),
    )
    assert result.tp == 2
    assert result.fp == result.fn == 0


def test_no_reference_punctuation_is_na() -> None:
    summary = punct_aggregate([score_punctuation("你好世界", "你好世界")])
    assert summary["f1"] is None


def test_itn_entities_match() -> None:
    result = score_itn("2014年3月5日开会，花了100元，增长3.5%", "2014年3月5日开会花了100元增长3.5%")
    assert result.total >= 3
    assert result.correct == result.total


def test_itn_normalizes_chinese_verbal_forms_before_comparison() -> None:
    result = score_itn(
        "2014年3月5日，3.5%，100元，5000g",
        "二零一四年三月五日，百分之三点五，一百元，五千克",
    )
    assert result.total == 4
    assert result.correct == 4
    assert result.by_type == {
        "date": (1, 1),
        "percent": (1, 1),
        "money": (1, 1),
        "unit": (1, 1),
    }


def test_itn_fullwidth_normalized() -> None:
    # 全角百分号/冒号经 NFKC 归一后应命中
    result = score_itn("增长３．５％", "增长3.5%")
    assert result.total == 1 and result.correct == 1


def test_itn_drops_tags_from_the_selected_profile() -> None:
    result = score_itn("2014[NOISE]年", "2014年", drop_tags=("[NOISE]",))
    assert result.total == 1 and result.correct == 1


def test_itn_applies_profile_script_and_filler_options() -> None:
    result = score_itn(
        "繁體嗯3.5%",
        "繁体3.5%",
        traditional="to_simplified",
        drop_fillers=("嗯",),
    )
    assert result.total == 1 and result.correct == 1


def test_itn_missing_entity() -> None:
    result = score_itn("2014年3月5日", "2014年")
    assert result.correct < result.total


def test_itn_no_entities_is_na() -> None:
    result = itn_aggregate([score_itn("今天天气不错", "今天天气不错")])
    assert result["value"] is None


def test_itn_extract_types() -> None:
    kinds = {kind for kind, _value in extract_entities("2024-01-02 12:30 花了￥3000 增长5%")}
    assert {"date", "time", "money", "percent"} <= kinds


def test_itn_aggregate_breaks_down_accuracy_by_entity_type() -> None:
    summary = itn_aggregate([score_itn("2024-01-02 增长5%", "2024-01-02 增长6%")])
    by_type = summary["by_type"]
    assert isinstance(by_type, dict)
    assert by_type["date"] == {"correct": 1, "total": 1, "accuracy": 1.0}
    assert by_type["percent"] == {"correct": 0, "total": 1, "accuracy": 0.0}
    assert summary["n"] == 2
