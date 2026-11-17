"""Run 读数带：CI、相对基线的 Δ/p、排名与矩阵下钻数据（M6 §5.5 / M8 §9.4 / v2 横评）。

为什么单独模块：显著性计算要重采样，不该塞进列表接口；
前端只消费结果，不自己算统计量（M8 §9.7 同一原则）。
"""

import random
import statistics
import uuid
from typing import Any
from typing import cast as tcast

from sqlalchemy import Float, Integer, and_, cast, func, or_, select
from sqlalchemy.orm import Session

from app.domain.benchmarks import dimension_value
from app.domain.compare import _bh_adjust, _bootstrap_delta
from app.domain.models import (
    CaseResult,
    CaseScore,
    Device,
    Evaluation,
    Item,
    RefText,
    Run,
    RunTarget,
)
from app.scoring.bootstrap import f1_ci, ratio_ci

READOUT_BUCKET_DIMS = {
    "duration_bucket",
    "lang",
    "dialect",
    "scene",
    "gender",
    "split",
    "source",
    "channel",
    "n_speakers",
    "overlap_bin",
    "text_len_bin",
}


def _case_units(
    db: Session,
    evaluation_id: uuid.UUID,
    target_id: uuid.UUID | None = None,
    *,
    dim: str = "duration_bucket",
) -> list[tuple[str, int, int, str]]:
    """(content_uid, edits, ref_len, bucket)；用于 bootstrap 与配对。"""
    s_expr = func.coalesce(
        cast(CaseScore.metrics["cer"]["s"].astext, Integer),
        cast(CaseScore.metrics["edits"].astext, Integer),
    )
    d_expr = func.coalesce(cast(CaseScore.metrics["cer"]["d"].astext, Integer), 0)
    i_expr = func.coalesce(cast(CaseScore.metrics["cer"]["i"].astext, Integer), 0)
    n_expr = func.coalesce(
        cast(CaseScore.metrics["cer"]["n"].astext, Integer),
        cast(CaseScore.metrics["ref_len"].astext, Integer),
    )
    stmt = (
        select(
            Item.content_uid,
            s_expr,
            d_expr,
            i_expr,
            n_expr,
            Item.duration_ms,
            Item.lang,
            Item.split,
            Item.buckets,
            Item.speakers,
            RefText.text,
        )
        .join(CaseResult, CaseResult.item_id == Item.id)
        .join(CaseScore, CaseScore.case_result_id == CaseResult.id)
        .outerjoin(
            RefText,
            and_(RefText.item_id == Item.id, RefText.kind == "verbatim"),
        )
        .where(CaseScore.evaluation_id == evaluation_id)
    )
    if target_id is not None:
        stmt = stmt.where(CaseResult.run_target_id == target_id)
    rows = db.execute(stmt).all()
    result: list[tuple[str, int, int, str]] = []
    for uid, s, d, i, n, duration, lang, split, buckets, speakers, ref_text in rows:
        mapped_dim = "duration_bin" if dim == "duration_bucket" else dim
        bucket_data = dict(buckets or {})
        if (
            mapped_dim == "lang"
            and "lang" not in bucket_data
            and "language" not in bucket_data
            and lang
        ):
            bucket_data["lang"] = lang
        bucket = dimension_value(
            mapped_dim,
            bucket_data,
            int(duration),
            list(speakers or []),
            str(split or "all"),
            str(ref_text) if ref_text is not None else None,
        )
        result.append((str(uid), int(s or 0) + int(d or 0) + int(i or 0), int(n or 0), bucket))
    return result


