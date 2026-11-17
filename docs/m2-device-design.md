# M2 设备与部署 · 详细设计卡 v0.1

> **实现状态（截至 2026-11-07）**
> - 已实现 `probe_lite`：`GET /v1/models` 选 STT 模型 → WS `init` → 落 `device/deployment/probe_run`。
> - 实测：205（etsstt）187ms、502（gateway）32ms；能力声明与真实输出一致（502 punctuation=false）。
> - 部署指纹 `uid` 只含身份字段；host/port/能力/性能不参与。
> - `probe_identity` 与本地设备 `probe_full` 已实现；full 使用固定双人音频、30ms 分片、1x 实时转写，记录 partial、分段、说话人、标点、usage 与流内收尾观测；观测失败时保留身份快照并列出未知项。
> - 2026-11-07：platform 205 真机 API E2E 连续 3 次通过，耗时 12.3–12.7s，部署 UID 一致，7 项音频观测均为 true；身份解析忽略误报为平台号的固件版本 `1.0.98`，保留台账平台码 `205`。
> - 2026-11-07：接入 Q7 Fernet 凭据库；设备 SSH 密码/私钥和 cloud API Key 只存密文，API 仅返回凭据类型与配置状态。SSH 身份采集和运维动作使用保存的凭据；cloud full 只通过 HTTPS `GET /v1/models` 验证 Bearer Key。
> - 2026-11-13：说话人映射改为**只信 final**：`transcript.final.global_spk_list` 是唯一可信来源，转写过程中的 `sentences[].spk` 会实时变化，适配器不再采纳；final 重发完整句子时整体替换（避免旧时间戳与标签错位），文本按去重后的句子重建（避免 final 重复 segment 造成文本翻倍）。标签缺失或长度不符时保留 `spk` 占位，DER 走 `na(no_diarization_output)`，不猜映射。
> - 2026-11-10：205 的 `/v1/models` 可能未开放；etsSTT 身份采集在 `/v1/models` 失败但 `get_version` 能给出型号/大版本时降级成功，并记录 warning，避免整次探测失败导致大版本、模型版本缺失。已有设备需重新探测以补齐字段。
> - 设备运维只保留 `restart`，按 Q8 保持人工触发，不自动重启；已接入 `device:ops`、空闲与指纹复核、异步 Job 和审计。**不再修改设备 SSFConfig 文件**：205/600 的 8701 回环访问由平台侧 SSH 本地转发解决（M2 §5.3.1）。
> - 2026-11-05：按附录 A 生成三段合成中文探测素材，完成采样规格、时长、响度、标注与哈希验收；素材已放入 `assets/probe/`。
> - 2026-11-06：完成 M2 §5.2.1 声纹链路自检及 M7 统一 Job 接口；首次部署或 deployment uid 变化后空闲自动排队，手动接口仍可触发；暂存结果缓存 30 分钟并验证 SHA-256。
> - 2026-11-06：完成 M2 §5.1.1 身份采集；支持 SSH/kubectl 版本命令、HTTP 模型与系统信息、序列号和部署指纹更新；etsstt 固定版本命令在登录 shell 执行以加载设备 PATH；Run 租约期间只拒绝会创建 ASR 会话的 lite 探测。
> - 2026-11-07：502 网关版本采集经 SSH 在网关宿主调用 `sudo -n kubectl exec -n ns-meagt`；`etsllm` 使用容器内固定路径 `/opt/etsme/bin/etsllm`，避免依赖开发机安装 `kubectl` 或容器 PATH。
> - 2026-11-07：502 网关声纹暂存也可经 SSH 转发到宿主 kubectl；`/mnt/voiceprint` 已实测可写。真实网关 E2E 的暂存、192 维提取、匹配回显和文件清理通过。
> - 2026-11-07：205 真机通过已核验的 SSH key 建立临时转发后完成声纹链路自检；A/B 均提取 192 维，`matched_id` 分别为 A（0.8394）和 B（0.6543），两条设备暂存文件均已删除。205/502 真机裸 `start/end` 均为整数毫秒，适配器据此区分整数毫秒与秒制浮点数。
> - 2026-11-07：仓库补齐 `tools/probe_etsstt.py`，输出消息类型计数、`final.text`、`global_spk_list`、分段与安全的匹配摘要；不输出声纹向量。205 真机按 30ms / 1× 模式验证通过，TTFT 1,055ms、finalize 1,312ms。

