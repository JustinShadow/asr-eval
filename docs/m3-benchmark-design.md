# M3 评测定义 · 详细设计卡 v0.1

> **实现状态（截至 2026-11-06）**
> - 已实现 MetricProfile 与 Benchmark 创建、查询、ETag 更新、归档及权限校验；口径按名称递增版本，被基准引用后通过新版本演进。
> - Run 创建时自动生成 `default_zh` 口径与 `<version>-default` Benchmark。
> - 已实现 all / random / stratified 确定性采样；支持 `order=item_key` 复现“每组前 N 条”与 `filter.speakers/item_key_prefix` 收窄样本；短分层在预览中告警。
> - `POST /benchmarks/preview` 返回每个子集抽样条数、时长、桶分布、数据缺口和设备 × 子集指标状态，不写入数据；列表也返回采样后的条数和时长。
> - 可用性由纯函数根据数据能力、设备能力、口径开关、覆盖阈值和声纹注册前置状态解析为 `ok / partial / na`，并提供原因码。
> - 前端已实现基准库、定义编辑器、分桶选择、预览矩阵、口径模板/版本管理和 profile hash 展示；保存前必须通过当前配置对应的预览。
> - 验收：后端 M3 相关集成/单元测试通过；前端构建、Lint、Playwright 主流程及 1440 浅色 / 1024 深色 Axe 检查通过。

> 上游：M1（`dataset_version.capabilities`）、M2（`deployment.capabilities`）
> 下游：M4（按 Benchmark 执行）、M5（按 MetricProfile 打分）、M6（按 bucket 出表）
> 本模块把"考卷 + 评分标准 + 抽样"组合成**不可变、可比**的 Benchmark。

---

## 1. 职责与边界

### 1.1 负责

| 职责 | 说明 |
|---|---|
| 口径定义 | MetricProfile：归一化、指标开关、DER 参数、性能参数 |
| 口径校验 | JSON Schema + 语义校验（未知指标、非法 collar 等） |
| 口径模板 | 预置常用口径，一键复用 |
| 组卷 | Benchmark = DatasetVersion + MetricProfile + Sampling + Buckets + Params |
| 采样 | all / random / stratified，确定性可复现 |
| 分桶规则 | 固定维度 + 派生区间 |
| 可用性解析 | `(dataset caps, device caps, profile) → ok/partial/na + reason` |
| 版本哈希 | `version_hash`，用于"可比性"判定 |

### 1.2 不负责

| 不负责 | 归属 |
|---|---|
| 数据导入与能力统计 | M1 |
| 设备能力 | M2 |
| 执行与调度 | M4 |
| 指标计算 | M5 |
| 图表与报告 | M6 |

### 1.3 关键约束

1. **Benchmark 不可变**：改口径/采样 = 新 Benchmark。
2. `version_hash` 相同才允许直接比较。
3. 采样必须**确定性**：同 seed 同结果。
4. 可用性解析必须**纯函数**：不查库、不调设备，输入输出可单测。

---

## 2. 领域对象

```python
MetricProfile:
  id, name, version, config: dict, hash: str, created_by, created_at

Benchmark:
  id, name
  dataset_version_id, metric_profile_id
  subsets: list[str]          # ★ 必填；默认取该版本的 primary
  sampling: dict
  bucket_rules: dict
  params: dict
  version_hash: str
  created_by, created_at, note

MetricStatus:
  name: str
  status: "ok" | "partial" | "na"
  reason: str | None       # 原因码
  coverage: float | None   # partial 时的覆盖率

RunGroup:
  id, name, kind, created_by, created_at
  # 组成员是 Run.group_id；一个 Benchmark 只绑定一个 DatasetVersion，
  # 多数据集横评由 RunGroup 为每个 Benchmark 各建一条 Run。
```

### 2.0 RunGroup 与多数据集