def _bucket_metrics(db: Session, evaluation_id: uuid.UUID, dim: str) -> list[dict[str, Any]]:
    """按分桶维度汇总 CER 之外的指标；同一套 case_score 明细，不读旧报告。"""
    statement = (
        select(
            CaseResult,
            CaseScore,
            Item.duration_ms,
            Item.buckets,
            Item.speakers,
            Item.split,
            Item.lang,
            RefText.text,
        )
        .join(CaseScore, CaseScore.case_result_id == CaseResult.id)
        .join(Item, Item.id == CaseResult.item_id)
        .outerjoin(RefText, and_(RefText.item_id == Item.id, RefText.kind == "verbatim"))
        .where(CaseScore.evaluation_id == evaluation_id)
    )
    buckets: dict[str, dict[str, Any]] = {}
    for case, score, duration, item_buckets, speakers, split, lang, ref_text in db.execute(
        statement
    ).all():
        bucket_data = dict(item_buckets or {})
        if dim == "lang" and "lang" not in bucket_data and "language" not in bucket_data and lang:
            bucket_data["lang"] = lang
        bucket = dimension_value(
            "duration_bin" if dim == "duration_bucket" else dim,
            bucket_data,
            int(duration),
            list(speakers or []),
            str(split or "all"),
            str(ref_text) if ref_text is not None else None,
        )
        acc: dict[str, Any] = buckets.setdefault(
            bucket,
            {
                "cer": [0, 0, 0, 0, []],
                "numeric": [0, 0, 0, 0],
                "der": [0, 0, 0, 0, []],
                "sa_der": [0, 0, 0, 0, []],
                "punct": [0, 0, 0, []],
                "naming": [0, 0],
                "itn": [0, 0],
                "rtf_dev": [],
                "rtf_e2e": [],
                "ttft": [],
                "finalize": [],
                "reasons": {},
            },
        )
        case_metrics = score.metrics or {}
        for reason_key in (
            "cer",
            "numeric_cer",
            "der",
            "sa_der",
            "punct_f1",
            "naming_acc",
            "itn_acc",
        ):
            reason_metric = case_metrics.get(reason_key)
            if isinstance(reason_metric, dict) and reason_metric.get("status") != "ok":
                reason_value = reason_metric.get("reason")
                if isinstance(reason_value, str):
                    acc["reasons"].setdefault(reason_key, reason_value)
        cer = case_metrics.get("cer")
        if isinstance(cer, dict) and cer.get("status") == "ok":
            acc["cer"][0] += int(cer.get("s", 0)) + int(cer.get("d", 0)) + int(cer.get("i", 0))
            acc["cer"][3] += int(cer.get("n", 0))
            acc["cer"][4].append(
                (
                    int(cer.get("s", 0)) + int(cer.get("d", 0)) + int(cer.get("i", 0)),
                    int(cer.get("n", 0)),
                )
            )
        numeric = case_metrics.get("numeric_cer")
        if isinstance(numeric, dict) and numeric.get("status") == "ok":
            acc["numeric"][0] += (
                int(numeric.get("s", 0)) + int(numeric.get("d", 0)) + int(numeric.get("i", 0))
            )
            acc["numeric"][3] += int(numeric.get("n", 0))
        for key, target in (("der", "der"), ("sa_der", "sa_der")):
            metric = case_metrics.get(key)
            if isinstance(metric, dict) and metric.get("status") == "ok":
                miss = int(metric.get("miss_frames", 0))
                fa = int(metric.get("fa_frames", 0))
                conf = int(metric.get("conf_frames", 0))
                ref = int(metric.get("ref_frames", 0))
                acc[target][0] += miss
                acc[target][1] += fa
                acc[target][2] += conf
                acc[target][3] += ref
                acc[target][4].append((miss + fa + conf, ref))
        punct = case_metrics.get("punct_f1")
        if isinstance(punct, dict) and punct.get("status") == "ok":
            acc["punct"][0] += int(punct.get("tp", 0))
            acc["punct"][1] += int(punct.get("fp", 0))
            acc["punct"][2] += int(punct.get("fn", 0))
            acc["punct"][3].append(
                (int(punct.get("tp", 0)), int(punct.get("fp", 0)), int(punct.get("fn", 0)))
            )
        naming = case_metrics.get("naming_acc")
        if isinstance(naming, dict) and naming.get("status") == "ok":
            acc["naming"][0] += int(naming.get("correct_count", 0))
            acc["naming"][1] += int(naming.get("n", 0))
        itn = case_metrics.get("itn_acc")
        if isinstance(itn, dict) and itn.get("status") == "ok":
            acc["itn"][0] += int(itn.get("correct", 0))
            acc["itn"][1] += int(itn.get("n", 0))
        for key, field in (
            ("rtf_dev", case.rtf_dev),
            ("rtf_e2e", case.rtf_e2e),
            ("ttft", case.ttft_ms),
            ("finalize", case.finalize_ms),
        ):
            if isinstance(field, int | float) and not isinstance(field, bool):
                acc[key].append(float(field))

    def ratio(values: tuple[int, int]) -> tuple[float | None, tuple[float | None, float | None]]:
        numerator, denominator = values
        value = numerator / denominator if denominator else None
        return value, ratio_ci([(numerator, denominator)], n_resample=200) if denominator else (
            None,
            None,
        )

    result: list[dict[str, Any]] = []
    for bucket, acc in sorted(buckets.items()):
        metric_rows: list[dict[str, Any]] = []
        cer_value, cer_ci = ratio((acc["cer"][0], acc["cer"][3]))
        metric_rows.append(
            {
                "metric": "cer",
                "value": cer_value,
                "n": acc["cer"][3],
                "status": "ok" if cer_value is not None else "na",
                "reason": None
                if cer_value is not None
                else acc["reasons"].get("cer", "no_reference_text"),
                "ci_low": cer_ci[0],
                "ci_high": cer_ci[1],
            }
        )
        numeric_value, numeric_ci = ratio((acc["numeric"][0], acc["numeric"][3]))
        metric_rows.append(
            {
                "metric": "numeric_cer",
                "value": numeric_value,
                "n": acc["numeric"][3],
                "status": "ok" if numeric_value is not None else "na",
                "reason": None
                if numeric_value is not None
                else acc["reasons"].get("numeric_cer", "no_numeric_reference"),
                "ci_low": numeric_ci[0],
                "ci_high": numeric_ci[1],
            }
        )
        for key in ("der", "sa_der"):
            miss, fa, conf, ref, pairs = acc[key]
            value = (miss + fa + conf) / ref if ref else None
            metric_rows.append(
                {
                    "metric": key,
                    "value": value,
                    "n": ref,
                    "status": "ok" if value is not None else "na",
                    "reason": None
                    if value is not None
                    else acc["reasons"].get(key, "no_segment_timeline"),
                    "ci_low": ratio_ci(pairs, n_resample=200)[0] if pairs else None,
                    "ci_high": ratio_ci(pairs, n_resample=200)[1] if pairs else None,
                }
            )
        tp, fp, fn, pairs = acc["punct"]
        f1 = (2 * tp / (2 * tp + fp + fn)) if (2 * tp + fp + fn) else None
        metric_rows.append(
            {
                "metric": "punct_f1",
                "value": f1,
                "n": len(pairs),
                "status": "ok" if f1 is not None else "na",
                "reason": None
                if f1 is not None
                else acc["reasons"].get("punct_f1", "no_sentence_punctuation"),
                "ci_low": f1_ci(pairs)[0] if pairs else None,
                "ci_high": f1_ci(pairs)[1] if pairs else None,
            }
        )
        correct, total = acc["naming"]
        naming = correct / total if total else None
        metric_rows.append(
            {
                "metric": "naming_acc",
                "value": naming,
                "n": total,
                "status": "ok" if naming is not None else "na",
                "reason": None
                if naming is not None
                else acc["reasons"].get("naming_acc", "no_voiceprint_match"),
            }
        )
        itn_correct, itn_total = acc["itn"]
        itn_value = itn_correct / itn_total if itn_total else None
        if itn_total:
            metric_rows.append(
                {
                    "metric": "itn_acc",
                    "value": itn_value,
                    "n": itn_total,
                    "status": "ok" if itn_value is not None else "na",
                    "reason": None,
                }
            )
        for key, label in (
            ("rtf_dev", "rtf_dev"),
            ("rtf_e2e", "rtf_e2e"),
            ("ttft", "ttft_ms"),
            ("finalize", "finalize_ms"),
        ):
            values = acc[key]
            metric_rows.append(
                {
                    "metric": label,
                    "value": statistics.median(values) if values else None,
                    "n": len(values),
                    "status": "ok" if values else "na",
                    "reason": None
                    if values
                    else (
                        "no_partial_timing"
                        if label == "ttft_ms"
                        else "no_realtime_timing"
                        if label == "finalize_ms"
                        else "no_inference_timing"
                    ),
                }
            )
        result.append({"bucket": {dim: bucket}, "metrics": metric_rows})
    return result