> 上游：`asr-eval-design-review.md`（12 项决策已冻结）
> 依赖：M9（配置/日志/DB 骨架）、M10（探测脚本，已有 `probe_etsstt.py`）
> 下游：M4 执行引擎（消费 Adapter / Slot / 健康状态）、M5（消费声纹向量）、M6（消费指纹做分组）

---

## 1. 职责与边界

### 1.1 负责

| 职责 | 说明 |
|---|---|
| 设备台账 | 增删改查、凭据引用、标签、启停 |
| 能力探测 | `declared`（init 返回）+ `observed`（实跑探测） |
| 部署指纹 | 自动合成、变化检测、历史保留 |
| 设备适配 | 三类 Adapter（etsstt / gateway / cloud）+ Mock |
| 远端暂存 | 声纹音频送达到设备可读路径 |
| 声纹提取 | 按 deployment 物化的 192 维向量与缓存 |
| 健康巡检 | 在线状态、需重启状态、最近一次错误 |
| 运维动作 | `restart`（人工触发、二次确认、审计） |

### 1.2 不负责

| 不负责 | 归属 |
|---|---|
| 任务排队与 slot 分配 | M4 |
| 评测参数与口径 | M3 |
| 打分与报告 | M5 / M6 |
| 自动重启设备 | 明确不做（Q8） |

### 1.3 关键约束（来自实测）

1. **禁止强杀转写进程**；超时走 `close` 帧。
2. **整机独占**：一台设备同一时刻只服务一个 Run；Run 内默认 1 路会话（Q9）。
3. `capabilities.punctuation` 不可信，门控只认 `observed.punctuation_output`。
4. `streaming` 声明可信，但仍记录 observed 结果。
5. 网关声纹暂存默认 `exec-cat`（不依赖容器内 tar）。

---

## 2. 领域对象

### 2.1 Device（物理设备）

```python
Device:
  id: uuid
  sn: str | None            # 序列号（唯一）
  name: str
  platform: str             # 205 | 600 | 502；首次身份探测前为 unknown
  kind: str                 # etsstt | gateway | cloud
  host: str
  port: int                 # 转写端口
  ssh_user: str | None
  ssh_port: int
  secret_ref: str | None    # 指向凭据库的 key，不存明文
  tags: dict
  status: str               # unknown | online | degraded | needs_restart | offline
  created_at, updated_at
```

### 2.2 Deployment（冻结快照）

```python
Deployment:
  id: uuid
  device_id: uuid
  uid: str                  # 16 hex，仅由"身份字段"算出
  platform, bigversion, etsllm_version
  accel: dict
  model_key, model_version
  quant: str | None         # 派生字段，不参与 uid
  soc: str | None           # 派生字段，不参与 uid
  capabilities: dict        # 布尔/枚举：能不能
  limits: dict              # 可选上限：null 表示"未知/无明确限制"
  probe_meta: dict          # 这次观测的元信息
  raw: dict
  captured_at
```

> **性能不在这里**。`ttft` / `rtf` 是评测结果，属于 Run；设备信息只描述"是什么、能不能、最多多少"。

### 2.3 能力 / 上限 / 元信息

**能力（Capabilities）**：只放布尔或枚举，回答"能不能做"。

```jsonc
{
  "declared": {                    // init 响应 + 产品型号事实
    "streaming": true, "vad": true, "itn": true,
    "punctuation": false,
    "naming": true,                // ★ 按 kind 的静态事实：etsstt/gateway=true，cloud=false
    "hotwords": {"supported": true, "max_count": 150},
    "languages": ["zh","en","yue"]
  },
  "observed": {                    // 实跑一次探测音频得到
    "partials": true,
    "segments": true,
    "speakers_in_sentence": false,
    "global_spk_list": true,
    "punctuation_output": true,
    "usage_field": true,
    "finalizes_in_stream": true,
    "naming": null                 // null=未自检；true/false=自检结果
  }
}
```

**上限（Limits）**：只放"最多能做多少"，**全部可选**，`null` 表示未知或无明确限制。

```jsonc
{
  "realtime_slots": 3,          // 并发会话数；未配置默认 1
  "max_audio_sec": null,        // 未知/无限制；cloud 适配器可自行上报
  "max_spk": null,
  "hotword_max": 150            // 通常直接取 declared.hotwords.max_count
}
```

> `etsstt` / `gateway` 对音频时长没有明确限制，因此 `max_audio_sec` 保持 `null`，**不作为必填、不作为开跑前置条件**。只有 cloud 适配器在文档明确有上限时才填。

