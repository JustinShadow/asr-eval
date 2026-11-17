# ASR 评测平台 · 总体设计 v1.0

> 全新设计。旧平台仅作为**产品接口事实**的参考，不继承数据、代码与表结构。
> 约束：内部测试/项目组共用、单台服务器、局域网、LDAP 登录、3~5 人、8 周首版。

---

## 0. 一页纸

**要解决的问题**：同一个模型在不同量化/编译/硬件上部署后，准确率与性能到底差多少；同一台设备换版本后，有没有退化。

**核心抽象（四层解耦）**

```
不可变资产 ──► 不可变考生 ──► 可缓存产物 ──► 纯函数评分
 DatasetVersion  Deployment    Hypothesis    MetricProfile
```

**六个实体**

| 实体 | 一句话 | 不可变性 |
|---|---|---|
| `DatasetVersion` | 一份冻结的考卷（题目 + 参考答案 + 标签） | 不可变 |
| `Deployment` | 一个冻结的考生（设备 + 固件 + 服务 + 模型 + 量化） | 不可变 |
| `Benchmark` | 考卷 + 评分标准 + 抽样规则 | 不可变（改动生成新版本） |
| `Run` | 一次评测 = Benchmark × Deployment[] | 可取消/可续跑，结果不可变 |
| `Hypothesis` | 一次推理的产物，内容寻址可复用 | 不可变 |
| `Score` | 打分结果，可随时用新口径重算 | 可重算 |

**十条关键决策**

| # | 决策 | 理由 |
|---|---|---|
| D1 | 推理与评分彻底分离 | 改口径不重跑设备；这是成本的唯一解 |
| D2 | `Benchmark` 而非 `Dataset` 是一等公民 | 只有"考卷+标准"才可比，数据集本身不可比 |
| D3 | 考生是 `Deployment` 而非 `Device` | 同设备换模型/固件即为不同考生，历史才可追溯 |
| D4 | 能力协商 + `N/A` 语义 | 不同产品能力不同，缺失指标不能记 0 |
| D5 | 编辑距离只用 `rapidfuzz` 一个引擎 | 与现有实现同源，结果一致；批量快一个量级 |
| D6 | DER 双实现 + 一次性口径验收 | pyannote 与 md-eval 的 collar 语义不同，不验收不能对外 |
| D7 | 并发档位仅对 perf 开放，默认单路 | 评测要复现单次录音的真实条件；多路只用于测算力上限 |
| D8 | Hypothesis 内容寻址缓存 | 同一音频跨 Run/跨口径复用，零设备成本 |
| D9 | PostgreSQL + 对象存储 | 多 worker 并发写、10 万行聚合、窗口函数 |
| D10 | OpenAPI 契约先行，前后端分离 | 3~5 人并行，前端不等人 |

**"秒级"的边界（先说清楚）**

| 动作 | 目标 | 说明 |
|---|---|---|
| 1 万条导入 | < 30s | 不含首次音频转码（懒加载） |
| 10 万条 item 列表 | < 300ms | 元数据预聚合 |
| 创建 Run（1 万条 × 3 目标） | < 2s | 不物化任务行，分片游标 |
| 1 万条打分 | < 60s | 纯 CPU，多进程 |
| **1 万条真实转写** | **小时级** | 受设备算力约束，不可能秒级；给准 ETA |

---

## 1. 领域模型

### 1.1 数据（不可变资产）

```
Dataset ── DatasetVersion ── Item ─┬── AudioAsset ── AudioVariant(16k/mono)
                                   ├── RefText     (verbatim / itn_expected)
                                   └── RefSegment  (start, end, speaker, text, skip)
```

**`Item` 是一切的原子单位**：一条音频 + 一份参考答案 + 一组标签。

```python
item_key      = "BAC009S0002W0122"          # 数据集内稳定标识
content_uid   = sha256(item_key|audio_sha|ref_sha|lang)[:24]
```

- `content_uid` 含音频哈希与参考哈希 → **换音频或改标注 = 新内容**，天然阻止"改题后还跟历史比"
- 与 old 平台的 `content_id` 思路一致，但公式重新定义，不背兼容包袱

### 1.2 考生（不可变部署快照）

```
Device(物理) ── Deployment(冻结快照)
```

```python
deployment_uid = sha256(platform|kind|bigversion|etsllm|accel|model_key|model_version|slots)[:16]
```

`Deployment` 同时携带 `capabilities`，因为**同一台设备换模型后能力会变**（标点/分离/热词）。

### 1.3 评测定义

```
Benchmark = DatasetVersion + MetricProfile + Sampling + Buckets + Params + StreamMode + Concurrency
version_hash = sha256(sorted(content_uid[]) + metric_profile.hash + sampling + params)[:16]
```

`version_hash` 相同 → 结果可直接比较；不同 → UI 必须显式提示。

### 1.4 执行

```
Run ── RunTarget(×Deployment×Subset) ── Shard ── Case ── Hypothesis
 └── Evaluation (1:N) ── CaseScore ── MetricValue(聚合)
```

- `Case` 的粒度 = `Item`（短句一条一句；长音频一整段，因为 DER 需要完整会话）
- **`Case` 与 `Evaluation` 分开**：`Case` 记录"跑没跑成"，`Evaluation` 记录"按哪个口径评、评得怎么样"
- 一次执行可被多次评分（换口径零设备成本）；执行状态不会被评分失败污染

---

## 2. 总体架构

### 2.1 逻辑分层

```
┌───────────────────────────────────────────────────────────────────────┐
│ 接入层   React SPA  ·  OpenAPI  ·  CI 触发  ·  LDAP                     │
├───────────────────────────────────────────────────────────────────────┤
│ 应用层   FastAPI（auth / dataset / benchmark / run / report / device）  │
├───────────────────────────────────────────────────────────────────────┤
│ 执行层   Scheduler ─► SlotPool ─► Adapter SPI ─► 设备                   │
│                   └► Scorer ─► Metric Plugin ─► Aggregator             │
├───────────────────────────────────────────────────────────────────────┤
│ 数据层   PostgreSQL（元数据/结果/队列/租约）                              │
│          本地对象目录（raw/报告/导出） + 只读 NFS（原始音频）             │
└───────────────────────────────────────────────────────────────────────┘
```

### 2.2 部署视图

```
lite（默认：本机 / 小服务器，2 容器）
├── app        uvicorn :8080（API + SPA + scheduler + 进程池评分）
├── postgres:16
├── data/objects（宿主机卷，raw/报告/导出）
└── /mnt/asr-data（NFS 只读，原始音频）

standard（服务器，可选 3~4 容器）
├── app        API + scheduler
├── worker     infer / score / import
├── postgres:16
└── caddy（可选，TLS）
```

无 Redis、无 MinIO、无 nginx；完整方案见 `m9-platform-design.md`。

**网络前提**：平台必须能直连设备 IP 与端口（205/600 的 8701、502 的 30025/30010、SSH 22）。

### 2.3 运行时数据流

