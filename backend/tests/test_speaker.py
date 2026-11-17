"""DER / SA-DER / 命名 / EER 的单元测试（M5 §5.5/§5.6/§8）。"""

from app.scoring.speaker import der, eer, naming

# 参考：A 0-2000ms，B 2000-4000ms（边界 2000 处会因 collar 忽略）
REF = [("A", 0, 2000), ("B", 2000, 4000)]
DURATION = 4000


def test_perfect_hypothesis_zero_der() -> None:
    hyp = [("x", 0, 2000), ("y", 2000, 4000)]
    result = der(REF, hyp, DURATION)
    assert result["value"] == 0.0
    assert result["miss"] == 0.0 and result["fa"] == 0.0 and result["conf"] == 0.0


def test_miss_only() -> None:
    # 假设只覆盖前 2s，后 2s 全 miss（collar 会忽略边界附近若干帧）
    hyp = [("x", 0, 2000)]
    result = der(REF, hyp, DURATION)
    assert result["value"] is not None and 0.4 < result["value"] < 0.6
    assert result["miss"] and result["fa"] == 0.0 and result["conf"] == 0.0


def test_false_alarm_only() -> None:
    # 参考之外多出 4-5s 的语音 → FA，但总时长若超出 UEM 需要算：扩展参考
    ref = [("A", 0, 2000)]
    hyp = [("x", 0, 2000), ("y", 4000, 5000)]
    result = der(ref, hyp, 5000)
    assert result["fa"] and result["fa"] > 0
    assert result["miss"] == 0.0 and result["conf"] == 0.0


def test_confusion_when_two_speakers_swapped() -> None:
    # 两个 speaker 完全对调：最优映射能救回来（DER=0）
    ref = [("A", 0, 1000), ("B", 1000, 2000)]
    hyp = [("y", 0, 1000), ("x", 1000, 2000)]
    assert der(ref, hyp, 2000)["value"] == 0.0

    # 三个 speaker 中只有一个对调，最优映射无法同时满足 → 出现 conf
    ref3 = [("A", 0, 1000), ("B", 1000, 2000), ("C", 2000, 3000)]
    hyp3 = [("x", 0, 1000), ("y", 1000, 2000), ("x", 2000, 3000)]
    result = der(ref3, hyp3, 3000)
    assert result["conf"] and result["conf"] > 0


def test_sa_der_uses_fixed_mapping() -> None:
    """SA-DER 禁用最优映射：固定的 matched_id 直接比对。"""
    ref = [("A", 0, 1000), ("B", 1000, 2000)]
    hyp = [("0", 0, 1000), ("1", 1000, 2000)]
    # 正确匹配 → 0
    correct = der(ref, hyp, 2000, fixed_mapping={"0": "A", "1": "B"})
    assert correct["value"] == 0.0
    # 匹配反了 → conf 很高（DER 会因为最优映射为 0）
    wrong = der(ref, hyp, 2000, fixed_mapping={"0": "B", "1": "A"})
    assert wrong["value"] is not None and wrong["value"] > 0.9
    # 未匹配 → unknown 桶，也算 conf
    unknown = der(ref, hyp, 2000, fixed_mapping={"0": None, "1": None})
    assert unknown["value"] is not None and unknown["value"] > 0.9


def test_collar_ignores_boundary_frames() -> None:
    """collar 0.25 单侧：边界 ±250ms 不计分（小段落在 collar 内不影响 DER）。"""
    ref = [("A", 0, 1000), ("B", 1000, 2000)]
    hyp = [("x", 0, 750), ("y", 1250, 2000)]  # 边界附近各空 250ms
    result = der(ref, hyp, 2000, collar_ms=250)
    # 空段都在 collar 内 → 不应产生 miss
    assert result["miss"] == 0.0


