# M5 打分与聚合 · 详细设计卡 v0.1

> **实现状态（更新于 2026-11-07）**
> - 已实现 CER 的 profile 归一化、逐样本分数、micro/macro 聚合与 utterance bootstrap CI；繁转简由 OpenCC 执行，语气词清理按 profile 配置。
> - 标点 F1、ITN 实体准确率、DER、SA-DER、命名与簇级 EER 已接入评分；DER 保留重叠说话人并按 session 计算 CI。
> - 性能指标已写入目标级与时长桶 `metric_value`，包含中位数、P95、吞吐；缓存结果不进入性能观测。
> - 已支持对既有 Run 按新 profile 重评分，保留旧 Evaluation；覆盖率状态、原因码与逐样本结果均落库。
> - 无 ITN 金标时记录 `numeric_cer` 数字相关 CER 近似读数；逐样本及 Evaluation、target、时长桶聚合均保存误差分解与覆盖率。该读数不替代正式 ITN 准确率。
> - 依赖锁定包含 `wetext==0.1.8` 与 `opencc-python-reimplemented`；PostgreSQL 集成测试、静态检查与迁移检查已通过。
> - AliMeeting `Test_Ali_far` 的 20 场会话已按官方 0.25 秒 no-score collar、保留重叠语音的口径完成 DER 交叉核对：项目实现、dscore、md-eval 与 pyannote 的全量及单会话最大差值均为 0.000pp。
> - Evaluation 评分已改为固定并发 worker 和有界队列（2 个 worker、100 个排队容量）；启动时恢复未完成任务，按 500 条一页提交进度，支持取消、游标续跑与 SSE。
> - M5 §9 验收门禁已通过：10,000 条 CER-only case 在 60 秒内完成；100 个任务排队时并发保持为 2；断点续跑结果与同口径从头评分逐项一致；一条 Run 的多个 Evaluation 相互独立。
> - PostgreSQL 全量后端测试、Ruff、格式检查、mypy，以及迁移升级/降级检查均已通过。
> - **M5 模块验收已通过，可以进入 M6。** 本地没有 `out/probe_*.json` 设备探测金标；AliMeeting 三引擎核对使用确定性探测假设，只验证评分口径，不代表设备识别质量。

> 上游：M4（`case_result` / `hypothesis` / `evaluation`）、M1（参考）、M3（口径与可用性）
> 下游：M6（报告与对比）
> 本模块只做"把执行结果变成可解释的数字"，**不接触设备**。

### 0.1 决策 → 章节追溯

| 决策 | 落点 |
|---|---|
| CER `<sc>` 默认 `strip`，可切 `count` | §5.2 |
| ITN 一期 6 类实体 | §5.4 |
| EER 接受簇级粒度（方案 A） | §5.6、§10 第 5 条 |
| DER 三引擎：dscore / pyannote / md-eval | §5.5、§8.2 |
| AliMeeting 官方口径 = dscore、collar 0.25、不跳重叠 | §5.5 |
| SA-DER 禁用最优映射 | §5.5 |
| 命名：matched_id + FA/FR + 阈值 | §5.6 |
| 单 Run CI 在评分时算；两两 p 值由 M6 调 | §5.8、§3.1 |
| bootstrap 单元：utterance / session | §5.8 |
| 覆盖率门槛 ok≥0.95 / partial / na | §5.9 |
| 标点仅在 `punct_supported` 时评 | §5.3、§7 |
| 性能指标排除 `cached` | §5.7 |
| 禁用 `usage.rtf` | §5.7 |
| 指标结果带 `status/reason/n/ci` | §2.1、§4 |

---

## 1. 职责与边界

### 1.1 负责

| 职责 | 说明 |
|---|---|
| 逐样本打分 | 读 `case_result` + `hypothesis` + 参考，写 `case_score` |
| 指标插件 | CER / 标点 / ITN / DER / SA-DER / 命名 / EER / 性能 |
| 归一化 | 文本归一化流水线（可配置、带 hash） |
| 聚合 | micro / macro、按 subset、按分桶 |
| 置信区间 | bootstrap（单 Run） |
| 覆盖判定 | `ok / partial / na` + 原因码 |
| 评分版本 | `scorer_version`，保证可追溯 |
| 断点续评 | 按 `case_result` 游标分页处理 |