def _bootstrap_ci(
    units: list[tuple[str, int, int, str]], *, n_resample: int = 600, seed: int = 7
) -> tuple[float | None, float | None, float | None, int]:
    """单 Run 的 micro CER 与 95% CI（按 utterance 重采样，M5 §5.8）。"""
    if not units:
        return None, None, None, 0
    edits = sum(unit[1] for unit in units)
    ref_len = sum(unit[2] for unit in units)
    value = edits / ref_len if ref_len else None
    if value is None:
        return None, None, None, len(units)
    rng = random.Random(seed)  # noqa: S311 - 可复现的 bootstrap
    size = len(units)
    samples: list[float] = []
    for _ in range(n_resample):
        total_edits = total_len = 0
        for _ in range(size):
            _, e, n, _ = units[rng.randrange(size)]
            total_edits += e
            total_len += n
        if total_len:
            samples.append(total_edits / total_len)
    samples.sort()
    low = samples[int(len(samples) * 0.025)]
    high = samples[min(len(samples) - 1, int(len(samples) * 0.975))]
    return value, low, high, len(units)


def _previous_evaluation(db: Session, run: Run, current_uids: set[str]) -> Evaluation | None:
    """选共同样本最多的历史 Evaluation 作基线（M6 §5.2 回归对比）。

    为什么不是"最近一个"：最近一次可能是 10 条冒烟，共同样本太少，
    Δ 的置信区间会失去意义；按 content_uid 重叠取最大更符合"回归对比"。
    """
    candidates = db.scalars(
        select(Evaluation)
        .join(Run, Run.id == Evaluation.run_id)
        .where(
            Run.benchmark_id == run.benchmark_id,
            Run.id != run.id,
            Evaluation.status == "completed",
        )
        .order_by(Evaluation.created_at.desc())
        .limit(5)
    ).all()
    best: Evaluation | None = None
    best_overlap = 0
    for candidate in candidates:
        overlap = len(current_uids & {unit[0] for unit in _case_units(db, candidate.id)})
        if overlap > best_overlap:
            best, best_overlap = candidate, overlap
    return best


