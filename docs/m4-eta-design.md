# M4 · ETA 设计说明

> 上游：`m2-device-design.md`（性能数据已从设备信息中移除）
> 结论：**ETA 基线由 M4 从历史评测结果派生**，不是设备属性，也不单独建"设备性能表"。

---

## 1. ETA 要解决什么

| 场景 | 需要 ETA 做什么 |
|---|---|
| 用户发起评测 | 知道"还要多久"，决定现在跑还是排到夜里 |
| 排队 | 估算前面任务何时释放设备 |
| 进度条 | 百分比要有意义，不能线性假设 |
| 异常 | 跑得比预期慢很多时，能提前发现（设备降频、过热、被其他负载影响） |

ETA 不是评测结果，是**运行期的估算**。它允许不准，但不允许"假装准"——所以要给出处与置信度。

---

## 2. 数据来源（M2 修改后的影响）

| 数据 | 来源 | 说明 |
|---|---|---|
| 每样本音频时长 `d_i` | `item.duration_ms` | 导入时已知，精确 |
| 每样本设备耗时 | `case_result.server_ms`（`usage.inference_ms`） | 评测产生 |
| 每样本端到端耗时 | `case_result.wall_ms` | 评测产生 |
| 每样本 `rtf_dev` | `case_result.rtf_dev` | 评测产生 |
| 条件键 | `mode / chunk_ms / input_rate / concurrency / diarization / itn` | 必须一起分组，否则不可比 |

**M2 修改后的影响**：

1. M2 不再提供性能表 → **M4 必须自己派生**；
2. 数据没有丢：`case_result` 本来就有 `server_ms / rtf_dev`；
3. M4 新增一个只读的派生模块（查询 + 缓存），**没有新的写路径**；
4. M2 仍提供 `limits.realtime_slots`，用于并发上限与排队。

**好处**：性能只有一个权威来源（评测结果），不会和设备表打架。

---

## 3. 基线是什么

"ETA 基线"就是**一组按条件分组的 `rtf_dev` 统计量**：

```
key = (deployment_id, mode, chunk_ms, input_rate, concurrency, diarization, itn, duration_bucket)
value = { p50, p90, n, window_start, window_end }
```

**为什么必须带 `duration_bucket`**：实测同一台设备，`rtf_dev` 随音频变长显著上升。

| 设备 | 4.2s | 30s | 120s |
|---|---|---|---|
| 205 / etsstt | 0.244 | 0.267 | 0.373 |
| 502 网关 | — | 0.077~0.15 | 0.11 |

用单一 rtf 会把长音频的耗时估少一半以上。

**时长桶**：`<5s / 5~15s / 15~60s / 1~5min / >5min`

---

## 4. 四级取值（从最可信到兜底）

```
L1 同 deployment + 同条件 + 同时长桶，最近 7 天，n >= 20   → median
L2 同 model_key + 同条件 + 同时长桶，n >= 20              → median（跨设备同模型）
L3 同 kind + 同条件 + 同时长桶，n >= 5                    → median
L4 内置常量表（按 kind × 桶）                              → 标记 fallback
```

`eta_source` 写入 Run：`deployment | cross_device | kind | fallback | observed`；运行中有足够本次样本时以 `observed` 标识滚动估算。
UI 上：`fallback` 显示"估算（样本不足）"，其他显示"基于历史 N 次评测"。

---

## 5. 计算公式（按 Run 类型分开）

记 `A` 为总音频时长，`c` 为并发（默认 1）。

### 5.1 accuracy（burst）

```
work = Σ_bucket (count_b × mean_d_b × rtf_b)
ETA  = work / c + fixed
```

### 5.2 perf（1x）

1x 节奏下墙上时间被"喂音频"钳住：

```
ETA = (A / c) × max(1, rtf_est) + tail_est + fixed
```

`rtf_est < 1` 时 ETA ≈ A/c + tail；`rtf_est > 1` 时开始积压。

### 5.3 fixed 固定开销

```
probe（20s）+ 声纹准备（若需要：n_spk × 1s + staging）+ scoring（≈ A × 0.005）+ 报告（5s）
```

### 5.4 失败与重试膨胀

```
ETA *= (1 + retry_rate_est × 2)      # 历史重试率，默认 0.02
```