```
① 导入    原始目录 ──scan──► manifest.jsonl ──► PG(item/ref/segment) + NFS(音频引用)
② 组卷    DatasetVersion + MetricProfile ──► Benchmark
③ 开跑    Benchmark × Deployment[] ──► Run（冻结 snapshot）
④ 推理    分片 ──► Slot ──► Adapter ──► Hypothesis（命中缓存则跳过）
⑤ 打分    Hypothesis × MetricProfile ──► Case.metrics ──► MetricValue（聚合+CI）
⑥ 出报    矩阵 / 分桶 / 回归 / Badcase ──► HTML / XLSX
```

---

## 3. 数据集子系统

### 3.1 统一模型

```python
@dataclass
class RawItem:
    item_key: str
    audio: Path
    ref_text: str | None                 # 无标点/原文
    itn_text: str | None                 # ITN 期望（可选）
    ref_segments: list[Seg] | None        # 长音频必需
    speakers: dict[str, dict]            # {id: {gender, age, accent}}
    buckets: dict                        # {dialect, scene, ...}
    lang: str

@dataclass
class Seg:
    start_ms: int
    end_ms: int
    speaker: str
    text: str
    skip: bool = False                   # [ENS]/[*] 等不评分区间
```

**两套标注并存**：
- `ref_text`：整段文本，用于 CER / 标点 / ITN
- `ref_segments`：时间轴，用于 DER / 命名
- 有 segments 时 `ref_text` 可由 segments 按时间拼接得到（不重复存）

### 3.2 Importer 插件

```python
class Importer(Protocol):
    name: str
    def detect(self, root: Path) -> bool: ...
    def scan(self, root: Path) -> Iterator[RawItem]: ...
```

新增数据集 = 新增一个文件，注册到 `importers/__init__.py`。不需要改核心。

### 3.3 六类数据集的真实映射

#### AISHELL-1（7,176 条 / 20 说话人）

```
data_aishell/transcript/aishell_transcript_v0.8.txt   "BAC009S0002W0122 而 对 楼市 ..."
data_aishell/wav/test/S0002/BAC009S0002W0122.wav      16k/16bit/mono
```

| 项 | 规则 |
|---|---|
| 文本 | 空格分词 → **去空格按字**；不能按词算 |
| 说话人 | 从 `utt_id[4:8]` 解析 `S0002` |
| 分段 | 无 → `ref_segments = [{0, duration, "S0002", text}]` |
| 可评 | **CER**；标点/ITN/DER/命名 = N/A |
| 陷阱 | 无标点；强行评标点会得 0 分，必须门控 |

#### AISHELL-3（88,035 条）

```
{train,test}/content.txt   "SSB06930002.wav\t武 wu3 术 shu4 始 shi3 ..."
spk-info.txt               "SSB1837  B  female  north"
```

| 项 | 规则 |
|---|---|
| 文本 | `split('\t')[1].split()` **取偶数下标**（奇数下标是拼音） |
| 音频 | 44.1kHz → 重采样（走 variant 缓存） |
| 分桶 | 性别 / 年龄段 / 口音 |
| 可评 | CER + 分桶 |

#### KeSpeech（19,723 条 / 9 个方言区）

```
kespeech/{Beijing,Ji-Lu,Jiang-Huai,Jiao-Liao,Lan-Yin,Mandarin,
          Northeastern,Southwestern,Zhongyuan}/<id>.{wav,txt}
```

| 项 | 规则 |
|---|---|
| 文本 | 同名 txt 单行，无标点 |
| 配对 | 按 `stem` 配对，不能按顺序 |
| 分桶 | **目录名 = 方言区**（`Mandarin` 5000 条作基线组） |
| 可评 | CER |

#### AliMeeting（官方 Test，20 段 / 10.75h）★ 最完整的评估集

```
Test_Ali_far/audio_dir/R8002_M8002_MS802.wav
Test_Ali_far/textgrid_dir/R8002_M8002.TextGrid
```

实测：4 个 tier / 会话（`N_SPK8005..8008`），每会话约 818 个 interval，**重叠 16.2%**，TextGrid 自带标点（`，。？`）。

| 项 | 规则 |
|---|---|
| 分段 | 合并所有 tier：`(xmin, xmax, speaker=tier名去N_前缀, text)`，按 xmin 排序 |
| 重叠 | 不同 tier 区间相交 → `overlap=true`（不合并、不丢弃） |
| CER | 按 xmin 顺序拼接，`<sc>` 分隔（**M2MeT 官方 FIFO 口径**） |
| 标点 | 可评（来自 TextGrid 文本） |
| ITN | **不可评**（官方无 ITN 对照） |
| DER | 可评；`collar=0.25` 单侧、`skip_overlap=false`、匈牙利映射 |
| 命名 | 可评（需为 4 个 SPK 准备注册音频） |

#### MagicData 方言（沪语 / 湘语 / 赣语）

```
AUDIOINFO.txt  CHANNEL  UTTRANS_ID  SPEAKER_ID[-1,-2]  TOPIC
SPKINFO.txt    CHANNEL  SPEAKER_ID  GENDER  AGE  REGION,CITY  DEVICE
TXT/*.txt      [3.080,7.060]\tG0003\tmale\t阿拉两个拧来聊聊金融方面呃
```

| 项 | 规则 |
|---|---|
| 解析 | `^\[(\d+\.?\d*),(\d+\.?\d*)\]\t(\S+)\t(\S+)\t(.*)$` |
| 特殊标记 | `[+]`重叠 / `[*]`不可懂 / `[ENS]`环境噪声 / `[LAUGHTER]` / `[SONANT]` / `[MUSIC]` / `[SYSTEM]` / `[FILLER]` / `[PII]` → `skip=true`，同时计入 DER 的非语音 |
| 非语音行 | `speaker=G0000, gender=none` → 跳过 |
| 性别字段 | 沪语 `male/female`，湘语 `男/女` → 解析层归一 |
| 声纹素材 | ★ 按 speaker + 时间轴从 TXT 切出片段，天然构成注册库 |
| 可评 | CER / 标点 / DER / 命名 |

#### APY / DataTang（粤语 / 四川话 / 闽语）

```
<ses>/ast_mic.wav   ast_mic.txt   ast_mic.metadata
txt: "start\tend\tO1\t文本"
metadata: LHD 格式（SAM/NCH/SCD/SEX/AGE/ACC/ACT/BIR）
```

| 项 | 规则 |
|---|---|
| 音频 | 粤语 44.1kHz **双声道** → 降混；四川话 16k 单声道 |
| 说话人 | `O1/O2`；元数据给性别/年龄/口音 |
| 多通道 | `ast_mic` / `ast_phone` 视为不同采集条件，可分桶 |
| 可评 | CER / 标点 / DER / 命名 |

#### 客家话（当前 2 段）

- 只做冒烟，不进正式榜单
- 后续接入：台湾 HAT / FSR-2025 客语评测集（繁体汉字 / 客罗马字双轨），或自建梅州/惠州集
- **口径提醒**：台湾客语为繁体，与普通话/粤语 CER 不可直接横比

### 3.4 版本与快照