- `POST /api/v1/runs/groups` 接收 `benchmark_ids[]` 与同一组设备目标，为每个 Benchmark 各创建一条 Run；
- 组内 Run 共享 `group_id`，名称追加为 `组名 / Benchmark`；
- `GET /api/v1/runs/groups/{id}` 返回成员 Run 与**按 ref_len 加权**的组级 CER：

```text
group_cer = Σ(S+D+I) / Σref_len
```

不同数据集样本量差异大，组级结果不能对每条 Run 的 CER 做等权平均。
子集必须取所选 Benchmark 的交集；没有共同子集时创建前就返回语义错误。

### 2.1 MetricProfile 结构

```jsonc
{
  "text": {
    "strip_punct": true,
    "strip_space": true,
    "lowercase": true,
    "traditional": "keep",              // keep | to_simplified
    "drop_fillers": [],
    "drop_tags": ["[+]","[*]","[ENS]","[LAUGHTER]","[SONANT]","[MUSIC]","[SYSTEM]","[FILLER]"]
  },
  "cer": {
    "enabled": true,                    // false 时 CER 记 na(metric_disabled)
    "engine": "rapidfuzz",
    "aggregate": "micro",               // micro | macro | both
    "mixed": "char"
    // <sc> 由评分器恒定剥离，不再暴露 join/separator
  },
  "punct": { "enabled": true, "match": "position_and_type" },
  "itn":   { "enabled": false, "normalizer": "wetext" },
  "der":   {
    "collar_ms": 250,
    "collar_semantics": "half_width",
    "skip_overlap": false,
    "uem": "full"
  },
  "naming": {
    "enabled": true,
    "threshold": 0.6,
    "unknown_policy": "fr"              // fr | fa
    // 命名固定 matching + 分段级
  },
  "perf": {
    "enabled": true,                    // false 时不落性能指标
    "mode": "batch",                    // stream | batch
    "chunk_ms": 1000,                   // 仅 stream 生效；batch 固定 1 秒块
    "stream_input_rate": 1.0            // 仅 stream 生效；batch 不节流
  }
}
```

### 2.2 采样

```jsonc
{ "mode": "all" }

{ "mode": "random", "n": 1000, "seed": 42 }

{ "mode": "stratified", "dim": "duration_bin",
  "per_stratum": 30, "seed": 42, "order": "seed_hash" }

// 复现历史固定测试集
{ "mode": "stratified", "dim": "dialect",
  "per_stratum": 250, "order": "item_key" }
{ "mode": "all", "filter": { "speakers": ["S0765", "S0766"] } }
```

### 2.3 分桶规则

```jsonc
{
  "dims": ["dialect", "scene", "duration_bin", "overlap_bin", "gender", "split"]
}
```

| 维度 | 来源 | 说明 |
|---|---|---|
| `dialect` / `scene` / `lang` / `gender` / `split` / `source` | `item.buckets`（导入时打标） | 静态 |
| `duration_bin` | 由 `item.duration_ms` 派生 | `<5s / 5~15s / 15~60s / 1~5min / >5min` |
| `overlap_bin` | 由版本 `capabilities.overlap_ratio` 或分段派生 | `0 / 0~10% / 10~30% / >30%` |
| `text_len_bin` | 由参考字数派生 | `<10 / 10~30 / 30~100 / >100` |

> 派生维度在 M5 聚合时计算，不写回 `item`，避免维度组合爆炸。

**扩展方式**：所有维度注册在 `BUCKET_DIMS` 表里（代码内注册表），每条含：

```python
BucketDim(name, kind: "static"|"derived", extractor, bins: list[BinDef] | None)
```

新增维度 = 注册一条 + 加 UI 标签，**不改表结构**。`bucket_rules` 里保存维度名与 bin 定义，已冻结的 Benchmark 不受后续新增维度影响。

---

## 3. 接口

### 3.1 服务

