# M4 执行引擎 · 详细设计卡 v1.0（定稿）

> **实现状态（代码与 PostgreSQL 集成验收，2026-11-06）**
> - Run 创建预览返回指标可用性、设备占用/排队位置及 P50/P90 ETA；创建时冻结各目标 ETA。
> - Run 进入 PostgreSQL `job` 持久队列，`FOR UPDATE SKIP LOCKED` 领取；固定 worker 数量、有界内存派发队列、启动恢复与断点续跑。
> - `accuracy` 默认单路；`perf` 按设备 `realtime_slots` 限制路数，按独立会话并发执行；设备仍由租约整机独占。
> - Adapter 支持流式分片、首个 partial 与 final 时延计时；样本 timing 落库。
> - ETA 支持四级历史基线、时长桶、校准切片、运行中按 target 滚动修正及连续慢速告警；Run ETA 汇总最慢目标。
> - Run 列表新建向导展示运行类型/路数、逐目标指标、排队位置与 ETA；列表显示 ETA 来源/置信度和速度异常。
> - 本地与 PostgreSQL 自动化测试覆盖队列、恢复、并行 lane、subset 隔离、预览、取消/续跑；真机吞吐回归仍需在目标设备环境单独验收。

> 上游：M1（item/subset）、M2（Adapter/能力/租约）、M3（Benchmark/Resolver/采样）
> 下游：M5（评分）、M6（报告）
> 本模块把"定义"变成"可观测、可中断、可续跑"的执行过程；**评分是独立实体**。

---

## 0. 决策记录（本模块相关）

| # | 决策 | 说明 |
|---|---|---|
| D-M4-1 | **Run = 执行记录，Evaluation = 评分记录，1:N** | 一次执行可换口径反复评分，不重跑设备 |
| D-M4-2 | **自动评分**：`completed` / `partial` 自动创建 Evaluation；`failed` / `cancelled` / `aborted` 不自动 | 可被 `auto_score=false` 关闭 |
| D-M4-3 | **`case_result` 不存 metrics**，逐样本指标由 `case_score` 承载 | 支持一号多评分 |
| D-M4-4 | **整机独占**：设备同一时刻只服务一个 Run | 同设备的多个 subset 串行 |
| D-M4-5 | **Run 内默认单路**；仅 `perf` 可配 `lanes>1` | Q9 |
| D-M4-6 | **条件变化 → 中断**（`sn` 或 `deployment.uid` 变化） | 5 分钟身份检查，不建 WS 会话 |
| D-M4-7 | **禁止强杀**；所有超时走 `close` 帧 | 实测强杀会让引擎卡死 |
| D-M4-8 | **不物化任务行**：分片 + 游标 | 万级任务启动 < 2s |
| D-M4-9 | **RunTarget = (deployment, subset)** | 多设备并行、同设备串行 |
| D-M4-10 | **ETA 由历史结果派生**，不建"设备性能表" | 四级取值 + 时长桶 + 滚动 |
| D-M4-11 | Run 创建时选 `device_id`，可选锁定 `deployment_uid`；不一致 → 409 | 日常用当前，回归用锁定 |
| D-M4-12 | 缓存默认只在本 Run 内复用；跨 Run 需 `deterministic=true` | 避免不可复现 |

### 0.1 决策 → 章节追溯

| 决策 | 落点 |
|---|---|
| D-M4-1 Run/Evaluation 1:N | §2.1、§2.2、§5.10 |
| D-M4-2 自动评分范围 | §5.10、§8 第 6 条 |
| D-M4-3 case_result 不存 metrics | §2.1、§4 表结构、§9 契约 |
| D-M4-4 整机独占 | §1.3、§5.4 |
| D-M4-5 默认单路 / perf 可多路 | §2.1 `lanes`、§5.5 |
| D-M4-6 条件变化中断 | §5.8 |
| D-M4-7 禁止强杀 | §1.3、§5.6 |
| D-M4-8 不物化任务行 | §5.5 |
| D-M4-9 RunTarget=(deployment, subset) | §2.1、§4 唯一键 |
| D-M4-10 ETA 由历史派生 | §5.7 |
| D-M4-11 device_id + 可选 deployment_uid | §5.2 |
| D-M4-12 缓存复用范围 | §5.6 |