- 导入生成 `DatasetVersion`，`manifest_hash = sha256(排序后的 item_key|audio_sha|ref_sha)`
- 版本不可变；改标注 = 新版本
- 支持 `diff(v1, v2)`：新增 / 删除 / 文本变更 / 音频替换
- `stats` 预聚合（按 split / bucket 的条数与时长），列表页不扫明细
- 导入的完整状态机、质量报告、错误码、增量策略见 `asr-eval-implementation.md` §2

### 3.5 性能设计

| 环节 | 手段 | 目标 |
|---|---|---|
| 扫描 | 多进程按目录切分；先 stat 后 hash | 1 万条 < 20s |
| 入库 | `COPY` 5000 行/批，单事务 | 1 万条 < 10s |
| 转码 | **懒加载**：首次用到才生成 16k/mono variant，内容寻址 | 导入不被转码拖慢 |
| 列表 | `dataset_version.stats` 预聚合 + 复合索引 | 10 万条首屏 < 300ms |
| 筛选 | `buckets jsonb` + GIN | 任意标签组合 < 200ms |

---

## 4. 被测对象子系统

### 4.1 三类产品形态（实测事实）

| platform | kind | 部署 | 转写端点 | 并发 | 版本采集 |
|---|---|---|---|---|---|
| 205 | `etsstt` | 普通 | `ws://:8701/v1/audio/transcription/realtime` | 1 | SSH `get_version` + `/v1/models` + `/v1/system/info:8080` |
| 600 | `etsstt` | 普通 | 同上 | 1 | 同上 |
| 502 | `gateway` | k3s 容器 | `ws://:30025/...`（NodePort） | **3** | `kubectl exec deploy/mebox -- get_version` + `deploy/etsllm` + `/v1/system/info:30010` |
| — | `cloud` | 托管 | WSS / HTTP | 1 | API Key 校验 |

### 4.2 能力模型：声明 + 观测（`punctuation` 声明不可信）

```python
Capabilities = {
  "declared": {                    # init 响应里的 capabilities，仅记录
    "streaming": bool, "vad": bool, "itn": bool,
    "punctuation": bool,           # ← 实测声明 false 但输出 100% 带标点
    "hotwords": {...}, "languages": [...]
  },
  "observed": {                    # 首次接入用固定探测素材实跑一次
    "segments": bool,              # 输出是否带 start/end
    "speakers": bool,              # 是否输出说话人（任意形式）
    "speakers_in_sentence": bool,  # sentence 级是否带 spk 字段
    "global_spk_list": bool,       # 是否用 global_spk_list 位置对齐
    "partials": bool,              # 是否发 transcript.partial
    "punctuation_output": bool,    # ★ 输出是否含标点（真正决定标点指标）
    "naming": bool,                # 是否回显 matched_id
    "usage_field": bool,           # 是否返回 usage.inference_ms
    "partial_step_ms": int,        # ★ partial 间隔（实测 ~600ms，由服务端步长决定）
    "ttft_ms": int,                # ★ 业务真实 30ms 分片 + 1x 下的实测首字延迟
    "realtime_slots": int,         # ★ 实测可并发的实时会话数（网关=3）
    "max_stream_sec": int,         # ★ 实测单会话可用时长（已验证 1800s）
    "max_audio_ok_sec": int,
    "probed_at": str, "probe_version": str
  },
  "slots": int,                    # 1 或 3
}
```

**指标门控只认 `observed`**，`declared` 仅用于展示与告警。原因与实测数据见 `asr-eval-implementation.md` §4.1。

**但 `streaming` 是例外——已实测稳定可信**：同一设备连续 3 次 `init` 返回一致，且与是否真的产生 partial 完全吻合（2/2）。因此设备管理可以直接用 `declared.streaming` 做初步标记，再用一次 10s 探测确认。

**设备管理探测两档**

| 档位 | 动作 | 耗时 | 产出 |
|---|---|---|---|
| `probe_lite` | WS 连接 + `init` + 关闭（不发音频） | ~200ms | `declared` 全量能力 |
| `probe_full` | + 10s 探测音频，`streaming:true`，30ms 分片、1x | ~12s | `observed.partials / ttft / partial_step_ms` |

触发：设备首次接入、`deployment.uid` 变化、手动「重新探测」。

**能力驱动指标裁剪**：Run 启动时按 `observed ∩ dataset_caps ∩ profile` 解析，不支持的指标记 `N/A` 并附原因码（`device_no_punctuation_output` / `no_punct_reference` / …），完整规则见实现细则 §3.3。

### 4.3 Adapter SPI

```python
class Adapter(Protocol):
    def probe(self) -> DeploymentFingerprint
    def capabilities(self) -> Capabilities
    def transcribe(self, audio: Path, params: AsrParams) -> Hypothesis
    def stage(self, local: Path, name: str) -> str          # 返回设备侧路径；可选
    def extract_voiceprint(self, remote_path: str) -> list[float]   # 可选
```

四个实现：`EtssttAdapter`（205/600）、`GatewayAdapter`（502）、`CloudAdapter`、`MockAdapter`。

### 4.4 部署指纹

```jsonc
{
  "platform": "502", "kind": "gateway",
  "bigversion": "3.2.412",
  "etsllm_version": "1.4.49",
  "accel": {"rknn3_sdk": "1.1.0"},          // 昇腾记 cann/driver
  "model_key": "model-asr-b-w8a8_rk3588",   // 键名已含量化与 SoC
  "model_version": "1.0.3",
  "slots": 3
}
```

派生字段（解析 `model_key`）：
- `quant`：`w8a8` / `w4a16` / `fp16`
- `soc`：`rk3588` / `rk1828` / `ascend310p`

**不新增人工录入字段**；只在需要时补 `note`。

### 4.5 远端暂存（声纹前置条件）

`POST /v1/audio/voiceprints` 只接受**设备侧 `file_path`**。两种形态路径不同：

| 形态 | 策略 |
|---|---|
| 205/600 | SFTP 到宿主机可写目录（如 `/data/voiceprint/`），etsllm 直接可读 |
| 502 | **文件必须被 etsllm 容器内进程读到** → 三级降级 |

**Gateway 三级策略**

```
① 共享挂载（首选）
   kubectl exec -n ns-meagt deploy/etsllm -- sh -c "mount | grep -E '/data|/opt/etsme'"
   → 若宿主与容器共享目录，SFTP 推到宿主路径，容器内用映射路径
② kubectl cp
   kubectl cp local.wav ns-meagt/<pod>:/tmp/vp/{sha}.wav
③ stdin 直写（不依赖容器有 tar）
   kubectl exec -i deploy/etsllm -- sh -c "cat > /tmp/vp/{sha}.wav" < local.wav
```

```python
class Staging(Protocol):
    def prepare(self) -> StagingInfo        # 探测可用方式 + 容器内可见路径
    def put(self, local: Path, name: str) -> str
    def cleanup(self, name: str) -> None
```

- 文件名 = `sha256(enrollment_audio)[:16].wav`，幂等
- `prepare()` 结果缓存 30 分钟，设备重启/换版本后失效
- 三级全失败 → 该 target 标 `voiceprint_unsupported`，命名指标 `N/A`，不阻塞整轮
- **运维建议**：在网关上固定预留 `/data/voiceprint/`（hostPath），比每次 `kubectl cp` 稳定

