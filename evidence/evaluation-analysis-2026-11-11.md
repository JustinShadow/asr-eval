# 评测进度与分析能力增强（2026-11-11）

## 1. 单条音频内部进度

### 后端

- `transcribe_file(..., on_progress=...)`：设备每收到 `transcript.partial` /
  `transcript.segment` / `transcript.final`，回调已处理音频位置。
- `_run_target` 用闭包维护 `live_progress`，节流 2s 写入：
  - `RunTarget.stats.live = {processed_ms, audio_ms, percent, items[]}`；
  - `Run.progress.live` 同步写入，SSE `/runs/{id}/events` 每秒轮询后推送。
- 完成样本或 target 结束时移除 `live`，避免残留。
- 支持 perf 多路：多路 `items` 聚合为总 processed/audio。

### 前端

- Run 列表：运行中不再显示“等待执行”，显示“正在识别首条”或
  “当前音频 42%”，进度条使用 indeterminate 动画。
- Run 详情：顶部运行条显示 `0/4 条 · 当前音频 42%`、ETA、当前样本名和
  当前音频进度条；目标表新增“当前音频”列。
- 长音频首条完成前，进度条使用 `live.percent`，随当前音频推进；
- Run 列表在存在运行中任务时每 3s 兜底轮询，Run 详情每 2.5s 轮询；
- 详情页只保留一个执行进度条（评分进度条仅在评分阶段出现）；
- 关闭动画（prefers-reduced-motion）时，indeterminate 改为整条低透明度占位，不再停在中段。

## 2. 总览指标明细

- `run_summary` 的指标现在包含 `detail/scope/ci_low/ci_high`，并排除分桶行，
  避免同一指标重复。
- Run 详情「总览」新增“指标明细”卡片：
  - 标点 F1：Precision / Recall / 句末标点准确率 / TP-FP-FN；
  - DER / SA-DER：Miss / FA / Conf 组件；
  - 命名：正确、FR/FA、可评段数、声纹 EER；
  - 性能与文本：CER、数字 CER、RTF e2e、吞吐。

## 3. DER 错误位置与说话人分析

- `speaker.der(..., collect_errors=True)` 返回：
  - `errors[]`：连续 `miss/fa/conf` 区间（最长 300 段，含参考/假设 speaker）；
  - `mapping`：假设 speaker → 参考 speaker。
- 新接口：

```http
GET /api/v1/evaluations/{evaluation_id}/speaker-analysis
```

  返回 DER 汇总、逐样本组件、错误区间、speaker 映射；默认分析前 20 个样本。

- Run 详情新增「说话人」页签：
  - DER/Miss/FA/Conf 汇总与组成条；
  - 每个样本的错误时间线（红=漏识、黄=虚检、紫=串人）；
  - 错误区间列表与 speaker 映射。

## 4. Badcase 改进

- 默认 CER 筛选从 `>20%` 改为 `全部错误 · CER>0%`。
- 聚焦视图的“当前段”同时展示参考与识别，并标出替换/删除/插入。
- 详情头部 sticky，CER 常驻可见；整篇 diff 最大高度 40vh 内部滚动。
- Run 详情 Badcase 表：参考/识别两列限制宽度并 line-clamp 2 行，
  CER 列固定宽度，不再需要横向拖动。

## 验证

```text
make test          404 passed
make lint          All checks passed
make openapi-check 契约与基线一致
cd web && npm run build / lint   通过
```

另用 0162 的 10s 音频验证了 adapter 进度回调：8 次事件，
partial 从 5s 递增到 10s，最终 final 正常。