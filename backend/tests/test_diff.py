"""字符对齐的单元测试。"""

from app.scoring.diff import char_diff


def test_equal_text_all_eq() -> None:
    ops = char_diff("你好", "你好")
    assert [op["type"] for op in ops] == ["eq", "eq"]


def test_substitution_is_merged() -> None:
    ops = char_diff("十宗", "时松")
    assert [op["type"] for op in ops] == ["sub", "sub"]
    assert ops[0]["ref"] == "十"
    assert ops[0]["hyp"] == "时"


def test_deletion_and_insertion() -> None:
    ops = char_diff("天气好", "天气很好")
    assert any(op["type"] == "ins" for op in ops)
    assert "".join(op.get("ref", "") for op in ops) == "天气好"
    assert "".join(op.get("hyp", "") for op in ops) == "天气很好"