### 4.6 版本采集

```
probe(deployment):
  etsstt : SSH.get_version / etsllm list / HTTP /v1/models / system/info
  gateway: kubectl exec mebox get_version, exec etsllm list & --version, system/info
  cloud  : GET /v1/models（仅校验 Key）
```

- 采集失败 → 拒绝开跑（不允许"版本未知的评测"）
- 任务进行中每 5 分钟做身份检查（不建会话）；发现 `sn`/`uid` 变化 → **中断该 target** 并提示重新评测
- ~~`prepare`（改 `SSFConfig.yaml` 并重启）~~ **已移除**：不修改目标设备文件；205/600 的 8701 回环访问改用平台侧 SSH 本地转发（M2 §5.3.2）。`restart` 仍是独立运维动作，需二次确认 + 审计日志

---

### 4.7 遥测口径（两套 RTF 都要，且必须分时长桶）

**流式/非流式是两种模式，不是两个模型**：由 `init.streaming` 切换，但真正能否流式取决于该设备部署的模型构建（`capabilities.streaming`）。实测 `asr-b`(RK3588) 为 true 且有 partial；`etsstt`(205) 为 false，传 `streaming:true` 也被忽略。

| 指标 | 定义 | 来源 |
|---|---|---|
| `rtf_e2e`（主） | `(首块音频 → final) / audio_duration` | 平台计时，跨设备可复现 |
| `rtf_dev`（辅） | `usage.inference_ms / usage.audio_duration_ms` | 设备自报，隔离算力 |
| `ttft_ms` | 首块音频 → 首个非空 partial（仅流式） | 平台计时 |
| `ttfa_ms` | 首块音频 → 首个 segment（仅非流式） | 平台计时 |
| `finalize_ms` | 最后一块音频 → `final` | 平台计时 |

> **`usage.rtf` 禁用**：实测其分母是"有效语音时长"而非音频总时长（9169 条样本中位数偏离 1.11 倍、P90 1.72、最大 11 倍），长音频/多静音样本会严重虚高。详见实现细则 §5.7。

**实测（2026-11-02）**

| 部署 | 音频 | `rtf` | 首输出 |
|---|---|---|---|
| 205 / `etsstt` | 4.2s | 0.244 | ttfa 4.2s |
| 205 / `etsstt` | 30s | 0.267 | ttfa **19.3s** |
| 205 / `etsstt` | 120s（4 人） | 0.373 | — |
| RK3588 / `asr-b` | 1260s | 0.211 | 首个 partial **2.1s** |

> ① RTF 随音频变长显著升高，**必须分时长桶**；② `usage.rtf` 长音频下严重偏离（0.789 vs 0.211），**禁止使用**；③ 非流式首个输出可能晚到音频的 2/3，必须单独用 `ttfa` 表达。

## 5. 指标子系统

### 5.1 评分流水线

```
Hypothesis(raw) ──assemble──► {text, segments[]}
                              │
                     normalize(profile)
                              ▼
        ┌──────────┬──────────┬──────────┬──────────────┐
        ▼          ▼          ▼          ▼              ▼
      CER        Punct       ITN        DER/SA-DER    Naming
   rapidfuzz   对齐+位置   wetext+实体  pyannote      matched_id
```

**Hypothesis 组装规则（实测强制，见实现细则 §4.3）**

```python
# ❌ 错误：final.text 只有句末标记 "。"
text = final["text"]

# ✅ 正确
sentences = [s for seg in segments for s in seg["sentences"]]   # 累加，不重复
text = "".join(s["text"] for s in sentences)
for i, s in enumerate(sentences):
    s["spk"] = s.get("spk", gsl[i] if gsl and i < len(gsl) else None)
# 注意 gsl 长度 = len(sentences) + 1（末尾是句末标记）
```

### 5.2 指标定义

| 指标 | 定义 | 实现 | 单位 |
|---|---|---|---|
| `cer` | `(S+D+I)/len(ref)`；中文字、英文词、混合按 token | `rapidfuzz` | item |
| `wer` | 英文/分词场景 | `rapidfuzz` | item |
| `punct_p/r/f1` | 字序列对齐后，比对"位置 + 标点类型" | 对齐后统计 | item |
| `itn_acc` | 实体级（数字/时间/金额/单位/百分比） | `WeTextProcessing` | item |
| `der` | `(FA+MISS+CONF)/TOTAL` | `pyannote.metrics` | item（session） |
| `sa_der` | 说话人属性化 DER，**禁用最优映射** | 固定身份映射后同 DER | session |
| `naming_acc` | `matched_id == gt` 的占比 | 句级 + 簇级（时长加权） | session |
| `speaker_fa/fr` | 错认 / 未识别 | `match_info` 统计 | session |
| `speaker_eer` | 对 `speaker_match_threshold` 扫描的等错误率 | `confidence` 做 ROC | dataset |
| `ttft_ms` | 首块音频 → 首个非空 partial（**仅流式**） | 平台计时 | case |
| `ttfa_ms` | 首块音频 → 首个 segment（**仅非流式**） | 平台计时 | case |
| `finalize_ms` | 最后一块音频 → `final` | 平台计时 | case |
| `partial_interval_ms` | 相邻 partial 间隔 | 平台计时 | case |
| `partial_lag_ms` | 已发送音频 − `partial.end_ms` | 平台计时 | case |
| `rtf_dev` | `inference_ms / audio_duration_ms`（设备计算耗时比） | 设备自报 `usage` | case |
| `rtf_e2e` | `(首块音频 → final) / audio_duration_ms`（**仅 burst 有效**） | 平台计时 | case |
| `overhead_ratio` | `(total_cost_ms − inference_ms) / audio_duration_ms` | 设备自报 | case |
| `rtf_realtime` | `(final − 最后一块音频) / audio_duration_ms`（1x 跟播能力） | 平台计时 | case |
| `npu_aicore` / `rss` | 采样均值/峰值 | `npu-smi` + `docker stats` | case |

> 流式与非流式的时延指标**不同名、不同列、禁止同列横评**；定义与硬规则见实现细则 §5.7、§5.9。
> **`rtf_e2e` 仅在 burst 下有效**：1x 发送时恒 ≥ 1，无区分度（实测设备 rtf_dev 0.120 vs 0.267，rtf_e2e 都是 1.03~1.15）。
> **`usage.rtf` 禁用**：其分母是"有效语音时长"，不是音频总时长（长音频偏离可达 3.7 倍）。

### 5.3 口径（MetricProfile）

