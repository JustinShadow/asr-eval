"""MetricProfile 口径模板、校验与分桶维度注册表（M3 §2.1/§2.3/§6）。

模板设计原则：**默认全开，由 resolver 判定 na**——不在模板里预关指标，
否则用户会以为平台不支持（M3 §6）。
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any

_ALLOWED_TOP = {
    "evaluator",
    "text",
    "cer",
    "punct",
    "itn",
    "der",
    "naming",
    "perf",
    "coverage",
}
_ALLOWED_FIELDS = {
    "text": {"strip_punct", "strip_space", "lowercase", "traditional", "drop_fillers", "drop_tags"},
    # join/separator 已移除：平台按单条 utterance 评分，没有多段拼接场景；
    # AliMeeting 的 <sc> 由 CER scorer 恒定剥离，不再作为可配置字段。
    "cer": {"enabled", "engine", "aggregate", "mixed"},
    "punct": {"enabled", "match"},
    "itn": {"enabled", "normalizer"},
    "der": {"enabled", "collar_ms", "collar_semantics", "skip_overlap", "uem"},
    # mode/granularity 已移除：命名固定为 matching + 分段级。
    "naming": {"enabled", "threshold", "unknown_policy"},
    "perf": {"enabled", "mode", "chunk_ms", "stream_input_rate"},
    "coverage": {"ok_threshold"},
}

# ── §2.3 BUCKET_DIMS：静态维度取 item.buckets，派生维度在聚合时计算 ──


@dataclass(frozen=True)
class BucketDim:
    """一个分桶维度；新增维度 = 注册一条，不改表结构。"""

    name: str
    kind: str  # static | derived
    extractor: str  # static: item.buckets 的键名；derived: 聚合侧函数名
    bins: list[str] | None = None


BUCKET_DIMS: dict[str, BucketDim] = {
    dim.name: dim
    for dim in [
        BucketDim("dialect", "static", "buckets.dialect"),
        BucketDim("scene", "static", "buckets.scene"),
        BucketDim("lang", "static", "buckets.lang"),
        BucketDim("gender", "static", "buckets.gender"),
        BucketDim("split", "static", "item.split"),
        BucketDim("source", "static", "buckets.source"),
        BucketDim("speaker", "static", "buckets.speaker"),
        BucketDim("channel", "static", "buckets.channel"),
        BucketDim("n_speakers", "derived", "speaker_count"),
        # duration_bin 与 M4 §3 时长桶边界一致
        BucketDim(
            "duration_bin", "derived", "duration_ms", ["<5s", "5~15s", "15~60s", "1~5min", ">5min"]
        ),
        BucketDim("overlap_bin", "derived", "overlap_ratio", ["0", "0~10%", "10~30%", ">30%"]),
        BucketDim("text_len_bin", "derived", "ref_len", ["<10", "10~30", "30~100", ">100"]),
    ]
}


# ── §2.1 config 结构（默认全开）──

_BASE_TEXT = {
    "strip_punct": True,
    "strip_space": True,
    "lowercase": True,
    "traditional": "keep",
    "drop_fillers": [],
    "drop_tags": [
        "[+]",
        "[*]",
        "[ENS]",
        "[LAUGHTER]",
        "[SONANT]",
        "[MUSIC]",
        "[SYSTEM]",
        "[FILLER]",
    ],
}


def _base_accuracy() -> dict[str, Any]:
    return {
        "text": dict(_BASE_TEXT),
        "cer": {
            "enabled": True,
            "engine": "rapidfuzz",
            "aggregate": "micro",
            "mixed": "char",
        },
        "punct": {"enabled": True, "match": "position_and_type"},
        "itn": {"enabled": True, "normalizer": "wetext"},
        "der": {
            "collar_ms": 250,
            "collar_semantics": "half_width",
            "skip_overlap": False,
            "uem": "full",
        },
        "naming": {
            "enabled": True,
            "threshold": 0.6,
            "unknown_policy": "fr",
        },
        "perf": {
            "enabled": True,
            "mode": "batch",
            # 批量（burst）固定 1 秒块；30ms 只属于流式基线（perf_30ms）。
            "chunk_ms": 1000,
            "stream_input_rate": 1.0,
        },
        "coverage": {"ok_threshold": 0.95},
    }


def _disable_accuracy(config: dict[str, Any]) -> dict[str, Any]:
    for key in ("punct", "itn", "der", "naming"):
        config[key]["enabled"] = False
    return config


# ── §6 七个预置口径模板 ──


def _with_evaluator(evaluator: str, params: dict[str, Any]) -> dict[str, Any]:
    """把 evaluator 身份写入 params，便于 hash、API 和前端锁定。"""
    return {"evaluator": evaluator, **params}


TEMPLATE_PARAMS: dict[str, dict[str, Any]] = {
    "default_zh": _with_evaluator("default_zh", _base_accuracy()),
    "alimeeting_official": _with_evaluator(
        "alimeeting_official",
        # 官方 AliMeeting 口径：250ms collar、重叠参与、<sc> 剥离均为评分器固定行为。
        _base_accuracy(),
    ),
    "dialect_sweep": _with_evaluator("default_zh", _base_accuracy()),
    "speaker_full": _with_evaluator("default_zh", _base_accuracy()),
    "perf_30ms": _with_evaluator(
        "perf_30ms",
        _disable_accuracy(
            {
                **_base_accuracy(),
                "perf": {
                    "enabled": True,
                    "mode": "stream",
                    "chunk_ms": 30,
                    "stream_input_rate": 1.0,
                },
            }
        ),
    ),
    "cer_only": _with_evaluator(
        "cer_only",
        _disable_accuracy(
            {
                **_base_accuracy(),
                # 仅 CER：性能指标不落库，报告不会出现无意义读数。
                "perf": {**_base_accuracy()["perf"], "enabled": False},
            }
        ),
    ),
}


@dataclass(frozen=True)
class EvaluatorSpec:
    """一个受版本约束的评分实现；locked 的 params 不允许用户修改。"""

    id: str
    name: str
    locked: bool
    description: str
    params: dict[str, Any]


EVALUATORS: dict[str, EvaluatorSpec] = {
    "default_zh": EvaluatorSpec(
        "default_zh",
        "中文通用",
        False,
        "通用中文归一化与准确率指标；参数可编辑。",
        TEMPLATE_PARAMS["default_zh"],
    ),
    "alimeeting_official": EvaluatorSpec(
        "alimeeting_official",
        "AliMeeting 官方",
        True,
        "官方 AliMeeting 口径：固定 250ms collar、重叠参与、<sc> 剥离。",
        TEMPLATE_PARAMS["alimeeting_official"],
    ),
    "perf_30ms": EvaluatorSpec(
        "perf_30ms",
        "30ms 流式性能",
        True,
        "固定 30ms / 1x 性能基线；关闭标点/ITN/DER/命名，保留 CER 与性能读数。",
        TEMPLATE_PARAMS["perf_30ms"],
    ),
    "cer_only": EvaluatorSpec(
        "cer_only",
        "仅 CER",
        True,
        "固定只保留 CER；关闭标点/ITN/DER/命名与性能读数。",
        TEMPLATE_PARAMS["cer_only"],
    ),
}


def evaluator_id(params: dict[str, Any]) -> str:
    """返回口径的 evaluator id；旧口径没有该字段时按 custom 处理。"""
    value = params.get("evaluator")
    return value if isinstance(value, str) and value in EVALUATORS else "custom"


def is_locked(params: dict[str, Any]) -> bool:
    """官方/预设口径不可原地修改。"""
    spec = EVALUATORS.get(evaluator_id(params))
    return bool(spec and spec.locked)


def effective_params(params: dict[str, Any]) -> dict[str, Any]:
    """官方/预设口径返回代码内固定参数；custom 返回用户参数。"""
    spec = EVALUATORS.get(evaluator_id(params))
    return dict(spec.params) if spec and spec.locked else params


# §6.1 Benchmark 预设：profile + subset + 采样 + 分桶的"填好的表单"
BENCHMARK_PRESETS: dict[str, dict[str, Any]] = {
    "cer_quick": {
        "template": "cer_only",
        "subset": "primary",
        "sampling": {"mode": "random", "n": 1000, "seed": 42},
        "bucket_dims": ["dialect"],
    },
    "full_accuracy": {
        "template": "default_zh",
        "subset": "primary",
        "sampling": {"mode": "all"},
        "bucket_dims": ["dialect", "duration_bin"],
    },
    "alimeeting_clean": {
        "template": "default_zh",
        "subset": "near",
        "sampling": {"mode": "all"},
        "bucket_dims": ["speaker", "duration_bin"],
    },
    "alimeeting_meeting": {
        "template": "alimeeting_official",
        "subset": "far",
        "sampling": {"mode": "all"},
        "bucket_dims": ["n_speakers", "overlap_bin"],
    },
    "apy_device_ab": {
        "template": "default_zh",
        "subset": ["phone", "mic"],
        "sampling": {"mode": "all"},
        "bucket_dims": ["channel"],
    },
    "perf_baseline": {
        "template": "perf_30ms",
        "subset": "primary",
        "sampling": {"mode": "stratified", "dim": "duration_bin", "per_stratum": 30},
        "bucket_dims": ["duration_bin"],
    },
}


def canonical_params(params: dict[str, Any]) -> str:
    """口径参数规范序列化；键顺序不影响 hash。"""
    return json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def profile_hash(params: dict[str, Any]) -> str:
    """只哈希影响分数数值的字段；名称和覆盖率展示策略不参与。"""
    scoring_fields = ("evaluator", "text", "cer", "punct", "itn", "der", "naming", "perf")
    scoring_params = {key: params[key] for key in scoring_fields if key in params}
    return hashlib.sha256(canonical_params(scoring_params).encode("utf-8")).hexdigest()[:16]


def validate_params(params: dict[str, Any]) -> list[str]:
    """口径语义校验（M3 §7）：错误信息定位到字段路径。"""
    errors: list[str] = []
    if not isinstance(params, dict):
        return ["params: 必须是对象"]
    for key in params:
        if key not in _ALLOWED_TOP:
            errors.append(f"{key}: 未知配置段（允许 {sorted(_ALLOWED_TOP)}）")
            continue
        if key == "evaluator":
            if not isinstance(params[key], str) or (
                params[key] not in EVALUATORS and params[key] != "custom"
            ):
                errors.append(f"evaluator: 非法值 {params[key]}")
            continue
        section = params[key]
        if not isinstance(section, dict):
            errors.append(f"{key}: 必须是对象")
            continue
        for field in section:
            if field not in _ALLOWED_FIELDS[key]:
                errors.append(f"{key}.{field}: 未知配置项")

    text = params.get("text")
    if isinstance(text, dict):
        for key in ("strip_punct", "strip_space", "lowercase"):
            if key in text and not isinstance(text[key], bool):
                errors.append(f"text.{key}: 必须是布尔")
        if "traditional" in text and (
            not isinstance(text["traditional"], str)
            or text["traditional"] not in {"keep", "to_simplified"}
        ):
            errors.append(f"text.traditional: 非法值 {text['traditional']}")
        for key in ("drop_fillers", "drop_tags"):
            if key in text and (
                not isinstance(text[key], list)
                or not all(isinstance(value, str) for value in text[key])
            ):
                errors.append(f"text.{key}: 必须是字符串数组")

    cer = params.get("cer")
    if isinstance(cer, dict):
        if "enabled" in cer and not isinstance(cer["enabled"], bool):
            errors.append("cer.enabled: 必须是布尔")
        if "engine" in cer and (
            not isinstance(cer["engine"], str) or cer["engine"] not in {"rapidfuzz"}
        ):
            errors.append(f"cer.engine: 非法值 {cer['engine']}")
        if "aggregate" in cer and (
            not isinstance(cer["aggregate"], str)
            or cer["aggregate"] not in {"micro", "macro", "both"}
        ):
            errors.append(f"cer.aggregate: 非法值 {cer['aggregate']}")
        if "mixed" in cer and cer["mixed"] != "char":
            errors.append(f"cer.mixed: 非法值 {cer['mixed']}")

    for key, allowed in (("punct", {"position_and_type"}), ("itn", {"wetext"})):
        section = params.get(key)
        if isinstance(section, dict):
            if "enabled" in section and not isinstance(section["enabled"], bool):
                errors.append(f"{key}.enabled: 必须是布尔")
            value_key = "match" if key == "punct" else "normalizer"
            if value_key in section and (
                not isinstance(section[value_key], str) or section[value_key] not in allowed
            ):
                errors.append(f"{key}.{value_key}: 非法值 {section[value_key]}")

    der = params.get("der")
    if isinstance(der, dict):
        if "enabled" in der and not isinstance(der["enabled"], bool):
            errors.append("der.enabled: 必须是布尔")
        if "collar_ms" in der and (
            isinstance(der["collar_ms"], bool)
            or not isinstance(der["collar_ms"], int)
            or der["collar_ms"] < 0
        ):
            errors.append("der.collar_ms: 必须 >= 0")
        if "collar_semantics" in der and (
            not isinstance(der["collar_semantics"], str)
            or der["collar_semantics"] not in {"half_width", "full_width"}
        ):
            errors.append(f"der.collar_semantics: 非法值 {der['collar_semantics']}")
        if "skip_overlap" in der and not isinstance(der["skip_overlap"], bool):
            errors.append("der.skip_overlap: 必须是布尔")
        if "uem" in der and der["uem"] != "full":
            errors.append(f"der.uem: 非法值 {der['uem']}")

    naming = params.get("naming")
    if isinstance(naming, dict):
        if "enabled" in naming and not isinstance(naming["enabled"], bool):
            errors.append("naming.enabled: 必须是布尔")
        threshold = naming.get("threshold")
        if "threshold" in naming and (
            isinstance(threshold, bool)
            or not isinstance(threshold, int | float)
            or not 0 <= threshold <= 1
        ):
            errors.append("naming.threshold: 必须在 [0,1]")
        if "unknown_policy" in naming and (
            not isinstance(naming["unknown_policy"], str)
            or naming["unknown_policy"] not in {"fr", "fa"}
        ):
            errors.append(f"naming.unknown_policy: 非法值 {naming['unknown_policy']}")

    perf = params.get("perf")
    if isinstance(perf, dict):
        if "enabled" in perf and not isinstance(perf["enabled"], bool):
            errors.append("perf.enabled: 必须是布尔")
        if "mode" in perf and (
            not isinstance(perf["mode"], str) or perf["mode"] not in {"stream", "batch"}
        ):
            errors.append(f"perf.mode: 非法值 {perf['mode']}")
        if "chunk_ms" in perf and (
            isinstance(perf["chunk_ms"], bool)
            or not isinstance(perf["chunk_ms"], int)
            or perf["chunk_ms"] <= 0
        ):
            errors.append("perf.chunk_ms: 必须是正整数")
        value = perf.get("stream_input_rate")
        if "stream_input_rate" in perf and (
            isinstance(value, bool) or not isinstance(value, int | float) or value <= 0
        ):
            errors.append("perf.stream_input_rate: 必须是正数")

    coverage = params.get("coverage")
    if isinstance(coverage, dict) and "ok_threshold" in coverage:
        threshold = coverage["ok_threshold"]
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, int | float)
            or not 0 < threshold <= 1
        ):
            errors.append("coverage.ok_threshold: 必须在 (0,1]")
    return errors