### 1.2 不负责

| 不负责 | 归属 |
|---|---|
| 执行与调度 | M4 |
| 两两显著性（p 值） | M6（调用 M5 的配对函数） |
| 图表与导出 | M6 |
| 设备能力判定 | M2 / M3 |

### 1.3 关键约束

1. **纯函数**：同样的输入 + 同样的 `scorer_version` + 同样的 `profile_hash` → 同样的数字。
2. **不补 0**：不可评就是 `na` + 原因码。
3. **逐样本指标与聚合指标分开存**：`case_score` / `metric_value`。
4. **评分失败不污染执行状态**：Evaluation 独立。

---

## 2. 领域对象

```python
Evaluation:              # 由 M4 创建
  id, run_id, metric_profile_id, profile_hash, scorer_version
  trigger, status, coverage

CaseScore:
  evaluation_id, case_result_id
  metrics: dict          # 见 §5
  detail_uri             # 对齐明细（本地对象目录）

MetricValue:             # 聚合
  evaluation_id, run_target_id, subset
  metric, scope          # micro | macro
  bucket: dict
  value, n
  ci_low, ci_high
  status                 # ok | partial | na
  reason
```

### 2.1 逐样本指标结构

```jsonc
{
  "cer": {"status": "ok", "value": 0.068, "s": 12, "d": 3, "i": 1, "n": 200},
  "punct_f1": {"status": "ok", "value": 0.79, "tp": 40, "fp": 12, "fn": 9},
  "der": {"status": "ok", "value": 0.123, "fa": 1.2, "miss": 0.8, "conf": 0.5},
  "naming_acc": {"status": "ok", "value": 0.881, "fa": 0.042, "fr": 0.077},
  "itn_acc": {"status": "na", "reason": "no_itn_reference"}
}
```

`detail_uri`：字符级对齐、DER 错误区间、命名混淆矩阵（体积大，放本地对象目录）。

---

## 3. 接口

### 3.1 服务

```python
class EvaluationService:
    def create(self, run_id, profile_id, trigger, actor) -> Evaluation
    def get(self, evaluation_id) -> Evaluation
    def list(self, run_id) -> list[Evaluation]
    def cancel(self, evaluation_id) -> None
    def events(self, evaluation_id) -> AsyncIterator[Event]

class ScoringEngine:
    def run(self, evaluation_id) -> None            # 幂等、可续
    def score_case(self, ctx: CaseContext, profile) -> CaseScore
    def aggregate(self, evaluation_id, target_id, subset) -> list[MetricValue]

class Significance:                                 # M6 调用
    def paired_delta(self, eval_a, eval_b, metric, bucket, n_resample) -> Delta
```

### 3.2 HTTP

```
POST   /api/v1/runs/{id}/evaluations
GET    /api/v1/runs/{id}/evaluations
GET    /api/v1/evaluations/{id}
POST   /api/v1/evaluations/{id}/cancel
GET    /api/v1/evaluations/{id}/events
GET    /api/v1/evaluations/{id}/matrix
GET    /api/v1/evaluations/{id}/buckets
GET    /api/v1/evaluations/{id}/cases?metric=&bucket=&cursor=
GET    /api/v1/evaluations/{id}/cases/{case_id}   # 三方对齐明细
```

### 3.3 事件

```
evaluation.queued / evaluation.scoring / evaluation.completed / evaluation.failed
evaluation.progress {scored, total, phase}
```

---

## 4. 数据结构

```sql
create table case_score (
  id bigserial primary key,
  evaluation_id uuid not null references evaluation(id) on delete cascade,
  case_result_id bigint not null references case_result(id) on delete cascade,
  metrics jsonb not null,
  detail_uri text,
  unique (evaluation_id, case_result_id)
);

create table metric_value (
  id bigserial primary key,
  evaluation_id uuid not null references evaluation(id) on delete cascade,
  run_target_id uuid not null references run_target(id) on delete cascade,
  subset text not null,
  metric text not null,
  scope text not null,                 -- micro | macro
  bucket jsonb not null default '{}',
  value double precision,
  n int not null default 0,
  ci_low double precision, ci_high double precision,
  status text not null,                -- ok | partial | na
  reason text
);
create index metric_lookup on metric_value (evaluation_id, subset, metric);
```

