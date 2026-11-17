# 评分口径 evaluator 化与 UI 一致性（2026-11-11）

## 1. 官方/预设口径锁定

新增 `EVALUATORS` 注册表（`backend/app/domain/metric_profiles.py`）：

| evaluator | 锁定 | 说明 |
|---|---|---|
| `default_zh` | 否 | 通用中文，可编辑 |
| `alimeeting_official` | 是 | 固定 250ms collar、重叠参与、`<sc>` 剥离 |
| `perf_30ms` | 是 | 固定 30ms/1x；保留 CER 与性能读数 |
| `cer_only` | 是 | 固定关闭 punct/itn/der/naming |

行为：

- `POST /metric-profiles` 创建官方口径时忽略用户篡改的参数，写回代码固定值；
- `PATCH /metric-profiles/{id}` 对锁定口径返回 `409 E_EVALUATOR_LOCKED`；
- `POST /metric-profiles/{id}/versions` 对锁定口径同样拒绝；
- `profile_hash` 纳入 `evaluator`，身份变化会改变 hash；
- `GET /metric-profiles/evaluators` 返回 `id/name/locked/description/params`。

## 2. UI 与后端一致

- 口径编辑器新增 evaluator 查询；官方 evaluator 显示“官方实现已锁定”，参数区 `fieldset disabled`；
- 删除/固定的无效控件：
  - `perf.repeat` 从模板和 UI 移除；
  - `cer.join/separator` 不再由用户编辑，显示“由 evaluator 固定”；
  - `punct.match`、`itn.normalizer` 固定显示；
  - `der.collar_semantics` 固定半宽；
  - `naming.mode=clustering`、`granularity` 固定，不再渲染；
- `naming.unknown_policy` 保持可编辑并已接入评分；
- `MetricProfilesPage` 对锁定口径显示“官方锁定”徽标。

## 3. 已接线的评分参数

- `der.collar_ms` 传入 `speaker.der()`；
- `der.skip_overlap` 实现并传入；
- `naming.unknown_policy` 实现并传入；
- `profile_hash` 包含 `evaluator`。

## 4. 验证

```text
make test          391 passed
make lint          All checks passed
make openapi-check 契约与基线一致
make e2e           10 passed, 1 skipped
cd web && npm run build   通过
cd web && npm run lint    0 errors（保留既有 warning）
```

新增测试：

- 官方 evaluator 创建时参数被固定、PATCH 返回 `E_EVALUATOR_LOCKED`；
- `der.skip_overlap` 降低评分分母；
- `der.collar_ms` 改变边界 no-score 区域；
- 模板集合与 evaluator 锁定断言。

## 5. `cer.enabled` / `perf.enabled` 与死字段清理

- `cer.enabled=false`：逐条落 `{"cer":{"status":"na","reason":"metric_disabled"}}`，不计算编辑距离；聚合只写一条 `na(metric_disabled)`。
- `perf.enabled=false`：不写 `ttft_ms/finalize_ms/rtf_*/overhead_ratio/throughput`，报告不出现“有值但无意义”的读数。
- `cer_only` evaluator 现在真正只保留 CER（性能读数关闭）；`perf_30ms` 保留 CER + 性能读数。
- 移除不再有实现的字段：
  - `cer.join` / `cer.separator`：平台按单条 utterance 评分，`<sc>` 由 CER scorer 恒定剥离；
  - `naming.mode` / `naming.granularity`：固定 matching + 分段级；
  - `perf.repeat`：runner 不支持重复测量。

## 6. RunGroup（跨数据集）

- 新增 `run_group` 表与 `run.group_id`（migration `0037_run_group`）。
- `POST /api/v1/runs/groups`：为每个 Benchmark 各创建一条 Run，先对全部 Benchmark 预检，避免留下半个组。
- `GET /api/v1/runs/groups` / `GET /api/v1/runs/groups/{id}`：组列表与成员汇总。
- 组级 CER = `Σ(S+D+I) / Σref_len`，不对每条 Run 的 CER 等权平均。
- 前端：Run 创建弹窗支持多选 Benchmark；多选时进入 `/run-groups/:id`，Run 列表用“组”徽标回跳。

## 7. 验证

```text
make test          393 passed（新增 RunGroup API 与 disabled 指标用例）
make lint          All checks passed
make openapi-check 契约与基线一致
make e2e           10 passed, 1 skipped
cd web && npm run build   通过
cd web && npm run lint    0 errors（保留既有 warning）
```

## 8. 尚未实施

- `naming` 聚类模式与簇级聚合：为避免继续暴露“配置了不生效”的字段，已从 schema/UI 移除；
- 由 schema 驱动的通用口径表单：当前仍是前端按设计稿分组渲染 + 后端 `validate_params`/evaluator 锁定。完整动态表单会牺牲 `MetricCard` 分组与视觉层级，暂不作为本轮目标。