---

## 1. 职责与边界

### 1.1 负责

| 职责 | 说明 |
|---|---|
| Run 编排 | 创建、快照冻结、预览、启动、取消、续跑 |
| 设备租约 | 整机独占；一个 Run 持有一台设备 |
| 调度 | 按 (deployment, subset) 展开；默认单路；perf 可多路 |
| 会话执行 | 调用 Adapter、记录 timing、落 Hypothesis |
| 缓存 | 内容寻址复用推理结果 |
| 看门狗 | finalize 超时、条件变化、overload 熔断 |
| ETA | 历史基线 + 校准切片 + 滚动修正 |
| 评分交接 | 执行终态时**自动创建 Evaluation**（可关） |

### 1.2 不负责

| 不负责 | 归属 |
|---|---|
| 数据集与 subset | M1 |
| 口径与采样 | M3 |
| 指标计算与聚合 | M5 |
| 图表与对比 | M6 |
| 自动重启设备 | 明确不做 |

### 1.3 关键约束

1. **整机独占**：设备同一时刻只服务一个 Run；Run 内默认 1 路会话。
2. **条件必须一致**：`sn` 或 `deployment.uid` 变化 → **中断**。
3. **禁止强杀**：所有超时走 `close` 帧并等待对端关闭。
4. **不物化任务行**：分片 + 游标。
5. **执行与评分分离**：`case_result` 只记"跑没跑成"与原始 timing；指标在 `case_score`。

---

## 2. 领域对象

### 2.1 实体

```python
Run:                          # 执行
  id, name, kind              # accuracy | perf
  benchmark_id
  snapshot: dict              # 冻结的全部条件
  status                      # 见 §5.3
  auto_score: bool
  eta_ms, eta_p90_ms, eta_source, eta_confidence
  progress: dict
  created_by, started_at, finished_at

RunTarget:                    # 一个 (deployment, subset) 一条
  id, run_id
  deployment_id, subset
  status
  lanes: int                  # 默认 1；仅 perf 可 >1
  stats: dict
  abort_reason
  started_at, finished_at

Case:                         # 执行结果，不含指标
  id, run_target_id, item_id
  status                      # ok|failed|timeout|aborted|skipped|cached
  wall_ms, dev_ms, ttft_ms, finalize_ms
  rtf_dev, rtf_e2e
  resource, error
  hypothesis_id

Hypothesis:                   # 内容寻址的推理产物
  id, cache_key
  deployment_id, item_id
  params_hash, mode, chunk_ms, input_rate, concurrency
  text, segments, speaker_match, usage
  raw_uri, created_at

Evaluation:                   # 评分（1 Run : N Evaluation）
  id, run_id
  metric_profile_id, profile_hash, scorer_version
  trigger                     # auto | manual | rescore
  status                      # queued | scoring | completed | failed | cancelled
  coverage: dict
  created_by, created_at, finished_at

CaseScore:                    # 逐样本指标
  id, evaluation_id, case_result_id
  metrics: dict
  detail_uri
```

### 2.2 关系

```
Run 1 ──< RunTarget 1 ──< Case ──1 Hypothesis
 │
 └──< Evaluation N ──< CaseScore
                  └──< MetricValue（M5 聚合）
```

**为什么是 1:N**：同一批执行结果会被不同口径反复评（官方口径、纯 CER、新归一化、不同 threshold）。若评分绑死在 Run 上，换口径只能克隆 Run，会被误解为"又跑了一遍设备"，浪费数小时。

### 2.3 Run.snapshot（冻结内容）

```jsonc
{
  "benchmark_id": "...",
  "benchmark_version_hash": "ab12...",
  "dataset_version_id": "...",
  "subsets": ["near"],
  "metric_profile_id": "...",
  "profile_hash": "cd34...",
  "sampling": {...},
  "bucket_rules": {...},
  "params": {"itn": true, "diarization": true, "max_spk": 4, "threshold": 0.6},
  "auto_score": true,
  "targets": [
    {"device_id": "...", "deployment_id": "...", "deployment_uid": "...", "subset": "near"}
  ]
}
```

---

## 3. 接口

### 3.1 服务