### 5.5 给出区间，不只给点

```
ETA_p50 = 用 median 算
ETA_p90 = 用 p90 算
```

UI 展示：`预计 1h20m（1h05m ~ 1h45m）`。比一个假精确的数字诚实得多。

---

## 6. 运行中滚动修正

### 6.1 触发

- 每完成 1 个样本更新内部估计；
- 每 5 秒通过 SSE 推一次（避免刷屏）。

### 6.2 算法（分模式）

**burst**：
```
observed_rate = Σ(已完成样本的 audio_s) / Σ(已完成样本的 wall_s)
剩余工作量 = Σ_remaining (d_i × rtf_bucket)
ETA_rem = 剩余工作量 / c / (observed_rate / (c / rtf_avg))   # 简化为：
ETA_rem = Σ_remaining (d_i × rtf_observed_bucket) / c
```
实现上直接按 bucket 维护滚动 median，避免被个别慢样本带偏。

**1x**：
```
已发送音频 / 已用时长 ≈ c
ETA_rem = 剩余音频 / (实测发送速率 × c) + tail_est
```

### 6.3 三个防抖

| 问题 | 处理 |
|---|---|
| 前几个样本包含模型 warm-up | 前 3 个样本不计入估计；固定加 `warmup_s` |
| 个别超长样本拉偏 | 用 median，不用 mean；按 bucket 分开 |
| 设备降频/被干扰 | 连续 2 个窗口速率下降 >30% → 提示"速度异常"，并把 ETA 改为 P90 |

---

## 7. 校准切片（可选，提升首估）

正式开跑前先跑一小批分层样本：

```
每个时长桶取 3 条，最多 12 条
burst 模式耗时 ≈ 12 × 5s × 0.3 ≈ 18s
```

用途：
1. 立刻拿到**本条件下**的 rtf，而不是依赖历史；
2. 顺带验证设备、参数、声纹是否就绪（相当于 smoke test）；
3. 校准结果写入本次 Run 的内存基线，不落设备表。

**默认开启**（成本 20 秒，收益明显）；可在 Run 参数里关闭。

---

## 8. 置信度

| 条件 | 置信度 | UI |
|---|---|---|
| 任一桶 n < 5 | low | "估算，样本不足" |
| 5 ≤ n < 20 | medium | "基于 N 次样本" |
| n ≥ 20 且来源为 deployment | high | "基于本设备 N 次历史" |

ETA 误差目标：
- 首估（无历史、无校准）：±50%
- 有校准切片：±25%
- 稳定段（已完成 >20%）：±15%

---

## 9. 多 target 与排队

```
Run 的 ETA = max(各 target 的 ETA)          # 设备并行
排队等待时间 = Σ(前面任务在该设备上的剩余 ETA)
用户看到的 ETA = 排队时间 + Run ETA
```

---

## 10. 与"设备性能表"方案的对比

| 维度 | 设备性能表（旧） | 派生基线（新） |
|---|---|---|
| 数据来源 | 探测或评测写入设备表 | 只读 `case_result` |
| 一致性 | 两处来源，可能冲突 | 单一来源 |
| 历史断裂风险 | 数值进设备表，易被当身份 | 无 |
| 条件维度 | 设备表放不下（mode/chunk/concurrency…） | 天然按条件分组 |
| 可重建 | 否 | 是（随时重算） |
| 概念 | 设备属性 | 评测结果的索引 |

---

## 11. 边界与异常

| 场景 | 行为 |
|---|---|
| 任务暂停/续跑 | ETA 只算剩余样本 |
| 条件变化中断 | ETA 停止更新，显示"已中断" |
| 设备 overload | ETA 置为"不可用"，直到恢复 |
| 多 target 部分失败 | 按存活 target 重算 |
| 历史里有异常样本（超时） | 排除 `status != ok` 的样本 |

---

## 12. 验收

1. 有 500 条历史时，Run 开始前 ETA 与最终真实耗时的误差 < 30%；
2. 无历史时，校准切片后误差 < 35%；
3. 稳定段（完成 20% 后）误差 < 15%；
4. ETA 不会出现"倒退变大"超过 1 次（除设备异常）；
5. 所有 ETA 都能回答来源（deployment/cross_device/kind/fallback）。