**元信息（probe_meta）**：描述"这次观测"本身。

```jsonc
{
  "probed_at": "2026-11-03T10:40:00",
  "probe_version": "probe-2",
  "probe_audio": "probe_two_spk_10s.wav",
  "observed_missing": []        // 哪些能力项没能探测出来
}
```

### 2.4 Fingerprint（uid 的组成）

```
uid = sha256(json.dumps({
  platform, kind, bigversion, etsllm_version,
  accel, model_key, model_version
}, sort_keys=True))[:16]
```

**参与**：只放"换了它就算是另一个部署"的字段。
**不参与**：

| 字段 | 原因 |
|---|---|
| `host` / `port` | 网络位置，不是部署身份 |
| `captured_at` / `raw` | 观测元信息 |
| `sn` | 属于 Device |
| `quant` / `soc` | 由 `model_key` / `platform` 派生，冗余 |
| `slots` / `limits` | 配置与容量，改了不该断历史 |
| `capabilities` | 能力会随探测波动（尤其 observed） |
| 任何性能数值 | 每次测量都不同 |

### 2.5 StagingInfo

```python
StagingInfo:
  method: "sftp" | "exec_cat" | "kubectl_cp" | "hostpath" | "unsupported"
  host_dir: str | None       # 宿主可写目录
  container_dir: str | None  # 容器内可见目录
  verified_at: datetime
```

### 2.6 Health

```python
Health:
  state: "online" | "degraded" | "needs_restart" | "offline"
  tcp_ok: bool
  http_ok: bool
  init_ok: bool
  last_error: str | None
  checked_at
```

---

### 2.7 性能数据放在哪里（不放在设备信息里）

**结论：设备信息里没有性能表。** TTTF / RTF 是评测结果，不是设备属性。

| 问题 | 答案 |
|---|---|
| 性能的权威来源 | Run 的 `case_result` / `metric_value`（M4/M5 产出） |
| 设备信息要不要存 | **不要**。设备只描述"是什么、能不能、最多多少" |
| 那 ETA 怎么估 | 从历史结果**派生查询**：按 `(deployment_id, mode, chunk_ms, input_rate, concurrency, diarization)` 分组，取最近 N 次的 median |
| 没有历史怎么办 | 用兜底常量（按 `kind` 分档），ETA 标注「估算，样本不足」 |
| 要不要物化 | 可选：做一个派生视图/缓存表加速，但它是**结果索引**，不是设备属性；随时可重建 |

**为什么不放设备表**

1. **来源冲突**：设备表和历史结果会是同一数字的两个来源，迟早不一致。
2. **生命周期不同**：设备是长期实体；性能随负载、温度、并发变化，每次评测都更新。
3. **概念污染**：一旦放进设备表，代码里就会出现"用设备属性判断结果好坏"的错误用法。
4. **指纹风险**：性能数值进任何身份哈希都会导致历史断裂（已在 §2.4 排除）。

**ETA 的取值顺序（M4 实现）**

```
1. 同 deployment + 同条件（mode/chunk/rate/concurrency/diarization）的历史 median
2. 同 model_key + 同条件的历史 median（跨设备同模型）
3. 同 kind 的默认常量（如 gateway 0.12、etsstt 0.28、cloud 0.3）
4. 标记 eta_source = historical | cross_device | fallback
```

## 3. 对外接口

### 3.1 服务接口（Python）

```python
class DeviceService:
    def create(self, req: DeviceCreate) -> Device
    def update(self, device_id: str, req: DeviceUpdate) -> Device
    def list(self, filters) -> list[Device]
    def get(self, device_id: str) -> Device
    def delete(self, device_id: str) -> None

    def probe(self, device_id: str,
              mode: Literal["lite","identity","full"] = "full") -> ProbeResult
    def check_voiceprint(self, device_id: str) -> VoiceprintCheckResult
    def deployments(self, device_id: str) -> list[Deployment]
    def current_deployment(self, device_id: str) -> Deployment
    def get_deployment(self, deployment_id: str) -> Deployment

    def health(self, device_id: str) -> Health
    def ops_restart(self, device_id: str, actor: str) -> OpsResult

class AdapterRegistry:
    def for_device(self, device: Device) -> Adapter
```

### 3.2 Adapter SPI