def _target_delta(
    db: Session,
    baseline: Evaluation,
    evaluation: Evaluation,
    target: RunTarget,
    *,
    dim: str,
) -> dict[str, Any] | None:
    """同设备 target 的配对 Δ（按 content_uid 配对）。"""
    baseline_target = db.scalar(
        select(RunTarget)
        .join(Run, Run.id == RunTarget.run_id)
        .where(Run.id == baseline.run_id, RunTarget.device_id == target.device_id)
    )
    if baseline_target is None:
        return None
    base_units = {
        unit[0]: unit for unit in _case_units(db, baseline.id, baseline_target.id, dim=dim)
    }
    cur_units = {unit[0]: unit for unit in _case_units(db, evaluation.id, target.id, dim=dim)}
    common = sorted(set(base_units) & set(cur_units))
    if not common:
        return None
    pairs = [
        (base_units[uid][1], base_units[uid][2], cur_units[uid][1], cur_units[uid][2])
        for uid in common
    ]
    result = _bootstrap_delta(pairs, n_resample=400)
    return {
        "delta": result["delta"],
        "ci_low": result["ci_low"],
        "ci_high": result["ci_high"],
        "p": result["p"],
        "n": result["n"],
        "significant": bool(
            result["p"] is not None
            and result["p"] < 0.05
            and result["ci_low"] is not None
            and (result["ci_low"] > 0 or result["ci_high"] < 0)
        ),
    }