---

## 5. 指标实现

### 5.1 归一化流水线

```
raw text
  → 去特殊标记（[+] [*] [ENS] …）
  → 繁简（按 profile.traditional）
  → 大小写（英文）
  → 去标点 / 空格（按 profile，CER 用）
  → 去语气词（默认关闭）
  → NormalizedText { text, index_map }   # 保留到原文字的索引映射
```

`index_map` 用于标点指标：在去标点后的字序列上做对齐，再把位置映射回原标点。

### 5.2 CER / WER

```
ops = rapidfuzz.distance.Levenshtein.editops(ref_norm, hyp_norm)
S/D/I = count(replace/delete/insert)
cer = (S+D+I) / len(ref_norm)
```

- 引擎：`rapidfuzz`
- 分字/分词：中文按字、英文按词、混合按"字+词"（`profile.cer.mixed`）
- 聚合：micro = Σ(S+D+I)/Σlen(ref)
- AliMeeting FIFO：参考按段起始排序拼接；`separator_policy` 决定 `<sc>` 是否计入
  - `strip`（默认）：只用于排序，CER 前去掉
  - `count`：严格复现官方（把 `<sc>` 当字符）

### 5.3 标点

```
1. 在去标点字序列上对齐 ref / hyp（复用 CER 的 editops）
2. 把 ref 的标点位置映射到对齐后的索引
3. 比对：位置对且类型对 → TP；参考有 hyp 无 → FN；hyp 有参考无 → FP
P/R/F1 + sentence_end_acc（句末标点单独统计）
```

门槛：`profile` 与 dataset 都必须 `punct_supported=true`。

### 5.4 ITN（一期实现算法，二期优化）

```
实体类型：数字 / 日期 / 时间 / 金额 / 百分比 / 单位
参考侧：profile.itn.enabled 且 dataset.itn_ratio >= 0.95
比较：ref.itn_expected vs hyp，按实体类型分别算准确率
归一化：WeTextProcessing（TN/ITN）
```

无金标 → 正式 ITN 指标记 `na(no_itn_reference)`。另从与 CER 相同的编辑对齐中归因参考数字/数词字符，输出 `numeric_cer` 近似读数：数字相关编辑数 ÷ 参考数字相关字符数；聚合到 Evaluation、target 与时长桶。没有数字参考时记 `na(no_numeric_reference)`。数词格式差异也计入该近似值，所以它不能解释为语义 ITN 准确率。

### 5.5 DER / SA-DER

**参考构建**：`ref_segments`（`skip=false`）→ RTTM。
**假设构建**：`hyp.segments` → RTTM。
**UEM**：`[0, audio_duration]`。

| 场景 | 引擎 | 参数 |
|---|---|---|
| AliMeeting 官方口径 | **dscore** | `collar=0.25`、不跳重叠、匈牙利映射 |
| 通用 | **pyannote.metrics** | `collar`、`skip_overlap=False` |
| CI 验收 | **md-eval.pl** | `-c 0.25` |

**collar 语义必须验收**：`dscore` / `md-eval` / `pyannote` 三者对 `0.25` 的解释要归一（单侧 vs 总宽），差值 < 0.2pp 才能上线。

