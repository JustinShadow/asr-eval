"""指标可用性纯函数与 MetricProfile 口径哈希验收。"""

from app.domain.metric_profiles import profile_hash, validate_params
from app.domain.metric_resolver import resolve_metrics


def _status(
    metrics: list[dict[str, str | float | None]], name: str
) -> dict[str, str | float | None]:
    return next(metric for metric in metrics if metric["name"] == name)


def test_resolver_marks_coverage_below_threshold_as_partial() -> None:
    metrics = resolve_metrics(
        {"text_ratio": 0.9},
        {},
        {"coverage": {"ok_threshold": 0.95}},
    )

    assert _status(metrics, "cer") == {
        "name": "cer",
        "status": "partial",
        "reason": "coverage_below_threshold",
        "coverage": 0.9,
    }


def test_resolver_uses_na_for_disabled_and_missing_capabilities() -> None:
    metrics = resolve_metrics(
        {"text_ratio": 1.0, "punct_sentence_ratio": 1.0},
        {},
        {"punct": {"enabled": False}},
    )

    assert _status(metrics, "cer")["status"] == "ok"
    assert _status(metrics, "punct_f1")["reason"] == "disabled_by_profile"
    assert _status(metrics, "itn_acc")["reason"] == "capability_unknown"


def test_naming_metrics_require_an_explicit_completed_enrollment() -> None:
    dataset = {"segment_ratio": 1.0, "speaker_ratio": 1.0}
    device = {"observed": {"segments": True, "speakers_in_sentence": True, "naming": True}}
    profile = {"naming": {"enabled": True}}

    missing = resolve_metrics(dataset, device, profile)
    ready = resolve_metrics(dataset, device, profile, {"enrollment": "complete"})

    assert _status(missing, "naming_acc")["reason"] == "enrollment_missing"
    assert _status(ready, "naming_acc")["status"] == "ok"


def test_profile_hash_tracks_scoring_fields_but_not_coverage_policy() -> None:
    base = {"cer": {"engine": "rapidfuzz"}, "coverage": {"ok_threshold": 0.95}}
    changed_coverage = {**base, "coverage": {"ok_threshold": 0.8}}
    changed_scoring = {"cer": {"engine": "other"}, "coverage": {"ok_threshold": 0.95}}

    assert profile_hash(base) == profile_hash(changed_coverage)
    assert profile_hash(base) != profile_hash(changed_scoring)


def test_profile_validation_reports_unhashable_enum_values_without_crashing() -> None:
    errors = validate_params(
        {
            "text": {"traditional": ["keep"]},
            "cer": {"engine": {"name": "rapidfuzz"}},
            "naming": {"unknown_policy": [{"name": "fr"}]},
        }
    )

    assert "text.traditional: 非法值 ['keep']" in errors
    assert any(error.startswith("cer.engine:") for error in errors)
    assert "naming.unknown_policy: 非法值 [{'name': 'fr'}]" in errors