```jsonc
{
  "text": {
    "strip_punct": true, "strip_space": true, "lowercase": true,
    "traditional": "keep",                 // keep | to_simplified
    "drop_fillers": [],                    // 默认不去，显式开启
    "drop_tags": ["[+]","[*]","[ENS]","[LAUGHTER]","[SONANT]","[MUSIC]","[SYSTEM]","[FILLER]"]
  },
  "cer":  {"engine": "rapidfuzz", "aggregate": "micro", "mixed": "char",
           "join": "concat", "separator": null},     // AliMeeting: separator:"<sc>"
  "punct": {"enabled": true, "match": "position_and_type"},
  "itn":  {"enabled": false, "normalizer": "wetext"},
  "der":  {"collar_ms": 250, "collar_semantics": "half_width",
           "skip_overlap": false, "uem": "full"},
  "naming": {"enabled": true, "granularity": ["sentence","cluster"],
             "mode": "matching",        // clustering | matching（实测两者簇数不同，不可混比）
             "threshold": 0.6,          // speaker_match_threshold，必须进口径
             "unknown_policy": "fr"},
  "perf": {"mode": "streaming|batch",      // 决定测 ttft 还是 ttfa
           "stream_input_rate": 1.0,       // 1.0=实时；>1=burst
           "chunk_ms": 20,                 // ★ 实测：>200ms 会显著低估 ttft
           "repeat": 3, "aggregate": "median",
           "sampling": {"mode": "stratified_by_duration", "per_bucket": 30},
           "concurrency_sweep": [1, 2, 3]}
}
```

`profile.hash` 进入 `Benchmark.version_hash`。**口径不同 → 禁止直接比较**。

### 5.4 聚合与显著性

- **micro**（按参考字数/时长加权，主口径）与 **macro**（按 item 平均）都报
- **分桶**：`dialect / scene / duration_bin / gender / age / text_len / overlap_ratio`
- **置信区间**：以 item 为重采样单元，bootstrap 1000 次 → 95% CI
- **A/B 显著性**：配对 bootstrap（同一批 content_uid 上两系统的差值）→ p 值
- **覆盖率**：`planned / scored / n_a / failed` 单独展示，绝不混入分母

### 5.5 开源实现选型与验收

| 用途 | 选型 | 许可 | 备注 |
|---|---|---|---|
| 编辑距离/对齐 | `rapidfuzz` | MIT | 与 `python-Levenshtein` 同源，结果一致 |
| 文本归一化 | `WeTextProcessing` | Apache-2.0 | TN/ITN，中英 |
| 繁简 | `opencc-python` | Apache-2.0 | 按语种开关 |
| 最优匹配 | `scipy.optimize.linear_sum_assignment` | BSD | 标准匈牙利，DER 与 SA-DER 共用 |
| DER | `pyannote.metrics` | MIT | 生产默认引擎 |
| DER 验收 | `md-eval.pl`（NIST） | 公有 | CI 门禁 + 正式报告可选引擎 |

**DER 双引擎分工（决策见实现细则 §1）**

| 角色 | 引擎 | 说明 |
|---|---|---|
| 每个 Run 的打分 | `pyannote.metrics` | 进程内调用，出结构化 MISS/FA/CONF |
| CI 一致性门禁 | `md-eval.pl` | AliMeeting 20 段全量对照，|ΔDER| < 0.2pp 才允许发布 |
| 对外正式报告 | `md-eval.pl`（`profile.der.scorer=md_eval`） | 一键切换，报告标注 `scorer: md-eval.pl (NIST)` |

**参数映射（必须锁定）**：md-eval `-c 0.25`（单侧 ±0.25s，总 0.5s）↔ pyannote `collar=0.5`（总宽）；`skip_overlap=False`；UEM 统一 `[0, duration]`。

**一次性口径验收（1~2 天，必做）**

1. 取 AliMeeting 1 个 session，构造 4 份 RTTM：完美 / 漏检 / 虚警 / 混淆
2. `md-eval.pl -c 0.25` 得基准
3. `pyannote.metrics` 调参至差值 < 0.2pp（重点验证 `collar` 是总宽还是半宽）
4. 参数写死进 `metric_profile.der`，报告标注 `scorer: pyannote@X (md-eval-aligned)`

> 不做这步，DER 只能内部看，不能对外。

---

## 6. 执行子系统

### 6.1 Run 生命周期

```
created ─► probing ─► preparing ─► running ─► completed
                │          │           │
                ▼          ▼           ▼
             failed    cancelled   partial / aborted_condition_changed

Evaluation（独立）：queued ─► scoring ─► completed / failed / cancelled
```

- `probing`：申请设备租约 → 探测 → 冻结 `Deployment`
- `preparing`：命名指标需要时准备声纹向量
- `running`：推理
- `partial`：已结束，`ok+cached` 占比 ≥ 90% 且 < 100%
- 执行终态后**自动创建 Evaluation**（`completed`/`partial` 才自动；`aborted` 不自动）

**`run.kind` 决定节奏**

| kind | 发送 | 分片 | 设备占用 | 会话数 | 指标 |
|---|---|---|---|---|---|
| `accuracy` | burst | 30s | **整机独占** | **1**（默认） | CER/标点/ITN/DER/命名 |
| `perf` | **1x 实时** | **30ms**（业务真实） | **整机独占** | 1，可显式配到 `slots` | ttft/partial_interval/lag/finalize/rtf |

**设备是整机独占的**：一个 Run 拿到设备后，同设备的其他 Run 一律排队。Run 内部**默认只用 1 路会话**；只有 `perf` Run 可以显式配置 `concurrency>1` 来测多路性能。

> `accuracy` 也可以用流式模式（流式同样产出 `segment` + `final`，实测 30min 通过），但必须记录 `mode`，同一 Run 内不得混用。

**会话生命周期（实测强制）**

| 规则 | 原因 |
|---|---|
| 禁止强杀转写进程，超时必须发 WS `close` 并等待对端关闭 | 强杀（TCP reset）会让引擎卡在"只收 AudioEnd、不产 segment"，后续全部 `engine_overloaded` |
| 看门狗：`audio.end` 后超过 `max(30s, 2×rtf×duration)` 无 `final` → 主动 close 并记 `finalize_timeout` | 防止会话悬挂 |
| `engine_overloaded` 且无活跃会话 → 标记设备「需重启」，平台不自动重启 | 引擎状态需要人工恢复 |
| 单设备实时会话上限 = 实测 `realtime_slots`（网关 3） | 超过即 overload |

### 6.2 调度

```
ETA = Σ_items(duration × rtf_dev_estimate) / concurrency + 固定开销
# concurrency 默认 1；仅 kind=perf 且显式指定时才 >1
```

- **不物化任务行**：`shard = hash(item_id) % N`，worker 用 `(run_target_id, shard, cursor)` 拉取
- **Slot 租约**：PG `device_lease` 单行原子占锁，`expires_at` 120s，30s 心跳续期
  - 205/600：1 个 slot
  - 502：3 个 slot，每个 slot 一条独立 WS 连接 + 独立 `init`
- **背压**：每连接 `query.status` 查 `lag_duration`，超阈值降速；3 路并发时监控**聚合 lag**
- **超时**：`duration × 3 + 60s`（可配）
- **断点续跑**：只补 `failed / missing / cancelled`

### 6.3 并发档位（默认单路，仅性能测试可显式开启）

**默认 `concurrency = 1`**：即使网关支持 3 路，普通准确率 Run 也只用 1 路。这是业务口径——评测要复现"单次录音转写"的真实条件，也避免多路干扰时延测量。