```python
class MetricProfileService:
    def create(self, req) -> MetricProfile
    def list(self) -> list[MetricProfile]
    def get(self, id) -> MetricProfile
    def templates(self) -> list[Template]
    def validate(self, config: dict) -> ValidationResult
    def from_template(self, name: str, overrides: dict) -> dict

class BenchmarkService:
    def create(self, req) -> Benchmark
    def list(self, filters) -> list[Benchmark]
    def get(self, id) -> Benchmark
    def preview(self, req) -> BenchmarkPreview
    def archive(self, id) -> None            # 被 Run 引用时只允许归档

class MetricResolver:                        # 纯函数
    def resolve(self, benchmark: Benchmark,
                caps_by_subset: dict[str, dict],
                device_caps: dict) -> dict[str, list[MetricStatus]]
    # 返回 {subset: [MetricStatus]}；每个 subset 独立判定
```

### 3.2 HTTP

```
GET    /api/v1/metric-profiles
POST   /api/v1/metric-profiles
GET    /api/v1/metric-profiles/{id}
POST   /api/v1/metric-profiles/validate
GET    /api/v1/metric-profiles/templates

GET    /api/v1/benchmarks
POST   /api/v1/benchmarks
GET    /api/v1/benchmarks/{id}
POST   /api/v1/benchmarks/preview
POST   /api/v1/benchmarks/{id}/archive
GET    /api/v1/benchmarks/{id}/availability?deployment_id=
```

### 3.3 事件

```
benchmark.created
benchmark.archived
```

---

## 4. 数据结构

```sql
create table metric_profile (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  version int not null default 1,
  config jsonb not null,
  hash char(16) not null,
  note text,
  created_by text,
  created_at timestamptz not null default now()
);
create unique index metric_profile_hash on metric_profile (hash);
create index metric_profile_name on metric_profile (name, version desc);

create table benchmark (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  dataset_version_id uuid not null references dataset_version(id),
  metric_profile_id uuid not null references metric_profile(id),
  subsets jsonb not null default '[]',
  sampling jsonb not null default '{"mode":"all"}',
  bucket_rules jsonb not null default '{"dims":["dialect","duration_bin"]}',
  params jsonb not null default '{}',      -- itn / diarization / max_spk / threshold
  version_hash char(16) not null,
  note text,
  archived boolean not null default false,
  created_by text,
  created_at timestamptz not null default now()
);
create index benchmark_dataset on benchmark (dataset_version_id, created_at desc);
create index benchmark_hash on benchmark (version_hash);
```

---

## 5. 关键流程

### 5.1 创建口径

```
选择模板 → 编辑 → validate()
  ├─ JSON Schema 校验（字段类型/枚举）
  ├─ 语义校验（指标名已注册、collar>=0、skip_overlap 与数据集匹配）
  └─ 生成 config 的规范化 JSON（键排序、去空值）
→ hash = sha256(canonical_json)[:16]
→ 持久化
```

**口径 hash 只覆盖"影响数字"的字段**：`text / cer / punct / itn / der / naming / perf`。
`name`、`note`、`created_by` 不参与。

### 5.2 创建 Benchmark

```
1. 选 DatasetVersion（必须 status ∈ {ready, ready_with_errors}）
2. 选 subset（默认勾选 `is_primary`；可多选，但要多条件对比时才勾多个）
3. 选 MetricProfile
4. 配 sampling（**按 subset 各自采样**，同一规则）
5. 配 params（itn / diarization / max_spk / threshold）
6. 配 bucket dims
7. preview：按 subset 分别给出条数、总时长、各桶计数、缺口提示
8. 生成 version_hash → 保存
```

### 5.3 采样（确定性）

采样在**每个 subset 内独立执行**（同一规则），避免 phone/mic 条数不同导致比例失衡。

**all**：全量。

**random**：
```
按 hash(seed + "|" + content_uid) 排序，取前 n 条
```
→ 与库内顺序无关，跨环境可复现。

