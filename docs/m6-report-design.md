# M6 报告与对比 · 详细设计卡 v0.1

> **实现状态（截至 2026-11-05）**
> - 已实现 Evaluation 对比 API：`POST /compare`、`GET /compare/available`。
> - 可比性校验：完成状态、profile_hash、scorer_version、dataset_version。
> - 前端：对比托盘（2~4 个）、`/compare` 页、阻断提示。
> - 报告快照生成现通过 M7 统一 Job 异步执行，产出不可变 payload、HTML 与 XLSX 文件。
> - 分桶、显著性和配对样本的接口与口径定义见下文。

> 上游：M5（`metric_value` / `case_score` / `detail_uri`）、M4（`run.snapshot` / `evaluation`）
> 下游：前端（M8）
> 本模块只读，不修改任何评测数据；所有产物可追溯、可快照。

### 0.1 决策记录（已确认）

| # | 决策 |
|---|---|
| D-M6-1 | **报告主体是 Evaluation**；Run 仅作为执行容器，头部同时展示两者关系 |
| D-M6-2 | 跨 subset：`phone/mic` 按 `pair_key` 逐句配对；`near/far` 只做聚合对比 |
| D-M6-3 | 分桶显著性默认 **BH 校正**并标注；可切换未校正（探索性） |
| D-M6-4 | 图表栈 = **CSS Grid + Recharts + 自定义 SVG**，**不引入 ECharts**（真实渲染对比见 UI 设计 §4.1） |
| D-M6-5 | 页面不直接依赖图表库，统一 `ChartSpec` + `renderer`，预留 ECharts 迁移能力 |
| D-M6-6 | 视觉方向"仪器面板"暂定，M8 前一页静态 demo 过 gate 后冻结 |

页面设计见 `m6-report-ui-design.md`（视觉方向、五类场景视图、组件与图表方案）。

---

## 1. 职责与边界

### 1.1 负责

| 职责 | 说明 |
|---|---|
| 单次报告 | 一个 Evaluation 的指标总览、分桶、覆盖率 |
| 横评矩阵 | 多个 Evaluation / 多个 target / 多个 subset 并排 |
| 回归对比 | 同 target 的历史 Evaluation 对比，Δ + CI + p |
| 条件对比 | 跨 subset 对比（near/far、phone/mic） |
| Badcase | 三方对齐（音频/参考/识别）、字符级 diff、说话人轨道 |
| 导出 | 自包含 HTML、XLSX |
| 快照 | 不可变报告（来源删除后仍可打开） |

### 1.2 不负责

| 不负责 | 归属 |
|---|---|
| 打分 | M5 |
| 执行 | M4 |
| 数据修正 | M1（新版本） |

### 1.3 关键约束

1. **只读**：不写 `case_score` / `metric_value`。
2. **报告以 Evaluation 为单位**，不直接以 Run 为单位（Run 只是执行的容器）。
3. **不可比必须显式提示**，禁止静默并排。
4. 快照不可变；重建 = 新快照。

---

## 2. 领域对象

```python
Report:
  id, kind                  # single | matrix | compare | regression
  evaluation_ids: list
  query: dict               # 指标/桶/筛选
  input_hash: str           # 参与计算的数据指纹
  template_version: str
  level: "summary" | "evidence"
  payload_uri, html_uri
  created_by, created_at

ComparisonSpec:
  evaluations: list[str]
  baseline: str
  metric: str
  scope: micro | macro
  bucket: dict
  pair_by: content_uid | pair_key | none
  strict: bool
```

---

## 3. 接口

### 3.1 HTTP

```
GET  /api/v1/evaluations/{id}/summary
GET  /api/v1/evaluations/{id}/matrix
GET  /api/v1/evaluations/{id}/buckets?metric=&dim=
GET  /api/v1/evaluations/{id}/cases?metric=&order=desc&bucket=&cursor=
GET  /api/v1/evaluations/{id}/cases/{case_id}

POST /api/v1/compare                 # 2~N 个 Evaluation
GET  /api/v1/compare/available?metric=&bucket=   # 可比性预检

POST /api/v1/reports                 # 生成快照
GET  /api/v1/reports
GET  /api/v1/reports/{id}
GET  /api/v1/reports/{id}/html
GET  /api/v1/reports/{id}/xlsx
DELETE /api/v1/reports/{id}
```

### 3.2 返回约定

- 所有数字附带 `{value, n, ci_low, ci_high, status, reason}`
- `status=na` 时 `value=null`，必须带 `reason`
- 矩阵单元格带 `subset` 与 `metric` 标识
- 分页统一游标

---

## 4. 数据结构

```sql
create table report (
  id uuid primary key default gen_random_uuid(),
  kind text not null,                 -- single|matrix|compare|regression
  evaluation_ids jsonb not null,
  query jsonb not null,
  input_hash char(64) not null,
  template_version text not null,
  level text not null default 'summary',
  payload_uri text not null,
  html_uri text not null,
  xlsx_uri text,
  note text,
  created_by text,
  created_at timestamptz not null default now()
);
create index report_created on report (created_at desc);
create index report_kind on report (kind, created_at desc);
```

**报告不存业务数字**：payload 是生成时的快照（JSON），HTML/XLSX 由它渲染。
来源 Evaluation 删除后，快照仍可打开，并标注"来源已删除"。

---

## 5. 关键流程

### 5.1 单次报告

```
GET /evaluations/{id}/summary
→ 头部：Benchmark hash / profile hash / scorer version / 覆盖 / 异常
→ 结论条：每个指标一行（value + CI + status）
→ 分桶表：按 bucket_rules 的维度
→ 性能表：ttft / finalize / rtf / throughput（按 duration_bin）
```