```python
class Adapter(Protocol):
    def capabilities(self) -> Capabilities: ...
    def transcribe(self, audio: Path, params: AsrParams,
                   on_event: Callable[[str, dict], None] | None = None) -> Hypothesis: ...
    def stage(self, local: Path, name: str) -> str: ...          # 返回容器/宿主可见路径
    def extract_voiceprint(self, remote_path: str) -> list[float]: ...
```

`AsrParams`：
```python
AsrParams:
  mode: "stream" | "batch"
  chunk_ms: int = 30
  input_rate: float = 1.0       # 1x
  itn: bool
  diarization: bool
  voiceprints: list[VoiceprintGroup] | None
  speaker_match_threshold: float
  max_spk: int
  timeout_s: int | None
```

`Hypothesis`（M4 直接消费）：
```python
Hypothesis:
  text: str
  segments: list[Segment]        # start_ms/end_ms/speaker/text
  speaker_match: dict[str, MatchInfo]   # label -> {matched_id, confidence}
  usage: dict
  timing: dict                   # ttft_ms, first_segment_ms, finalize_ms, wall_ms
  raw_uri: str
  mode: str
```

### 3.3 HTTP

```
GET    /api/v1/devices
POST   /api/v1/devices
PATCH  /api/v1/devices/{id}
DELETE /api/v1/devices/{id}
GET    /api/v1/devices/{id}/credential             # 只返回类型与配置状态
PUT    /api/v1/devices/{id}/credential             # If-Match；明文只接收、不回显
DELETE /api/v1/devices/{id}/credential             # If-Match

POST   /api/v1/devices/{id}/probe?mode=lite|identity|full
POST   /api/v1/devices/{id}/voiceprint-check          # 声纹链路自检
GET    /api/v1/devices/{id}/deployments
GET    /api/v1/deployments/{id}
GET    /api/v1/devices/{id}/health

POST   /api/v1/devices/{id}/ops/restart     # admin + confirm
GET    /api/v1/devices/{id}/ops/history
```

新增设备时填写名称、类型、主机地址和服务端口。etsSTT 还需填写 SSH 用户、端口与密码/私钥；网关填写 SSH 用户和端口，并选择 SSH 密码或私钥（与 etsSTT 一致；未保存凭据时仍可使用服务端 SSH 身份，但添加流程要求填写）；云端填写 API Key。平台码可省略，前端登记后自动启动 `identity` 探测，从 `/v1/system/info` 识别 205 / 600 / 502；型号和能力也由探测补齐。云端类型没有硬件平台码，服务验证依赖 API Key。

凭据写接口仅接受 `ssh_password`、`ssh_private_key` 或 `cloud_api_key`。每台设备只保留一种凭据；更换时覆盖旧密文。etsSTT 与网关接受 SSH 密码/私钥，cloud 只接受 API Key；网关保存的 SSH 凭据用于身份采集与健康检查，声纹暂存仍使用服务进程的 SSH 身份连接设备宿主并调用宿主 kubectl 上下文。保存和删除凭据会递增设备版本，并拒绝 Run、探测、自检或运维任务运行期间的修改。

### 3.4 事件（SSE / 内部）

```
device.probe.started / device.probe.finished
device.health.changed
device.deployment.created
device.overloaded
```

---

## 4. 数据结构

```sql
create table device (
  id uuid primary key default gen_random_uuid(),
  sn text unique,
  name text not null,
  platform text not null,
  kind text not null,
  host text not null,
  port int not null,
  ssh_user text,
  ssh_port int not null default 22,
  secret_ref text,
  tags jsonb not null default '{}',
  status text not null default 'unknown',
  last_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table device_credential (
  id text primary key,
  device_id uuid not null unique references device(id) on delete cascade,
  kind text not null,                -- ssh_password | ssh_private_key | cloud_api_key
  ciphertext bytea not null,         -- Fernet token；不保存明文
  updated_at timestamptz not null default now()
);

create table deployment (
  id uuid primary key default gen_random_uuid(),
  device_id uuid not null references device(id) on delete cascade,
  uid char(16) not null,
  platform text, bigversion text, etsllm_version text,
  accel jsonb, model_key text, model_version text,
  quant text, soc text,
  capabilities jsonb not null,
  limits jsonb not null default '{}',
  probe_meta jsonb not null default '{}',
  raw jsonb,
  captured_at timestamptz not null default now(),
  unique (device_id, uid)
);
create index deployment_device on deployment (device_id, captured_at desc);

create table probe_run (
  id uuid primary key default gen_random_uuid(),
  device_id uuid not null references device(id) on delete cascade,
  mode text not null,               -- lite|identity|full|voiceprint
  status text not null,             -- ok|failed
  duration_ms int,
  result jsonb,
  error text,
  created_at timestamptz not null default now()
);

create table staging_probe (
  device_id uuid primary key references device(id) on delete cascade,
  method text not null,
  host_dir text, container_dir text,
  verified_at timestamptz not null,
  error text
);

create table ops_action (
  id uuid primary key default gen_random_uuid(),
  device_id uuid not null references device(id) on delete cascade,
  action text not null,             -- restart（历史 prepare 记录只读展示）
  actor text not null,
  status text not null,
  detail jsonb,
  created_at timestamptz not null default now()
);
```