`concurrency ∈ {1..slots}` 仅对 `kind=perf` 开放，且必须显式配置。用于产出「并发-吞吐-成功率」曲线：

```
并发 1 → rtf_dev 0.09, 成功率 100%, 吞吐 1.0x
并发 3 → rtf_dev 0.22, 成功率 98%,  吞吐 1.9x
```

> 单路 RTF 说明不了算力是否够用；这条曲线才是选型依据。

### 6.4 Hypothesis 缓存

```
cache_key = sha256(deployment_uid|content_uid|params_hash|mode|pace|chunk_ms|concurrency)[:32]
```

- 命中 → 不调用设备，直接复用
- 默认**同 Run 内必复用**；跨 Run 复用需 `deterministic=true`（模型声明可复现）
- 缓存命中率写入 Run 统计，便于判断"这次到底跑了多少新推理"

### 6.5 失败处理

| 情况 | 处理 |
|---|---|
| 单条超时/异常 | 重试 2 次（指数退避），仍失败记 `failed`，继续后续 |
| 连续失败 > 5 | 熔断该 deployment，其余 target 继续 |
| `no slot available` / `engine_overloaded` | 立即熔断，标记"设备需检查" |
| 设备/模型中途变化 | **中断该 target**（`aborted_condition_changed`），已完成结果保留但不参与对比 |
| worker 崩溃 | 租约到期自动重新入队 |

---

## 7. 存储设计

### 7.1 PostgreSQL 表（核心）

```sql
-- ===== 资产 =====
create table audio_asset (
  id uuid primary key default gen_random_uuid(),
  sha256 char(64) not null unique,
  uri text not null, bytes bigint, duration_ms int,
  sample_rate int, channels smallint, codec text,
  created_at timestamptz not null default now()
);
create table audio_variant (
  asset_id uuid references audio_asset(id) on delete cascade,
  profile text not null,             -- "16k_mono_s16"
  sha256 char(64) not null, uri text not null,
  primary key (asset_id, profile)
);

-- ===== 数据集 =====
create table dataset (
  id uuid primary key default gen_random_uuid(),
  name text not null unique, source_type text, description text,
  tags jsonb not null default '{}', created_at timestamptz default now()
);
create table dataset_version (
  id uuid primary key default gen_random_uuid(),
  dataset_id uuid not null references dataset(id) on delete cascade,
  version text not null,
  status text not null default 'importing',   -- importing|ready|failed
  manifest_key text, manifest_hash char(64),
  item_count int not null default 0,
  total_duration_ms bigint not null default 0,
  stats jsonb not null default '{}',
  created_at timestamptz default now(),
  unique (dataset_id, version)
);
create table item (
  id bigserial primary key,
  dataset_version_id uuid not null references dataset_version(id) on delete cascade,
  item_key text not null,
  content_uid char(24) not null,
  audio_asset_id uuid not null references audio_asset(id),
  split text not null default 'all',
  duration_ms int not null,
  lang text,
  speakers text[] not null default '{}',
  buckets jsonb not null default '{}',
  unique (dataset_version_id, item_key)
);
create index item_split on item (dataset_version_id, split);
create index item_buckets on item using gin (buckets jsonb_path_ops);
create index item_uid on item (content_uid);

create table ref_text (
  item_id bigint references item(id) on delete cascade,
  kind text not null,                -- verbatim | itn_expected
  lang text, text text not null, sha256 char(16) not null,
  primary key (item_id, kind)
);
create table ref_segment (
  id bigserial primary key,
  item_id bigint not null references item(id) on delete cascade,
  start_ms int not null, end_ms int not null,
  speaker text not null, text text not null,
  skip boolean not null default false,
  overlap boolean not null default false
);
create index ref_segment_item on ref_segment (item_id, start_ms);

create table speaker_profile (
  dataset_version_id uuid references dataset_version(id) on delete cascade,
  speaker_id text not null,
  gender text, age text, accent text, meta jsonb,
  primary key (dataset_version_id, speaker_id)
);

-- ===== 考生 =====
create table device (
  id uuid primary key default gen_random_uuid(),
  sn text unique, name text not null,
  platform text not null, kind text not null,   -- etsstt|gateway|cloud
  host text not null, port int not null,
  ssh_user text, ssh_port int default 22,
  secret_ref text, slots smallint not null default 1,
  tags jsonb not null default '{}', status text default 'unknown',
  created_at timestamptz default now()
);
create table deployment (
  id uuid primary key default gen_random_uuid(),
  device_id uuid not null references device(id) on delete cascade,
  uid char(16) not null,
  platform text, bigversion text, etsllm_version text,
  accel jsonb, model_key text, model_version text,
  quant text, soc text,
  capabilities jsonb not null,
  raw jsonb, captured_at timestamptz default now(),
  unique (device_id, uid)
);

-- ===== 评测定义 =====
create table metric_profile (
  id uuid primary key default gen_random_uuid(),
  name text not null, version int not null default 1,
  config jsonb not null, hash char(16) not null unique,
  created_at timestamptz default now()
);
create table benchmark (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  dataset_version_id uuid not null references dataset_version(id),
  metric_profile_id uuid not null references metric_profile(id),
  sampling jsonb not null default '{"mode":"all"}',
  buckets jsonb not null default '[]',
  params jsonb not null default '{}',
  stream_mode text not null default 'file',    -- file|realtime
  concurrency smallint not null default 1,
  version_hash char(16) not null,
  created_at timestamptz default now()
);

-- ===== 执行 =====
create table campaign (
  id uuid primary key default gen_random_uuid(),
  name text not null, note text, created_at timestamptz default now()
);
create table run (
  id uuid primary key default gen_random_uuid(),
  campaign_id uuid references campaign(id) on delete set null,
  benchmark_id uuid not null references benchmark(id),
  kind text not null default 'accuracy',     -- accuracy | perf
  name text not null, status text not null default 'created',
  snapshot jsonb not null,        -- 冻结 benchmark + deployments + hashes
  created_by text, eta_ms bigint, progress jsonb not null default '{}',
  started_at timestamptz, finished_at timestamptz, created_at timestamptz default now()
);
create table run_target (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references run(id) on delete cascade,
  deployment_id uuid not null references deployment(id),
  status text not null default 'pending',
  slots smallint not null default 1,
  stats jsonb not null default '{}',
  unique (run_id, deployment_id)
);
create table hypothesis (
  id uuid primary key default gen_random_uuid(),
  cache_key char(32) not null unique,
  deployment_id uuid not null references deployment(id),
  item_id bigint not null references item(id),
  params_hash char(16) not null, stream_mode text, concurrency smallint,
  text text, segments jsonb, usage jsonb,
  raw_uri text, created_at timestamptz default now()
);
create table case_result (
  id bigserial primary key,
  run_target_id uuid not null references run_target(id) on delete cascade,
  item_id bigint not null references item(id),
  hypothesis_id uuid references hypothesis(id),
  status text not null,             -- ok|failed|timed_out|skipped|n_a
  wall_ms int, dev_ms int, ttft_ms int, finalize_ms int,
  rtf_dev real, rtf_e2e real,
  resource jsonb, error text,
  unique (run_target_id, item_id)
);
create index case_status on case_result (run_target_id, status);

-- 评分：一个 Run 可有多份
create table evaluation (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references run(id) on delete cascade,
  metric_profile_id uuid not null references metric_profile(id),
  profile_hash char(16) not null,
  scorer_version char(64) not null,
  trigger text not null default 'auto',      -- auto|manual|rescore
  status text not null default 'queued',
  coverage jsonb not null default '{}',
  created_by text,
  created_at timestamptz not null default now(),
  finished_at timestamptz
);
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
  run_target_id uuid references run_target(id) on delete cascade,
  metric text not null, scope text not null,     -- micro|macro
  bucket jsonb not null default '{}',
  value double precision, n int not null,
  ci_low double precision, ci_high double precision
);
create index metric_lookup on metric_value (evaluation_id, run_target_id, metric);

-- ===== 后台任务（导入/导出/声纹提取等）=====
create table task (
  id uuid primary key default gen_random_uuid(),
  kind text not null,               -- dataset_import | voiceprint_extract | report_export
  ref_id uuid,                      -- 关联实体 id
  status text not null default 'queued',
  progress jsonb not null default '{}',
  result jsonb, error text,
  created_by text, created_at timestamptz default now(),
  started_at timestamptz, finished_at timestamptz
);
create index task_status on task (kind, status, created_at);

-- ===== 声纹 =====
create table speaker (
  id uuid primary key default gen_random_uuid(),
  name text not null unique, note text, created_at timestamptz default now()
);
create table enrollment (
  id uuid primary key default gen_random_uuid(),
  speaker_id uuid not null references speaker(id) on delete cascade,
  audio_asset_id uuid not null references audio_asset(id),
  start_ms int, end_ms int, created_at timestamptz default now()
);
create table voiceprint (
  id uuid primary key default gen_random_uuid(),
  enrollment_id uuid not null references enrollment(id) on delete cascade,
  deployment_id uuid not null references deployment(id) on delete cascade,
  dim int not null, vec bytea not null,
  created_at timestamptz default now(),
  unique (enrollment_id, deployment_id)
);
```