### 5.2 横评矩阵

```
GET /evaluations/{id}/matrix
→ 行 = (run_target, subset)
→ 列 = 指标
→ 单元格 = value + status
```

多 Evaluation 横评：

```
POST /compare {evaluations:[a,b,c], metric:"cer", scope:"micro"}
→ 行 = target/subset（按 baseline 对齐）
→ 列 = 各 Evaluation
→ 附带 Δ 与显著性
```

### 5.3 可比性判定（硬规则）

| 差异 | 处理 |
|---|---|
| `benchmark_version_hash` 不同 | **阻断**，提示"不同考卷" |
| `profile_hash` 不同 | **阻断**（或显式 `strict=false` 降级为警告） |
| `scorer_version` 不同 | **阻断**，提示重新评分 |
| `subset` 集合不同 | 取交集，显示"覆盖不足" |
| `sampling` 不同 | 按 `content_uid` 取交集 |
| 一方 `partial` | 允许，但显示覆盖与警告 |
| Run `aborted_condition_changed` | **不参与**对比 |

### 5.4 配对方式

| 场景 | pair_by | 说明 |
|---|---|---|
| 同一 subset 的两个 Evaluation | `content_uid` | 同音频 |
| phone vs mic（同一版本两个 subset） | `pair_key` | 同内容不同音频 |
| near vs far | `none` | 只能聚合对比（句子级无法一一对应） |

### 5.5 显著性

```
配对 bootstrap：
  取共同样本
  按指标单位重采样（utterance / session）
  1000 次 → Δ 的分布 → 95% CI 与 p 值

展示：Δ + 95% CI + p
   CI 跨 0 → "无显著差异"
```

**分桶多重比较**：默认 BH 校正；页面标注 `BH-adjusted`，可切换为未校正（探索性）。

### 5.6 Badcase

```
GET /evaluations/{id}/cases?metric=cer&order=desc
→ 按 case_score.metrics 里的指标值排序
→ 返回 case_result + item + 摘要
GET /evaluations/{id}/cases/{case_id}
→ detail_uri 取对齐明细
→ 音频 URL、参考分段、识别分段、字符 diff、说话人轨道
```

筛选维度：指标阈值、bucket、错误类型（替换/删除/插入）、是否 cached。

### 5.7 导出

| 格式 | 内容 |
|---|---|
| HTML | 自包含（内联 CSS、SVG 图表），可邮件/归档 |
| XLSX | 三个 sheet：Summary / Buckets / Cases |

导出中的指标名称使用中文标签；比率类指标在 HTML 中以百分比呈现。XLSX 的比率值保留 0–1 数值以便筛选和计算，并设置百分比单元格格式；不可评指标仍为空值并显示原因。

音频默认只放路径/URL，不嵌入。

### 5.8 快照

```
POST /reports {kind, evaluation_ids, query, level}
→ 返回 202 + Location: /api/v1/jobs/{job_id}（M7 §6）
→ Job 计算 payload、写本地对象目录、写 report 行
→ Job 成功结果包含 report_id
```

- `input_hash = sha256(evaluation_ids + 各自 metric_value 的 hash + query)`
- `template_version` 变更 → 旧快照不自动重建
- 重新生成 = 新快照

---

## 6. 异常与降级

| 场景 | 行为 |
|---|---|
| Evaluation 仍在 scoring | 报告显示"评分中"，禁用导出 |
| 指标 `na` | 显示 `—` + 原因，不参与排序 |
| 共同样本太少（< 50） | 警告"样本不足，显著性不可靠" |
| 来源 Evaluation 被删 | 快照可打开，标注"来源已删除" |
| detail_uri 失效 | 显示摘要，提示"明细不可用" |
| 分桶过多（> 100） | 只返回 top/bottom + 折叠 |

---

## 7. 测试点

### 7.1 单元

| 用例 | 断言 |
|---|---|
| 可比性阻断 | hash 不同 → 阻断 |
| 交集对齐 | content_uid / pair_key 各自正确 |
| BH 校正 | 已知 p 值序列校正正确 |
| 快照 input_hash | 同输入同 hash |
| na 渲染 | 不显示 0 |

### 7.2 集成

| 用例 | 断言 |
|---|---|
| 3 设备 × 2 subset 矩阵 | < 500ms |
| 两版本回归 | Δ + CI + p 正确 |
| phone vs mic | 按 pair_key 配对 |
| Badcase 下钻 | 音频/参考/识别对齐 |
| 导出 | HTML 可离线打开；XLSX 可透视 |
| 快照 | 删除来源后仍可打开 |

---

## 8. 验收

1. 任一数字都能回答：口径 hash / 样本量 / CI / 是否显著。
2. 不可比的两份结果并排时，必须出现阻断提示。
3. 3 设备 × 2 subset × 8 指标的矩阵首屏 < 500ms。
4. Badcase 从矩阵单元格到样本明细 ≤ 2 次点击。
5. 导出的 HTML 在无网络环境可完整打开。
6. 快照在来源删除后仍可打开。

---

## 9. 契约与风险

| 下游 | 需要什么 |
|---|---|
| M8 | 矩阵/分桶/对比/Badcase/导出接口 |
| M8 | 每个数字的 `status/reason/n/ci` |

| # | 风险 | 处理 |
|---|---|---|
| 1 | 报告口径混淆 | 头部固定展示三个 hash |
| 2 | Badcase 排序慢 | 先按 `metrics->metric->value` 排序，量大再加归一化表 |
| 3 | 快照膨胀 | `summary` 默认；`evidence` 单独触发 |
| 4 | 分桶多重比较假阳性 | 默认 BH 校正并标注 |