---

## 5. 关键流程

### 5.1 probe_lite（约 200ms）

```
TCP connect → WS connect → 发 init → 收 init 响应（超时 10s）
  ├─ 成功：记录 declared + init 延迟
  └─ 失败：status = offline / needs_restart（依错误码）
不抓取版本（版本只在 full 里抓，避免拖慢列表刷新）
```

### 5.1.1 probe_identity（约 1~3s，不建 WS）

```
etsstt: SSH get_version + etsllm --version（登录 shell 加载设备 PATH）
gateway: SSH → sudo -n kubectl exec -n ns-meagt deploy/mebox -- get_version
         SSH → sudo -n kubectl exec -n ns-meagt deploy/etsllm -- /opt/etsme/bin/etsllm --version
HTTP:        /v1/models + /v1/system/info（取 sn / platform / model-type）
→ 重算 uid + sn
```

**唯一用途**：任务进行中的条件变化检测（§5.3）。不建 WS 会话，因此不会破坏"整机独占 + 单路"的运行条件。

### 5.2 probe_full（约 12~20s）

```
1. probe_lite → declared
2. 版本采集
   etsstt : SSH get_version / etsllm list / HTTP /v1/models / system/info
   gateway: SSH 到网关宿主后 kubectl exec mebox get_version / etsllm 绝对路径 --version / system/info
   cloud  : GET /v1/models（仅校验 Key）
3. 观测探测（内置 10s 双人探测音频，30ms 分片，1x）
   一次会话（开分离，不传声纹）→ partials / segments /
   speakers_in_sentence / global_spk_list / punctuation_output /
   usage_field / finalizes_in_stream
   ※ 不记录 ttft / rtf / 时长上限——那些是评测结果或测试结论
4. 生成 fingerprint → upsert deployment
5. 写 probe_run

**耗时**：约 12~15s。

> `naming` 不在此步检查：etsstt / gateway 默认支持声纹与匹配，属**型号事实**，写入 `declared.naming`。是否真的能跑通，由单独的「声纹链路自检」验证（§5.2.1）。
```

**降级**：第 3 步失败不影响第 2 步结果；`observed` 字段缺失时记 `unknown`，指标门控按 `declared` 兜底并标 `observed_missing`。

### 5.2.1 声纹链路自检（按需，6~10s）

**它验证的不是"支持不支持"**（`etsstt` / `gateway` 默认支持声纹与匹配，属型号事实），
而是**这条链在当前部署上是否真的通**：

1. 暂存能写入，且容器内可读（配置 SSH 用户时经 SSH 转发至网关宿主 kubectl；无 SSH 用户时使用服务端 kubectl 上下文）
2. `POST /v1/audio/voiceprints` 能提取 192 维向量
3. `init.voiceprints` 能被服务端接受
4. 转写返回里 `spk_voiceprints[*].match_info.matched_id` 能回显

**触发时机**

| 时机 | 说明 |
|---|---|
| 设备首次接入 | 一次性验证 |
| `deployment.uid` 变化后 | 固件/模型升级可能改变行为，空闲时自动排一次 |
| 手动「自检」 | admin 触发 |
| 命名指标异常（FA/FR 突增） | 作为诊断手段 |

**流程**

```
staging.put(probe_spk_A.wav) → POST /v1/audio/voiceprints → vecA
staging.put(probe_spk_B.wav) → POST /v1/audio/voiceprints → vecB
transcribe(probe_two_spk_10s.wav,
           diarization=true, voiceprints=[A,B], mode=batch)
→ 断言 spk_voiceprints 存在，且至少一个 matched_id ∈ {A,B}
→ capabilities.observed.naming = true / false
```

**结果处理**