```python
class RunService:
    def preview(self, req) -> RunPreview
    def create(self, req) -> Run
    def get(self, run_id) -> Run
    def list(self, filters) -> Page[Run]
    def cancel(self, run_id, actor) -> None
    def resume(self, run_id) -> None
    def events(self, run_id) -> AsyncIterator[Event]

class EvaluationService:                 # M5 提供，M4 调用
    def create(self, run_id, profile_id, trigger, actor) -> Evaluation
    def cancel(self, evaluation_id) -> None
    def events(self, evaluation_id) -> AsyncIterator[Event]

class Scheduler:
    def tick(self) -> None
    def dispatch_once(self, run_target_id) -> None

class Runner:
    def execute(self, run_target, lease) -> None
```

### 3.2 HTTP

```
POST   /api/v1/runs/preview
POST   /api/v1/runs
POST   /api/v1/runs/groups                # 多 Benchmark 一次建组（每个各一条 Run）
GET    /api/v1/runs/groups
GET    /api/v1/runs/groups/{id}
GET    /api/v1/runs
GET    /api/v1/runs/{id}
POST   /api/v1/runs/{id}/cancel
POST   /api/v1/runs/{id}/resume
GET    /api/v1/runs/{id}/events            # SSE（执行事件）
GET    /api/v1/runs/{id}/resources         # 执行期 CPU/内存/NPU/温度采样
GET    /api/v1/runs/{id}/targets
GET    /api/v1/runs/{id}/cases

GET    /api/v1/runs/{id}/evaluations       # 该 Run 的所有评分
POST   /api/v1/runs/{id}/evaluations       # 手动评分 / 换口径重评
GET    /api/v1/evaluations/{id}
POST   /api/v1/evaluations/{id}/cancel
GET    /api/v1/evaluations/{id}/events
```

Run 创建请求包含 `name`、`dataset_version_id`、可选 `benchmark_id` 与 `targets[{device_id, subset}]`；提供 Benchmark 时，服务端校验版本归属、归档状态和 subset，并按所选 Benchmark 冻结 Run。未提供 `benchmark_id` 时保留默认 Benchmark 兼容行为。

### 3.3 事件

```
run.created / run.probing / run.running / run.completed
run.partial / run.aborted / run.failed / run.cancelled
target.started / target.progress / target.finished
target.aborted_condition_changed
case.failed
eta.updated

evaluation.queued / evaluation.scoring / evaluation.completed / evaluation.failed
```

---

## 4. 数据结构

```sql
create table run (
  id uuid primary key default gen_random_uuid(),
  campaign_id uuid references campaign(id) on delete set null,
  benchmark_id uuid not null references benchmark(id),
  kind text not null default 'accuracy',        -- accuracy | perf
  name text not null,
  status text not null default 'created',
  snapshot jsonb not null,
  auto_score boolean not null default true,
  eta_ms bigint, eta_p90_ms bigint,
  eta_source text, eta_confidence text,
  progress jsonb not null default '{}',
  created_by text,
  started_at timestamptz, finished_at timestamptz,
  created_at timestamptz not null default now()
);

create table run_target (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references run(id) on delete cascade,
  deployment_id uuid not null references deployment(id),
  subset text not null,
  status text not null default 'pending',
  lanes smallint not null default 1,
  stats jsonb not null default '{}',
  abort_reason text,
  started_at timestamptz, finished_at timestamptz,
  unique (run_id, deployment_id, subset)
);

create table hypothesis (
  id uuid primary key default gen_random_uuid(),
  cache_key char(32) not null unique,
  deployment_id uuid not null references deployment(id),
  item_id bigint not null references item(id),
  params_hash char(16) not null,
  mode text not null, chunk_ms int, input_rate real, concurrency smallint,
  text text, segments jsonb, speaker_match jsonb, usage jsonb,
  raw_uri text,
  created_at timestamptz not null default now()
);

-- 执行结果：只有 timing 与状态，没有 metrics
create table case_result (
  id bigserial primary key,
  run_target_id uuid not null references run_target(id) on delete cascade,
  item_id bigint not null references item(id),
  hypothesis_id uuid references hypothesis(id),
  status text not null,                 -- ok|failed|timeout|aborted|skipped|cached
  wall_ms int, dev_ms int, ttft_ms int, finalize_ms int,
  rtf_dev real, rtf_e2e real,
  resource jsonb, error text,
  created_at timestamptz null,           -- 样本结果落库时间；旧记录保持 null
  unique (run_target_id, item_id)
);
create index case_status on case_result (run_target_id, status);

-- 评分
create table evaluation (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references run(id) on delete cascade,
  metric_profile_id uuid not null references metric_profile(id),
  profile_hash char(16) not null,
  scorer_version char(64) not null,
  trigger text not null default 'auto',  -- auto|manual|rescore
  status text not null default 'queued',
  coverage jsonb not null default '{}',
  created_by text,
  created_at timestamptz not null default now(),
  finished_at timestamptz
);
create index evaluation_run on evaluation (run_id, created_at desc);

create table case_score (
  id bigserial primary key,
  evaluation_id uuid not null references evaluation(id) on delete cascade,
  case_result_id bigint not null references case_result(id) on delete cascade,
  metrics jsonb not null,
  detail_uri text,
  unique (evaluation_id, case_result_id)
);
```