def test_der_can_skip_overlap_frames() -> None:
    """skip_overlap=true 时重叠帧不进入 DER 分母。"""
    ref = [("A", 0, 1000), ("B", 500, 1500)]
    hyp = [("x", 0, 1000)]
    kept = der(ref, hyp, 2000, collar_ms=0, skip_overlap=False)
    skipped = der(ref, hyp, 2000, collar_ms=0, skip_overlap=True)

    assert skipped["ref_frames"] is not None
    assert kept["ref_frames"] is not None
    assert skipped["ref_frames"] < kept["ref_frames"]


def test_der_uses_custom_collar() -> None:
    """collar_ms 会改变边界附近的 no-score 区域。"""
    ref = [("A", 0, 1000), ("B", 1000, 2000)]
    hyp = [("x", 0, 750), ("y", 1250, 2000)]
    assert der(ref, hyp, 2000, collar_ms=250)["miss"] == 0.0
    assert der(ref, hyp, 2000, collar_ms=0)["miss"] > 0.0


def test_der_preserves_overlapping_speakers() -> None:
    ref = [("A", 0, 1000), ("B", 0, 1000)]
    perfect = [("x", 0, 1000), ("y", 0, 1000)]
    assert der(ref, perfect, 1000)["value"] == 0.0

    missing_one = der(ref, [("x", 0, 1000)], 1000)
    assert missing_one["value"] == 0.5
    assert missing_one["miss_frames"] == 50
    assert missing_one["ref_frames"] == 100


def test_der_counts_each_overlapping_false_alarm_speaker() -> None:
    result = der(
        [("A", 0, 1000)],
        [("x", 0, 1000), ("y", 0, 1000)],
        1000,
    )
    assert result["fa_frames"] == 50
    assert result["ref_frames"] == 50
    assert result["value"] == 1.0


def test_no_hypothesis_is_total_miss() -> None:
    result = der(REF, [], DURATION)
    assert result["value"] == 1.0


def test_no_reference_is_na() -> None:
    assert der([], [("x", 0, 1000)], 1000)["reason"] == "no_segment_timeline"


def test_naming_fa_fr() -> None:
    ref = [("A", 0, 1000), ("B", 1000, 2000)]
    hyp = [("0", 0, 1000), ("1", 1000, 2000)]
    correct = naming(ref, hyp, {"0": "A", "1": "B"})
    assert correct["value"] == 1.0 and correct["fa"] == 0.0 and correct["fr"] == 0.0

    mixed = naming(ref, hyp, {"0": "B", "1": None})
    assert mixed["value"] == 0.0
    assert mixed["fa"] == 0.5 and mixed["fr"] == 0.5


def test_naming_missing_voiceprint_is_na() -> None:
    ref = [("A", 0, 1000)]
    hyp = [("0", 0, 1000)]
    assert naming(ref, hyp, {})["reason"] == "no_voiceprint_match"


def test_eer_separable_clusters() -> None:
    trials = [
        (0.9, True),
        (0.85, True),
        (0.8, True),
        (0.75, True),
        (0.7, True),
        (0.6, False),
        (0.55, False),
        (0.5, False),
        (0.45, False),
        (0.4, False),
    ]
    result = eer(trials)
    assert result["status"] == "ok"
    assert result["value"] is not None and result["value"] < 0.2


def test_eer_insufficient_trials() -> None:
    assert eer([(0.9, True), (0.4, False)])["reason"] == "insufficient_trials"


def test_der_collects_error_intervals_for_position_analysis() -> None:
    """collect_errors 返回连续 miss/fa 区间与 speaker 映射，供 DER 定位。"""
    ref = [("A", 0, 3000)]
    hyp = [("x", 0, 1000)]
    result = der(ref, hyp, 3000, collar_ms=0, collect_errors=True)

    assert result["mapping"] == {"x": "A"}
    errors = result["errors"]
    assert errors
    miss = next(item for item in errors if item["type"] == "miss")
    assert miss["start_ms"] == 1000
    assert miss["end_ms"] == 3000
    assert miss["ref_speakers"] == ["A"]
