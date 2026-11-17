"""CER 黄金样例回归：固定 P0 人工抽查集，防止归一化或对齐口径漂移。"""

import csv
from pathlib import Path

import pytest

from app.scoring.cer import cer, normalize_for_cer

GOLDEN_CSV = Path(__file__).resolve().parents[3] / "evidence" / "p0" / "spotcheck-20.csv"
EXPECTED_MICRO_CER = 0.008  # 2 edits / 250 参考字符
EXPECTED_MACRO_CER = 0.007692307692307693


def _rows() -> list[dict[str, str]]:
    """读取 20 条人工抽查记录；文件缺失时直接失败，不能静默跳过。"""
    assert GOLDEN_CSV.is_file(), f"黄金样例不存在：{GOLDEN_CSV}"
    with GOLDEN_CSV.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_golden_cases_match_expected_cer() -> None:
    """逐条验证 CER 与 P0 抽查记录一致，包括真实错误样本。"""
    rows = _rows()
    assert len(rows) == 20

    for row in rows:
        result = cer(row["ref"], row["hyp"])
        assert result.value == pytest.approx(float(row["cer"]), abs=1e-12), row["item_key"]
        assert result.n == len(normalize_for_cer(row["ref"])), row["item_key"]


def test_golden_aggregate_matches_micro_and_macro() -> None:
    """micro 按总编辑数/总参考字符、macro 按样本平均，二者都必须稳定。"""
    rows = _rows()
    results = [cer(row["ref"], row["hyp"]) for row in rows]
    edits = sum(result.edits for result in results)
    reference_chars = sum(result.n for result in results)
    micro = edits / reference_chars
    macro = sum(result.value for result in results) / len(results)

    assert (edits, reference_chars) == (2, 250)
    assert micro == pytest.approx(EXPECTED_MICRO_CER, abs=1e-12)
    assert macro == pytest.approx(EXPECTED_MACRO_CER, abs=1e-12)


def test_golden_normalization_ignores_punctuation_and_spacing() -> None:
    """中文标点与空格默认不计入 CER，避免把格式差异当成识别错误。"""
    result = cer("一、二线城市，虽然也处于调整中。", "一二线城市虽然也处于调整中")

    assert result.value == 0.0
    assert result.n == 13