**租约**用 PG：`device_lease(device_id pk, run_target_id, holder, expires_at)`，`insert … on conflict … where expires_at < now()` 原子抢占，120s 过期，30s 心跳。

> 轻量版（M9 v2）去掉了 Redis/arq：队列改为 PG `job` 表 + `FOR UPDATE SKIP LOCKED`。调度状态机本来就在业务层，替换只影响投递实现。

---

## 5. 关键流程

### 5.1 预览（创建前）

```
POST /runs/preview {benchmark_id, targets:[{device_id, deployment_uid?}], kind}
→ 对每个 target × subset：
   1. 取 subset 的 capabilities（M1）
   2. 取 deployment 的 capabilities（M2）
   3. M3 resolver → [MetricStatus]
   4. 检查声纹注册完整度（若启用 naming）
   5. ETA（§5.7）
   6. 设备是否被占用、前方排队数
→ 返回矩阵 + 警告
```

### 5.2 创建

```
1. 校验 Benchmark（ready/ready_with_errors，subsets 非空）
2. 冻结 snapshot：benchmark 全字段 + 每个 target 的 deployment_uid + auto_score
3. 若指定了 deployment_uid 且与当前不一致 → 409 `deployment_changed`
4. 生成 run + run_target（每个 deployment×subset 一条）
5. 入队
```

### 5.3 状态机

```
Run:
created → probing → preparing → running → completed
                        │          │
                        └──► partial / failed / cancelled / aborted_condition_changed

Evaluation:
queued → scoring → completed
                 └──► failed / cancelled
```

| 状态 | 判定 |
|---|---|
| `completed` | 所有 case 终态，且 `ok+cached` 占比 = 100% |
| `partial` | 已结束，`ok+cached` 占比 ≥ 90% 且 < 100% |
| `failed` | 致命错误，或 `ok+cached` 占比 < 90% |
| `aborted_condition_changed` | 执行中 `sn`/`uid` 变化 |
| `cancelled` | 用户取消 |

- `probing`：申请租约 → `probe_full`（空闲）或 `probe_identity` → 冻结 fingerprint
- `preparing`：命名指标需要时准备声纹向量
- `running`：执行
- 终态后按 §5.10 创建 Evaluation

### 5.4 设备租约与调度

```
tick():
  for each pending run_target（按创建时间）:
     if lease_acquire(device_id, run_target_id) 成功:
        dispatch(run_target)
```

- 租约 TTL 120s，worker 每 30s 续租
- 续租失败 → 立即停止派发
- 设备被占用 → target 保持 `pending`，UI 显示"排队中（前方 N 个）"
- 进程崩溃 → 租约到期自动可重取
- **同一设备的多个 subset（near/far）串行**，因为整机独占

### 5.5 分片与游标（不物化任务行）

```
items = SELECT id FROM item
        WHERE subset_id = :subset
          AND id IN (采样结果，见 M3)
        ORDER BY id
```

- 默认 `lanes=1`：单游标顺序消费
- `perf` 且 `lanes=c`：c 个游标，`offset=lane_index`、步长 c
- 每条处理完写 `last_item_id` 到 `run_target.stats`
- 失败不阻塞后续，计入 `stats.failed`

