"""按数据集能力、设备能力和口径配置解析指标可用性。"""

from typing import Any

MetricResult = dict[str, str | float | None]


def _number(source: dict[str, Any], key: str) -> float | None:
    value = source.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return max(0.0, min(float(value), 1.0))


def resolve_metrics(
    dataset_caps: dict[str, Any],
    device_caps: dict[str, Any],
    profile: dict[str, Any],
    benchmark_params: dict[str, Any] | None = None,
) -> list[MetricResult]:
    """返回 ok/partial/na 与原因；输入为快照，不读数据库也不访问设备。"""
    declared_value = device_caps.get("declared")
    observed_value = device_caps.get("observed")
    declared = declared_value if isinstance(declared_value, dict) else {}
    observed = observed_value if isinstance(observed_value, dict) else {}
    coverage_config = profile.get("coverage")
    threshold_value = (
        coverage_config.get("ok_threshold", 0.95) if isinstance(coverage_config, dict) else 0.95
    )
    threshold = (
        float(threshold_value)
        if isinstance(threshold_value, int | float) and not isinstance(threshold_value, bool)
        else 0.95
    )

    def result(
        name: str,
        status: str,
        reason: str | None = None,
        coverage: float | None = None,
    ) -> MetricResult:
        return {"name": name, "status": status, "reason": reason, "coverage": coverage}

    def metric(
        name: str,
        profile_section: str,
        ratios: list[float | None],
        device_ok: bool,
        missing_reason: str,
        device_reason: str,
    ) -> MetricResult:
        config = profile.get(profile_section, {})
        if isinstance(config, dict) and config.get("enabled") is False:
            return result(name, "na", "disabled_by_profile")
        present = [value for value in ratios if value is not None]
        if len(present) != len(ratios) or not present:
            return result(name, "na", "capability_unknown")
        coverage = min(present)
        if coverage <= 0:
            return result(name, "na", missing_reason, 0.0)
        if not device_ok:
            return result(name, "na", device_reason)
        if coverage < threshold:
            return result(name, "partial", "coverage_below_threshold", coverage)
        return result(name, "ok", None, coverage)

    text_ratio = _number(dataset_caps, "text_ratio")
    output: list[MetricResult] = [
        metric("cer", "cer", [text_ratio], True, "no_reference_text", "device_unsupported")
    ]

    punct_ratio = _number(dataset_caps, "punct_sentence_ratio")
    if punct_ratio is None and isinstance(dataset_caps.get("punct_supported"), bool):
        punct_ratio = 1.0 if dataset_caps["punct_supported"] else 0.0
    output.append(
        metric(
            "punct_f1",
            "punct",
            [punct_ratio],
            bool(observed.get("punctuation_output")),
            "no_punct_reference",
            "device_no_punctuation",
        )
    )

    output.append(
        metric(
            "itn_acc",
            "itn",
            [_number(dataset_caps, "itn_ratio")],
            bool(declared.get("itn")),
            "no_itn_reference",
            "device_no_itn",
        )
    )

    speaker_coverage = [
        _number(dataset_caps, "segment_ratio"),
        _number(dataset_caps, "speaker_ratio"),
    ]
    has_speaker_output = bool(
        observed.get("segments")
        and (observed.get("speakers_in_sentence") or observed.get("global_spk_list"))
    )
    for name in ("der", "sa_der"):
        output.append(
            metric(
                name,
                "der",
                speaker_coverage,
                has_speaker_output,
                "no_speaker_reference",
                "device_no_speaker_output",
            )
        )
    enrollment = (benchmark_params or {}).get("enrollment")
    enrollment_ready = enrollment == "complete" or (
        isinstance(enrollment, dict) and enrollment.get("status") == "complete"
    )
    for name in ("naming_acc", "speaker_eer"):
        status = metric(
            name,
            "naming",
            speaker_coverage,
            bool(observed.get("naming")),
            "no_speaker_reference",
            "device_no_naming",
        )
        if status["status"] != "na" and not enrollment_ready:
            status = result(name, "na", "enrollment_missing")
        output.append(status)

    for name, field in (
        ("rtf_dev", "usage_field"),
        ("ttft_ms", "partials"),
        ("ttfa_ms", "segments"),
    ):
        perf = profile.get("perf", {})
        if isinstance(perf, dict) and perf.get("enabled") is False:
            output.append(result(name, "na", "disabled_by_profile"))
        elif observed.get(field):
            output.append(result(name, "ok", coverage=1.0))
        else:
            output.append(result(name, "na", f"device_no_{field}"))
    return output
