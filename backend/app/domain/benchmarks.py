"""口径与 Benchmark 服务。"""

import hashlib
import json
import uuid
from typing import Any, cast

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.domain import audit
from app.domain.metric_profiles import (
    BUCKET_DIMS,
    effective_params,
    is_locked,
    profile_hash,
    validate_params,
)
from app.domain.metric_resolver import resolve_metrics
from app.domain.models import (
    Benchmark,
    DatasetSubset,
    DatasetVersion,
    Deployment,
    Item,
    MetricProfile,
    RefText,
    Run,
)


def create_profile(db: Session, *, name: str, params: dict[str, Any]) -> MetricProfile:
    """创建口径；同名 409；语义非法 422（M3 §7，错误定位字段）。"""
    exists = db.scalar(select(MetricProfile).where(MetricProfile.name == name))
    if exists is not None:
        raise ApiError("E_CONFLICT", "口径名称已存在", status_code=409)
    params = effective_params(params)
    errors = validate_params(params)
    if errors:
        raise ApiError("E_VALIDATION", "口径参数非法", status_code=422, detail={"errors": errors})
    profile = MetricProfile(name=name, params=params, profile_hash=profile_hash(params))
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def create_profile_version(
    db: Session,
    profile_id: uuid.UUID,
    *,
    params: dict[str, Any],
    actor: str,
) -> MetricProfile:
    """以新版本保存口径，不改写被 Benchmark 引用的历史定义。"""
    source = db.get(MetricProfile, profile_id)
    if source is None:
        raise ApiError("E_NOT_FOUND", "口径不存在", status_code=404)
    if is_locked(source.params):
        raise ApiError(
            "E_EVALUATOR_LOCKED",
            "官方/预设口径不可修改；请新建自定义口径",
            status_code=409,
        )
    params = effective_params(params)
    errors = validate_params(params)
    if errors:
        raise ApiError("E_VALIDATION", "口径参数非法", status_code=422, detail={"errors": errors})
    # 锁住同名的全部历史版本，避免两个并发请求同时分配相同版本号。
    profile_versions = db.scalars(
        select(MetricProfile)
        .where(MetricProfile.name == source.name)
        .order_by(MetricProfile.version)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    latest_version = max((item.version for item in profile_versions), default=source.version)
    profile = MetricProfile(
        name=source.name,
        version=int(latest_version or source.version) + 1,
        params=params,
        profile_hash=profile_hash(params),
        updated_by=actor,
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _canonical(payload: Any) -> str:
    """口径/采样/参数的规范序列化；键顺序不影响 hash。"""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_version_hash(
    db: Session,
    *,
    dataset_version_id: uuid.UUID,
    subsets: list[str],
    profile_hash: str,
    sampling: dict[str, Any],
    bucket_rules: dict[str, Any],
    params: dict[str, Any],
) -> str:
    """按 M3 §5.6 计算 version_hash。

    content_uids 流式读取（yield_per），不落盘；
    任一输入变化 → 新 hash → 默认禁止与旧结果并排比较。
    """
    digest = hashlib.sha256()
    uids: list[str] = []
    for (uid,) in db.execute(
        select(Item.content_uid).where(Item.dataset_version_id == dataset_version_id)
    ).yield_per(5000):
        uids.append(uid)
    parts = [
        ",".join(sorted(uids)),
        ",".join(sorted(subsets)),
        profile_hash,
        _canonical(sampling),
        _canonical(bucket_rules),
        _canonical(params),
    ]
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x1f")
    return digest.hexdigest()[:16]


def _primary_subset_names(db: Session, dataset_version_id: uuid.UUID) -> list[str]:
    """缺省 subset 取 primary（M3 §5.2 步骤 2）；没有 subset 行时回退 all。"""
    names = db.scalars(
        select(DatasetSubset.name)
        .where(
            DatasetSubset.dataset_version_id == dataset_version_id,
            DatasetSubset.is_primary.is_(True),
        )
        .order_by(DatasetSubset.name)
    ).all()
    return list(names) if names else ["all"]


def create_benchmark(
    db: Session,
    *,
    name: str,
    dataset_version_id: uuid.UUID,
    metric_profile_id: uuid.UUID,
    subsets: list[str] | None = None,
    sampling: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    bucket_rules: dict[str, Any] | None = None,
    note: str | None = None,
) -> Benchmark:
    """创建 Benchmark（M3 §5.2）。

    - 版本必须 ready → 否则 409（§7）；
    - subsets 缺省取 primary subset；
    - version_hash 冲突 → 幂等复用已有 Benchmark（§7）。
    """
    version = db.get(DatasetVersion, dataset_version_id)
    if version is None:
        raise ApiError("E_NOT_FOUND", "数据集版本不存在", status_code=404)
    # M4 §5.2：ready / ready_with_errors 都可用于评测（后者需人工确认过）
    if version.status not in {"ready", "ready_with_errors"}:
        raise ApiError("E_CONFLICT", "数据集版本未就绪", status_code=409)
    profile = db.scalar(
        select(MetricProfile)
        .where(MetricProfile.id == metric_profile_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if profile is None:
        raise ApiError("E_NOT_FOUND", "口径不存在", status_code=404)

    resolved_subsets = _resolve_subsets(db, dataset_version_id, subsets)
    sampling = sampling or {}
    params = params or {}
    bucket_rules = bucket_rules or {}
    errors = validate_benchmark_definition(sampling, bucket_rules)
    if errors:
        raise ApiError("E_VALIDATION", "基准参数非法", status_code=422, detail={"errors": errors})

    exists = db.scalar(
        select(Benchmark).where(
            Benchmark.dataset_version_id == dataset_version_id, Benchmark.name == name
        )
    )
    if exists is not None:
        raise ApiError("E_CONFLICT", "该版本下已存在同名 Benchmark", status_code=409)

    version_hash = compute_version_hash(
        db,
        dataset_version_id=dataset_version_id,
        subsets=resolved_subsets,
        profile_hash=profile.profile_hash,
        sampling=sampling,
        bucket_rules=bucket_rules,
        params=params,
    )
    # 同一评测定义（hash 相同）直接复用，不产生第二条（M3 §7 幂等）
    reused = db.scalar(select(Benchmark).where(Benchmark.version_hash == version_hash))
    if reused is not None:
        return reused

    benchmark = Benchmark(
        name=name,
        dataset_version_id=dataset_version_id,
        metric_profile_id=metric_profile_id,
        subsets=resolved_subsets,
        sampling=sampling,
        params=params,
        bucket_rules=bucket_rules,
        version_hash=version_hash,
        note=note,
    )
    db.add(benchmark)
    db.commit()
    db.refresh(benchmark)
    return benchmark


def _resolve_subsets(
    db: Session, dataset_version_id: uuid.UUID, subsets: list[str] | None
) -> list[str]:
    """缺省选 primary，并拒绝重复或不属于当前版本的 subset。"""
    selected = list(subsets) if subsets else _primary_subset_names(db, dataset_version_id)
    if not selected:
        return ["all"]
    if len(selected) != len(set(selected)):
        raise ApiError("E_VALIDATION", "Subset 不能重复选择", status_code=422)
    if selected == ["all"]:
        return selected
    names = set(
        db.scalars(
            select(DatasetSubset.name).where(DatasetSubset.dataset_version_id == dataset_version_id)
        ).all()
    )
    missing = sorted(set(selected) - names)
    if missing:
        raise ApiError("E_NOT_FOUND", f"Subset 不存在：{', '.join(missing)}", status_code=404)
    return selected


def validate_benchmark_definition(
    sampling: dict[str, Any], bucket_rules: dict[str, Any]
) -> list[str]:
    """检查采样与分桶配置，错误路径可直接显示在编辑表单。"""
    errors: list[str] = []
    mode = sampling.get("mode", "all")
    if not isinstance(mode, str) or mode not in {"all", "random", "stratified"}:
        errors.append(f"sampling.mode: 未知采样模式 {mode}")
    order = sampling.get("order", "seed_hash")
    if not isinstance(order, str) or order not in {"seed_hash", "item_key"}:
        errors.append(f"sampling.order: 非法值 {order}")
    sampling_filter = sampling.get("filter")
    if sampling_filter is not None:
        if not isinstance(sampling_filter, dict):
            errors.append("sampling.filter: 必须是对象")
        else:
            unknown = set(sampling_filter) - {"speakers", "item_key_prefix"}
            if unknown:
                errors.append(f"sampling.filter: 未知配置项 {sorted(unknown)}")
            speakers = sampling_filter.get("speakers")
            if speakers is not None and (
                not isinstance(speakers, list)
                or not all(isinstance(value, str) and value for value in speakers)
            ):
                errors.append("sampling.filter.speakers: 必须是字符串数组")
            prefix = sampling_filter.get("item_key_prefix")
            if prefix is not None and not isinstance(prefix, str):
                errors.append("sampling.filter.item_key_prefix: 必须是字符串")
    if mode == "random":
        count = sampling.get("n", 1000)
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            errors.append("sampling.n: 必须是正整数")
    if mode == "stratified":
        dim = sampling.get("dim", "duration_bin")
        if not isinstance(dim, str) or dim not in BUCKET_DIMS:
            errors.append(f"sampling.dim: 未注册的分层维度 {dim}")
        count = sampling.get("per_stratum", 30)
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            errors.append("sampling.per_stratum: 必须是正整数")

    dims = bucket_rules.get("dims", [])
    if not isinstance(dims, list) or not all(isinstance(dim, str) for dim in dims):
        errors.append("bucket_rules.dims: 必须是维度名称数组")
    else:
        if len(dims) != len(set(dims)):
            errors.append("bucket_rules.dims: 维度不能重复")
        for dim in dims:
            if dim not in BUCKET_DIMS:
                errors.append(f"bucket_rules.dims: 未注册维度 {dim}")
    return errors


def _duration_bin(duration_ms: int) -> str:
    if duration_ms < 5_000:
        return "<5s"
    if duration_ms < 15_000:
        return "5~15s"
    if duration_ms < 60_000:
        return "15~60s"
    if duration_ms < 300_000:
        return "1~5min"
    return ">5min"


def _ratio_bin(value: float) -> str:
    if value <= 0:
        return "0"
    if value < 0.1:
        return "0~10%"
    if value < 0.3:
        return "10~30%"
    return ">30%"


def dimension_value(
    dim: str,
    buckets: dict[str, Any],
    duration_ms: int,
    speakers: list[str],
    split: str,
    ref_text: str | None,
) -> str:
    """按 M3 注册表从 item 快照中取静态或派生分桶值。"""
    if dim == "duration_bin":
        return _duration_bin(duration_ms)
    if dim == "text_len_bin":
        length = len(ref_text or "")
        return (
            "<10"
            if length < 10
            else "10~30"
            if length < 30
            else "30~100"
            if length < 100
            else ">100"
        )
    if dim == "n_speakers":
        return str(len(speakers))
    if dim == "overlap_bin":
        value = buckets.get("overlap_ratio")
        if isinstance(value, int | float) and not isinstance(value, bool):
            return _ratio_bin(float(value))
        stored = buckets.get("overlap_bin")
        return str(stored) if stored is not None else "unknown"
    if dim == "lang":
        # 导入器历史上写 buckets.language；lang 维度需要同时兼容两种键。
        value = buckets.get("lang", buckets.get("language"))
        return str(value) if value is not None else "unknown"
    value = split if dim == "split" else buckets.get(dim)
    return str(value) if value is not None else "unknown"


def profile_dict(profile: MetricProfile) -> dict[str, Any]:
    return {
        "id": str(profile.id),
        "name": profile.name,
        "params": profile.params,
        "evaluator": (profile.params or {}).get("evaluator", "custom"),
        "evaluator_locked": is_locked(profile.params or {}),
        "profile_hash": profile.profile_hash,
        "created_at": profile.created_at.isoformat(),
        "version": profile.version,
        "updated_by": profile.updated_by,
        "updated_at": profile.updated_at.isoformat() if profile.updated_at else None,
    }


def benchmark_dict(db: Session, benchmark: Benchmark) -> dict[str, Any]:
    version = db.get(DatasetVersion, benchmark.dataset_version_id)
    profile = db.get(MetricProfile, benchmark.metric_profile_id)
    return {
        "id": str(benchmark.id),
        "name": benchmark.name,
        "dataset_version_id": str(benchmark.dataset_version_id),
        "dataset_version": version.version if version else None,
        "item_count": version.item_count if version else 0,
        "dataset_duration_ms": version.total_duration_ms if version else 0,
        "metric_profile_id": str(benchmark.metric_profile_id),
        "metric_profile": profile.name if profile else None,
        "metric_profile_version": profile.version if profile else None,
        "profile_hash": profile.profile_hash if profile else None,
        "subsets": benchmark.subsets or [],
        "sampling": benchmark.sampling,
        "params": benchmark.params,
        "bucket_rules": benchmark.bucket_rules,
        "version_hash": benchmark.version_hash,
        "archived": bool(benchmark.archived),
        "created_at": benchmark.created_at.isoformat(),
        "version": benchmark.version,
        "updated_by": benchmark.updated_by,
        "updated_at": benchmark.updated_at.isoformat() if benchmark.updated_at else None,
    }


def delete_metric_profile(db: Session, profile_id: uuid.UUID, *, actor: str) -> None:
    """删除未被 Benchmark 引用的口径版本（M3 §9）。"""
    profile = db.scalar(
        select(MetricProfile).where(MetricProfile.id == profile_id).with_for_update()
    )
    if profile is None:
        raise ApiError("E_NOT_FOUND", "口径不存在", status_code=404)
    benchmark_count = db.scalar(
        select(func.count()).select_from(Benchmark).where(Benchmark.metric_profile_id == profile.id)
    )
    if benchmark_count:
        raise ApiError(
            "E_METRIC_PROFILE_IN_USE",
            "口径已被 Benchmark 引用，不能删除；可创建新版本后归档旧版本",
            status_code=409,
            detail={"benchmark_count": int(benchmark_count)},
        )
    audit.record(
        db,
        "metric_profile.delete",
        actor=actor,
        resource=f"metric_profile:{profile.id}",
        detail={"name": profile.name, "version": profile.version},
    )
    db.delete(profile)
    db.commit()


def delete_benchmark(db: Session, benchmark_id: uuid.UUID, *, actor: str) -> None:
    """删除未被 Run 引用的基准；归档只影响新 Run，删除则彻底移除定义（M3 §9）。"""
    benchmark = db.scalar(select(Benchmark).where(Benchmark.id == benchmark_id).with_for_update())
    if benchmark is None:
        raise ApiError("E_NOT_FOUND", "Benchmark 不存在", status_code=404)
    run_count = db.scalar(
        select(func.count()).select_from(Run).where(Run.benchmark_id == benchmark.id)
    )
    if run_count:
        raise ApiError(
            "E_BENCHMARK_IN_USE",
            "Benchmark 已被 Run 引用，不能删除；可使用归档",
            status_code=409,
            detail={"run_count": int(run_count)},
        )
    audit.record(
        db,
        "benchmark.delete",
        actor=actor,
        resource=f"benchmark:{benchmark.id}",
        detail={"name": benchmark.name, "dataset_version_id": str(benchmark.dataset_version_id)},
    )
    db.delete(benchmark)
    db.commit()


def bucket_coverage(
    db: Session,
    *,
    dataset_version_id: uuid.UUID,
    subsets: list[str] | None = None,
) -> dict[str, Any]:
    """统计版本（或指定子集）内每个分桶维度的有值覆盖率。

    预览里出现 unknown 说明该维度在数据中缺失；前端据此自动勾选可用维度。
    返回 `known/total/coverage/distinct`，不返回完整值分布，避免 speaker 等
    高基数字段把响应撑大。
    """
    resolved = [str(name) for name in (subsets or [])] or ["all"]
    statement = select(
        Item.buckets,
        Item.duration_ms,
        Item.speakers,
        Item.split,
        RefText.text,
    ).outerjoin(RefText, and_(RefText.item_id == Item.id, RefText.kind == "verbatim"))
    if "all" in resolved:
        statement = statement.where(Item.dataset_version_id == dataset_version_id)
    else:
        subset_rows = db.execute(
            select(DatasetSubset.name, DatasetSubset.id).where(
                DatasetSubset.dataset_version_id == dataset_version_id,
                DatasetSubset.name.in_(resolved),
            )
        ).all()
        subset_by_name = {str(name): subset_id for name, subset_id in subset_rows}
        missing = sorted(set(resolved) - set(subset_by_name))
        if missing:
            raise ApiError("E_NOT_FOUND", f"subset 不存在: {', '.join(missing)}", status_code=404)
        statement = statement.where(Item.subset_id.in_(subset_by_name.values()))

    rows = db.execute(statement).all()
    counts: dict[str, dict[str, int]] = {dim: {} for dim in BUCKET_DIMS}
    for buckets, duration_ms, speakers, split, ref_text in rows:
        for dim in BUCKET_DIMS:
            value = dimension_value(
                dim,
                cast(dict[str, Any], buckets or {}),
                int(duration_ms or 0),
                cast(list[str], speakers or []),
                str(split or "all"),
                str(ref_text) if ref_text is not None else None,
            )
            counts[dim][value] = counts[dim].get(value, 0) + 1

    total = len(rows)
    items = []
    for key, spec in BUCKET_DIMS.items():
        values = counts[key]
        known = total - values.get("unknown", 0)
        items.append(
            {
                "key": key,
                "kind": spec.kind,
                "known": known,
                "total": total,
                "coverage": (known / total) if total else 0.0,
                "distinct": len([value for value in values if value != "unknown"]),
            }
        )
    return {
        "dataset_version_id": str(dataset_version_id),
        "subsets": resolved,
        "total": total,
        "items": items,
    }


def sampled_item_ids(db: Session, benchmark: Benchmark, limit: int | None = None) -> list[int]:
    """按 Benchmark.sampling 选样本；结果只依赖内容，跨环境可复现。

    采样在每个 subset 内独立执行、同一规则（M3 §5.3），
    避免 phone/mic 条数不同导致比例失衡；subsets 为 ["all"] 时取整个版本。
    """
    sampling = cast(dict[str, Any], benchmark.sampling or {})
    mode = str(sampling.get("mode", "all"))
    seed = str(sampling.get("seed", 42))
    order = str(sampling.get("order", "seed_hash"))
    sampling_filter = sampling.get("filter")
    sampling_filter = sampling_filter if isinstance(sampling_filter, dict) else {}

    def order_key(content_uid: str) -> str:
        # 按哈希排序取前 n：与库内顺序无关，同 seed 跨环境可复现（§5.3 random）
        return hashlib.sha256(f"{seed}|{content_uid}".encode()).hexdigest()

    SampleRow = tuple[int, str, dict[str, Any], int, list[str], str, str | None, str]

    def matches(row: SampleRow) -> bool:
        """手工过滤：说话人交集、item_key 前缀；用于复现历史固定测试集。"""
        speakers = sampling_filter.get("speakers")
        if isinstance(speakers, list) and speakers:
            allowed = {str(value) for value in speakers}
            if not allowed.intersection(str(value) for value in row[4]):
                return False
        prefix = sampling_filter.get("item_key_prefix")
        if isinstance(prefix, str) and prefix:
            return row[7].startswith(prefix)
        return True

    def sorted_rows(rows: list[SampleRow]) -> list[SampleRow]:
        """order=item_key 复现“按文件名排序取前 N 条”；默认仍用 seed 哈希。"""
        if order == "item_key":
            return sorted(rows, key=lambda row: row[7])
        return sorted(rows, key=lambda row: order_key(row[1]))

    def apply(rows: list[SampleRow]) -> list[int]:
        rows = [row for row in rows if matches(row)]
        if mode == "all":
            return [row[0] for row in rows]
        if mode == "random":
            n = int(str(sampling.get("n", 1000)))
            return [row[0] for row in sorted_rows(rows)[:n]]
        if mode == "stratified":
            dim = str(sampling.get("dim", "split"))
            per_stratum = int(str(sampling.get("per_stratum", 30)))
            groups: dict[str, list[SampleRow]] = {}
            for row in rows:
                key = dimension_value(dim, row[2], row[3], row[4], row[5], row[6])
                groups.setdefault(key, []).append(row)
            selected: list[int] = []
            for key in sorted(groups):
                ordered = sorted_rows(groups[key])
                selected.extend(row[0] for row in ordered[:per_stratum])
            return selected
        raise ApiError("E_SEMANTIC", f"未知采样模式 {mode}", status_code=422)

    subsets = [str(s) for s in (benchmark.subsets or [])] or ["all"]
    if subsets == ["all"]:
        raw_rows = db.execute(
            select(
                Item.id,
                Item.content_uid,
                Item.buckets,
                Item.duration_ms,
                Item.speakers,
                Item.split,
                RefText.text,
                Item.item_key,
            )
            .outerjoin(RefText, and_(RefText.item_id == Item.id, RefText.kind == "verbatim"))
            .where(Item.dataset_version_id == benchmark.dataset_version_id)
            .order_by(Item.id)
        ).all()
        rows: list[SampleRow] = [
            (
                int(row[0]),
                str(row[1]),
                cast(dict[str, Any], row[2] or {}),
                int(row[3] or 0),
                cast(list[str], row[4] or []),
                str(row[5] or "all"),
                str(row[6]) if row[6] is not None else None,
                str(row[7]),
            )
            for row in raw_rows
        ]
        if limit:
            rows = rows[:limit]
        return apply(rows)

    # 命名 subset：逐个解析，各自独立采样后合并
    subset_rows = db.execute(
        select(DatasetSubset.name, DatasetSubset.id).where(
            DatasetSubset.dataset_version_id == benchmark.dataset_version_id,
            DatasetSubset.name.in_(subsets),
        )
    ).all()
    subset_by_name = {str(row[0]): row[1] for row in subset_rows}
    if len(subset_by_name) != len(set(subsets)):
        missing = sorted(set(subsets) - set(subset_by_name))
        raise ApiError("E_NOT_FOUND", f"subset 不存在: {', '.join(missing)}", status_code=404)

    selected: list[int] = []
    for subset_name in subsets:
        subset_id = subset_by_name[subset_name]
        raw_rows = db.execute(
            select(
                Item.id,
                Item.content_uid,
                Item.buckets,
                Item.duration_ms,
                Item.speakers,
                Item.split,
                RefText.text,
                Item.item_key,
            )
            .outerjoin(RefText, and_(RefText.item_id == Item.id, RefText.kind == "verbatim"))
            .where(Item.subset_id == subset_id)
            .order_by(Item.id)
        ).all()
        rows = [
            (
                int(row[0]),
                str(row[1]),
                cast(dict[str, Any], row[2] or {}),
                int(row[3] or 0),
                cast(list[str], row[4] or []),
                str(row[5] or "all"),
                str(row[6]) if row[6] is not None else None,
                str(row[7]),
            )
            for row in raw_rows
        ]
        if limit:
            # limit 只用于冒烟：每个 subset 内截断
            rows = rows[: max(limit // len(subsets), 1)]
        selected.extend(apply(rows))
    return selected


def preview_benchmark(
    db: Session,
    *,
    dataset_version_id: uuid.UUID,
    metric_profile_id: uuid.UUID,
    subsets: list[str] | None,
    sampling: dict[str, Any] | None,
    bucket_rules: dict[str, Any] | None,
    params: dict[str, Any] | None,
    deployment_ids: list[uuid.UUID],
) -> dict[str, Any]:
    """只读预览采样规模、分桶和各部署的指标可用性。"""
    version = db.get(DatasetVersion, dataset_version_id)
    if version is None:
        raise ApiError("E_NOT_FOUND", "数据集版本不存在", status_code=404)
    if version.status not in {"ready", "ready_with_errors"}:
        raise ApiError("E_CONFLICT", "数据集版本未就绪", status_code=409)
    profile = db.get(MetricProfile, metric_profile_id)
    if profile is None:
        raise ApiError("E_NOT_FOUND", "口径不存在", status_code=404)

    selected_subsets = _resolve_subsets(db, version.id, subsets)
    sampling_config = sampling or {"mode": "all"}
    bucket_config = bucket_rules or {"dims": []}
    params_config = params or {}
    errors = validate_benchmark_definition(sampling_config, bucket_config)
    if errors:
        raise ApiError("E_VALIDATION", "基准参数非法", status_code=422, detail={"errors": errors})
    profile_errors = validate_params(cast(dict[str, Any], profile.params or {}))
    if profile_errors:
        raise ApiError(
            "E_VALIDATION", "口径参数非法", status_code=422, detail={"errors": profile_errors}
        )

    subset_rows = db.scalars(
        select(DatasetSubset).where(
            DatasetSubset.dataset_version_id == version.id,
            DatasetSubset.name.in_(selected_subsets),
        )
    ).all()
    subset_by_name = {item.name: item for item in subset_rows}
    subset_previews: list[dict[str, Any]] = []
    for subset_name in selected_subsets:
        temporary = Benchmark(
            name="预览",
            dataset_version_id=version.id,
            metric_profile_id=profile.id,
            subsets=[subset_name],
            sampling=sampling_config,
            params=params_config,
            bucket_rules=bucket_config,
        )
        item_ids = sampled_item_ids(db, temporary)
        item_rows = (
            db.execute(
                select(
                    Item.duration_ms,
                    Item.buckets,
                    Item.speakers,
                    Item.split,
                    RefText.text,
                )
                .outerjoin(RefText, and_(RefText.item_id == Item.id, RefText.kind == "verbatim"))
                .where(Item.id.in_(item_ids))
            ).all()
            if item_ids
            else []
        )
        dims = cast(list[str], bucket_config.get("dims", []))
        bucket_counts: dict[str, dict[str, int]] = {dim: {} for dim in dims}
        durations: list[int] = []
        stratum_counts: dict[str, int] = {}
        for row in item_rows:
            duration = int(row[0] or 0)
            buckets = cast(dict[str, Any], row[1] or {})
            speakers = cast(list[str], row[2] or [])
            split = str(row[3] or "all")
            ref_text = str(row[4]) if row[4] is not None else None
            durations.append(duration)
            for dim in dims:
                value = dimension_value(dim, buckets, duration, speakers, split, ref_text)
                bucket_counts[dim][value] = bucket_counts[dim].get(value, 0) + 1
            if sampling_config.get("mode") == "stratified":
                dim = str(sampling_config.get("dim", "duration_bin"))
                value = dimension_value(dim, buckets, duration, speakers, split, ref_text)
                stratum_counts[value] = stratum_counts.get(value, 0) + 1

        subset = None if subset_name == "all" else subset_by_name.get(subset_name)
        source_statement = select(func.count(), func.coalesce(func.sum(Item.duration_ms), 0)).where(
            Item.dataset_version_id == version.id
        )
        if subset is not None:
            source_statement = source_statement.where(Item.subset_id == subset.id)
        if subset_name == "all":
            source_statement = source_statement.where(Item.dataset_version_id == version.id)
        source_count, source_duration = db.execute(source_statement).one()
        source_count = int(source_count or 0)
        source_duration = int(source_duration or 0)
        warnings = [
            {
                "code": "stratum_short",
                "dimension": str(sampling_config.get("dim", "duration_bin")),
                "value": value,
                "selected": count,
            }
            for value, count in sorted(stratum_counts.items())
            if sampling_config.get("mode") == "stratified"
            and count < int(sampling_config.get("per_stratum", 30))
        ]
        gaps = []
        if not item_rows:
            gaps.append("subset_empty")
        subset_caps = subset.capabilities if subset else version.capabilities
        if not (subset_caps or {}).get("text_ratio"):
            gaps.append("reference_text_missing_or_unknown")
        for dim, counts in bucket_counts.items():
            if counts.get("unknown"):
                gaps.append(f"bucket_missing:{dim}")
        subset_previews.append(
            {
                "name": subset_name,
                "source_count": source_count,
                "source_duration_ms": source_duration,
                "selected_count": len(item_rows),
                "selected_duration_ms": sum(durations),
                "buckets": bucket_counts,
                "warnings": warnings,
                "gaps": gaps,
            }
        )

    temporary_for_hash = Benchmark(
        name="预览",
        dataset_version_id=version.id,
        metric_profile_id=profile.id,
        subsets=selected_subsets,
        sampling=sampling_config,
        params=params_config,
        bucket_rules=bucket_config,
    )
    temporary_for_hash.version_hash = compute_version_hash(
        db,
        dataset_version_id=version.id,
        subsets=selected_subsets,
        profile_hash=profile.profile_hash,
        sampling=sampling_config,
        bucket_rules=bucket_config,
        params=params_config,
    )
    availability_by_device = [
        availability(db, temporary_for_hash, deployment_id) for deployment_id in deployment_ids
    ]
    return {
        "dataset_version_id": str(version.id),
        "metric_profile_id": str(profile.id),
        "profile_hash": profile.profile_hash,
        "version_hash": temporary_for_hash.version_hash,
        "sampling": sampling_config,
        "subsets": subset_previews,
        "selected_count": sum(item["selected_count"] for item in subset_previews),
        "selected_duration_ms": sum(item["selected_duration_ms"] for item in subset_previews),
        "availability": availability_by_device,
    }


def availability(
    db: Session, benchmark: Benchmark, deployment_id: uuid.UUID | None = None
) -> dict[str, Any]:
    """逐 subset 解析可评指标，并保留总体覆盖状态。"""
    version = db.get(DatasetVersion, benchmark.dataset_version_id)
    if version is None:
        raise ApiError("E_NOT_FOUND", "数据集版本不存在", status_code=404)
    deployment = (
        db.get(Deployment, deployment_id)
        if deployment_id
        else db.scalar(select(Deployment).order_by(Deployment.captured_at.desc()).limit(1))
    )
    if deployment_id and deployment is None:
        raise ApiError("E_NOT_FOUND", "部署不存在", status_code=404)

    caps = cast(dict[str, Any], (deployment.capabilities or {}) if deployment else {})
    profile = db.get(MetricProfile, benchmark.metric_profile_id)
    if profile is None:
        raise ApiError("E_NOT_FOUND", "口径不存在", status_code=404)
    selected_subsets = [str(value) for value in (benchmark.subsets or [])] or ["all"]
    if selected_subsets == ["all"]:
        subset_data = [("all", version.capabilities or {}, version.item_count)]
    else:
        rows = db.scalars(
            select(DatasetSubset).where(
                DatasetSubset.dataset_version_id == version.id,
                DatasetSubset.name.in_(selected_subsets),
            )
        ).all()
        by_name = {row.name: row for row in rows}
        subset_data = [
            (
                name,
                by_name[name].capabilities or version.capabilities or {},
                by_name[name].item_count,
            )
            for name in selected_subsets
            if name in by_name
        ]
        if len(subset_data) != len(selected_subsets):
            raise ApiError("E_NOT_FOUND", "Benchmark 引用的 Subset 已不存在", status_code=409)
    resolved: list[dict[str, Any]] = [
        {
            "name": name,
            "item_count": item_count,
            "metrics": resolve_metrics(
                cast(dict[str, Any], capabilities),
                caps,
                cast(dict[str, Any], profile.params or {}),
                cast(dict[str, Any], benchmark.params or {}),
            ),
        }
        for name, capabilities, item_count in subset_data
    ]
    metric_names = [str(item["name"]) for item in (resolved[0]["metrics"] if resolved else [])]
    aggregate: list[dict[str, Any]] = []
    for name in metric_names:
        values = [
            next(metric for metric in item["metrics"] if metric["name"] == name)
            for item in resolved
        ]
        statuses = {str(item["status"]) for item in values}
        coverage_values = [
            (float(item["coverage"]), int(subset["item_count"]))
            for subset, item in zip(resolved, values, strict=True)
            if isinstance(item.get("coverage"), int | float)
        ]
        covered_count = sum(count for _, count in coverage_values)
        coverage = (
            sum(value * count for value, count in coverage_values) / covered_count
            if covered_count
            else None
        )
        if statuses == {"ok"}:
            status, reason = "ok", None
        elif statuses == {"na"}:
            status = "na"
            reasons = {str(item["reason"]) for item in values}
            reason = next(iter(reasons)) if len(reasons) == 1 else "subset_unavailable"
        else:
            status, reason = "partial", "subset_coverage_differs"
        aggregate.append({"name": name, "status": status, "reason": reason, "coverage": coverage})
    return {
        "benchmark_id": str(benchmark.id),
        "deployment_id": str(deployment.id) if deployment else None,
        "metrics": aggregate,
        "subsets": resolved,
    }
