# ruff: noqa: E501 - HTML/XLSX 模板是数据，不是代码行
"""M6 报告构建：payload → 自包含 HTML / XLSX（M6 §5.7/§5.8）。

安全约定（M7 D-M7-12）：
- HTML 由服务端生成，所有文本经 html.escape；
- 下载/预览响应带 CSP sandbox，不在主域内联渲染；
- 报告只读，不修改评测数据。
"""

import html
import io
import json
import zipfile
from datetime import UTC, datetime
from typing import Any
from xml.sax.saxutils import escape as xml_escape

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.domain.models import (
    AudioAsset,
    Benchmark,
    CaseResult,
    CaseScore,
    Dataset,
    DatasetVersion,
    Deployment,
    Device,
    Evaluation,
    Item,
    MetricValue,
    Run,
    RunResourceSample,
    RunTarget,
)

TEMPLATE_VERSION = "report-2"

_PERCENT_METRICS = {
    "cer",
    "wer",
    "der",
    "speaker_eer",
    "coverage",
    "success_rate",
    "numeric_cer",
    "punct_f1",
    "sentence_end_acc",
    "itn_acc",
    "naming_acc",
    "sa_der",
}
_REPORT_METRIC_LABELS = {
    "cer": "字错误率（CER）",
    "wer": "词错误率（WER）",
    "punct_f1": "标点 F1",
    "sentence_end_acc": "句末标点准确率",
    "itn_acc": "ITN 实体准确率",
    "der": "说话人分离错误率（DER）",
    "sa_der": "说话人关联分离错误率（SA-DER）",
    "naming_acc": "声纹命名准确率",
    "speaker_eer": "说话人等错误率（EER）",
    "ttft_ms": "首字延迟（ms）",
    "finalize_ms": "最终结果延迟（ms）",
    "rtf_dev": "非流式RTF",
    "rtf_e2e": "非流式E2E RTF",
    "rtf_realtime": "流式RTF",
    "overhead_ratio": "设备开销比",
    "throughput": "音频吞吐率",
    "coverage": "覆盖率",
    "success_rate": "成功率",
    "numeric_cer": "数字相关 CER（近似）",
}


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _buckets(db: Session, evaluation_id: Any) -> list[dict[str, Any]]:
    """只读取评分阶段落库的聚合桶，沿用其 CI 与状态。"""
    metric_rows = db.scalars(
        select(MetricValue)
        .where(
            MetricValue.evaluation_id == evaluation_id,
            MetricValue.run_target_id.is_(None),
            MetricValue.metric == "cer",
        )
        .order_by(MetricValue.id)
    ).all()
    return [
        {
            "bucket": row.bucket or {},
            "metric": row.metric,
            "scope": row.scope,
            "value": row.value,
            "ci_low": row.ci_low,
            "ci_high": row.ci_high,
            "n": row.n,
            "status": row.status,
            "reason": row.reason,
        }
        for row in metric_rows
        if row.bucket
    ]