### 5.6 执行会话

```python
for item in cursor:
    if run_target.status != running: break
    if identity_changed(): abort("aborted_condition_changed"); break

    key = cache_key(deployment.uid, item.content_uid, params_hash,
                    mode, pace, chunk_ms, concurrency)
    if hit := cache.get(key):
        case.status = "cached"       # 无 timing；性能指标不参与
        continue

    hyp = adapter.transcribe(audio, params, on_event)
    case = persist(hyp, timing)
```

**缓存复用规则**

| 范围 | 是否复用 | 说明 |
|---|---|---|
| 同一 Run 内（重试、同音频多 subset） | ✅ 默认 | 内容寻址，命中即跳过 |
| 跨 Run | 仅 `deterministic=true` | 设备结果未必完全可复现 |
| 命中后 | `case.status=cached` | 无 timing；**性能指标不参与**，准确率照常可评 |

**单条失败处理**

| 情况 | 动作 |
|---|---|
| 连接错误 / 超时 | 重试 2 次（1s/3s 退避） |
| `finalize_timeout` | 关闭连接，记失败，**不重试** |
| `engine_overloaded` | 立即中断该 target，设备标 `needs_restart` |
| 连续 5 条失败 | 熔断该 target |
| 单条失败 | 继续后续 |

### 5.7 ETA

```
阶段 0（创建前）：
  rtf_est = L1 deployment+bucket → L2 model+bucket → L3 kind+bucket → L4 常量
  ETA = Σ_bucket(count × mean_d × rtf) / lanes + fixed
  ETA_p50 / ETA_p90（median / p90）

阶段 1（校准切片，默认开，约 20s）：
  每桶取 3 条先跑，得到本条件 rtf，替换 L1

阶段 2（运行中）：
  每 5 秒更新剩余工作量；按 bucket 用滚动 median
  前 3 条不计入（warm-up）
  速率连续两窗下降 >30% → 速度异常，ETA 切 P90
```

`eta_source`：`deployment | cross_device | kind | fallback | observed`（运行中使用本次校准数据时为 `observed`）
`eta_confidence`：`high | medium | low`
Run 的 ETA = `max(各 target ETA)` + 排队时间。

### 5.8 条件变化中断

```
每 5 分钟（不建 WS 会话）：
  sn + platform + bigversion + etsllm + accel + model_key/model_version
  → 重算 uid
  if uid != snapshot.deployment_uid or sn != snapshot.sn:
      run_target.status = aborted_condition_changed
      run_target.abort_reason = "condition_changed"
      停止派发；对 in-flight 会话发 close
      保留已完成 case，默认不参与对比
      事件 target.aborted_condition_changed
```

### 5.9 取消 / 续跑

| 操作 | 行为 |
|---|---|
| 取消 | `cancelling` → 停止派发 → 关闭 in-flight → `cancelled`；**不自动评分** |
| 续跑 | 只处理 `failed / timeout / pending`；重新申请租约；不改变已完成的 case |

### 5.10 评分交接（自动创建 Evaluation）

```
Run 终态：
  completed / partial 且 auto_score=true
    → EvaluationService.create(run_id, benchmark.metric_profile_id, trigger="auto")
    → evaluation.status = queued → scoring
  failed / cancelled / aborted
    → 不自动创建（用户可手动 POST /runs/{id}/evaluations）
  auto_score=false
    → 不自动创建
```

**规则**

1. Evaluation 冻结 `profile_hash` 与 `scorer_version`。
2. 一个 Run 可有多个 Evaluation；默认列表按创建时间倒序。
3. `partial` 的 Run 自动评分时，Evaluation 的 `coverage` 记录覆盖情况。
4. Run 的 UI 状态由两部分派生：
   ```
   执行：completed/partial/...
   评分：取最近一次 Evaluation 的 status
   ```
   执行状态不会被评分失败污染。

### 5.11 手动评分 / 重算

```
POST /runs/{id}/evaluations {metric_profile_id?}
  - 不传 → 用 Run 快照里的口径
  - 传了 → 换口径，trigger=rescore
  - 不重跑设备；只读 case_result + hypothesis
```

---

## 6. 异常与降级