def run_readout(
    db: Session,
    run: Run,
    *,
    dim: str = "duration_bucket",
    evaluation_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """横评读数：总体 CER+CI、Δ/p、排名与可选维度分桶。"""
    if dim not in READOUT_BUCKET_DIMS:
        raise ValueError(f"不支持的分桶维度: {dim}")
    evaluation_statement = select(Evaluation).where(Evaluation.run_id == run.id)
    if evaluation_id is not None:
        evaluation_statement = evaluation_statement.where(Evaluation.id == evaluation_id)
    else:
        evaluation_statement = evaluation_statement.order_by(Evaluation.created_at.desc())
    evaluation = db.scalar(evaluation_statement)
    targets = db.execute(
        select(RunTarget, Device)
        .join(Device, Device.id == RunTarget.device_id)
        .where(RunTarget.run_id == run.id)
        .order_by(RunTarget.id)
    ).all()

    if evaluation is None:
        return {
            "evaluation_id": None,
            "dim": dim,
            "cer": None,
            "ci_low": None,
            "ci_high": None,
            "n": 0,
            "baseline": None,
            "buckets": [],
            "bucket_metrics": [],
            "ranking": [],
        }

    units = _case_units(db, evaluation.id, dim=dim)
    value, ci_low, ci_high, n = _bootstrap_ci(units)

    baseline = _previous_evaluation(db, run, {unit[0] for unit in units})
    baseline_payload = None
    overall_delta = None
    if baseline is not None:
        base_units = {unit[0]: unit for unit in _case_units(db, baseline.id, dim=dim)}
        cur_units = {unit[0]: unit for unit in units}
        common = sorted(set(base_units) & set(cur_units))
        if common:
            pairs = [
                (
                    base_units[uid][1],
                    base_units[uid][2],
                    cur_units[uid][1],
                    cur_units[uid][2],
                )
                for uid in common
            ]
            overall_delta = _bootstrap_delta(pairs, n_resample=600)
            overall_delta["significant"] = bool(
                overall_delta["p"] is not None
                and overall_delta["p"] < 0.05
                and overall_delta["ci_low"] is not None
                and (overall_delta["ci_low"] > 0 or overall_delta["ci_high"] < 0)
            )
        baseline_payload = {
            "evaluation_id": str(baseline.id),
            "run_id": str(baseline.run_id),
            "created_at": baseline.created_at.isoformat(),
        }

    ranking: list[dict[str, Any]] = []
    for target, device in targets:
        target_units = _case_units(db, evaluation.id, target.id, dim=dim)
        t_value, t_low, t_high, t_n = _bootstrap_ci(target_units)
        ranking.append(
            {
                "target_id": str(target.id),
                "device_id": str(device.id),
                "device_name": device.name,
                "subset": target.subset,
                "status": target.status,
                "cer": t_value,
                "ci_low": t_low,
                "ci_high": t_high,
                "n": t_n,
                "delta": _target_delta(db, baseline, evaluation, target, dim=dim)
                if baseline is not None
                else None,
                "deployment_uid": run.snapshot.get("deployment_uid"),
            }
        )
    ranking.sort(key=lambda row: float(row["cer"]) if row["cer"] is not None else float("inf"))

    # 分桶：duration_bucket 的 CER 与相对基线 Δ（BH 校正，M6 D-M6-3）
    buckets = []
    bucket_names = sorted({unit[3] for unit in units})
    for bucket in bucket_names:
        bucket_units = [unit for unit in units if unit[3] == bucket]
        b_value, b_low, b_high, b_n = _bootstrap_ci(bucket_units, n_resample=300)
        entry: dict[str, Any] = {
            "bucket": {dim: bucket},
            "cer": b_value,
            "ci_low": b_low,
            "ci_high": b_high,
            "n": b_n,
            "delta": None,
            "p_bh": None,
            "significant_bh": False,
        }
        if baseline is not None:
            base_units = {
                unit[0]: unit for unit in _case_units(db, baseline.id, dim=dim) if unit[3] == bucket
            }
            cur_bucket_units = {unit[0]: unit for unit in bucket_units}
            common = sorted(set(base_units) & set(cur_bucket_units))
            if common:
                pairs = [
                    (
                        base_units[uid][1],
                        base_units[uid][2],
                        cur_bucket_units[uid][1],
                        cur_bucket_units[uid][2],
                    )
                    for uid in common
                ]
                delta = _bootstrap_delta(pairs, n_resample=300)
                entry["delta"] = delta["delta"]
                entry["ci_low_delta"] = delta["ci_low"]
                entry["ci_high_delta"] = delta["ci_high"]
                entry["p"] = delta["p"]
                entry["n_common"] = delta["n"]
                entry["significant"] = bool(
                    delta["p"] is not None
                    and delta["p"] < 0.05
                    and delta["ci_low"] is not None
                    and (delta["ci_low"] > 0 or delta["ci_high"] < 0)
                )
        buckets.append(entry)
    adjusted = _bh_adjust([float(entry["p"]) for entry in buckets if entry.get("p") is not None])
    iterator = iter(adjusted)
    for entry in buckets:
        if entry.get("p") is not None:
            entry["p_bh"] = next(iterator)
            entry["significant_bh"] = bool(
                entry["p_bh"] < 0.05
                and entry.get("ci_low_delta") is not None
                and (entry["ci_low_delta"] > 0 or entry.get("ci_high_delta", 0) < 0)
            )

    return {
        "evaluation_id": str(evaluation.id),
        "dim": dim,
        "cer": value,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "n": n,
        "baseline": baseline_payload,
        "delta": overall_delta,
        "buckets": buckets,
        "bucket_metrics": _bucket_metrics(db, evaluation.id, dim),
        "ranking": ranking,
    }


def target_cell(
    db: Session,
    run: Run,
    target: RunTarget,
    *,
    evaluation_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """矩阵单元格下钻（M8 §9.4：返回 CER 分布与最多 20 条最差样本）。"""
    evaluation_statement = select(Evaluation).where(Evaluation.run_id == run.id)
    if evaluation_id is not None:
        evaluation_statement = evaluation_statement.where(Evaluation.id == evaluation_id)
    else:
        evaluation_statement = evaluation_statement.order_by(Evaluation.created_at.desc())
    evaluation = db.scalar(evaluation_statement)
    if evaluation is None:
        return {"histogram": [], "worst": [], "n": 0, "cer": None}

    rows = db.execute(
        select(
            CaseScore.id,
            Item.item_key,
            RefText.text,
            CaseResult.text,
            func.coalesce(
                cast(CaseScore.metrics["cer"]["value"].astext, Float),
                cast(CaseScore.metrics["cer"].astext, Float),
            ),
        )
        .join(CaseResult, CaseResult.id == CaseScore.case_result_id)
        .join(Item, Item.id == CaseResult.item_id)
        .join(RefText, and_(RefText.item_id == Item.id, RefText.kind == "verbatim"))
        .where(
            CaseScore.evaluation_id == evaluation.id,
            CaseResult.run_target_id == target.id,
            # 兼容旧扁平结构：nested.status=ok 或旧数据无 status
            or_(
                CaseScore.metrics["cer"]["status"].astext == "ok",
                CaseScore.metrics["cer"]["status"].astext.is_(None),
            ),
        )
    ).all()
    values = [
        (int(case_id), key, ref, hyp, float(cer))
        for case_id, key, ref, hyp, cer in rows
        if cer is not None
    ]
    if not values:
        return {
            "histogram": [],
            "worst": [],
            "n": 0,
            "cer": None,
            "evaluation_id": str(evaluation.id),
        }

    edges = [0, 0.05, 0.1, 0.2, 0.4, 0.6]
    histogram = []
    for index, low in enumerate(edges):
        high = edges[index + 1] if index + 1 < len(edges) else None
        count = sum(1 for item in values if low <= item[4] and (high is None or item[4] < high))
        label = f"≥{int(low * 100)}%" if high is None else f"{int(low * 100)}–{int(high * 100)}%"
        histogram.append(
            {
                "bin": label,
                "low": low,
                "high": high,
                "count": count,
            }
        )

    total_edits = 0
    total_len = 0
    for case_id, _key, _ref, _hyp, _cer in values:
        score = db.get(CaseScore, case_id)
        if score is None:
            continue
        nested = score.metrics.get("cer")
        if isinstance(nested, dict):
            total_edits += (
                int(nested.get("s") or 0) + int(nested.get("d") or 0) + int(nested.get("i") or 0)
            )
            total_len += int(nested.get("n") or 0)
        else:
            # 旧扁平结构：metrics.edits / metrics.ref_len
            total_edits += int(tcast(float, score.metrics.get("edits") or 0))
            total_len += int(tcast(float, score.metrics.get("ref_len") or 0))
    # M8 §9.4 的目标×指标矩阵抽屉固定展示最差 20 条，便于查看尾部识别错误。
    worst = sorted(values, key=lambda item: item[4], reverse=True)[:20]

    return {
        "evaluation_id": str(evaluation.id),
        "cer": (total_edits / total_len) if total_len else None,
        "n": len(values),
        "histogram": histogram,
        "worst": [
            {
                "case_id": case_id,
                "item_key": key,
                "cer": cer,
                "ref": ref[:200],
                "hyp": (hyp or "")[:200],
            }
            for case_id, key, ref, hyp, cer in worst
        ],
    }