| 结果 | 行为 |
|---|---|
| `true` | 记录 confidence；命名指标可评 |
| `false` | `observed.naming=false` → 命名 N/A(`voiceprint_link_failed`) + 告警 |
| 未跑 | `observed.naming=null` → 指标允许，但报告标注「未自检」 |

**权限**：与 `probe_full` 相同；运行中禁止。

### 5.3 指纹变化检测

| 时机 | 行为 |
|---|---|
| Run 启动 probe | 与最近一次 deployment 比较；不同 → 新建 deployment，Run 绑定新 uid |
| Run 结束 probe | 若 uid 变化 → `run_target.status = drift`，默认不参与对比，提示重跑 |
| 巡检（5 分钟） | 只做 lite；变化只记录，不打断任务 |

**为什么不在每条样本间探测**：开销大；且引擎在跑任务时探测会与任务争抢 slot。

> 只比较**身份字段**（`sn` + `uid`）。`limits`、`capabilities`、性能数值的变化都不算条件变化。

**为什么中断而不是只告警**：同一任务内混用两个部署，得到的聚合指标没有意义（既不是 A 也不是 B）。中断是最诚实的选择。

**为什么用"身份检查"而不是 `probe_full`**：Run 期间设备被整机独占且默认单路，再开 WS 会话会破坏单路条件、甚至触发 overload。版本采集走 SSH/kubectl + HTTP，不占用 ASR 会话。

**2026-11-10 UI 合并**：用户只操作一个“检测设备”按钮，请求 `mode=auto`。设备空闲时执行 `full`（身份检查 + 能力探测 + 双人探测音频）；Run 租约有效时自动降为 `identity`，不创建 ASR 会话。`lite/identity/full` 仍保留给脚本和诊断使用，历史记录按实际执行模式展示。

### 5.3.1 设备租约与探测权限

| 设备状态 | `probe_lite` | `probe_identity` | `probe_full` |
|---|---|---|---|
| 空闲 | ✅ | ✅ | ✅ |
| 被 Run 独占（有租约） | ❌ 会开 WS | ✅ 不建会话 | ❌ 会开 WS + 暂存文件 |

### 5.3.2 205/600 回环端口的 SSH 本地转发

205/600 的 ASR 只监听设备 `127.0.0.1:8701`。平台**不修改设备 SSFConfig，也不把 8701 暴露到局域网**，而是在
`backend/app/adapters/ssh_tunnel.py` 中按设备建立一条只绑定平台 `127.0.0.1` 的 SSH 本地转发：

```
平台 127.0.0.1:<临时端口> --ssh--> 设备 127.0.0.1:8701
```

规则：

- 仅 `kind=etsstt` 且配置了 `ssh_user` 时启用；
- 先探测设备 `host:port` 是否直连可达；可达则直连，不建立隧道；
- 直连不可达时用 `ssh -N -L` 建立转发，复用平台用户的 SSH key/agent 与 known_hosts，不把密钥写到命令行；可用 `ASR_DEVICE_SSH_KNOWN_HOSTS` 指定已核验的 known_hosts；
- ASR HTTP/WS 请求走临时本地端口，`/v1/system/info` 与 SSH 版本命令仍走设备真实地址；
- 隧道进程退出后下一次调用自动重建；应用退出时 `atexit` 清理；
- 手工矩阵/CLI 仍可用 `tools/device_tunnel.sh`，平台自动路径不依赖它。

因此 `probe_lite`、`probe_identity`、Run 转写、声纹提取/自检都通过同一个 `device_endpoint()` 取 ASR 地址。

- 租约由 M4 持有（PG `device_lease`），M2 通过注入的 `lease_active(device_id)` 判断；
- 运行期间只允许 `probe_identity`；
- 巡检只做 `probe_identity`（不再做 lite），避免与运行争抢会话。

### 5.4 声纹暂存三级探测

```
prepare():
  1) etsstt: 确保 /data/voiceprint 存在且可写（SFTP）
  2) gateway:
     a) kubectl exec deploy/etsllm -- sh -c "mount | grep -E ' /mnt| /opt/etsme| /tmp'"
        → 有 PVC/共享目录：SFTP 到宿主对应路径（若可定位），或直接用 exec-cat
     b) kubectl cp（容器有 tar 时）
     c) kubectl exec -i -- sh -c "cat > /mnt/voiceprint/{name}"
  3) 校验：容器内 md5sum 与本地一致
  4) 缓存 StagingInfo 30 分钟
```