def build_payload(db: Session, evaluation: Evaluation, *, level: str = "summary") -> dict[str, Any]:
    """汇总报告 payload：只读 metric_value / case_score / 元信息（M6 §3.2）。"""
    run = db.get(Run, evaluation.run_id)
    benchmark = db.get(Benchmark, run.benchmark_id) if run else None
    version = db.get(DatasetVersion, benchmark.dataset_version_id) if benchmark else None

    metrics = []
    for value_row, target_name in db.execute(
        select(MetricValue, Device.name)
        .outerjoin(RunTarget, RunTarget.id == MetricValue.run_target_id)
        .outerjoin(Device, Device.id == RunTarget.device_id)
        .where(MetricValue.evaluation_id == evaluation.id)
        .order_by(MetricValue.id)
    ).all():
        metrics.append(
            {
                "metric": value_row.metric,
                "subset": value_row.subset,
                "scope": value_row.scope,
                "bucket": value_row.bucket or {},
                "value": value_row.value,
                "n": value_row.n,
                "ci_low": value_row.ci_low,
                "ci_high": value_row.ci_high,
                "detail": value_row.detail or {},
                "status": value_row.status,
                "reason": value_row.reason,
                "target_id": str(value_row.run_target_id) if value_row.run_target_id else None,
                "target": target_name,
            }
        )

    # 分桶行单独走「分桶指标」；主指标表只保留全局聚合，避免 all/far 重复。
    metrics = [metric for metric in metrics if not metric["bucket"]]
    target_subsets = {metric["subset"] for metric in metrics if metric["target_id"]}
    if len(target_subsets) == 1:
        metrics_with_target = {metric["metric"] for metric in metrics if metric["target_id"]}
        metrics = [
            metric
            for metric in metrics
            if metric["target_id"] is not None or metric["metric"] not in metrics_with_target
        ]

    dataset = db.get(Dataset, version.dataset_id) if version is not None else None
    target_rows = db.execute(
        select(RunTarget, Device, Deployment)
        .join(Device, Device.id == RunTarget.device_id)
        .outerjoin(Deployment, Deployment.id == RunTarget.deployment_id)
        .where(RunTarget.run_id == evaluation.run_id)
        .order_by(RunTarget.id)
    ).all()
    run_targets = [
        {
            "id": str(target.id),
            "device_id": str(device.id),
            "device_name": device.name,
            "platform": device.platform,
            "kind": device.kind,
            "sn": device.sn,
            "subset": target.subset,
            "deployment_uid": deployment.uid if deployment is not None else None,
            "model_key": deployment.model_key if deployment is not None else None,
            "bigversion": deployment.bigversion if deployment is not None else None,
            "etsllm_version": deployment.etsllm_version if deployment is not None else None,
            "model_version": deployment.model_version if deployment is not None else None,
            "quant": deployment.quant if deployment is not None else None,
            "soc": deployment.soc if deployment is not None else None,
            "accel": deployment.accel if deployment is not None else {},
            "deployment_captured_at": _iso(deployment.captured_at)
            if deployment is not None
            else None,
        }
        for target, device, deployment in target_rows
    ]

    resource_rows = db.scalars(
        select(RunResourceSample)
        .where(RunResourceSample.run_id == evaluation.run_id)
        .order_by(RunResourceSample.sampled_at)
    ).all()
    resource_metrics = [row.metrics or {} for row in resource_rows]

    def resource_values(key: str) -> list[float]:
        values: list[float] = []
        for metric in resource_metrics:
            value = metric.get(key)
            if isinstance(value, int | float) and not isinstance(value, bool):
                values.append(float(value))
        return values

    resource_cpu = resource_values("cpu_pct")
    resource_memory = resource_values("mem_used_pct")
    resource_npu = resource_values("npu_pct")
    resource_temp = resource_values("temp_c_max")
    resource_summary = {
        "available": bool(resource_rows),
        "sample_count": len(resource_rows),
        "cpu_avg": sum(resource_cpu) / len(resource_cpu) if resource_cpu else None,
        "cpu_max": max(resource_cpu) if resource_cpu else None,
        "mem_avg": sum(resource_memory) / len(resource_memory) if resource_memory else None,
        "mem_max": max(resource_memory) if resource_memory else None,
        "npu_avg": sum(resource_npu) / len(resource_npu) if resource_npu else None,
        "npu_max": max(resource_npu) if resource_npu else None,
        "temp_max": max(resource_temp) if resource_temp else None,
        "first_at": resource_rows[0].sampled_at.isoformat() if resource_rows else None,
        "last_at": resource_rows[-1].sampled_at.isoformat() if resource_rows else None,
    }

    case_totals = db.execute(
        select(CaseResult.status, func.count())
        .join(RunTarget, RunTarget.id == CaseResult.run_target_id)
        .where(RunTarget.run_id == evaluation.run_id)
        .group_by(CaseResult.status)
    ).all()

    case_rows: list[dict[str, Any]] = []
    if level == "evidence":
        from app.domain.models import RefText

        evidence_rows = db.execute(
            select(
                CaseResult,
                Item,
                RunTarget,
                Device,
                AudioAsset.uri,
                RefText.text,
                CaseScore.metrics,
            )
            .join(RunTarget, RunTarget.id == CaseResult.run_target_id)
            .join(Item, Item.id == CaseResult.item_id)
            .join(AudioAsset, AudioAsset.id == Item.audio_asset_id)
            .join(Device, Device.id == RunTarget.device_id)
            .outerjoin(
                RefText,
                and_(RefText.item_id == Item.id, RefText.kind == "verbatim"),
            )
            .outerjoin(
                CaseScore,
                and_(
                    CaseScore.case_result_id == CaseResult.id,
                    CaseScore.evaluation_id == evaluation.id,
                ),
            )
            .where(RunTarget.run_id == evaluation.run_id)
            .order_by(CaseResult.id)
        ).all()
        case_rows = [
            {
                "case_result_id": case_result.id,
                "item_key": item.item_key,
                "content_uid": item.content_uid,
                "target": device.name,
                "subset": target.subset,
                "audio_uri": audio_uri,
                "status": case_result.status,
                "reference": reference,
                "hypothesis": case_result.text,
                "metrics": score_metrics or {},
            }
            for case_result, item, target, device, audio_uri, reference, score_metrics in evidence_rows
        ]

    return {
        "kind": "single",
        "template_version": TEMPLATE_VERSION,
        "level": level,
        "generated_at": datetime.now(UTC).isoformat(),
        "evaluation": {
            "id": str(evaluation.id),
            "status": evaluation.status,
            "trigger": evaluation.trigger,
            "profile_hash": evaluation.profile_hash,
            "scorer_version": evaluation.scorer_version,
            "coverage": evaluation.coverage,
            "metrics": evaluation.metrics,
            "created_at": _iso(evaluation.created_at),
            "finished_at": _iso(evaluation.finished_at),
        },
        "run": {
            "id": str(run.id) if run else None,
            "name": run.name if run else None,
            "status": run.status if run else None,
            "created_by": run.created_by if run else None,
            "started_at": _iso(run.started_at) if run else None,
            "finished_at": _iso(run.finished_at) if run else None,
        },
        "benchmark": {
            "id": str(benchmark.id) if benchmark else None,
            "name": benchmark.name if benchmark else None,
            "version_hash": benchmark.version_hash if benchmark else None,
            "dataset_version": version.version if version else None,
            "dataset": str(version.dataset_id) if version else None,
            "dataset_name": dataset.name if dataset is not None else None,
            "subsets": benchmark.subsets if benchmark else [],
        },
        "targets": run_targets,
        "resources": resource_summary,
        "metrics": metrics,
        "buckets": _buckets(db, evaluation.id),
        "cases": {str(status): int(count) for status, count in case_totals},
        "case_rows": case_rows,
    }


def _fmt(value: float | None, unit: str = "%") -> str:
    if value is None:
        return "—"
    return f"{value * 100:.2f}{unit}" if unit == "%" else f"{value:.4f}"