**关键取舍**
- `ref_segment` 行式存储（不是 JSONB）：可查询、可聚合、可做 DER
- `case_score.metrics` 用 JSONB 一行：避免"指标 × 样本"炸成 50 万行
- `case_result` 不存指标，评分结果按 `evaluation_id` 隔离（一号多评分）
- `metric_value` 只存聚合：报表查询走它，不扫明细
- `hypothesis.raw_uri` 指向本地对象目录：长音频响应可达数百 KB

### 7.2 对象目录布局

```
data/objects/
├── variant/16k_mono/{sha}.wav
├── raw/{run_id}/{case_result_id}.json.gz
├── reports/{report_id}/report.html
├── reports/{report_id}/report.xlsx
└── exports/{job_id}/matrix.xlsx
```

原始音频**不拷贝**，`audio_asset.uri` 直接指向 NFS 路径。
对象存储走 `ObjectStore` SPI：默认 `LocalObjectStore`，将来可换 S3/MinIO，不改业务代码。

### 7.3 性能预算

| 场景 | 目标 |
|---|---|
| 10 万 item 列表 | < 300ms |
| 1 万条导入 | < 30s（不含转码） |
| 创建 Run | < 2s |
| 结果写入 | > 5000 行/s |
| 1 万条 CER 重算 | < 60s（8 进程） |
| 200 个 session DER | < 5min |
| 矩阵页（3 目标 × 8 指标 × 20 桶） | < 500ms |

---

## 8. API 设计

### 8.1 资源与端点

```
POST   /api/v1/auth/login                 LDAP bind → JWT
GET    /api/v1/auth/me

POST   /api/v1/datasets
GET    /api/v1/datasets
POST   /api/v1/datasets/{id}/versions     （异步导入任务）
GET    /api/v1/dataset-versions/{id}
GET    /api/v1/dataset-versions/{id}/items?split=&bucket=&q=&cursor=
GET    /api/v1/dataset-versions/{id}/diff?against=
POST   /api/v1/dataset-versions/{id}/activate

GET    /api/v1/devices
POST   /api/v1/devices
POST   /api/v1/devices/{id}/probe
GET    /api/v1/devices/{id}/deployments

GET    /api/v1/metric-profiles
POST   /api/v1/metric-profiles
POST   /api/v1/benchmarks
GET    /api/v1/benchmarks

POST   /api/v1/runs
GET    /api/v1/runs?status=&benchmark=&device=
GET    /api/v1/runs/{id}
POST   /api/v1/runs/{id}/cancel | /resume
GET    /api/v1/runs/{id}/evaluations
POST   /api/v1/runs/{id}/evaluations        # 手动评分 / 换口径重评
GET    /api/v1/evaluations/{id}/matrix
GET    /api/v1/runs/{id}/buckets
GET    /api/v1/runs/{id}/cases?status=&bucket=&cursor=
GET    /api/v1/runs/{id}/cases/{case_id}
GET    /api/v1/runs/{id}/compare?with=run_id,run_id
GET    /api/v1/runs/{id}/events            SSE：进度/ETA/阶段
POST   /api/v1/runs/{id}/report           生成 HTML/XLSX

GET    /api/v1/speakers
POST   /api/v1/speakers
POST   /api/v1/speakers/{id}/enrollments
POST   /api/v1/speakers/{id}/extract?deployment_id=
```

### 8.2 约定

- 游标分页（`cursor`），不用 offset，避免深翻页退化
- 所有列表默认返回轻量视图，明细单独取
- 错误统一 `{code, message, detail, trace_id}`；`code` 机读、`message` 人读
- SSE 用一次性 stream ticket 鉴权（EventSource 不能设 header）
- 异步操作统一返回 `202 + Location: /jobs/{id}`
- 创建类支持 `Idempotency-Key`，配置类支持 `If-Match` ETag
- OpenAPI 3.1 自动生成 → 前端 `openapi-typescript` 出类型，CI 做破坏性变更加门禁

### 8.3 权限

权限字符串（`run:create`）而非角色硬编码，角色是权限包：

| 角色 | 能力 |
|---|---|
| `viewer` | 只读一切（默认角色） |
| `tester` | 建 Run、取消本人 Run、手动评分、生成报告；不改数据/设备/口径 |
| `engineer` | + 建数据集/设备/口径/基准、维护说话人库、取消任意 Run |
| `admin` | + 删除、设备凭据与运维动作、审计、service token、用户授权 |

支持**按人 grant**（只加不减，可带到期），用于"测试同学也要维护某个测试集"这类例外。

LDAP group → 角色映射配置在环境变量，默认 `viewer`，多组命中取最高。
CI 用独立 **service token**（scopes 受限，不能碰设备运维）。

> 完整契约见 **`m7-api-auth-design.md`**（端点全集、认证流程、限流、审计、错误码）。

### 8.4 多端一致性

多人同时使用时的一致性靠五件事，不做分布式锁/CRDT：