**失败处理**：三级全失败 → `method=unsupported`，命名指标 `N/A(staging_unavailable)`，不阻塞其他指标。

### 5.5 声纹提取与缓存

```
get_or_extract(enrollment_id, deployment_id):
  1. 查 voiceprint 表（enrollment_id, deployment_id）
  2. 未命中：
     staging.put(enrollment.audio, sha16 + ".wav")
     POST /v1/audio/voiceprints {model, file_path}
     → 192 维 → 存库（float32 768B）
  3. 返回向量
```

**失效条件**：`deployment.uid` 变化（新的 voiceprint 模型）→ 自然 miss。

### 5.6 转写会话生命周期

```
open WS
  → init（model, sample_rate, streaming, itn, diarization, voiceprints, threshold）
  → 逐块发送（chunk_ms=30，input_rate=1.0）
  → audio.end
  → 等 final（看门狗 max(30s, 2×rtf×duration)）
      ├─ 收到：正常 close
      └─ 超时：发 close 帧 → 等 5s → 记 finalize_timeout
失败判定：无 final / 收到 error / 连接中断
```

**绝不**：`kill -9`、直接关闭 TCP、重试时不换连接。

### 5.7 健康巡检

```
每 5 分钟（并发 5，超时 10s）：
  1) TCP connect
  2) probe_identity（SSH/kubectl 取版本 + HTTP /v1/models + system/info）
  状态迁移：
    online → offline       （连续 3 次 TCP/HTTP 失败）
    online → degraded      （HTTP 通但版本采集失败）
    any    → needs_restart （收到 engine_overloaded 且当前无活跃会话）
```

> 巡检**不再开 WS 会话**，避免与运行中的评测争抢单路条件。

**needs_restart 只能由 `ops_restart` 清除。**

### 5.8 运维动作

| 动作 | 权限 | 前置 | 行为 | 审计 |
|---|---|---|---|---|
| `restart` | admin | 无运行中任务 | `systemctl` / `supervisorctl` / `kubectl rollout restart` | 记录命令与输出 |

失败只写 `ops_action.detail`，不修改设备配置文件。

---

## 6. 异常与降级

| 场景 | 行为 | 用户可见 |
|---|---|---|
| TCP 拒绝 | status=offline | 设备卡片红色 + hint |
| init 超时 | 重试 1 次；仍失败 → offline | 同上 |
| `engine_overloaded` | needs_restart，熔断该设备 | 红色徽标 + "需重启" |
| 版本采集部分失败 | deployment 仍创建，缺字段为 null | 指纹卡片显示"部分未知" |
| 探测音频失败 | observed 记 unknown，不阻塞 | 指标门控标 `observed_missing` |
| staging 失败 | 命名 N/A | 原因码 `staging_unavailable` |
| 声纹提取失败 | 该 enrollment 重试 1 次；仍失败 → 该说话人 N/A | 说话人卡片黄色 |
| Run 中身份变化（sn/uid） | **中断该 target**，标 `aborted_condition_changed`；已完成结果保留但不参与对比 | 红色横幅：设备或模型已变更，请重新发起评测；展示新旧指纹对比 |
| 凭据解密失败 | 拒绝操作 | "凭据不可用，请重新保存" |
| 运行中请求 probe_lite/full | 拒绝并提示 | "设备正在评测中，仅支持身份检查" |

---

## 7. 测试点

### 7.1 单元

| 用例 | 断言 |
|---|---|
| 解析 RKNN `get_version` | quant=w8a8、soc=rk3588 |
| 解析 Ascend `version.info` + `system/info` | accel 三字段、soc=310p-24g-arm64、model_version=null |
| capabilities 合并 | observed 覆盖 declared；缺失记 unknown |
| fingerprint 稳定性 | 同输入同 uid；改一个字段变 uid |
| staging 选择 | 502 → exec_cat；205 → sftp |
| 超时看门狗 | 无 final 时触发 close，不 kill |

### 7.2 集成

| 用例 | 环境 |
|---|---|
| probe 205 | 真实设备，连续 3 次结果一致 |
| probe 502 | 真实网关门，kubectl 路径通 |
| 声纹链路自检 | 推文件 → 提取 → 转写 → matched_id 回显（`check_voiceprint`） |
| overload 识别 | 人为占满会话后探测 |
| 条件变化中断 | 任务中人为改模型/换设备，5 分钟内中断并提示 |

### 7.3 黄金样例