def _reason_text(reason: str | None) -> str:
    mapping = {
        "no_diarization_output": "无识别分段（设备未开分离）",
        "no_segment_timeline": "参考无分段",
        "no_voiceprint_match": "设备未回声纹匹配",
        "insufficient_trials": "簇样本不足",
        "empty_reference": "参考为空",
        "low_coverage": "覆盖不足",
        "no_scoreable_cases": "无可评样本",
        "no_numeric_reference": "无数字参考",
        None: "",
    }
    return mapping.get(reason, reason or "")


def _metric_text(metric: str, value: float | None) -> str:
    if value is None:
        return "—"
    if metric in _PERCENT_METRICS:
        return _fmt(value)
    return f"{value:.4f}"


def _metric_label(metric: str) -> str:
    return _REPORT_METRIC_LABELS.get(metric, metric)


def _render_composite_html(payload: dict[str, Any]) -> str:
    """渲染横评、回归及多 Evaluation 快照。"""
    comparison = payload.get("comparison") or {"evaluations": [], "rows": []}
    evaluations = comparison.get("evaluations", [])
    significance = payload.get("significance") or {}
    query = payload.get("query") or {}
    baseline_id = (
        significance.get("baseline_evaluation_id")
        or query.get("baseline")
        or (evaluations[0].get("id") if evaluations else "")
    )
    baseline_entry: dict[str, Any] = next(
        (entry for entry in evaluations if entry.get("id") == str(baseline_id)),
        {},
    )
    baseline_label = baseline_entry.get("run_name") or str(baseline_id)[:8] or "—"
    pair_labels = {
        "content_uid": "content_uid（同音频）",
        "pair_key": "pair_key（跨条件配对）",
        "none": "仅聚合",
    }
    pair_label = pair_labels.get(str(query.get("pair_by", "content_uid")), "content_uid（同音频）")
    headers = "".join(
        f"<th>{html.escape(str(entry.get('run_name') or entry.get('id', '')))}"
        f"<br><small>{html.escape(str(entry.get('id', ''))[:8])}</small></th>"
        for entry in evaluations
    )
    matrix_rows = []
    for row in comparison.get("rows", []):
        bucket = " / ".join(str(value) for value in row.get("bucket", {}).values())
        cells = []
        for cell in row.get("values", []):
            ci = (
                f"{_metric_text(row['metric'], cell['ci_low'])} – {_metric_text(row['metric'], cell['ci_high'])}"
                if cell.get("ci_low") is not None and cell.get("ci_high") is not None
                else "—"
            )
            reason = _reason_text(cell.get("reason"))
            detail = f"n={cell.get('n', 0)} · CI {ci} · {cell.get('status', 'na')}"
            if reason:
                detail += f" · {reason}"
            cells.append(
                f"<td class='num'>{_metric_text(row['metric'], cell.get('value'))}"
                f"<small>{html.escape(detail)}</small></td>"
            )
        matrix_rows.append(
            "<tr>"
            f"<td>{html.escape(str(row.get('target') or 'Evaluation 总计'))}</td>"
            f"<td>{html.escape(str(row.get('subset') or '—'))}</td>"
            f"<td>{html.escape(_metric_label(str(row.get('metric') or '')))}</td>"
            f"<td>{html.escape(str(row.get('scope') or '—'))}</td>"
            f"<td>{html.escape(bucket or '—')}</td>"
            f"{''.join(cells)}</tr>"
        )
    delta_rows = []
    for result in significance.get("comparisons", []):
        evaluation: dict[str, Any] = next(
            (entry for entry in evaluations if entry.get("id") == result["evaluation_id"]),
            {},
        )
        ci = (
            f"{_metric_text('cer', result['ci_low'])} – {_metric_text('cer', result['ci_high'])}"
            if result.get("ci_low") is not None and result.get("ci_high") is not None
            else "—"
        )
        sample_count_cells = (
            f"<td class='num'>{result.get('n_baseline', 0)}</td>"
            f"<td class='num'>{result.get('n_comparison', 0)}</td>"
            if pair_label == "仅聚合"
            else f"<td class='num'>{result.get('n', 0)}</td>"
        )
        delta_rows.append(
            "<tr>"
            f"<td>{html.escape(str(evaluation.get('run_name') or result['evaluation_id']))}</td>"
            f"<td class='num'>{_metric_text('cer', result.get('delta'))}</td>"
            f"<td class='num'>{ci}</td>"
            f"<td class='num'>{result.get('p') if result.get('p') is not None else '—'}</td>"
            f"{sample_count_cells}</tr>"
        )
    case_rows = []
    for item in payload.get("case_rows", []):
        case_rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('evaluation_id', ''))[:8])}</td>"
            f"<td>{html.escape(str(item.get('target') or '—'))}</td>"
            f"<td>{html.escape(str(item.get('subset') or '—'))}</td>"
            f"<td>{html.escape(str(item.get('item_key') or ''))}</td>"
            f"<td>{html.escape(str(item.get('audio_uri') or ''))}</td>"
            f"<td>{html.escape(str(item.get('status') or ''))}</td>"
            f"<td>{html.escape(str(item.get('reference') or ''))}</td>"
            f"<td>{html.escape(str(item.get('hypothesis') or ''))}</td></tr>"
        )
    source_rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(item.get('run_name') or ''))}</td>"
        f"<td>{html.escape(str(item.get('id', '')))}</td>"
        f"<td>{html.escape(str(item.get('dataset_version_id') or ''))}</td>"
        f"<td>{html.escape(str(item.get('profile_hash', '')))}</td>"
        f"<td>{html.escape(str(item.get('scorer_version', '')))}</td>"
        f"<td>{html.escape(str(item.get('benchmark_version_hash') or '—'))}</td></tr>"
        for item in evaluations
    )
    warnings = "".join(
        f"<li>{html.escape(str(issue.get('message', issue)))}</li>"
        for issue in comparison.get("warnings", [])
    )
    kind_names = {"matrix": "横评矩阵", "compare": "条件对比", "regression": "版本回归"}
    title = kind_names.get(payload["kind"], "评测报告")
    case_section = ""
    if payload.get("level") == "evidence":
        case_section = f"""
  <h2>样本证据</h2><div class="panel"><table>
    <thead><tr><th>评分</th><th>目标</th><th>Subset</th><th>样本</th><th>音频路径</th><th>状态</th><th>参考</th><th>识别</th></tr></thead>
    <tbody>{"".join(case_rows) or '<tr><td colspan="8">无样本证据</td></tr>'}</tbody>
  </table></div>"""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>ASR {title}</title>