AliMeeting 官方评测计划采用 0.25 秒 collar，重叠区参与打分；测试集包含 20 场远场会话（[M2MeT 论文](https://arxiv.org/abs/2110.07393)、[AliMeeting 数据集说明](https://www.openslr.org/119/)）。`md-eval-22.pl -c 0.25` 与项目的 ±250ms 一致；pyannote 的 `collar=0.5` 实测与该口径一致。验证脚本 `backend/validate_der_engines.py` 使用 dscore commit `e02f949ac6592279300a2c33d03daf9e0c12fd27`、锁定依赖中的 pyannote.metrics 和配套 `md-eval-22.pl`。脚本为每场会话固定生成包含 miss、false alarm、confusion 的探测假设，并校验 20 场逐场及全量汇总；本地结果最大差值均为 `0.000pp`。

复跑方式（数据路径指向 `Test_Ali_far`）：

```bash
cd backend
uv run --group diarization-validation python validate_der_engines.py \
  --dataset-root /path/to/Test_Ali_far \
  --dscore-dir /path/to/dscore
```

**SA-DER**：把 hyp 的簇标签替换为 `matched_id`（未匹配 → 唯一 unknown 桶），**禁用最优映射**，直接按标签比对；复用同一套帧级算法。

### 5.6 命名

```
ref: 每个 segment 的 speaker
hyp: 每个 sentence 的 spk → spk_voiceprints[spk].match_info.matched_id
匹配：按时间重叠取参考说话人
指标：
  sentence_acc = 正确句数 / 有 spk 的句数
  cluster_acc  = Σ(簇时长 × 正确?) / Σ簇时长
  FA = matched_id 存在但 ≠ 参考
  FR = matched_id 为空
```

**EER**：把 `confidence` 当分数，扫 `speaker_match_threshold`。

**置信度粒度说明（重要）**

设备返回的 `confidence` 在 `spk_voiceprints[label].match_info.confidence`，是**每个说话人簇一个值**：

```jsonc
"spk_voiceprints": {
  "0": {"match_info": {"matched_id": "SPK8005", "confidence": 0.70}},
  "1": {"match_info": null},
  "2": {"match_info": {"matched_id": "SPK8008", "confidence": 0.55}}
}
```

也就是说：同一簇里的所有句子共享同一个分数。EER 的"试验单元"因此是**簇**，不是句子。

后果：
- 一次 AliMeeting 会话只有 2~4 个簇，20 段合计约 60~80 个试验 → EER 分辨率约 1.5%，置信区间很宽
- 混簇（一个簇里混了多个真人）的分数没有意义，需单独标注
- 无法做"逐句阈值"分析

要拿到句级 EER，需要设备返回**每句/每切片**的置信度或声纹向量；当前协议没有。

> 2026-11-11 实现审计：性能/口径字段与代码的实际差异见 `docs/metric-profile-configuration.md`。

### 5.7 性能指标

| 指标 | 来源 | 聚合 |
|---|---|---|
| `ttft_ms` | `case_result.ttft_ms` | median / P95 |
| `finalize_ms` | `case_result.finalize_ms` | median / P95 |
| `rtf_dev` | `case_result.rtf_dev` | median / P95 |
| `rtf_e2e` | `case_result.rtf_e2e` | median（**仅 burst**） |
| `rtf_realtime` | `finalize_ms / audio_duration` | median（仅 1x） |
| `overhead_ratio` | `(total_cost − inference) / duration` | median（仅 burst，设备自报） |
| `throughput` | Σaudio / Σwall | 每 run_target |

- **排除 `cached`** 的 case
- 按 `duration_bin` 分桶
- 1x 模式下 `rtf_e2e` 不参与横评（恒 ≥ 1）
- **禁止使用 `usage.rtf`**：其分母是"有效语音时长"而非音频时长，长音频偏差可达 3.7 倍；一律用 `inference_ms / audio_duration_ms`
- 流式与非流式的时延指标**分列**，不合并、不跨模式比较

### 5.7.1 性能指标的有效性条件

| 指标 | 有效条件 |
|---|---|
| `rtf_dev` | 已 warmup；并发与功能开关固定 |
| `rtf_e2e` | 仅 burst 发送 |
| `rtf_realtime` | 仅 1x 发送 |
| `ttft_ms` | 仅流式，且 `observed.partials=true` |
| `finalize_ms` | 仅 1x 有意义；burst 下等于积压排空 |
| `overhead_ratio` | 仅 burst |

### 5.8 聚合与置信区间

```
micro：Σ分子 / Σ分母（按 subset、按桶）
macro：逐 case 求均值

CI：bootstrap
  CER / 标点 / ITN → 重采样单元 = utterance
  DER / 命名 / SA-DER → 重采样单元 = session
  默认 1000 次，95% 区间
```

**为什么单元不同**：同会话内句子高度相关，按句子重采样会低估方差。

### 5.9 覆盖率与状态

```
coverage = 该指标可评 case 数 / 计划 case 数
>= 0.95 → ok
(0, 0.95) → partial
= 0 或硬能力缺失 → na（带原因码）
```

`metric_value.status` 与 `reason` 直接落库，报告不再二次判断。

---

## 6. 执行流程

```
Evaluation.queued
  → scoring:
      for each run_target（按 id）:
         for page in case_result（500/页，按 id 游标）:
             score_case → 批量写 case_score
             更新 progress
         aggregate → 写 metric_value（含 CI）
      → completed
```

- **幂等**：`(evaluation_id, case_result_id)` 唯一；重跑覆盖
- **可续**：记录 `last_case_result_id`
- **取消**：置 `cancelling`，当前页完成后退出
- **失败**：写 `evaluation.status=failed` + error，不影响 Run

---

## 7. 异常与降级

| 场景 | 行为 |
|---|---|
| 参考为空 | 该 case `na(empty_reference)`，不计入分母 |
| hypothesis 缺失 | `na(no_hypothesis)` |
| 设备无标点输出 | 整指标 `na(device_no_punctuation_output)` |
| 数据集无句末标点 | `na(no_sentence_punctuation)` |
| DER 参考无分段 | `na(no_segment_timeline)` |
| 命名缺声纹 | `na(enrollment_missing)` |
| 三方引擎差值 > 0.2pp | Evaluation 标 `degraded`，报告警 |
| 打分异常（单条） | 该 case `error`，不中断整批 |

---

## 8. 测试点

### 8.1 单元

| 用例 | 断言 |
|---|---|
| CER 基本 | 手工构造 S/D/I，数值精确 |
| 归一化 | 繁简、标点、空格、语气词开关 |
| 标点位置映射 | 去标点后仍能定位原标点 |
| DER 完美假设 | 0 |
| DER 单一错误 | 与手算一致 |
| SA-DER 固定映射 | 不做最优置换 |
| 命名 FA/FR | 构造错认/未识别 |
| EER | 构造已知分布，EER 可复现 |
| 覆盖率 | 0.94 → partial，0.95 → ok |
| bootstrap | 固定 seed，CI 可复现 |

### 8.2 集成 / 一致性

| 用例 | 断言 |
|---|---|
| AliMeeting 20 场会话 | `backend/validate_der_engines.py`：项目实现 / dscore / md-eval / pyannote 全量和逐会话差值 < 0.2pp；验证包含 miss、FA、confusion |
| AliMeeting CER | FIFO 口径与手工拼接一致 |
| AISHELL-1 | 只有 CER，其他 na 且原因正确 |
| APY | punct=na；DER/命名 ok |
| 性能 | cached 被排除；分桶正确 |
| 断点续评 | 中途 kill 后补齐，不重复 |

### 8.3 黄金样例

有设备探测结果时，将 `out/probe_*.json` 转成 hypothesis，配合 AliMeeting TextGrid 做端到端断言。本地尚无该类 probe 输出；当前 DER 引擎交叉核对使用脚本生成的确定性探测假设，不代表设备识别质量评测。

---

## 9. 验收

1. AliMeeting 20 段：DER 三引擎差值 < 0.2pp。
2. 同一 `evaluation` 重跑，`case_score` 与 `metric_value` 完全一致（可复现）。
3. 不可评指标全部为 `na` + 原因码，无 0 值。
4. 10,000 case 打分 < 60s（不含 DER）。
5. 100 个 Evaluation 并发排队时，资源可控（评分 worker 池）。
6. 一号多评分互不覆盖；终态评分可从 Run 详情删除（`DELETE /evaluations/{id}`，admin，报告引用时拒绝）。

---

## 10. 契约与风险

| 下游 | 需要什么 |
|---|---|
| M6 | `metric_value`（含 CI/status/reason）、`case_score`、`detail_uri` |
| M6 | `Significance.paired_delta`（配对 bootstrap） |
| M7 | 评分进度 SSE、取消 |

| # | 风险 | 处理 |
|---|---|---|
| 1 | collar 语义不一致 | 三方验收门禁 |
| 2 | 归一化规则漂移 | `profile_hash` 进报告 |
| 3 | 打分代码升级 | `scorer_version` 进 Evaluation |
| 4 | DER 样本少（20 段） | 报告标注 CI 与样本量 |
| 5 | EER 粒度是簇级 | 报告中注明 |