用已保存的真实报文做契约测试：
`out/probe_diar.json`、`out/probe_naming.json`、`out/b300s_30ms.json`（组装器）、
`data/jobs/**/environment.json`（RKNN 版本样例）。

---

## 8. 验收标准

1. 205 与 502 各一台：probe 出的 `quant/soc/model_key` 与人工核对一致。
2. 同一设备连续 3 次 `probe_full`，`uid` 完全一致。
3. 声纹链路自检：`probe_spk_A/B` 提取向量后转写，`matched_id` 正确回显，confidence > 0.5。
4. 人为制造 `engine_overloaded`，平台在一次巡检内标记 `needs_restart`，且不再重试。
5. 代码审查确认：无 `SIGKILL` 路径，所有超时走 `close`。
6. `probe_full` 耗时 < 40s（含命名子检查；不含版本采集失败重试）。

---

## 9. 与其它模块的契约

| 下游 | 需要什么 | 形式 |
|---|---|---|
| M4 | `Adapter.transcribe`、`Capabilities`、`limits.realtime_slots`、健康状态 | Python 接口 |
| M4 | **ETA 用的性能基线**：从历史 `case_result` 派生，不由 M2 提供 | SQL 查询 |
| M4 | 设备独占锁的持有者标识 | `device_id` |
| M5 | 声纹向量 | `voiceprint.vec` |
| M6 | 指纹分组维度 | `quant`、`soc`、`model_key`、`bigversion` |
| M7 | 设备 CRUD / probe / ops | HTTP |
| M8 | 设备卡片、部署时间线、能力徽标 | HTTP |

---

## 10. 待定与风险

| # | 事项 | 影响 | 处理 |
|---|---|---|---|
| 1 | 600 的量化/SoC 值与持续容量尚未完整验收 | 横评缺 `quant/soc` 维度，长时稳定性未知 | 2026-11-07 identity、声纹链路和 AISHELL-1 单条 Run CER 已通过；补核部署快照字段，再跑 100 条与 5 分钟并发，不改接口 |
| 2 | 昇腾量化信息拿不到 | 横评缺 `quant` 维度 | 允许人工补 `note`，不伪造 |
| 3 | 探测音频版权 | 内置素材 | 用自录 10s 双人素材，随平台发布 |
| 4 | 云端设备的 `stage`/`voiceprint` | 不支持 | Adapter 返回 `Unsupported`，指标 N/A |
| 5 | `realtime_slots` 取值 | 目前按型号配置（网关 3 / 其他 1） | 可选 `probe_slots` 压测验证，默认不跑 |

---

## 附录 A · 内置探测素材规格

随平台发布，放在 `assets/probe/`，版本化；`probe_meta.probe_audio` 记录文件名与 sha256。

| 文件 | 时长 | 内容要求 | 用途 |
|---|---|---|---|
| `probe_two_spk_10s.wav` | 10s | 2 个说话人交替；含 `，。？` 各至少 1 个；中段有 0.5~1s **重叠**；SNR > 30dB；无背景音乐/第三人声 | 能力探测（partial/标点/说话人）+ 声纹链路自检 |
| `probe_spk_A.wav` | 6s | 与上面 A 说话人同一人；单人、干净、无重叠 | 声纹提取 |
| `probe_spk_B.wav` | 6s | 与上面 B 说话人同一人 | 声纹提取 |
| `probe_ref.json` | — | 上述 10s 的参考文本与分段（可选） | smoke CER / 标点自检 |

**音频格式**：16 kHz / 16 bit / 单声道 / PCM WAV。

**生成检查清单**

- [x] A、B 使用不同合成音色；测试确认音色标识及 PCM 内容不同
- [x] 10s 片段里两人都有 ≥3s 语音
- [x] 标点覆盖 `，` `。` `？`
- [x] 中段包含 800ms 双人重叠
- [x] 只拼接 A/B 两种 TTS 音色，无其他声源、背景音乐或混响处理；不包含开源数据集录音
- [x] 三段的响度差不超过 2dB

验收证据：`backend/tests/test_probe_assets.py` 校验 WAV 参数、实际时长、SHA-256、声道/位深、响度、双人时长、重叠区间和标点；素材与参考分段见 `assets/probe/manifest.json`、`assets/probe/probe_ref.json`。本地重跑：`uv run pytest tests/test_probe_assets.py -q`，1 passed。

**为什么不再用开源数据集切片**：避免许可问题；且探测素材需要"双人 + 标点 + 重叠"同时具备，自录最可控。
