"""分段级对齐归因的单元测试（M5 §5 / M8 §9.7.1）。"""

from app.scoring.segments import annotate_segments, segment_bucket_summary


def test_errors_attributed_to_containing_segment() -> None:
    segments = [
        {"speaker": "A", "start_ms": 0, "end_ms": 2000, "text": "你好世界"},
        {"speaker": "B", "start_ms": 2000, "end_ms": 4000, "text": "今天天气好"},
    ]
    # 第二段第 1 个字符被替换
    alignment = [
        {"type": "eq", "ref": "你", "hyp": "你"},
        {"type": "eq", "ref": "好", "hyp": "好"},
        {"type": "eq", "ref": "世", "hyp": "世"},
        {"type": "eq", "ref": "界", "hyp": "界"},
        {"type": "sub", "ref": "今", "hyp": "金"},
        {"type": "eq", "ref": "天", "hyp": "天"},
        {"type": "eq", "ref": "天", "hyp": "天"},
        {"type": "eq", "ref": "气", "hyp": "气"},
        {"type": "eq", "ref": "好", "hyp": "好"},
    ]
    annotated = annotate_segments(segments, alignment)
    assert annotated[0]["errors"] == 0
    assert annotated[0]["hyp"] == "你好世界"
    assert annotated[1]["errors"] == 1
    assert annotated[1]["hyp"] == "金天天气好"
    assert annotated[1]["cer"] == 0.2


def test_insertion_attributed_to_current_segment() -> None:
    segments = [
        {"speaker": "A", "start_ms": 0, "end_ms": 1000, "text": "你好"},
        {"speaker": "A", "start_ms": 1000, "end_ms": 2000, "text": "再见"},
    ]
    alignment = [
        {"type": "eq", "ref": "你", "hyp": "你"},
        {"type": "eq", "ref": "好", "hyp": "好"},
        {"type": "ins", "hyp": "嗯"},
        {"type": "eq", "ref": "再", "hyp": "再"},
        {"type": "eq", "ref": "见", "hyp": "见"},
    ]
    annotated = annotate_segments(segments, alignment)
    # 插入落在第二段开头（参考位置已跨过第一段）
    assert annotated[0]["errors"] == 0
    assert annotated[1]["errors"] == 1
    assert annotated[1]["hyp"].startswith("嗯")


def test_deletion_counts_and_clamp() -> None:
    segments = [{"speaker": "A", "start_ms": 0, "end_ms": 1000, "text": "你好"}]
    alignment = [
        {"type": "eq", "ref": "你", "hyp": "你"},
        {"type": "del", "ref": "好"},
    ]
    annotated = annotate_segments(segments, alignment)
    assert annotated[0]["errors"] == 1
    assert annotated[0]["hyp"] == "你"


def test_minute_buckets() -> None:
    segments = [
        {"speaker": "A", "start_ms": 0, "end_ms": 1000, "text": "a", "errors": 1},
        {"speaker": "A", "start_ms": 61_000, "end_ms": 62_000, "text": "b", "errors": 2},
        {"speaker": "A", "start_ms": 62_000, "end_ms": 63_000, "text": "c", "errors": 3},
    ]
    buckets = segment_bucket_summary(segments)
    assert buckets[0]["minute"] == 0 and buckets[0]["errors"] == 1
    assert buckets[1]["minute"] == 1 and buckets[1]["errors"] == 5