| 场景 | 行为 | 用户可见 |
|---|---|---|
| 设备被占用 | target 排队 | "排队中，前方 N 个" |
| 租约丢失 | 停止派发，target 回 pending | 顶部提示 |
| `probe_full` 失败 | target failed | 原因 + 建议 |
| 声纹准备失败 | 命名指标 N/A，其余继续 | 原因码 |
| 条件变化 | **中断** | 红色横幅 + 新旧指纹；不自动评分 |
| `engine_overloaded` | 中断 + 设备 needs_restart | 红色徽标 |
| 连续失败 5 条 | 熔断 | "该设备连续失败，已暂停" |
| 进程崩溃 | 租约到期，restart 后续跑 | — |
| 缓存命中 | case 标 `cached` | 报告注明"复用"，性能指标不计入 |
| 评分失败 | Evaluation failed | Run 仍显示执行完成；可重试评分 |

---

## 7. 测试点

### 7.1 单元

| 用例 | 断言 |
|---|---|
| cache_key | 同条件同 key；改 pace/chunk/concurrency 变 key |
| 游标 | 单路顺序、多路不重复不遗漏 |
| 重试 | 2 次后标 failed；overload 立即中断 |
| ETA 公式 | 已知 rtf 与条数算出预期值 |
| 条件变化 | uid/sn 变化触发 abort |
| 取消 | 停止派发并关闭会话 |
| 自动评分规则 | completed/partial 建 Evaluation；aborted 不建 |

### 7.2 集成

| 用例 | 断言 |
|---|---|
| 1,000 条单设备 | 完成、状态推进、case 齐全 |
| 双 Run 抢同一设备 | 第二个排队，不并发 |
| 同设备 near+far | 两个 target 串行 |
| 条件变化 | 改模型后 5 分钟内 abort，且无 Evaluation |
| 断点续跑 | kill 后续跑，不重复 |
| 取消 | 30s 内停止 |
| perf 3 路 | lanes=3，case 不重复 |
| 缓存复用 | 第二次同条件命中率 > 0 |
| 一号多评分 | 同一 Run 生成 2 个 Evaluation，互不影响 |

### 7.3 黄金样例

`out/probe_*.json` 验证 Hypothesis 组装；`out/b300s_30ms.json` 验证 timing 字段。

---

## 8. 验收

1. 1,000 条单设备跑通，`case_result` 数量与计划一致。
2. 同一设备不可能被两个 Run 同时使用。
3. 条件变化 5 分钟内中断，`aborted_condition_changed`，且**不自动评分**。
4. 无 SIGKILL 路径；所有超时走 `close`。
5. 缓存命中 `case.status=cached`，不重复调用设备，性能指标不计入。
6. `completed`/`partial` 自动生成 Evaluation；`aborted`/`failed`/`cancelled` 不生成。
7. 同一 Run 可生成多个 Evaluation（不同口径），互不覆盖。
8. 评分失败不影响 Run 的执行状态。
9. ETA 稳定段误差 < 15%。

---

## 9. 契约

| 下游 | 需要什么 |
|---|---|
| M5 | `case_result`（无 metrics）+ `hypothesis` + `subset` + `evaluation` |
| M5 | 逐样本指标写 `case_score`；聚合写 `metric_value(evaluation_id, ...)` |
| M6 | 报告以 **Evaluation** 为单位；`run.snapshot`、`evaluation.profile_hash/scorer_version`、abort 原因 |
| M7 | SSE（run 与 evaluation 两套事件）、取消/续跑/手动评分接口 |

---

## 10. 风险

| # | 风险 | 处理 |
|---|---|---|
| 1 | 单实例调度成为瓶颈 | 当前设备 ≤10，够用；后续可多实例 + 租约 |
| 2 | 长任务 worker 崩溃 | 租约 + 断点续跑 |
| 3 | 条件变化检测延迟 5 分钟 | 可配；过短会增加 SSH 压力 |
| 4 | 缓存复用导致结果不可比 | `cached` 标记，性能指标不计入 |
| 5 | 自动评分放大评分 bug 影响面 | `auto_score=false` 可关；Evaluation 可单独取消 |
| 6 | 一号多评分造成报告口径混淆 | 报告必须显示 `profile_hash` 与 `scorer_version` |