**stratified**：
```
按 dim 分组
每个 stratum：min(per_stratum, size) 条
默认按 hash(seed + "|" + content_uid) 排序
```

**order = item_key**（random / stratified 可选）：
```
组内按 item_key 升序排序后再取前 N 条
```
用于复现“每个子目录前 250 条”“某说话人前 N 条”这类历史固定测试集。
`item_key` 是导入时的文件名 stem，KeSpeech 导入器按文件名排序，因此排序结果与
导入顺序一致；默认仍是 `seed_hash`，不改变既有基准。

**filter**（所有模式可选）：
```jsonc
{"speakers": ["S0765", "S0766"], "item_key_prefix": "BAC009S0765"}
```
先按说话人交集 / item_key 前缀过滤，再执行 all/random/stratified。
`filter` 进入 `version_hash`；同一配置跨环境得到同一批样本。

> 历史复现示例：
> - KeSpeech 每方言前 250 条：`stratified + dim=dialect + per_stratum=250 + order=item_key`（9 个方言目录共 2250 条）；
> - AISHELL-1 S0765/S0766：`all + filter.speakers=[S0765,S0766]`（701 条）。

### 5.4 分桶

- 静态维度直接读 `item.buckets`；
- 派生维度在 M5 聚合 SQL 里用 `CASE WHEN duration_ms < 5000 THEN '<5s' ...` 计算；
- 桶的边界定义写进 `bucket_rules`，进入 `version_hash`，保证历史可比。
- `GET /dataset-versions/{id}/bucket-coverage?subsets=` 返回每个维度的
  `known/total/coverage/distinct`；`known=0` 表示该维度在数据中缺失。
  基准编辑器据此自动勾选 `duration_bin` 与覆盖率 ≥95%、取值 2–20 的静态维度，
  并置灰未知维度，避免选中后预览全是 unknown。

### 5.5 可用性解析（核心纯函数）

```python
RULES = {
  "cer":       [("ds.text_ratio", 0.95)],
  "wer":       [("ds.text_ratio", 0.95), ("ds.lang", "en")],
  "punct_f1":  [("ds.punct_supported", True), ("dev.observed.punctuation_output", True)],
  "itn_acc":   [("ds.itn_ratio", 0.95), ("dev.declared.itn", True)],
  "der":       [("ds.segment_ratio", 0.95), ("ds.speaker_ratio", 0.95),
                ("dev.observed.speakers", True)],
  "sa_der":    [("ds.segment_ratio", 0.95), ("ds.speaker_ratio", 0.95),
                ("dev.observed.naming", True)],
  "naming_acc":[("ds.segment_ratio", 0.95), ("ds.speaker_ratio", 0.95),
                ("dev.observed.naming", True), ("bm.enrollment", "complete")],
  "speaker_eer":[("ds.segment_ratio", 0.95), ("ds.speaker_ratio", 0.95),
                 ("dev.observed.naming", True), ("bm.enrollment", "complete")],
  "rtf_dev":   [("dev.observed.usage_field", True)],
  "ttft_ms":   [("dev.observed.partials", True)],
  "ttfa_ms":   [("dev.observed.segments", True)],
}
```

输出：

```jsonc
{ "name": "punct_f1", "status": "na", "reason": "no_punct_reference", "coverage": null }
{ "name": "der", "status": "ok" }
{ "name": "naming_acc", "status": "partial", "reason": "enrollment_missing", "coverage": 0.75 }
```

**规则**：
- 不满足 → `na` + 原因码，**绝不记 0**
- 覆盖率 < 0.95 但 > 0 → `partial`
- `enabled=false` → `na(reason=disabled_by_profile)`

### 5.5.1 `partial` 是干什么的

三个状态不是"好/中/差"，而是**"这个指标代表多少数据"**：

