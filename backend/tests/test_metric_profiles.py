"""口径模板与校验的单元测试（M3 §2.1/§6/§7）。"""

from app.domain.metric_profiles import (
    BENCHMARK_PRESETS,
    BUCKET_DIMS,
    EVALUATORS,
    TEMPLATE_PARAMS,
    effective_params,
    is_locked,
    profile_hash,
    validate_params,
)


def test_six_templates_exist() -> None:
    assert set(TEMPLATE_PARAMS) == {
        "default_zh",
        "alimeeting_official",
        "dialect_sweep",
        "speaker_full",
        "perf_30ms",
        "cer_only",
    }


def test_official_evaluator_is_locked_and_fixed() -> None:
    """官方/预设口径由代码固定，不能通过 params 改写。"""
    modified = {
        **TEMPLATE_PARAMS["alimeeting_official"],
        "der": {"collar_ms": 999, "skip_overlap": True},
    }
    assert is_locked(modified)
    assert effective_params(modified)["der"]["collar_ms"] == 250
    assert effective_params(modified)["der"]["skip_overlap"] is False
    assert EVALUATORS["alimeeting_official"].locked is True


def test_default_zh_all_enabled() -> None:
    """默认全开，由 resolver 判 na（M3 §6 模板设计原则）。"""
    config = TEMPLATE_PARAMS["default_zh"]
    for key in ("punct", "itn", "naming"):
        assert config[key]["enabled"] is True


def test_alimeeting_official_uses_fixed_scoring() -> None:
    """官方口径不再暴露 join/separator：<sc> 由评分器恒定剥离。"""
    cer = TEMPLATE_PARAMS["alimeeting_official"]["cer"]
    assert "join" not in cer
    assert "separator" not in cer
    assert cer["enabled"] is True
    assert effective_params(TEMPLATE_PARAMS["alimeeting_official"])["der"]["collar_ms"] == 250


def test_default_perf_batch_uses_one_second_chunks() -> None:
    """批量模式走 burst 固定 1 秒块；30ms 只属于流式基线。"""
    perf = TEMPLATE_PARAMS["default_zh"]["perf"]
    assert perf["mode"] == "batch"
    assert perf["chunk_ms"] == 1000


def test_perf_30ms_disables_accuracy() -> None:
    config = TEMPLATE_PARAMS["perf_30ms"]
    assert config["perf"]["mode"] == "stream"
    assert config["perf"]["chunk_ms"] == 30
    assert config["cer"]["enabled"] is True
    assert config["perf"]["enabled"] is True
    for key in ("punct", "itn", "der", "naming"):
        assert config[key]["enabled"] is False


def test_cer_only_disables_other_metrics() -> None:
    config = TEMPLATE_PARAMS["cer_only"]
    assert config["punct"]["enabled"] is False
    assert config["itn"]["enabled"] is False
    assert config["der"]["enabled"] is False
    assert config["naming"]["enabled"] is False
    assert config["perf"]["enabled"] is False


def test_profile_hash_key_order_insensitive() -> None:
    a = {"cer": {"aggregate": "micro"}, "text": {"lowercase": True}}
    b = {"text": {"lowercase": True}, "cer": {"aggregate": "micro"}}
    assert profile_hash(a) == profile_hash(b)


def test_validate_unknown_section_and_values() -> None:
    errors = validate_params({"unknown_section": {}, "cer": {"aggregate": "weird"}})
    assert any("unknown_section" in e for e in errors)
    assert any("cer.aggregate" in e for e in errors)


def test_validate_collar_and_threshold() -> None:
    errors = validate_params({"der": {"collar_ms": -1}, "naming": {"threshold": 1.5}})
    assert any("der.collar_ms" in e for e in errors)
    assert any("naming.threshold" in e for e in errors)


def test_validate_text_normalization_options() -> None:
    assert (
        validate_params({"text": {"traditional": "to_simplified", "drop_fillers": ["嗯", "呃"]}})
        == []
    )
    errors = validate_params({"text": {"drop_fillers": "嗯"}})
    assert any("text.drop_fillers" in error for error in errors)


def test_validate_cer_aggregate_modes() -> None:
    assert validate_params({"cer": {"aggregate": "both"}}) == []
    assert any("cer.aggregate" in error for error in validate_params({"cer": {"aggregate": "x"}}))


def test_valid_params_pass() -> None:
    assert validate_params(TEMPLATE_PARAMS["default_zh"]) == []


def test_bucket_dims_registered() -> None:
    """维度注册表：静态取 buckets，派生在聚合时算（M3 §2.3）。"""
    assert BUCKET_DIMS["dialect"].kind == "static"
    assert BUCKET_DIMS["duration_bin"].kind == "derived"
    assert BUCKET_DIMS["duration_bin"].bins is not None
    assert len(BUCKET_DIMS["duration_bin"].bins) == 5


def test_presets_combine_profile_sampling_buckets() -> None:
    """§6.1 预设 = 口径 + subset + 采样 + 分桶的组合。"""
    assert BENCHMARK_PRESETS["apy_device_ab"]["subset"] == ["phone", "mic"]
    assert BENCHMARK_PRESETS["perf_baseline"]["template"] == "perf_30ms"
    assert BENCHMARK_PRESETS["cer_quick"]["sampling"]["mode"] == "random"