<style>
  body {{ margin:0; background:#f4f6f4; color:#15191b; font-family:"Instrument Sans","Noto Sans SC",system-ui,sans-serif; font-size:13px; line-height:1.5; }}
  .page {{ max-width:1200px; margin:0 auto; padding:32px; }} h1 {{ font-size:22px; margin:0 0 6px; font-weight:600; }}
  h2 {{ font-size:13.5px; margin:26px 0 8px; font-weight:600; }} .meta, small {{ color:#5d6a64; font-size:11px; }}
  .panel {{ background:#fff; border:1px solid #d6ddd8; border-radius:6px; overflow:auto; }}
  table {{ width:100%; border-collapse:collapse; }} th,td {{ padding:8px 10px; border-bottom:1px solid #e6eae6; text-align:left; vertical-align:top; }}
  th {{ background:#eaeef0; color:#5d6a64; font-weight:500; }} .num {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }}
  .num small {{ display:block; white-space:nowrap; }} ul {{ color:#5d6a64; }}
</style></head><body><main class="page">
  <h1>{title}</h1><div class="meta">生成时间 {html.escape(str(payload["generated_at"]))} · 模板 {html.escape(str(payload["template_version"]))} · 级别 {html.escape(str(payload["level"]))}<br>基线 {html.escape(str(baseline_label))} · 配对方式 {html.escape(pair_label)}</div>
  <h2>来源 Evaluation</h2><div class="panel"><table><thead><tr><th>Run</th><th>Evaluation</th><th>数据版本</th><th>口径 hash</th><th>评分器</th><th>基准 hash</th></tr></thead><tbody>{source_rows}</tbody></table></div>
  {f"<ul>{warnings}</ul>" if warnings else ""}
  <h2>指标矩阵</h2><div class="panel"><table><thead><tr><th>目标</th><th>Subset</th><th>指标</th><th>范围</th><th>分桶</th>{headers}</tr></thead>
    <tbody>{"".join(matrix_rows) or '<tr><td colspan="6">无指标</td></tr>'}</tbody></table></div>
  {f'<h2>{"聚合差异（不含显著性检验）" if pair_label == "仅聚合" else "配对差异与显著性"}</h2><div class="panel"><table><thead><tr><th>对比</th><th>Δ CER</th><th>95% CI</th><th>p</th>{"<th>n 基线</th><th>n 对比</th>" if pair_label == "仅聚合" else "<th>共同样本 n</th>"}</tr></thead><tbody>{"".join(delta_rows) or '<tr><td colspan="6">当前设置不提供配对显著性</td></tr>'}</tbody></table></div>' if significance else ""}
  {case_section}
  <footer class="meta">不可评指标以 — 显示；快照数字来自 Evaluation 与 metric_value / case_score。</footer>
</main></body></html>"""


def render_html(payload: dict[str, Any]) -> str:
    """自包含 HTML（内联 CSS + 简单 SVG 条），所有动态文本转义。"""
    if payload.get("kind") in {"matrix", "compare", "regression"}:
        return _render_composite_html(payload)
    evaluation = payload["evaluation"]
    run = payload["run"]
    benchmark = payload["benchmark"]

    def esc(value: Any) -> str:
        return html.escape(str(value if value is not None else ""))

    device_rows = []
    for item in payload.get("targets", []):
        accel = item.get("accel") if isinstance(item.get("accel"), dict) else {}
        accel_text = ", ".join(f"{key}={value}" for key, value in accel.items()) if accel else "—"
        device_rows.append(
            "<tr>"
            f"<td>{esc(item.get('device_name'))}</td>"
            f"<td>{esc(item.get('platform'))}/{esc(item.get('kind'))}</td>"
            f"<td>{esc(item.get('subset'))}</td>"
            f"<td class='mono'>{esc(item.get('deployment_uid'))}</td>"
            f"<td>{esc(item.get('bigversion'))}</td>"
            f"<td>{esc(item.get('etsllm_version'))}</td>"
            f"<td>{esc(item.get('model_key'))}</td>"
            f"<td>{esc(item.get('model_version'))}</td>"
            f"<td>{esc(item.get('quant') or '—')} / {esc(item.get('soc') or '—')} {esc(accel_text)}</td>"
            f"<td>{esc(item.get('deployment_captured_at') or '—')}</td>"
            "</tr>"
        )

    resource_rows_html = []
    resources = payload.get("resources") or {}
    if resources.get("available"):
        for label, avg_key, max_key, unit in (
            ("CPU", "cpu_avg", "cpu_max", "%"),
            ("内存", "mem_avg", "mem_max", "%"),
            ("NPU", "npu_avg", "npu_max", "%"),
            ("最高温度", None, "temp_max", "°C"),
        ):
            avg = resources.get(avg_key) if avg_key else None
            peak = resources.get(max_key)
            if avg is None and peak is None:
                continue

            avg_text = "—" if avg is None else f"{avg:.1f}{unit}"
            peak_text = "—" if peak is None else f"{peak:.1f}{unit}"
            resource_rows_html.append(
                "<tr>"
                f"<td>{esc(label)}</td>"
                f"<td class='num'>{esc(avg_text)}</td>"
                f"<td class='num'>{esc(peak_text)}</td>"
                f"<td class='num'>{esc(resources.get('sample_count'))}</td>"
                "</tr>"
            )

    rows = []
    for metric in payload["metrics"]:
        ci = (
            f"{_metric_text(metric['metric'], metric['ci_low'])} – "
            f"{_metric_text(metric['metric'], metric['ci_high'])}"
            if metric["ci_low"] is not None and metric["ci_high"] is not None
            else "—"
        )
        value = _metric_text(metric["metric"], metric["value"])
        status = (
            "ok"
            if metric["status"] == "ok"
            else f"{metric['status']}（{_reason_text(metric['reason'])}）"
        )
        rows.append(
            "<tr>"
            f"<td>{esc(_metric_label(metric['metric']))}</td>"
            f"<td>{esc(metric['subset'])}</td>"
            f"<td>{esc(metric.get('scope', 'micro'))}</td>"
            f"<td class='num'>{esc(value)}</td>"
            f"<td class='num'>{esc(metric['n'])}</td>"
            f"<td class='num'>{esc(ci)}</td>"
            f"<td>{esc(status)}</td>"
            "</tr>"
        )

    bucket_rows = []
    max_value = max(
        (bucket["value"] for bucket in payload["buckets"] if bucket["value"] is not None),
        default=0.0,
    )
    for bucket in payload["buckets"]:
        bucket_name = next(iter(bucket["bucket"].values()), "—")
        width = int((bucket["value"] / max_value) * 240) if max_value and bucket["value"] else 0
        ci = (
            f"{_fmt(bucket['ci_low'])} – {_fmt(bucket['ci_high'])}"
            if bucket.get("ci_low") is not None and bucket.get("ci_high") is not None
            else "—"
        )
        status = bucket["status"]
        if bucket.get("reason"):
            status += f"（{_reason_text(bucket['reason'])}）"
        bucket_rows.append(
            "<tr>"
            f"<td>{esc(bucket_name)}</td>"
            f"<td>{esc(bucket.get('scope', 'micro'))}</td>"
            f"<td class='num'>{esc(_fmt(bucket['value']))}</td>"
            f"<td class='num'>{esc(bucket['n'])}</td>"
            f"<td class='num'>{esc(ci)}</td>"
            f"<td>{esc(status)}</td>"
            f"<td><svg width='250' height='10'><rect x='0' y='2' width='240' height='6' fill='#eaeef0'/>"
            f"<rect x='0' y='2' width='{width}' height='6' fill='#1f5e8c'/></svg></td>"
            "</tr>"
        )

    case_rows = "".join(
        f"<li>{esc(status)}：{esc(count)}</li>"
        for status, count in sorted(payload["cases"].items())
    )
    evidence_rows = "".join(
        "<tr>"
        f"<td>{esc(item.get('target'))}</td><td>{esc(item.get('subset'))}</td>"
        f"<td>{esc(item.get('item_key'))}</td><td>{esc(item.get('audio_uri'))}</td>"
        f"<td>{esc(item.get('status'))}</td>"
        f"<td>{esc(item.get('reference'))}</td><td>{esc(item.get('hypothesis'))}</td></tr>"
        for item in payload.get("case_rows", [])
    )

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>ASR 评测报告 {esc(run["name"])}</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ margin:0; background:#f4f6f4; color:#15191b;
         font-family:"Instrument Sans","Noto Sans SC",system-ui,sans-serif; font-size:13px; line-height:1.5; }}
  .page {{ max-width:1000px; margin:0 auto; padding:32px; }}
  h1 {{ font-size:22px; margin:0 0 6px; font-weight:600; letter-spacing:-.012em; }}
  h2 {{ font-size:13.5px; margin:26px 0 8px; font-weight:600; }}
  .meta {{ color:#5d6a64; font-size:11.5px; }}
  .panel {{ background:#fff; border:1px solid #d6ddd8; border-radius:6px; overflow:hidden; }}
  table {{ width:100%; border-collapse:collapse; }}
  th, td {{ padding:8px 12px; border-bottom:1px solid #e6eae6; text-align:left; font-size:12px; }}
  th {{ background:#eaeef0; color:#5d6a64; font-weight:500; }}
  tr:last-child td {{ border-bottom:0; }}
  .num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  .na {{ color:#93a19a; }}
  ul {{ margin:0; padding:12px 28px; color:#5d6a64; }}
  footer {{ margin-top:28px; color:#8a968f; font-size:10.5px; }}
</style></head>
<body><div class="page">
  <h1>{esc(run["name"]) or "评测报告"}</h1>
  <div class="meta">
    评分 {esc(evaluation["id"][:8])} · 口径 {esc(evaluation["profile_hash"])} ·
    评分器 {esc(evaluation["scorer_version"])} · 状态 {esc(evaluation["status"])}
    <br>数据集 {esc(benchmark.get("dataset_name") or "—")} · 版本 {esc(benchmark["dataset_version"])} ·
    基准 {esc(benchmark["name"])} · 子集 {esc(", ".join(benchmark.get("subsets") or ["all"]))} ·
    基准哈希 {esc(benchmark["version_hash"] or "—")}
    <br>设备 {esc("；".join(f"{item['device_name']}（{item['platform']}/{item['kind']}，subset {item['subset']}，{item['model_key'] or '—'}，部署 {item['deployment_uid'] or '—'}，大版本 {item['bigversion'] or '—'}，etsllm {item['etsllm_version'] or '—'}，模型 {item['model_version'] or '—'}）" for item in payload.get("targets", [])) or "—")}
    <br>生成时间 {esc(payload["generated_at"])} · 模板 {esc(payload["template_version"])} · 级别 {esc(payload["level"])}
  </div>

  <h2>设备与版本</h2>
  <div class="panel"><table>
    <thead><tr><th>设备</th><th>平台</th><th>Subset</th><th>部署 UID</th><th>大版本</th><th>etsllm</th><th>模型</th><th>模型版本</th><th>量化 / SoC</th><th>探测时间</th></tr></thead>
    <tbody>{"".join(device_rows) or '<tr><td colspan="10">无设备信息</td></tr>'}</tbody>
  </table></div>

  <h2>指标</h2>
  <div class="panel"><table>
    <thead><tr><th>指标</th><th>Subset</th><th>范围</th><th class="num">值</th><th class="num">n</th><th class="num">95% CI</th><th>状态</th></tr></thead>
    <tbody>{"".join(rows) or '<tr><td colspan="7">无指标</td></tr>'}</tbody>
  </table></div>

  <h2>硬件资源</h2>
  <div class="panel"><table>
    <thead><tr><th>指标</th><th class="num">平均</th><th class="num">峰值</th><th class="num">样本</th></tr></thead>
    <tbody>{"".join(resource_rows_html) or '<tr><td colspan="4">该 Run 未采集硬件资源</td></tr>'}</tbody>
  </table></div>

  <h2>分桶指标</h2>
  <div class="panel"><table>
    <thead><tr><th>桶</th><th>范围</th><th class="num">CER</th><th class="num">n</th><th class="num">95% CI</th><th>状态 / 原因</th><th>相对</th></tr></thead>
    <tbody>{"".join(bucket_rows) or '<tr><td colspan="7">无桶级 metric_value</td></tr>'}</tbody>
  </table></div>

  <h2>样本执行</h2>
  <div class="panel"><ul>{case_rows or "<li>无记录</li>"}</ul></div>
  {f'<h2>样本证据</h2><div class="panel"><table><thead><tr><th>目标</th><th>Subset</th><th>样本</th><th>音频路径</th><th>状态</th><th>参考</th><th>识别</th></tr></thead><tbody>{evidence_rows or '<tr><td colspan="7">无样本证据</td></tr>'}</tbody></table></div>' if payload.get("level") == "evidence" else ""}

  <footer>
    报告为不可变快照；所有数字来自 metric_value / case_score，生成时未修改评测数据。
    na 表示不可评，值不显示为 0。
  </footer>
</div></body></html>"""


def _xml_cell(ref: str, value: Any, style_id: int | None = None) -> str:
    if value is None:
        return f'<c r="{ref}"/>'
    if isinstance(value, (int, float)):
        style = f' s="{style_id}"' if style_id is not None else ""
        return f'<c r="{ref}"{style}><v>{value}</v></c>'
    text = xml_escape(str(value))
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'


def _column_name(index: int) -> str:
    """把从 1 开始的列序号转为 XLSX 字母列名。"""
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _sheet_xml(rows: list[list[Any]], percentage_cells: set[tuple[int, int]] | None = None) -> str:
    lines = []
    for row_index, row in enumerate(rows, start=1):
        cells = []
        for col_index, value in enumerate(row):
            column = _column_name(col_index + 1)
            style_id = (
                1 if percentage_cells and (row_index, col_index + 1) in percentage_cells else None
            )
            cells.append(_xml_cell(f"{column}{row_index}", value, style_id))
        lines.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(lines)}</sheetData></worksheet>"
    )


def render_xlsx(payload: dict[str, Any]) -> bytes:
    """最小 XLSX（3 个 sheet：Summary / Buckets / Cases），无第三方依赖。"""
    is_composite = payload.get("kind") in {"matrix", "compare", "regression"}
    summary_percentage_cells: set[tuple[int, int]] = set()
    bucket_percentage_cells: set[tuple[int, int]] = set()
    if is_composite:
        evaluation_payloads = payload["evaluations"]
        comparison = payload.get("comparison") or {"evaluations": [], "rows": []}
        summary = [
            ["字段", "值"],
            ["报告类型", payload["kind"]],
            ["生成时间", payload["generated_at"]],
            ["配对方式", payload.get("query", {}).get("pair_by", "—")],
            [],
            ["Run", "Evaluation", "状态", "口径 hash", "评分器", "基准哈希"],
        ]
        summary.extend(
            [
                item["run"]["name"],
                item["evaluation"]["id"],
                item["evaluation"]["status"],
                item["evaluation"]["profile_hash"],
                item["evaluation"]["scorer_version"],
                item["benchmark"]["version_hash"],
            ]
            for item in evaluation_payloads
        )
        summary.extend(
            [
                [],
                ["目标", "Subset", "指标", "范围", "分桶"]
                + [
                    f"{entry.get('run_name') or entry.get('id')} {column}"
                    for entry in comparison["evaluations"]
                    for column in ("值", "n", "CI 下限", "CI 上限", "状态", "原因")
                ],
            ]
        )
        for row in comparison["rows"]:
            summary_row_index = len(summary) + 1
            metric_name = str(row.get("metric") or "")
            summary.append(
                [
                    row.get("target", "Evaluation 总计"),
                    row.get("subset", ""),
                    _metric_label(metric_name),
                    row.get("scope", ""),
                    " / ".join(str(value) for value in row.get("bucket", {}).values()),
                ]
                + [
                    value
                    for cell in row["values"]
                    for value in (
                        cell.get("value"),
                        cell.get("n"),
                        cell.get("ci_low"),
                        cell.get("ci_high"),
                        cell.get("status"),
                        _reason_text(cell.get("reason")),
                    )
                ]
            )
            if metric_name in _PERCENT_METRICS:
                for cell_index in range(len(row["values"])):
                    value_column = 6 + cell_index * 6
                    summary_percentage_cells.update(
                        {
                            (summary_row_index, value_column),
                            (summary_row_index, value_column + 2),
                            (summary_row_index, value_column + 3),
                        }
                    )
        if payload.get("significance"):
            summary.extend(
                [
                    [],
                    [
                        "相对基线差异（Δ CER = 基线 − 对比）",
                        "Δ CER",
                        "CI 下限",
                        "CI 上限",
                        "p",
                        "共同样本 n",
                        "n 基线",
                        "n 对比",
                        "显著",
                        "原因",
                    ],
                ]
            )
            for result in payload["significance"]["comparisons"]:
                summary_row_index = len(summary) + 1
                summary.append(
                    [
                        result["evaluation_id"],
                        result.get("delta"),
                        result.get("ci_low"),
                        result.get("ci_high"),
                        result.get("p"),
                        result.get("n", 0),
                        result.get("n_baseline"),
                        result.get("n_comparison"),
                        result.get("significant", False),
                        result.get("reason") or result.get("warning"),
                    ]
                )
                summary_percentage_cells.update(
                    {
                        (summary_row_index, 2),
                        (summary_row_index, 3),
                        (summary_row_index, 4),
                    }
                )
        buckets = [
            ["来源 Evaluation", "桶", "范围", "CER", "n", "CI 下限", "CI 上限", "状态", "原因"]
        ]
        for item in evaluation_payloads:
            for bucket in item["buckets"]:
                bucket_row_index = len(buckets) + 1
                buckets.append(
                    [
                        item["evaluation"]["id"],
                        next(iter(bucket["bucket"].values()), ""),
                        bucket.get("scope", "micro"),
                        bucket["value"],
                        bucket["n"],
                        bucket.get("ci_low"),
                        bucket.get("ci_high"),
                        bucket["status"],
                        _reason_text(bucket["reason"]),
                    ]
                )
                bucket_percentage_cells.update(
                    {
                        (bucket_row_index, 4),
                        (bucket_row_index, 6),
                        (bucket_row_index, 7),
                    }
                )
        if payload.get("significance"):
            buckets.extend(
                [
                    [],
                    [
                        "对比 Evaluation",
                        "时长桶",
                        "Δ CER",
                        "共同样本 n",
                        "CI 下限",
                        "CI 上限",
                        "p",
                        "p BH",
                        "显著",
                        "警告",
                    ],
                ]
            )
            for result in payload["significance"]["comparisons"]:
                for bucket in result["buckets"]:
                    bucket_row_index = len(buckets) + 1
                    buckets.append(
                        [
                            result["evaluation_id"],
                            next(iter(bucket["bucket"].values()), ""),
                            bucket.get("delta"),
                            bucket.get("n", 0),
                            bucket.get("ci_low"),
                            bucket.get("ci_high"),
                            bucket.get("p"),
                            bucket.get("p_bh"),
                            bucket.get("significant_bh", False),
                            bucket.get("warning"),
                        ]
                    )
                    bucket_percentage_cells.update(
                        {
                            (bucket_row_index, 3),
                            (bucket_row_index, 5),
                            (bucket_row_index, 6),
                        }
                    )
        cases = [
            ["Evaluation", "目标", "Subset", "样本", "音频路径", "状态", "参考文本", "识别文本"]
        ]
        if payload.get("level") == "evidence":
            cases.extend(
                [
                    item.get("evaluation_id"),
                    item.get("target"),
                    item.get("subset"),
                    item.get("item_key"),
                    item.get("audio_uri"),
                    item.get("status"),
                    item.get("reference"),
                    item.get("hypothesis"),
                ]
                for item in payload.get("case_rows", [])
            )
        else:
            cases = [["Evaluation", "状态", "条数"]]
            for item in evaluation_payloads:
                cases.extend(
                    [item["evaluation"]["id"], status, count]
                    for status, count in sorted(item["cases"].items())
                )
    else:
        summary = [
            ["字段", "值"],
            ["Run", payload["run"]["name"]],
            ["Evaluation", payload["evaluation"]["id"]],
            ["状态", payload["evaluation"]["status"]],
            ["口径 hash", payload["evaluation"]["profile_hash"]],
            ["评分器", payload["evaluation"]["scorer_version"]],
            ["数据集", payload["benchmark"].get("dataset_name") or "—"],
            ["数据集版本", payload["benchmark"]["dataset_version"]],
            ["基准", payload["benchmark"]["name"]],
            ["基准哈希", payload["benchmark"]["version_hash"]],
            [
                "设备",
                "；".join(
                    f"{item['device_name']}（{item['platform']}/{item['kind']}，subset {item['subset']}，"
                    f"{item['model_key'] or '—'}，{item['deployment_uid'] or '—'}）"
                    for item in payload.get("targets", [])
                )
                or "—",
            ],
            ["生成时间", payload["generated_at"]],
        ]
        resources = payload.get("resources") or {}
        if resources.get("available"):
            summary.extend(
                [
                    [],
                    ["硬件资源", "平均", "峰值", "样本"],
                    [
                        "CPU %",
                        resources.get("cpu_avg"),
                        resources.get("cpu_max"),
                        resources.get("sample_count"),
                    ],
                    [
                        "内存 %",
                        resources.get("mem_avg"),
                        resources.get("mem_max"),
                        resources.get("sample_count"),
                    ],
                    [
                        "NPU %",
                        resources.get("npu_avg"),
                        resources.get("npu_max"),
                        resources.get("sample_count"),
                    ],
                    ["最高温度 °C", None, resources.get("temp_max"), resources.get("sample_count")],
                ]
            )
        summary.extend(
            [
                [],
                ["目标", "指标", "Subset", "范围", "值", "n", "CI 下限", "CI 上限", "状态", "原因"],
            ]
        )
        for metric in payload["metrics"]:
            summary_row_index = len(summary) + 1
            summary.append(
                [
                    metric.get("target"),
                    _metric_label(metric["metric"]),
                    metric["subset"],
                    metric.get("scope", "micro"),
                    metric["value"],
                    metric["n"],
                    metric["ci_low"],
                    metric["ci_high"],
                    metric["status"],
                    _reason_text(metric["reason"]),
                ]
            )
            if metric["metric"] in _PERCENT_METRICS:
                summary_percentage_cells.update(
                    {
                        (summary_row_index, 5),
                        (summary_row_index, 7),
                        (summary_row_index, 8),
                    }
                )

        buckets = [["桶", "范围", "CER", "n", "CI 下限", "CI 上限", "状态", "原因"]]
        for bucket in payload["buckets"]:
            bucket_row_index = len(buckets) + 1
            buckets.append(
                [
                    next(iter(bucket["bucket"].values()), ""),
                    bucket.get("scope", "micro"),
                    bucket["value"],
                    bucket["n"],
                    bucket.get("ci_low"),
                    bucket.get("ci_high"),
                    bucket["status"],
                    _reason_text(bucket["reason"]),
                ]
            )
            bucket_percentage_cells.update(
                {
                    (bucket_row_index, 3),
                    (bucket_row_index, 5),
                    (bucket_row_index, 6),
                }
            )

        if payload.get("level") == "evidence":
            cases = [
                [
                    "目标",
                    "Subset",
                    "样本",
                    "内容 UID",
                    "音频路径",
                    "状态",
                    "参考文本",
                    "识别文本",
                    "指标",
                ]
            ]
            cases.extend(
                [
                    item.get("target"),
                    item.get("subset"),
                    item.get("item_key"),
                    item.get("content_uid"),
                    item.get("audio_uri"),
                    item.get("status"),
                    item.get("reference"),
                    item.get("hypothesis"),
                    json.dumps(item.get("metrics", {}), ensure_ascii=False, sort_keys=True),
                ]
                for item in payload.get("case_rows", [])
            )
        else:
            cases = [["状态", "条数"]]
            for status, count in sorted(payload["cases"].items()):
                cases.append([status, count])

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            + "".join(
                f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                for index in (1, 2, 3)
            )
            + '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            + "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        archive.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            "<sheets>"
            '<sheet name="Summary" sheetId="1" r:id="rId1"/>'
            '<sheet name="Buckets" sheetId="2" r:id="rId2"/>'
            '<sheet name="Cases" sheetId="3" r:id="rId3"/>'
            "</sheets></workbook>",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(
                f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>'
                for index in (1, 2, 3)
            )
            + '<Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            + "</Relationships>",
        )
        archive.writestr(
            "xl/styles.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<numFmts count="1"><numFmt numFmtId="164" formatCode="0.00%"/></numFmts>'
            '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<fills count="2"><fill><patternFill patternType="none"/></fill>'
            '<fill><patternFill patternType="gray125"/></fill></fills>'
            '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
            '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
            "</styleSheet>",
        )
        archive.writestr("xl/worksheets/sheet1.xml", _sheet_xml(summary, summary_percentage_cells))
        archive.writestr("xl/worksheets/sheet2.xml", _sheet_xml(buckets, bucket_percentage_cells))
        archive.writestr("xl/worksheets/sheet3.xml", _sheet_xml(cases))
    return buffer.getvalue()


def payload_hash(payload: dict[str, Any]) -> str:
    """数据指纹：去掉生成时间后哈希，用于判断同数据重复生成。"""

    def without_generated_at(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: without_generated_at(item)
                for key, item in value.items()
                if key != "generated_at"
            }
        if isinstance(value, list):
            return [without_generated_at(item) for item in value]
        return value

    canonical = without_generated_at(payload)
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    import hashlib

    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