| 状态 | 覆盖率 | 含义 | 报告怎么显示 |
|---|---|---|---|
| `ok` | ≥ 95% | 指标能代表整个数据集 | 正常数字 |
| `partial` | 0 < 覆盖率 < 95% | 只能代表**一部分**样本 | 数字 + 「覆盖 80%」徽标 |
| `na` | = 0 或硬能力缺失 | 根本算不了 | `—` + 原因码 |

**为什么不能只有 ok/na**：

举例一：APY 粤语有 20 个文件，其中 18 个参考带标点（`punct_ratio=0.90`）。

- 记 `na` → 标点能力完全丢失，用户不知道其实可以评 90%
- 记 `ok` → 0.79 的 F1 被当成"全部语料的分数"，实际只覆盖 90%
- 记 `partial` → 显示 `F1 0.79（覆盖 90%，n=18）`，既不丢信息也不误导

举例二：某数据集 80% 的会话有时间戳，20% 没有。DER 只能在 80% 上算。若记 `ok`，用户会拿它和"100% 覆盖"的设备横评，结论不可靠。

**三条使用规则**

1. **分母只含可评样本**，缺失样本绝不补 0。
2. **`partial` 的 Run 与 `ok` 的 Run 对比时**，默认按共同样本取交集；页面上必须同时显示"各自覆盖"和"共同样本"两个值。
3. **`partial` 不阻断评测**，只是标注；是否接受由用户判断（可在 Run 参数里设 `allow_partial=true/false`）。

**95% 是策略，不是定律**：它写进 MetricProfile（`coverage.ok_threshold`，默认 0.95），不同口径可以调。低于阈值越多，指标越不可信。

### 5.6 版本哈希

```
version_hash = sha256(
  sorted(content_uids) || sorted(subsets) || profile.hash ||
  canonical(sampling) || canonical(bucket_rules) || canonical(params)
)[:16]
```

- `content_uids` 流式计算，不落盘
- 任一变化 → 新 hash → 默认禁止与旧结果并排比较

---

## 6. 预置口径模板

| 模板 | 适用 | 关键配置 |
|---|---|---|
| `default_zh` | 通用中文 | evaluator `default_zh`（可编辑）；归一化 + 所有准确率指标开启 |
| `alimeeting_official` | AliMeeting | evaluator `alimeeting_official`（**锁定**）；collar 250 半宽、重叠参与、`<sc>` 剥离 |
| `dialect_sweep` | 方言横评 | 复用 `default_zh` evaluator；差异在 Benchmark 的 dialect/duration_bin 分桶 |
| `speaker_full` | 会议/对话 | 复用 `default_zh` evaluator；naming matching + threshold 0.6 已是默认 |
| `perf_30ms` | 性能基线 | evaluator `perf_30ms`（**锁定**）；30ms/1x，关闭 punct/itn/der/naming，保留 CER 与性能读数 |
| `cer_only` | **快速 CER 回归** | evaluator `cer_only`（**锁定**）；只保留 CER，punct/itn/der/naming 与性能读数全部关闭 |

> 2026-11-11 实现审计：各字段与 runner/scorer 的实际差异见 `docs/metric-profile-configuration.md`；部分字段目前是保留字段，不会改变结果。

### 6.1 Benchmark 预设（profile + 采样 + 分桶的组合）

模板只是口径；用户实际点的是"预设"，它把三样东西一起配好：

| 预设 | profile | subset | 采样 | 分桶 | 场景 |
|---|---|---|---|---|---|
| `cer_quick` | `cer_only` | primary | `random(1000, seed=42)` | `[dialect]` | 改完模型先跑 5 分钟看有没有崩 |
| `full_accuracy` | `default_zh` | primary | `all` | `[dialect, duration_bin]` | 正式准确率评测 |
| `alimeeting_clean` | `default_zh` | `near` | `all` | `[speaker, duration_bin]` | 干净条件 CER 基线 |
| `alimeeting_meeting` | `alimeeting_official` | `far` | `all` | `[n_speakers, overlap_bin]` | 会议分离/多说话人 |
| `apy_device_ab` | `default_zh` | `phone`+`mic` | `all` | `[channel]` | 录音条件对 CER 的影响 |
| `perf_baseline` | `perf_30ms` | primary | `stratified(duration_bin, 30)` | `[duration_bin]` | 性能基线 |

