# 评分口径参数配置与实现差异（2026-11-11 审计）

> 本文回答三件事：参数怎么配、性能模式与 runner 实际行为是否一致、预置模板是否真的对应设计中的评测流程。
> 结论先写：**模板可以作为默认值，但部分字段目前是“保留字段”，不会改变评分/执行结果**；配置时不要假设它们已生效。

---

## 1. 参数怎么配

### 1.1 `text`：文本归一化

| 字段 | 建议 | 实际影响 |
|---|---|---|
| `strip_punct` | 中文 CER 默认 `true` | CER/ITN 的参考和假设都去标点；标点 F1 单独用原始文本，不受影响 |
| `strip_space` | 中文默认 `true` | 中文按字，英文/混合语料按词时可设 `false` |
| `lowercase` | 英文/混合语料 `true` | 只影响大小写 |
| `traditional` | 参考与设备输出繁简不一致时设 `to_simplified` | `keep` 不做转换 |
| `drop_fillers` | 只放真正的语气词 | 从 CER 文本中删除 |
| `drop_tags` | 默认已含 `[+]`、`[*]`、`[ENS]` 等 | 从文本中删除标记；不要把真实内容加入 |

### 1.2 `cer`

| 字段 | 建议 | 实际影响 |
|---|---|---|
| `enabled` | 默认 true | 关闭后逐条落 `na(metric_disabled)`，不计算编辑距离 |
| `engine` | `rapidfuzz` | 当前只支持这一个 |
| `aggregate` | 默认 `micro`；要等权看每条样本用 `macro`，报告两者都要用 `both` | 影响 `metric_value` 聚合 |
| `mixed` | `char` | 当前只支持字符级 |

`join` / `separator` 已移除：平台按单条 utterance 评分，没有多段拼接场景；AliMeeting 的 `<sc>` 由评分器恒定剥离。

### 1.3 `punct`

- `enabled=true` 表示参与评分；
- 数据集无标点参考 → `na(no_punct_reference)`；
- 设备无标点输出 → `na(device_no_punctuation)`；
- `match` 当前只接受 `position_and_type`，没有其他算法可选。

### 1.4 `itn`

- `enabled=true` 只在数据集有 ITN 参考时有效；
- `normalizer` 当前只接受 `wetext`；
- 没有正式 ITN 金标时，平台只给 `numeric_cer` 数字近似读数，不给 `itn_acc`。

### 1.5 `der`

| 字段 | 建议 | 实际影响 |
|---|---|---|
| `enabled` | 有说话人分段时开 | 开/关生效 |
| `collar_ms` | 官方 250 | 已传入 DER 评分 |
| `collar_semantics` | `half_width` | 固定半宽；UI 不再提供修改 |
| `skip_overlap` | 官方 AliMeeting 为 `false` | 已传入 DER 评分 |
| `uem` | `full` | 只支持 full |

### 1.6 `naming`

| 字段 | 建议 | 实际影响 |
|---|---|---|
| `enabled` | 有注册声纹时开 | 开/关生效 |
| `threshold` | 0.6 | 生效：低于阈值的 matched_id 视为未匹配 |
| `unknown_policy` | `fr` | 已生效：可选 FR/FA |

`mode` / `granularity` 已移除：命名固定为 matching + 分段级，不再提供无效开关。

### 1.7 `perf`

| 字段 | 建议 | 实际影响 |
|---|---|---|
| `enabled` | 默认 true | 关闭后不落 ttft/rtf/throughput，报告不出现假读数 |
| `mode` | 性能 Run 用 `stream` 或 `batch` | 见 §2；`batch` 实际是 WebSocket burst，不是非流式 HTTP |
| `chunk_ms` | 流式 30ms；批量 1000ms | 只在 `kind=perf` 且 `mode=stream` 生效；batch 固定 1 秒块 |
| `stream_input_rate` | 性能基线 1.0 | 只在 `kind=perf` 且 `mode=stream` 时生效；batch 不节流 |

`repeat` 已移除：runner 不支持重复测量，不再保留无效字段。

### 1.8 `coverage`

