"""说话人指标：DER / SA-DER / 命名 FA·FR / EER（M5 §5.5/§5.6）。

口径（AliMeeting 官方）：
- 帧长 10ms，UEM = [0, audio_duration]；
- collar 0.25s：参考分段边界的 ±250ms 不参与打分（md-eval/dscore 语义）；
- skip_overlap = False：重叠语音照常计分；
- DER 用匈牙利/最优映射（参考↔假设 speaker 一一对应）；
- SA-DER 禁用最优映射：假设 speaker 直接用 matched_id 比对，未匹配进独立 unknown 桶。

实现说明：
- 使用 10ms 帧保留参考与假设侧的重叠说话人集合；
- 最优映射按重叠时长最大化，SA-DER 直接使用声纹匹配结果；
- 假设侧没有分段/说话人时全记 miss，参考无分段返回 na。
"""

from collections.abc import Iterable, Sequence
from itertools import permutations
from typing import Any

FRAME_MS = 10
DEFAULT_COLLAR_MS = 250


Segment = tuple[str, int, int]  # (speaker, start_ms, end_ms)


def _frame_count(duration_ms: int) -> int:
    return max(0, (duration_ms + FRAME_MS - 1) // FRAME_MS)


def _active_frames(
    segments: Sequence[Segment], duration_ms: int, *, prefix: str = ""
) -> list[set[str]]:
    """把分段铺到 10ms 帧上，同帧所有活动说话人都保留下来。"""
    frames: list[set[str]] = [set() for _ in range(_frame_count(duration_ms))]
    for speaker, start, end in segments:
        first = max(0, start // FRAME_MS)
        last = min(len(frames), (end + FRAME_MS - 1) // FRAME_MS)
        for index in range(first, last):
            frames[index].add(prefix + speaker)
    return frames


def _collar_mask(ref_segments: Sequence[Segment], duration_ms: int, collar_ms: int) -> bytearray:
    """参考边界 ±collar_ms 的帧标记为忽略（md-eval -c 语义）。"""
    mask = bytearray(_frame_count(duration_ms))
    for _speaker, start, end in ref_segments:
        for boundary in (start, end):
            first = max(0, (boundary - collar_ms) // FRAME_MS)
            last = min(len(mask), (boundary + collar_ms + FRAME_MS - 1) // FRAME_MS)
            for index in range(first, last):
                mask[index] = 1
    return mask


def _overlap_matrix(
    ref_segments: Sequence[Segment],
    hyp_segments: Sequence[Segment],
    duration_ms: int,
    collar_ms: int,
) -> tuple[dict[tuple[str, str], int], int]:
    ref_frames = _active_frames(ref_segments, duration_ms)
    hyp_frames = _active_frames(hyp_segments, duration_ms)
    mask = _collar_mask(ref_segments, duration_ms, collar_ms)
    matrix: dict[tuple[str, str], int] = {}
    total = 0
    for index in range(len(ref_frames)):
        if mask[index]:
            continue
        reference_speakers = ref_frames[index]
        hypothesis_speakers = hyp_frames[index]
        total += len(reference_speakers)
        for ref_speaker in reference_speakers:
            for hyp_speaker in hypothesis_speakers:
                key = (ref_speaker, hyp_speaker)
                matrix[key] = matrix.get(key, 0) + 1
    return matrix, total


def _best_mapping(matrix: dict[tuple[str, str], int]) -> dict[str, str]:
    """最优 speaker 映射（相当于匈牙利）：穷举假设 speaker 的排列。"""
    ref_speakers = sorted({ref for ref, _hyp in matrix})
    hyp_speakers = sorted({hyp for _ref, hyp in matrix})
    best_score = -1
    best: dict[str, str] = {}
    # 只对较小一侧做排列，避免 8! 以上的组合
    if len(hyp_speakers) <= len(ref_speakers):
        for perm in permutations(ref_speakers, len(hyp_speakers)):
            mapping = dict(zip(hyp_speakers, perm, strict=True))
            score = sum(count for (ref, hyp), count in matrix.items() if mapping.get(hyp) == ref)
            if score > best_score:
                best_score, best = score, mapping
    else:
        for perm in permutations(hyp_speakers, len(ref_speakers)):
            mapping = dict(zip(perm, ref_speakers, strict=True))
            score = sum(count for (ref, hyp), count in matrix.items() if mapping.get(hyp) == ref)
            if score > best_score:
                best_score, best = score, mapping
    return best


def _interval_collector(duration_ms: int) -> tuple[Any, Any]:
    """返回 (add, finish)：把连续同类型帧合并成 DER 错误区间。"""
    intervals: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        intervals.append(
            {
                "type": current["type"],
                "start_ms": current["start_ms"],
                "end_ms": current["end_ms"],
                "ref_speakers": sorted(current["ref_speakers"]),
                "hyp_speakers": sorted(current["hyp_speakers"]),
            }
        )
        current = None

    def add(kind: str, index: int, ref_active: set[str], hyp_active: set[str]) -> None:
        nonlocal current
        start_ms = index * FRAME_MS
        end_ms = min(duration_ms, start_ms + FRAME_MS)
        if current is not None and current["type"] == kind and current["end_ms"] == start_ms:
            current["end_ms"] = end_ms
            current["ref_speakers"].update(ref_active)
            current["hyp_speakers"].update(hyp_active)
            return
        flush()
        current = {
            "type": kind,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "ref_speakers": set(ref_active),
            "hyp_speakers": set(hyp_active),
        }

    def finish() -> list[dict[str, Any]]:
        flush()
        # 只保留最长的 300 段，避免超长会议把 CaseScore/响应撑大。
        intervals.sort(key=lambda item: item["end_ms"] - item["start_ms"], reverse=True)
        return intervals[:300]

    return add, finish


def der(
    ref_segments: Sequence[Segment],
    hyp_segments: Sequence[Segment],
    duration_ms: int,
    *,
    collar_ms: int = DEFAULT_COLLAR_MS,
    skip_overlap: bool = False,
    fixed_mapping: dict[str, str | None] | None = None,
    collect_errors: bool = False,
) -> dict[str, Any]:
    """DER（fixed_mapping=None）或 SA-DER（fixed_mapping 给定）。

    fixed_mapping: 假设 speaker → matched_id；None 表示未匹配（进 unknown 桶）。
    返回 {value, miss, fa, conf, ref_frames, speakers}；无参考语音时 value=None。
    """
    if not ref_segments or duration_ms <= 0:
        return {
            "value": None,
            "miss": 0,
            "fa": 0,
            "conf": 0,
            "ref_frames": 0,
            "reason": "no_segment_timeline",
        }
    if not hyp_segments:
        # 参考有、假设完全没有说话人分段 → 全 miss（这是真实错误，不是 na）
        ref_frames = _active_frames(ref_segments, duration_ms)
        mask = _collar_mask(ref_segments, duration_ms, collar_ms)
        if skip_overlap:
            mask = bytearray(
                1 if (ignored or len(active) > 1) else 0
                for ignored, active in zip(mask, ref_frames, strict=False)
            )
        add_error, finish_errors = (
            _interval_collector(duration_ms) if collect_errors else (None, None)
        )
        total = 0
        for index, active_speakers in enumerate(ref_frames):
            if mask[index]:
                continue
            total += len(active_speakers)
            if add_error is not None:
                add_error("miss", index, active_speakers, set())
        result: dict[str, Any] = {
            "value": 1.0 if total else None,
            "miss": total,
            "fa": 0,
            "conf": 0,
            "ref_frames": total,
            "speakers": {"ref": len({s[0] for s in ref_segments}), "hyp": 0},
        }
        if finish_errors is not None:
            result["errors"] = finish_errors()
            result["mapping"] = {}
        return result

    ref_frames = _active_frames(ref_segments, duration_ms)
    hyp_frames = _active_frames(hyp_segments, duration_ms)
    mask = _collar_mask(ref_segments, duration_ms, collar_ms)
    if skip_overlap:
        mask = bytearray(
            1 if (ignored or len(active) > 1) else 0
            for ignored, active in zip(mask, ref_frames, strict=False)
        )

    if fixed_mapping is None:
        matrix, _total = _overlap_matrix(ref_segments, hyp_segments, duration_ms, collar_ms)
        mapping: dict[str, str | None] = dict(_best_mapping(matrix))
    else:
        mapping = dict(fixed_mapping)

    add_error, finish_errors = _interval_collector(duration_ms) if collect_errors else (None, None)
    miss = fa = conf = total = 0
    for index in range(len(ref_frames)):
        if mask[index]:
            continue
        ref_active = ref_frames[index]
        hyp_active = hyp_frames[index]
        if not ref_active:
            if not hyp_active:
                continue
            fa += len(hyp_active)
            if add_error is not None:
                add_error("fa", index, set(), hyp_active)
            continue

        ref_count = len(ref_active)
        hyp_count = len(hyp_active)
        total += ref_count
        frame_miss = max(0, ref_count - hyp_count)
        frame_fa = max(0, hyp_count - ref_count)
        miss += frame_miss
        fa += frame_fa
        mapped_active = {mapping.get(speaker) or f"__unknown__:{speaker}" for speaker in hyp_active}
        correct = len(ref_active.intersection(mapped_active))
        frame_conf = max(0, min(ref_count, hyp_count) - correct)
        conf += frame_conf
        if add_error is not None:
            if frame_conf > 0:
                add_error("conf", index, ref_active, hyp_active)
            elif frame_miss > 0:
                add_error("miss", index, ref_active, set())
            elif frame_fa > 0:
                add_error("fa", index, set(), hyp_active)

    value = (miss + fa + conf) / total if total else None
    detailed_result: dict[str, Any] = {
        "value": value,
        "miss": miss / total if total else None,
        "fa": fa / total if total else None,
        "conf": conf / total if total else None,
        "miss_frames": miss,
        "fa_frames": fa,
        "conf_frames": conf,
        "ref_frames": total,
        "speakers": {
            "ref": len({s[0] for s in ref_segments}),
            "hyp": len({s[0] for s in hyp_segments}),
        },
    }
    if finish_errors is not None:
        detailed_result["errors"] = finish_errors()
        detailed_result["mapping"] = mapping
    return detailed_result


def naming(
    ref_segments: Sequence[Segment],
    hyp_segments: Sequence[Segment],
    matched_ids: dict[str, str | None],
    *,
    unknown_policy: str = "fr",
) -> dict[str, Any]:
    """命名 FA/FR（M5 §5.6）：每段取时间重叠最多的假设 speaker，再比对 matched_id。

    matched_ids: 假设 speaker → 设备返回的 matched_id（None = 未识别）。
    """
    if not ref_segments:
        return {"status": "na", "reason": "no_speaker_reference"}
    if not hyp_segments:
        return {"status": "na", "reason": "no_diarization_output"}
    if not matched_ids:
        return {"status": "na", "reason": "no_voiceprint_match"}

    correct = fa = fr = 0
    total = 0
    for ref_speaker, start, end in ref_segments:
        overlap: dict[str, int] = {}
        for hyp_speaker, hyp_start, hyp_end in hyp_segments:
            span = min(end, hyp_end) - max(start, hyp_start)
            if span > 0:
                overlap[hyp_speaker] = overlap.get(hyp_speaker, 0) + span
        if not overlap:
            fr += 1
            total += 1
            continue
        hyp_best = max(overlap, key=lambda key: overlap[key])
        matched = matched_ids.get(hyp_best)
        total += 1
        if matched is None:
            if unknown_policy == "fa":
                fa += 1
            else:
                fr += 1
        elif matched == ref_speaker:
            correct += 1
        else:
            fa += 1
    return {
        "status": "ok",
        "value": correct / total if total else None,
        "sentence_acc": correct / total if total else None,
        "fa": fa / total if total else None,
        "fr": fr / total if total else None,
        "n": total,
        "correct_count": correct,
        "fa_count": fa,
        "fr_count": fr,
    }


def eer(scores: Iterable[tuple[float, bool]]) -> dict[str, Any]:
    """EER：把 confidence 当分数扫描阈值（M5 §5.6）。

    scores: (confidence, 是否正确匹配) 的簇级试验；样本太少返回 na。
    """
    trials = list(scores)
    positives = sum(1 for _score, correct in trials if correct)
    negatives = len(trials) - positives
    if len(trials) < 10 or positives == 0 or negatives == 0:
        return {"status": "na", "reason": "insufficient_trials", "n": len(trials)}

    thresholds = sorted({score for score, _ in trials}, reverse=True)
    best = (1.0, 1.0, 1.0)  # (eer, far, frr)
    best_gap = float("inf")
    for threshold in thresholds:
        tp = sum(1 for score, correct in trials if correct and score >= threshold)
        fn = positives - tp
        fp = sum(1 for score, correct in trials if not correct and score >= threshold)
        tn = negatives - fp
        far = fp / (fp + tn) if (fp + tn) else 0.0
        frr = fn / (fn + tp) if (fn + tp) else 0.0
        if abs(far - frr) < best_gap:
            best_gap = abs(far - frr)
            best = ((far + frr) / 2, far, frr)
    return {"status": "ok", "value": best[0], "far": best[1], "frr": best[2], "n": len(trials)}