预设只是"填好的表单"，用户可改任意字段；改完生成新的 `version_hash`。

**模板设计原则：默认全开，由 resolver 判定 `na`。**

不要在模板里预先关掉某项指标——否则用户会以为平台不支持。例如 `default_zh` 开着 `punct`，遇到 AISHELL-1（无标点）时由 resolver 返回 `na(no_punct_reference)`，而不是模板里就写死 `punct.enabled=false`。

---

## 7. 异常与校验

| 场景 | 行为 |
|---|---|
| 未知指标名 | 422，指出字段路径 |
| `collar_ms < 0` | 422 |
| `sampling.n > 可用条数` | 422 或自动截断 + 警告（配置项） |
| `bucket dim` 未注册 | 422 |
| DatasetVersion 未 ready | 409 |
| MetricProfile 被 Benchmark 引用 | 只能新建版本，不能原地改 |
| Benchmark 被 Run 引用 | 只能归档，不能删 |
| `version_hash` 冲突 | 复用已有 Benchmark（幂等） |

---

## 8. 测试点

### 8.1 单元

| 用例 | 断言 |
|---|---|
| profile hash 稳定性 | 键顺序不同、hash 相同 |
| profile 语义校验 | 未知指标被拒 |
| random 采样 | 同 seed 同结果；不同 seed 不同 |
| stratified 采样 | 每桶条数正确；不足时标 short |
| version_hash | 改 sampling / params / profile 任一 → 变 |
| resolver | AISHELL-1 只 ok `cer`，其余 na |
| resolver | AliMeeting + alimeeting_official → cer/punct/der/naming ok，itn na |
| bucket 边界 | 4999ms→`<5s`，5000ms→`5~15s` |

### 8.2 集成

| 用例 | 断言 |
|---|---|
| 一键套模板 | 生成可用于 AliMeeting 的 Benchmark |
| preview | 条数/时长/桶计数与库一致 |
| 归档 | 已被 Run 引用的 Benchmark 不能删 |
| 可用性页 | 3 台设备 × 1 Benchmark 的矩阵正确 |

---

## 9. 验收

1. 相同输入 → 相同 `version_hash`（跨环境）。
2. AliMeeting + `alimeeting_official`：`cer/punct/der/naming = ok`，`itn = na`。
3. AISHELL-1 + `default_zh`：只有 `cer = ok`，其余 `na` 且原因码正确。
4. 1 万条 stratified 采样（每桶 30）结果可复现。
5. 非法口径被拒绝且错误信息能定位到字段。
6. 已被 Run 引用的 Benchmark 无法删除；未被引用的基准可从列表删除（`DELETE /benchmarks/{id}`，admin）。口径版本同样支持删除，被 Benchmark 引用时返回 `409 E_METRIC_PROFILE_IN_USE`。

---

## 10. 契约与风险

| 下游 | 需要什么 |
|---|---|
| M4 | `Benchmark` 全量 + `MetricResolver` + 采样后的 `item_id` 列表 |
| M5 | `MetricProfile.config`、指标 spec 版本 |
| M6 | `bucket_rules` 与桶标签（中文） |

| # | 风险 | 处理 |
|---|---|---|
| 1 | 口径悄悄变化导致历史不可比 | `version_hash` 进报告抬头 |
| 2 | 指标实现升级 | `scorer_version` 与 `profile.hash` 分开记 |
| 3 | 派生桶边界调整 | 边界写进 `bucket_rules`，进 hash |
| 4 | 采样导致跨 Run 样本不同 | 对比时默认取交集，并显示各自覆盖 |