| 机制 | 作用 |
|---|---|
| 服务端状态机 | Run/Evaluation/设备动作的最终裁决；非法操作 `409` |
| 用户级事件流 `GET /events` | 跨页面广播 `resource.changed`，列表与设备状态不滞后 |
| 资源级 SSE | 当前页面的进度/ETA 实时更新 |
| `ETag + If-Match` | 配置类并发修改：`428`/`412` + 当前对象 + 字段差异，前端弹冲突框 |
| `Idempotency-Key` | 创建类重试不产生重复 Run/报告 |

补充：设备租约返回 `holder`（谁、哪个 Run、何时释放）；refresh 轮换有 30s 幂等宽限，避免多标签页互相踢下线。细节见 M7 §7.6/§4.6 与 M8 §6。

---

## 9. 前端

独立文档：`asr-eval-uiux.md`。要点：

- IA：总览 / 评测 / 数据集 / 设备 / 基准 / 声纹 / 设置
- **Run 详情 = 一屏决策**：KPI 结论条 → Target×Metric 矩阵 → 分桶 → Badcase
- **全局对比托盘**：任意 Run「加入对比」，最多 4 个，随时开比
- **Badcase 键盘流**：J/K 切换、空格播放、1-9 打标，目标 10 条/分钟
- **口径徽标**：每个数字可回答"什么口径、多少样本、是否显著"
- 技术：React 18 + TS + Vite + Tailwind + shadcn/ui + TanStack
- 图表：矩阵用 **CSS Grid**、标准柱/线用 **Recharts**、领域专用图（版本轨道/slope/说话人泳道/波形）用**自定义 SVG**；**不引入 ECharts**（页面经 `ChartSpec` + renderer 抽象，预留迁移）

---

## 10. 部署与运维

> 完整方案见 **`m9-platform-design.md`**（拓扑、配置与密钥、迁移、备份恢复、可观测性、运维手册）。

### 10.1 环境

```
OS      Ubuntu 22.04 LTS
CPU     16 core
RAM     32 GB
Disk    1 TB SSD
网络     局域网；需可达设备 8701 / 30025，只读挂载 NFS
```

### 10.2 服务

```yaml
services:
  gateway:      nginx（80/443，SSE 不缓冲）
  api:          uvicorn ×2
  scheduler:    app 内后台协程（PG advisory lock 防双开）
  worker-infer: 4
  worker-score: 2
  worker-import: 1
  migrate:      一次性 alembic
  postgres:     16（按 digest 固定）
```

### 10.3 运维要点

- **配置分层**：镜像默认 < 环境变量 < `/run/secrets/*`；启动时严格校验
- **密钥**：`jwt_key`、`credentials_key`、LDAP bind、PG 密码；`data/secrets/` 0600
- **迁移**：expand / contract，单 head；`migrate` 成功后才起 API
- **备份**：每日 `pg_dump` + `tar data/objects` + **`credentials.key`**；RPO 24h / RTO 2h
- **日志**：JSON 到 stdout，Docker 轮转 50MB×5；审计进 PG
- **健康**：`/healthz`、`/readyz`（PG/本地存储/NFS/磁盘）、`/system/status`
- **告警**：磁盘、备份失败、队列积压、设备离线、证书到期；一期 webhook 可选
- **清理**：`raw/` 90 天、导出 7 天、临时 1 天；音频不落盘

---

## 11. 分期与验收

| 阶段 | 周 | 交付 | 验收标准 |
|---|---|---|---|
| **P0 打通** | 1 | PG schema、Adapter SPI、`EtssttAdapter`、Remote Staging 探测、AISHELL-1 导入、CER | 1 台设备跑通 1,000 条，CER 与人工抽查一致 |
| **P1 可用** | 3 | 6 类 Importer、Benchmark、Run、矩阵、SSE 进度 | 3 台设备 × 3 数据集横评出榜 |
| **P2 完整** | 2 | 说话人模式（分离+声纹+DER/SA-DER/命名/EER）、口径验收 | AliMeeting 20 段出 DER/SA-DER/命名三套数 |
| **P3 决策** | 2 | 性能双 RTF + 并发档位、回归显著性、Badcase、LDAP | 同设备两版本给出 p 值与回退样本 |
| **P4 打磨** | 持续 | 前端精细化、导出、CI 门禁、客家话扩充 | — |

### 关键验收指标

- 从"打开平台"到"看到某设备 CER 变化" ≤ 3 次点击
- Run 详情首屏 ≤ 1.5s
- 任取一个数字，能回答：口径版本 / 样本量 / 置信区间 / 是否显著
- DER 与 `md-eval.pl` 差值 < 0.2pp

---

## 12. 关键假设与开工前必验

| # | 假设 | 状态 | 结论 |
|---|---|---|---|
| 1 | 分离后能拿到说话人与句子的对应 | ✅ **已验（有偏差）** | `sentences[].spk` **不存在**；用 `global_spk_list[i]` 对齐，且长度 = 句数 + 1 |
| 2 | `match_info.matched_id` 回显传入 id | ✅ **已验** | 回显，带 confidence（实测 0.55~0.70） |
| 3 | 网关容器可写可见路径 | ⏳ 未测 | 需在 502 上执行 `kubectl exec deploy/etsllm -- mount` |
| 4 | 30min 音频单次可转写 | ⏳ 未测 | 120s 已通（RTF 0.37），30min 待验 |
| 5 | `usage.inference_ms` 可信 | ✅ **已验** | 可信；`total_cost_ms ≈ wall`，比 `inference_ms` 高约 25% |
| 6 | 3 路并发稳定 | ⏳ 未测 | 需在 502 上扫 1/2/3 档 |
| 7 | `capabilities` 声明可信 | ❌ **已证伪** | 标点声明与实际不一致（400/400 输出含标点）；改为 `observed` 门控 |
| 8 | `final.text` 即转写结果 | ❌ **已证伪** | 实测恒为 `"。"`；必须累加 `transcript.segment` |
| 9 | `etsstt` 内含"流式/非流式两个模型" | 🔶 **部分属实** | 是同一服务两种模式（`init.streaming`），但是否可流式由**模型构建**决定：`asr-b`=true 有 partial，`etsstt`(205)=false 传了也无效 |
| 10 | partial 粒度 | ✅ **已验** | 服务端步长 ~600ms；`chunk≤100ms` 时 TTFT 稳定 0.96~0.98s |
| 11 | `init.capabilities.streaming` 可用于设备管理探测 | ✅ **已验** | 连续 3 次稳定；`etsstt`(205)=false/无 partial，`qwen3-asr-1.7b`(502)=true/有 partial，2/2 一致 |
| 12 | 1s 分片测 TTFT | ❌ **已证伪** | 1s 分片 0.076s vs 30ms 分片 0.976s，差 13 倍；必须用 ≤100ms，业务口径 30ms |

---

## 13. 明确不做（一期）

- 人工标注/校对工作流（外部工具产出标准稿即可）
- 多租户与项目隔离（单部署，角色足够）
- badcase 自动聚类（先做筛选 + 人工打标）
- 模型自动部署到设备（`prepare` 仅作运维动作）
- 流式实时延迟的分位数统计（先做均值与 P50）