- `ok_threshold` 默认 0.95；
- 只影响指标可用性状态 `ok/partial/na`，不改变分数值。

---

## 2. 性能模式与代码实际行为

### 2.1 accuracy Run

代码固定：

```text
execution_mode = "burst"
transcribe_mode = "burst"
chunk_ms = 1000
input_rate = 1.0
lanes = 1
```

`profile.perf.*` 对 accuracy Run **不生效**；它只进入 profile hash，可能让缓存 key 变化。

### 2.2 perf Run

```text
profile.perf.mode = "stream" -> transcribe mode = stream，使用 chunk_ms / stream_input_rate
profile.perf.mode = "batch"  -> transcribe mode = burst（WebSocket 尽快发送）
lanes = Run 创建时的 1~8
```

“batch”不是一次上传整段音频的批处理接口；适配器仍然走 WebSocket，只是发送节奏变成 burst。

### 2.3 性能指标

- `rtf_dev = usage.inference_ms / audio_duration_ms`；
- `rtf_e2e` 只适用于 burst；
- `ttft_ms` 只在流式且有 partial 时有效；
- `finalize_ms` 在 burst 下只是积压排空时间；
- 缓存命中的 case 不进入性能统计；
- 性能指标**不读取** `usage.rtf`。

### 2.4 当前未实现/不生效

- 无遗留“配置了不生效”的字段：`cer.join/separator`、`naming.mode/granularity`、`perf.repeat` 已移除；
- `der.collar_semantics` 固定半宽；`collar_ms`/`skip_overlap` 已接入；
- `naming.unknown_policy` 已生效；
- `cer.enabled` / `perf.enabled` 已接入：关闭后只落 `na(metric_disabled)`，不写 0。

---

## 3. 预置模板实际差异

| 模板 | 与 default_zh 的实际差异 | 是否匹配名称/流程 |
|---|---|---|
| `default_zh` | 全指标开，resolver 按数据/设备判 N/A | ✅ 通用准确率默认值 |
| `alimeeting_official` | **锁定 evaluator**；固定 250ms collar、重叠参与、`<sc>` 剥离 | ✅ official 不在 UI 中改参数 |
| `dialect_sweep` | 口径与 default_zh 完全相同；差异在 Benchmark preset 的 bucket dims | ⚠️ 只是默认值/分桶组合，不是独立口径 |
| `speaker_full` | 口径与 default_zh 完全相同；naming 默认已是 matching+0.6 | ⚠️ 同上 |
| `perf_30ms` | 锁定；关闭 punct/itn/der/naming，保留 CER 与性能读数 | ✅ 名称已按实际行为调整 |
| `cer_only` | 锁定；只保留 CER，性能读数也关闭 | ✅ 名称与实现一致 |

Benchmark preset 的 subset / sampling / bucket dims 是实际生效的；模板本身只是 MetricProfile 参数。

---

## 4. 推荐配置

| 场景 | 推荐 |
|---|---|
| 普通中文准确率 | `default_zh` + micro CER + 需要时开 DER/命名 |
| AliMeeting far | `alimeeting_official` + `alimeeting_meeting` preset；暂时把 DER 视为固定 collar 250、重叠参与 |
| 方言横评 | `dialect_sweep` + `dialect/duration_bin` 分桶 |
| 快速回归 | `cer_only`；报告只有 CER，不含性能读数 |
| 性能基线 | `perf_30ms` + `perf_baseline` preset；Run 类型必须选 `perf`，并设置 lanes |
| ITN | 目前用 `default_zh`；没有单独的严格 ITN evaluator |

## 5. 待修正（按优先级）

1. `der.collar_ms` / `skip_overlap` 已接入；`collar_semantics` 固定半宽。
2. `perf.repeat` 已移除，runner 不再有无效字段。
3. `cer.join/separator` 已移除；`<sc>` 恒定剥离。
4. `naming.unknown_policy` 已生效；`mode/granularity` 已移除。
5. `cer.enabled`/`perf.enabled` 已接入；`cer_only` 真正只算 CER。
6. 官方 evaluator 已锁定；后续 `evaluator_version` 变更需新版本。