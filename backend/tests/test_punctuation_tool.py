"""205 标点基准工具的句段选择与 micro 汇总测试。"""

import sys
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(TOOLS_DIR))

from bench_punctuation import _load_cases, _summarize  # noqa: E402

TEXTGRID = """File type = "ooTextFile"
Object class = "TextGrid"

item []:
    item [1]:
        class = "IntervalTier"
        name = "c1"
        intervals: size = 3
        intervals [1]:
            xmin = 1.0
            xmax = 2.0
            text = "你好。"
        intervals [2]:
            xmin = 2.0
            xmax = 3.0
            text = "没有标点"
        intervals [3]:
            xmin = 3.0
            xmax = 4.0
            text = "再见！"
"""


def test_load_cases_keeps_only_punctuated_intervals(tmp_path: Path) -> None:
    """只选带标点的非空区间，并保留源音频路径。"""
    near = tmp_path / "near"
    (near / "audio_dir").mkdir(parents=True)
    (near / "textgrid_dir").mkdir()
    audio = near / "audio_dir" / "R8002_M8002_N_SPK8005.wav"
    audio.write_bytes(b"wav")
    (near / "textgrid_dir" / "R8002_M8002_N_SPK8005.TextGrid").write_text(
        TEXTGRID, encoding="utf-8"
    )

    cases = _load_cases(near, 2)

    assert [case.reference for case in cases] == ["你好。", "再见！"]
    assert cases[0].audio == audio
    assert (cases[0].start_s, cases[0].end_s) == (1.0, 2.0)


def test_load_cases_fails_when_limit_is_not_met(tmp_path: Path) -> None:
    """句段不足时明确失败，避免把 99 条当成 100 条。"""
    near = tmp_path / "near"
    (near / "audio_dir").mkdir(parents=True)
    (near / "textgrid_dir").mkdir()
    (near / "audio_dir" / "meeting.wav").write_bytes(b"wav")
    (near / "textgrid_dir" / "meeting.TextGrid").write_text(TEXTGRID, encoding="utf-8")

    with pytest.raises(ValueError, match="少于要求"):
        _load_cases(near, 3)


def test_summary_uses_micro_punctuation_counts() -> None:
    """标点 F1 用 TP/FP/FN 汇总，CER 用总编辑数/总参考字符。"""
    cases = [
        {
            "ok": True,
            "edits": 1,
            "reference_chars": 10,
            "punct": {
                "tp": 2,
                "fp": 1,
                "fn": 1,
                "ref_punct": 3,
                "hyp_punct": 3,
                "end_total": 2,
                "end_correct": 1,
            },
        },
        {
            "ok": False,
            "error": "timeout",
        },
    ]

    summary = _summarize(cases)

    assert summary["samples"] == 2
    assert summary["successes"] == 1
    assert summary["cer"] == pytest.approx(0.1)
    assert summary["punct_f1"] == pytest.approx(2 / 3)
    assert summary["sentence_end_acc"] == pytest.approx(0.5)
    assert summary["naming"]["status"] == "na"
