"""分段级对齐与误差归因（M5 §5 / M8 §9.7.1）。

为什么需要：30 分钟会议转写上万字，整篇 diff 既不可读也无法定位；
把整篇字符对齐按参考分段切分后，Badcase 才能做"当前段/通读/错误榜"。

归因规则：
- 替换/删除按参考字符所在分段归属；
- 插入没有参考字符，归到"当前参考位置所在分段"（前一段末尾或后一段开头）；
- 分段 hyp 由该段的对齐操作拼回，保证 ref/hyp 两列可并排渲染。
"""

from typing import Any


def annotate_segments(
    segments: list[dict[str, Any]],
    alignment: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """把整篇对齐切分到参考分段；返回带 hyp/errors/cer/alignment 的分段。

    segments 必须按 ref_text 的拼接顺序给出（导入时按 start_ms 排序）。
    """
    if not segments:
        return []

    offsets: list[tuple[int, int]] = []
    position = 0
    for segment in segments:
        length = len(str(segment.get("text") or ""))
        offsets.append((position, position + length))
        position += length

    buckets: list[dict[str, Any]] = [{"ops": [], "errors": 0, "hyp": []} for _ in segments]
    ref_index = 0
    active = 0
    for op in alignment:
        kind = op.get("type")
        # 插入/替换/删除都按"当前参考位置"归属；插入时先把游标推进到
        # 该位置所在的分段（边界字符属于后一段，与切片语义一致）
        while active < len(offsets) - 1 and ref_index >= offsets[active][1]:
            active += 1
        if kind == "ins":
            target = active
        else:
            target = active
            ref_index += 1
        if target >= len(buckets):
            target = len(buckets) - 1
        bucket = buckets[target]
        bucket["ops"].append(op)
        if kind != "eq":
            bucket["errors"] += 1
        if op.get("hyp"):
            bucket["hyp"].append(op["hyp"])

    annotated: list[dict[str, Any]] = []
    for index, segment in enumerate(segments):
        ref_len = offsets[index][1] - offsets[index][0]
        errors = int(buckets[index]["errors"])
        annotated.append(
            {
                **segment,
                "hyp": "".join(buckets[index]["hyp"]),
                "errors": errors,
                "ref_len": ref_len,
                "cer": (errors / ref_len) if ref_len else None,
                "alignment": buckets[index]["ops"],
            }
        )
    return annotated


def segment_bucket_summary(annotated: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按段聚合错误（M5 §5.6 / Badcase 概览）。

    这里按"分钟"分桶不依赖 item duration_bin：长音频的目标是定位，
    分钟粒度比 5 分钟桶更有用（M8 §9.7.1 概览：每分钟错误密度）。
    """
    if not annotated:
        return []
    minutes: dict[int, dict[str, Any]] = {}
    for segment in annotated:
        minute = int(int(segment.get("start_ms") or 0) // 60_000)
        bucket = minutes.setdefault(minute, {"minute": minute, "errors": 0, "segments": 0})
        bucket["errors"] += int(segment.get("errors") or 0)
        bucket["segments"] += 1
    return [minutes[key] for key in sorted(minutes)]
