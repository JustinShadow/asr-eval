# M1 数据接入 · 详细设计卡 v0.1

> 上游：M2 已冻结（设备侧只引用音频路径，不托管原始文件）
> 下游：M3（用 `dataset_version.capabilities` 判定可评指标）、M4（读 `item` / `audio_asset`）、M5（读 `ref_text` / `ref_segment`）
> 本模块是"考卷的生产线"：把散落的开源数据变成**不可变、可复现、可分析**的 DatasetVersion。

---

## 1. 职责与边界

### 1.1 负责

| 职责 | 说明 |
|---|---|
| 数据集注册 | 名称、来源类型、标签、说明 |
| 版本导入 | 扫描 → 解析 → 落库 → 能力分析 → 质量报告 |
| 统一模型 | 音频资产、参考文本、参考分段、说话人元数据 |
| 音频资产管理 | 内容寻址（sha256）、派生变体（16k mono / ch0） |
| 能力统计 | 标点/分段/说话人/ITN 覆盖率、重叠率、时长分布 |
| Manifest | 可复现的清单 + 哈希 + 版本 diff |
| 检索 | 按 split / bucket / 关键词查条目 |

### 1.2 不负责

| 不负责 | 归属 |
|---|---|
| 设备、指纹、探测 | M2 |
| 口径、Benchmark、采样 | M3 |
| 任务调度、转写 | M4 |
| 打分 | M5 |
| 报告 | M6 |
| 人工标注/校对 | 一期不做（外部产出标准稿） |

### 1.3 关键约束

1. **版本不可变**：任何修正都生成新版本。
2. **音频不拷贝**（Q1）：只存路径 + sha256；派生文件可缓存。
3. `content_uid` 含音频与参考哈希：改题即换身份。
4. 导入必须能**断点续跑**，且重复导入幂等。
5. 路径必须在**白名单根目录**内。

---

## 2. 领域对象

```python
Dataset:
  id, name, source_type, description, tags, created_at

DatasetVersion:
  id, dataset_id, version, status
  manifest_key, manifest_hash
  item_count, total_duration_ms
  capabilities: dict     # 所有 subset 的汇总
  stats
  created_at

DatasetSubset:              # ★ 采集条件（phone/mic、far/near）
  id, dataset_version_id, name
  audio_profile          # as_is | mono_mix | mono_ch0
  is_primary             # 默认口径
  item_count, total_duration_ms
  capabilities: dict      # 本 subset 的能力
  stats

Item:
  id, dataset_version_id, subset_id
  item_key               # 版本内唯一（含通道，如 ses001/ast_phone）
  pair_key               # ★ 跨条件配对键（同内容、不同音频，如 ses001/ast）
  content_uid            # 24 hex，含音频哈希
  audio_asset_id
  split, duration_ms, lang
  speakers: list[str]
  buckets: dict

RefText:
  item_id, kind(verbatim|itn_expected), lang, text, sha256

RefSegment:
  id, item_id, start_ms, end_ms, speaker, text, skip, overlap

SpeakerMeta:
  dataset_version_id, speaker_id, gender, age, accent, meta json

AudioAsset:
  id, sha256, uri, bytes, duration_ms, sample_rate, channels, codec

AudioVariant:
  asset_id, profile, sha256, uri
```

---

## 3. 接口

### 3.1 Importer SPI

```python
class Importer(Protocol):
    name: str
    version: str
    def detect(self, root: Path) -> bool: ...
    def scan(self, root: Path, params: dict) -> Iterator[RawItem]: ...

@dataclass
class RawItem:
    item_key: str
    audio: Path
    ref_text: str | None
    itn_text: str | None
    segments: list[RawSegment]
    speakers: dict[str, SpeakerMeta]
    buckets: dict
    lang: str
    split: str

@dataclass
class RawSegment:
    start_ms: int
    end_ms: int
    speaker: str
    text: str
    skip: bool = False
    overlap: bool = False
```

注册：`importers/__init__.py` 自动发现；新增数据集 = 一个文件 + `register()`。

### 3.1.1 版本号规则

```
默认：系统自动递增 —— v1、v2、v3 …
可手动指定：v1.0-clean、v1.1-fix-itn
可附加 note：本次导入了什么、修了什么
唯一性：(dataset_id, version) 唯一；重复则拒绝
```

自动递增只依赖数据集内已有版本数量 + 1，不做语义解析（`v1.10` 与 `v1.9` 只按字符串存，排序用自然排序）。

### 3.2 DatasetService

```python
class DatasetService:
    def create(self, req) -> Dataset
    def list(self, filters) -> list[Dataset]
    def get(self, dataset_id) -> Dataset
    def delete(self, dataset_id) -> None                            # 任一版本有基准引用/活动任务则拒绝

    def preview(self, dataset_id, root, importer, params, limit=50) -> list[RawItem]
    def import_version(self, dataset_id, req) -> Task              # 异步
    def versions(self, dataset_id) -> list[DatasetVersion]
    def get_version(self, version_id) -> DatasetVersion
    def subsets(self, version_id) -> list[DatasetSubset]
    def diff(self, a: version_id, b: version_id) -> DiffResult
    def delete_version(self, version_id) -> None                   # 被引用则拒绝

    def items(self, version_id, filters, cursor, limit) -> Page[Item]
    def item(self, item_id) -> ItemDetail
    def audio_path(self, asset_id, profile=None) -> Path           # 触发变体生成
```

### 3.3 HTTP

```
POST   /api/v1/datasets
GET    /api/v1/datasets
GET    /api/v1/datasets/{id}
DELETE /api/v1/datasets/{id}                  # 整组无基准引用/活动任务时删除全部版本

POST   /api/v1/datasets/{id}/preview
POST   /api/v1/datasets/{id}/versions        # 异步导入
GET    /api/v1/datasets/{id}/versions
GET    /api/v1/dataset-versions/{id}
GET    /api/v1/dataset-versions/{id}/subsets
GET    /api/v1/dataset-versions/{id}/capabilities
DELETE /api/v1/dataset-versions/{id}
GET    /api/v1/dataset-versions/{id}/diff?against=
GET    /api/v1/dataset-versions/{id}/items?split=&bucket=&q=&cursor=&limit=
GET    /api/v1/items/{id}
GET    /api/v1/items/{id}/audio?profile=16k_mono
POST   /api/v1/dataset-versions/{id}/reanalyze

GET    /api/v1/meta/enums          # 语言/方言/性别/指标/原因码的中文显示名
```

`GET /api/v1/meta/enums` 返回代码到中文显示名的映射，需 `meta:read` 权限：

```json
{
  "languages": {"zh": "普通话"},
  "dialects": {"Mandarin": "普通话"},
  "genders": {"M": "男", "F": "女", "unknown": "未知"},
  "age_groups": {"A": "14 岁以下"},
  "splits": {"train": "训练集", "dev": "开发集", "test": "测试集", "all": "全部"},
  "metrics": {"cer": "字错误率（CER）"},
  "reasons": {"no_sentence_punctuation": "参考缺少句末标点"}
}
```

各类别包含 §7.2 与 M5 指标、原因码附录定义的完整映射。前端未知代码回退显示原始 code；枚举查询按 M8 §5.3 缓存 1 小时。

`GET /api/v1/dataset-versions/{id}/capabilities` 返回版本加权汇总和按名称排序的 subset 能力：

```json
{
  "dataset_version_id": "uuid",
  "capabilities": {"text_ratio": 1.0},
  "subsets": [
    {"name": "far", "is_primary": false, "capabilities": {"text_ratio": 1.0}},
    {"name": "near", "is_primary": true, "capabilities": {"text_ratio": 1.0}}
  ]
}
```

版本汇总仅供列表展示；M3 指标可用性判断必须使用对应 subset 的能力值（§5.5/§5.6）。

### 3.4 事件

```
dataset.import.started / progress / finished
dataset.import.failed
dataset.version.ready
```

进度事件字段：`phase, scanned, completed, persisted, total, errors, eta_ms`。扫描阶段每 50 条提交一次已扫描数；扫描结束后确定总条数，落库阶段继续报告已处理/已登记数、错误数与预计剩余时间。

---

## 4. 数据结构

```sql
create table dataset (
  id uuid primary key default gen_random_uuid(),
  name text not null unique,
  source_type text,                -- aishell1|aishell3|kespeech|alimeeting|magicdata|apy|datatang|srt
  description text,
  tags jsonb not null default '{}',
  created_at timestamptz not null default now()
);

create table dataset_version (
  id uuid primary key default gen_random_uuid(),
  dataset_id uuid not null references dataset(id) on delete cascade,
  version text not null,
  status text not null default 'importing',   -- importing|ready|ready_with_errors|failed
  manifest_key text, manifest_hash char(64),
  item_count int not null default 0,
  total_duration_ms bigint not null default 0,
  capabilities jsonb not null default '{}',
  stats jsonb not null default '{}',
  import_params jsonb not null default '{}',
  created_by text,
  created_at timestamptz not null default now(),
  unique (dataset_id, version)
);

create table dataset_subset (
  id uuid primary key default gen_random_uuid(),
  dataset_version_id uuid not null references dataset_version(id) on delete cascade,
  name text not null,                    -- phone | mic | far | near
  audio_profile text not null,           -- as_is | mono_mix | mono_ch0
  is_primary boolean not null default false,
  item_count int not null default 0,
  total_duration_ms bigint not null default 0,
  capabilities jsonb not null default '{}',
  stats jsonb not null default '{}',
  unique (dataset_version_id, name)
);

create table audio_asset (
  id uuid primary key default gen_random_uuid(),
  sha256 char(64) not null unique,
  uri text not null,
  bytes bigint,
  duration_ms int,
  sample_rate int,
  channels smallint,
  codec text,
  origin_path text,
  created_at timestamptz not null default now()
);

create table audio_variant (
  asset_id uuid not null references audio_asset(id) on delete cascade,
  profile text not null,              -- 16k_mono | 16k_mono_ch0
  sha256 char(64) not null,
  uri text not null,
  bytes bigint,
  created_at timestamptz not null default now(),
  primary key (asset_id, profile)
);

create table item (
  id bigserial primary key,
  dataset_version_id uuid not null references dataset_version(id) on delete cascade,
  subset_id uuid not null references dataset_subset(id) on delete cascade,
  item_key text not null,
  pair_key text,
  content_uid char(24) not null,
  audio_asset_id uuid not null references audio_asset(id),
  split text not null default 'all',
  duration_ms int not null,
  lang text,
  speakers text[] not null default '{}',
  buckets jsonb not null default '{}',
  ref_sha char(16),
  unique (dataset_version_id, item_key)
);
create index item_subset on item (subset_id);
create index item_split on item (dataset_version_id, split);
create index item_buckets on item using gin (buckets jsonb_path_ops);
create index item_uid on item (content_uid);
create index item_pair on item (dataset_version_id, pair_key);

create table ref_text (
  item_id bigint not null references item(id) on delete cascade,
  kind text not null,                 -- verbatim | itn_expected
  lang text,
  text text not null,
  sha256 char(16) not null,
  primary key (item_id, kind)
);

create table ref_segment (
  id bigserial primary key,
  item_id bigint not null references item(id) on delete cascade,
  start_ms int not null,
  end_ms int not null,
  speaker text not null,
  text text not null,
  skip boolean not null default false,
  overlap boolean not null default false
);
create index ref_segment_item on ref_segment (item_id, start_ms);

create table speaker_meta (
  dataset_version_id uuid not null references dataset_version(id) on delete cascade,
  speaker_id text not null,
  gender text, age text, accent text, meta jsonb,
  primary key (dataset_version_id, speaker_id)
);

create table import_task (
  id uuid primary key default gen_random_uuid(),
  dataset_id uuid not null references dataset(id) on delete cascade,
  dataset_version_id uuid references dataset_version(id) on delete cascade,
  status text not null default 'queued',
  phase text, progress jsonb not null default '{}',
  report jsonb, error text,
  created_by text, created_at timestamptz not null default now(),
  started_at timestamptz, finished_at timestamptz
);
```

---

## 5. 关键流程

### 5.1 导入状态机

```
created ─► scanning ─► persisting ─► analyzing ─► ready
   │           │            │            │
   └───────────┴────────────┴────────────┴──► failed
                                         └──► ready_with_errors
```

| 阶段 | 做什么 | 进度分母 |
|---|---|---|
| scanning | 遍历目录并解析标注、生成 `RawItem` | 扫描中持续计数；完成后取得准确条数 |
| persisting | 算 sha256/时长、批量入库 | 条目数 |
| analyzing | 算 capabilities、stats、manifest | 条目数 |

**失败阈值**：`failed / total > 1%` → `ready_with_errors`（需人工确认才能用于评测）。

### 5.2 预览（导入前）

```
preview(root, importer, params, limit=50)
→ 只解析前 50 条，不落库
→ 返回：item_key / 时长 / 文本截断 / 说话人 / 分桶 / 问题标记
```

目的：在写库前发现"配错列、路径错、编码错"。

### 5.3 扫描与解析（按数据集）

各 Importer 的字段映射见 §6。通用规则：

- `item_key`：优先用数据集原生 id；否则用相对路径（去扩展名）
- 同一版本内 `item_key` 重复 → `E_DUP_KEY`，保留第一条
- 文本统一 UTF-8；非 UTF-8 先尝试 UTF-8 / GB18030 自动识别，失败记 `E_ENCODING`

### 5.4 音频资产与变体

```
audio_asset:
  sha256 = 流式 sha256(文件字节)
  duration_ms / sample_rate / channels / codec = ffprobe 或 wave 头
  uri = 原始绝对路径（只读挂载）

audio_variant（懒生成）:
  profile = 16k_mono | 16k_mono_ch0
  由所属 subset 的 audio_profile 决定
  首次访问时 ffmpeg 生成，sha256 命名落缓存目录
```

Subset 的 `mono_mix` 映射到 `audio_variant.profile=16k_mono`，`mono_ch0` 映射到
`16k_mono_ch0`；`as_is` 直接使用已校验的 16k 单声道源，不创建变体。

**为什么要有 `mono_ch0`**：AliMeeting 是 8 声道远场，取第 0 声道是官方基线做法；直接混音会引入相位问题。

**重要约束：ASR 模型只接受单声道**。因此所有 profile 的最终产物都是 mono：

| profile | 处理 | 适用 |
|---|---|---|
| `mono_ch0` | 取第 0 声道 → 16k mono | AliMeeting 8 声道 |
| `mono_mix` | 降混为单声道 → 16k | APY 粤语 44.1k 双声道 |
| `as_is` | 已是 16k mono，仅校验 | AISHELL / KeSpeech |

**mp4（客家话）**：导入时一次性抽轨为 16k mono WAV，落到托管缓存目录，作为 `audio_asset.uri`。这是唯一"导入即转码"的例外。

### 5.5 能力分析（产出 `dataset_version.capabilities`）

```python
{
  "item_count": 9982,
  "total_duration_ms": 41200000,
  "duration_p50_ms": 4100,
  "duration_p95_ms": 28600,
  "duration_max_ms": 2058963,

  "text_ratio": 1.00,          # 有参考文本的占比
  "punct_any_ratio": 0.78,        # 参考含任意标点（，。？！、；：）的占比
  "punct_sentence_ratio": 0.02,   # ★ 分段以句末标点（。！？）结尾的比例 —— 标点可评性的真正依据
  "punct_supported": false,       # punct_sentence_ratio >= 0.8 且无数据集覆盖
  "itn_ratio": 0.00,           # 有 itn_expected 的占比
  "segment_ratio": 1.00,       # 有时间戳分段的占比
  "speaker_ratio": 1.00,       # 分段带说话人的占比
  "overlap_ratio": 0.162,      # 重叠语音时长 / 语音总时长

  "speaker_count": 20,
  "langs": ["zh"],
  "bucket_counts": {"dialect": {"mandarin": 5000, "yue": 2000}}
}
```

M3 用它判定可评指标；M5 用它决定是否跳过某项打分。

`overlap_ratio` 的分子是至少两名不同说话人同时发声的时间并集；分母是有效说话人分段时长之和。该定义对应 AliMeeting `Test_Ali_far` 的 M1 验收值，约为 `0.162`。

> 该 JSON **按 subset 各算一份**，写在 `dataset_subset.capabilities`；版本级只存加权汇总。
> 例：AliMeeting `far` 的 `overlap_ratio≈0.162`、`speaker_ratio=1.0`；`near` 的 `speaker_ratio=1.0` 但每个 item 只有 1 人 → DER/命名不适用。

### 5.6 Subset 规则（采集条件）

**一次导入可以生成多个 subset。**

| 数据集 | subset | audio_profile | primary |
|---|---|---|---|
| APY | `phone` | `as_is`（16k mono） | ✅ |
| APY | `mic` | `mono_mix`（44.1k 双声道降混） | — |
| AliMeeting | `near` | `as_is`（16k mono 单人轨） | ✅（业务确认） |
| AliMeeting | `far` | `mono_ch0`（8 声道取 ch0） | — |

规则：

1. 每个 subset 有**独立的** `audio_profile` / `capabilities` / `stats` / `item_count`。
2. 每个版本**有且仅有一个** `is_primary=true`。
3. `item.subset_id` 必填；`item_key` 在版本内唯一（含 subset 前缀）。
4. `pair_key` 跨 subset 相同（如 `R8002_M8002/ast` 在 phone/mic 下一致），用于配对对比。
5. 版本级 `capabilities` 是各 subset 的**汇总**（按条数加权），仅用于列表页展示；**指标门控必须用 subset 级**。
6. 删除 subset 不允许；删除版本时级联。

### 5.7 Manifest 与哈希

```
manifest.jsonl.gz   # 每行一个 item
manifest.meta.json  # 参数、计数、能力、哈希

manifest_hash = sha256(排序后的 "item_key|audio_sha|ref_sha")[:64]
```

`ref_sha = sha256(verbatim 文本 或 分段文本按时间拼接)`。

### 5.8 版本 diff

```
diff(a, b):
  按 item_key 对齐
  → added / removed
  → text_changed（ref_sha 不同）
  → audio_changed（audio_sha 不同）
  → bucket_changed
返回计数 + 前 100 条明细
```

### 5.9 断点续跑

- 每批 500 条提交一次；记录 `progress.completed` 与 `progress.persisted`
- 重启后重新扫描，跳过已存在的 `(dataset_version_id, item_key)`
- `import_task.progress` 可从任意阶段恢复（scanning 需重扫）

---

## 6. 数据集信息优先级与官方字段

### 6.0 数据集信息优先级（官方口径优先）

**原则：官方元数据 > 目录/命名约定 > 文件名启发式。**

```
1. 官方元数据文件（metadata / AUDIOINFO.txt / SPKINFO.txt / content.txt / TextGrid / README）
2. 目录结构与命名约定（split / 方言区 / 说话人目录）
3. 文件名解析（如 utt_id 里的说话人编号）——仅作兜底，且必须记 warning
```

每个版本导入时记录 `import_params.official_files`（用了哪些官方文件）与 `warnings`（哪些字段退化为启发式）。冲突时以官方文件为准。

### 6.1 各数据集的官方文件与字段

| 数据集 | 官方文件 | 提供什么 | 缺失时 |
|---|---|---|---|
| AISHELL-1 | `transcript/aishell_transcript_v0.8.txt`；`resource_aishell/speaker.info`（若存在） | 文本；说话人性别 | 从 `utt_id` 推导说话人，标 warning |
| AISHELL-3 | `content.txt`、`spk-info.txt`、`phone_set.txt` | 文本、性别/年龄段/口音、音素表 | 拒绝导入（文本必需） |
| KeSpeech | 同名 `.txt`；官方元数据（若存在） | 文本；方言区/说话人 | 目录名作方言区 |
| AliMeeting | `TextGrid`（唯一标注）；README/论文口径定义 far/near | 分段、说话人、文本 | 拒绝导入（无 TextGrid 无法评 DER） |
| MagicData | `AUDIOINFO.txt`、`SPKINFO.txt`、`README.txt`、`TXT/*.txt` | 说话人、性别、年龄、地区、设备、topic、标记说明 | 拒绝导入（缺 SPKINFO 无法做性别分桶） |
| APY / DataTang | `*.metadata`（LHD）、`*.txt` | 采样率、声道、说话人、性别、年龄、口音、语言、label 文件名 | 拒绝导入（缺 metadata 无法确定声道/语言） |
| 客家话 | `.srt` / `.txt` | 时间轴、文本 | 无元数据，只能冒烟 |

### 6.2 关键官方字段解析

**APY / DataTang（LHD）**

```
SAM 44100      → 采样率
NCH 2          → 声道数
SCD O1,O2      → 说话人 id
SEX O1_Male,O2_Male
AGE 40;45
ACC GuangZhou;GuangZhou   → 口音/地区
ACT Cantonese             → 语言
BIR GuangZhou;GuangZhou   → 出生地
LBD ast_mic.txt           → 对应的标注文件
```

- 同一 session 有多个"域 × 设备"组合：`ast_phone` / `ast_mic` / `cel_phone` / `cel_mic` / `tra_*` / `pla_*` …
- `*_phone`：16 kHz / 单声道（**默认选它**，与模型输入一致）
- `*_mic`：44.1 kHz / 双声道（评测笔/麦克风条件）
- **一次导入生成两个 subset**：`phone`（primary）、`mic`
- `buckets` 写入 `domain=ast|cel|tra|...`（subset 本身表达设备条件）、`language` 与 `dialect`
- `ACT` 不总是语言名（闽语包的 ACT 是“漳州/泉州”地点）；语言优先从包目录名推断（如 `APY210930001-闽语` → `minnan/闽南语`），再回退 ACT 关键词
- `pair_key` = `session_id/domain`：**同一句话在 phone 与 mic 下配对**，用于"录音条件对 CER 的影响"分析
- 部分 APY/DataTang 包使用 LHD 通用文件名（如 `2_154_001.wav`，无 `_phone/_mic` 后缀）；此时按 metadata 的 `MIT` 判断：`android/iphone/phone` → `phone`，`mic/microphone` → `mic`，缺失时按 `SAM=16000` 且 `NCH=1` 兜底为 `phone`

**MagicData**

```
AUDIOINFO.txt: CHANNEL UTTRANS_ID SPEAKER_ID[-1,-2] TOPIC
SPKINFO.txt:   CHANNEL SPEAKER_ID GENDER AGE REGION,CITY DEVICE
README.txt:    特殊标记定义（[+] [*] [ENS] …）
```

- `buckets.language` / `buckets.dialect` 从**导入根目录名**推断并统一：
  沪/吴→`wuu`/`沪语`、湘→`xiang`/`湘语`、赣→`gan`/`赣语`；未知保留目录名并回退 `zh`；
- 目录名是 MagicData 唯一的方言信号，移动或重命名目录会改变结果；
- 两者都会进入 item、`bucket_counts` 与 subset/version 能力快照。

**AliMeeting（两种形态）**

| 形态 | 目录 | 音频 | 标注 | 说明 |
|---|---|---|---|---|
| **far** | `Test_Ali_far/audio_dir/*.wav` | 8 声道阵列，16k | `textgrid_dir/*.TextGrid`，多 tier（`N_SPKxxxx`） | 需要 DER / 多说话人 |
| **near** | `Test_Ali_near/audio_dir/*_N_SPKxxxx.wav` | 单人耳机麦，16k mono | 同名 TextGrid，单 tier（`c1`） | 单人轨，不需要 DER |

- far 的说话人来自 tier 名；near 的说话人来自**文件名** `_N_SPKxxxx`
- **一次导入生成两个 subset**：`near`（primary，业务确认）、`far`
- near 每个 item 只有一个说话人 → DER/命名不适用（resolver 会按 subset 能力返回 `na`）

### 6.3 官方口径调查结论

**AliMeeting**

- OpenSLR SLR119 明确：`Test_Ali.tar.gz` **同时包含** 8 声道阵列（far）与每人耳机麦（near）
- M2MeT 论文：near 用于单说话人 ASR 基线，报告了 `Eval-Ali-near` 的 CER（7.8~9.9%）
- 官方基线仓库 `yufan-aslp/AliMeeting`：`run_local_conformer_near_alimeeting.sh` 即单说话人 near 训练
- 结论：**near 是官方认可的评测集**，可作为独立导入模式

**APY / DataTang**

- 官方数据页（datatang.com/dataset/1026）规格：16kHz/44.1kHz、16bit、mono/stereo、手机+录音笔
- 标注：文本、句子起止、说话人标识
- **没有公开的评测协议**，也没有"只准用 phone"的说明
- 结论：phone 与 mic 都是官方条件；phone 更贴合 ASR 输入（16k mono），适合作为**默认主条件**

## 7. 六类 Importer 映射（汇总）

| 数据集 | 配对规则 | 文本 | 分段/说话人 | 分桶 | 语言 | 音频 |
|---|---|---|---|---|---|---|
| **AISHELL-1** | `transcript` 行 ↔ `wav/{split}/{spk}/{utt}.wav` | 按空格分词 → 去空格按字 | 无分段、无说话人 → 整段 + `S0002` | split | zh | 16k mono |

> AISHELL-1 根目录可填 `data_aishell`，也可填它的上一级 `aishell1/`；导入器会自动下钻到 `data_aishell`（2026-11-10）。
| **AISHELL-3** | `content.txt` ↔ `{split}/wav/{spk}/*.wav` | `split('\t')[1].split()` **取偶数下标** | 无 | gender / accent / age_group | zh | 44.1k → 重采样 |
| **KeSpeech** | 目录内同 stem 的 `.wav` + `.txt` | 单行纯文本 | 无 | **dialect = 目录名** | zh | 16k mono |
| **AliMeeting far** | `audio_dir/*.wav` ↔ `textgrid_dir/*.TextGrid`（多 tier） | 各 tier interval 按 xmin 拼接 | 分段 + 说话人 + 重叠 | n_speakers / scene=far | zh | 8ch → **ch0** |
| **AliMeeting near** | `audio_dir/*_N_SPKxxxx.wav` ↔ 同名 TextGrid（单 tier `c1`） | 单 tier interval 文本 | 分段；说话人取文件名 | scene=near / speaker | zh | 16k mono |
| **MagicData 沪/湘/赣** | `TXT/*.txt` ↔ `WAV/*.wav` | `[s,e]\tspk\tgender\ttext` | 有分段 + 说话人 | dialect / gender | yue/wuu/xiang/gan | 视数据集 |
| **APY 粤/川/闽** | `<ses>/*.txt` ↔ 同名 `.wav` | `start\tend\tspk\ttext` | 有分段 + 说话人 | accent / gender / device | yue/sichuan/minnan | 粤 44.1k **双声道降混为 mono** |
| **客家话** | `.mp4` ↔ `.srt`/`.txt` | 识别 SRT 时码；无时码时保留纯文本 | 有时码时记录分段、说话人未知 | — | hak | mp4 → 受管缓存并抽轨 16k mono |

### 7.1 标点可评性的判定（重要）

**"有标点" ≠ "能评标点"。** 如果参考只有句中逗号、句子之间没有标点，模型输出的句号会被全部判成误报，指标毫无意义。

实测（本机数据）：

| 数据集 | 句末标点率（以 。！？ 结尾的分段占比） | 含任意标点 | 结论 |
|---|---|---|---|
| AliMeeting | **100.0%** | 100.0% | ✅ 可评 |
| MagicData 沪语 | 0.6% | 60.4% | ❌ |
| MagicData 湘语 | 12.1% | 72.0% | ❌ |
| MagicData 赣语 | 4.9% | 39.5% | ❌ |
| APY 粤语 | 8.1% | 29.6% | ❌ |
| APY 四川话 | 0.0% | 0.0% | ❌ |
| APY 闽语 | 87.3% | 87.6% | ⚠️ 按业务口径统一关闭 |

**判定规则**

```
punct_supported =
    dataset_override_punct == False  ? false
  : punct_sentence_ratio >= 0.8      ? true
  : false
```

- 阈值 `0.8` 可配置；
- **业务确认（2026-11-03）：APY 全系 + MagicData 全系一律 `punct_supported=false`**；
- 一期实际可评标点的只有 **AliMeeting**（far 与 near 都满足 100%）；
- N/A 原因码统一为 `no_sentence_punctuation`。

### 7.2 代码与中文显示名

**存储用代码，展示用中文**。M1 只存 code；前端通过 `/api/v1/meta/enums` 取 label。

| 类别 | code → 中文 |
|---|---|
| 语言 | `zh` 普通话 · `yue` 粤语 · `wuu` 吴语（沪语） · `xiang` 湘语 · `gan` 赣语 · `sichuan` 四川话 · `minnan` 闽南语 · `hak` 客家话 · `en` 英语 |
| 方言区（KeSpeech） | `Beijing` 北京 · `Ji-Lu` 冀鲁 · `Jiang-Huai` 江淮 · `Jiao-Liao` 胶辽 · `Lan-Yin` 兰银 · `Mandarin` 普通话 · `Northeastern` 东北 · `Southwestern` 西南 · `Zhongyuan` 中原 |
| 性别 | `M` 男 · `F` 女 · `unknown` 未知 |
| 年龄段 | `A` 14 岁以下 · `B` 14~25 · `C` 26~40 · `D` 41 岁以上 |
| split | `train` 训练集 · `dev` 开发集 · `test` 测试集 · `all` 全部 |

> 新增 code 时必须同时补中文名，否则前端会回退显示 code。

**特殊标记处理**

| 标记 | 处理 |
|---|---|
| `[+]` 重叠 | `overlap=true`，文本保留 |
| `[*]` 不可懂 / `[ENS]` 噪声 / `[LAUGHTER]` / `[SONANT]` / `[MUSIC]` / `[SYSTEM]` / `[FILLER]` / `[PII]` | `skip=true`，从 CER 分母剔除；DER 仍按非语音处理 |
| `G0000` + `none` | 整行跳过（非语音） |

---

## 8. 异常与错误码

| code | 含义 | 处理 |
|---|---|---|
| `E_AUDIO_MISSING` | 标注有、音频无 | 跳过 + 报告 |
| `E_AUDIO_DECODE` | 无法解码 / 非 PCM | 跳过 + 提示 ffmpeg |
| `E_REF_MISSING` | 音频有、参考无 | **默认跳过**；仅 `allow_missing_ref=true` 时保留（默认关闭） |
| `E_REF_EMPTY` | 参考去空白为空 | 跳过 |
| `E_DUP_KEY` | `item_key` 重复 | 保留第一条 |
| `E_SEGMENT_INVALID` | `end <= start` | 修正为 `start+1ms` + 告警 |
| `E_SEGMENT_OVERFLOW` | `end > duration + 200ms` | 截断 + 告警 |
| `E_ENCODING` | 非 UTF-8 且无法识别 | 失败该条 |
| `E_PATH_ESCAPE` | 路径不在白名单根目录 | 拒绝整次导入 |
| `E_NO_IMPORTER` | 无法识别数据集类型 | 拒绝 + 给出候选 |

**错误报告**：按 code 分组计数 + 前 20 条样例（路径 + 原因）。

---

## 9. 测试点

### 9.1 单元

| 用例 | 断言 |
|---|---|
| AISHELL-1 文本去空格 | `"而 对 楼市"` → `"而对楼市"` |
| AISHELL-3 取汉字 | 交替序列取偶数下标正确 |
| KeSpeech 配对 | 同 stem 配对，不按顺序 |
| AliMeeting TextGrid | 4 tier 合并、按 xmin 排序、重叠标记 |
| MagicData 标记 | `[+]`→overlap，`[ENS]`→skip |
| APY 双声道 | channels=2，profile=mono_mix |
| content_uid | 同输入同值；改文本/音频则变 |
| manifest_hash | 顺序无关、内容相关 |
| capabilities | 标点/分段/说话人覆盖率计算正确 |

### 9.2 集成

| 用例 | 断言 |
|---|---|
| AISHELL-1 全量 | 7,176 条，CER 参考可算 |
| AliMeeting 全量 | 20 段，`punct_sentence_ratio=1.0`、`overlap_ratio≈0.162` |
| MagicData 沪语 | 分段/说话人齐全，`[ENS]` 被 skip |
| APY 粤语 | 双声道降混成功 |
| 客家话 mp4 | 抽轨成功、SRT 时间轴正确 |
| 重复导入 | 409 或 `?replace=true` 生成新版本 |
| 断点续跑 | 中途 kill，重启后补齐不重复 |

### 9.3 性能

| 用例 | 目标 |
|---|---|
| AISHELL-1 7,176 条导入 | < 25s（不含转码） |
| 1 万条列表查询 | < 300ms |
| manifest 生成 | < 5s |

**实测（2026-11-04，WSL2 + /mnt/f）**

| 场景 | 耗时 | 说明 |
|---|---|---|
| 1,000 条（热缓存） | 3.4s | 优化后；约 293 条/秒 |
| 7,176 条（热缓存） | 15.6s | 达到 <25s 目标 |
| 7,176 条（冷缓存） | 112s | 需读 1.16GB，/mnt/f 的 9p 约 10MB/s，受存储带宽限制 |

2026-11-07 AliMeeting far 隔离验收：对 `Test_Ali_far` 的 20 条真实录音完成预览与完整导入。预览 2,165ms 返回 20/20 有效项、无问题；只读取 WAV 容器头，不哈希 9.93GB 音频。完整导入耗时 372.75s，版本 `ready`，20 条、16,358 个参考分段、0 解码错误；原始音频仍保留 8 声道并使用 `mono_ch0` profile。能力统计为 `punct_sentence_ratio=1.0`、`overlap_ratio=0.162`、`segment_ratio=1.0`、`speaker_ratio=1.0`，manifest hash 为 `76804879e9f823e7ec04eb89b5179e5ba10ede000f83542e8fa1c239281cd309`。临时库与对象目录已清理。

2026-11-07 补充隔离验收：从 `/mnt/f/测试数据/测试文件/AI测试素材/开源数据集/aishell1/data_aishell` 导入 7,176 条到独立临时 PostgreSQL，版本为 `ready`，item 数为 7,176，总时长 36,106,777ms，能力统计及 `manifest.jsonl.gz` / `manifest.meta.json` 均生成，manifest hash 为 `a2fe84f1705cdcb912b37981c9528dbfffbaceba55fdeb7df3cb6034d9fa98dd`。耗时 593.49s；该次缓存状态未控，不能替代热缓存基准，明显慢于此前记录，需在固定缓存条件下复测。临时库与对象目录已删除，开发库条目数保持 1,160。

2026-11-09 KeSpeech 当前开发库导入：19,723 条、音频 3,600,479,778 bytes，耗时 2,230s（约 1.54 MiB/s），来源文件位于 WSL `9p` 挂载。该次结果低于设计目标，且与 AISHELL-1 热缓存数据不同；它显示 9p 小文件读取是主要限制。本次代码调整减少了批次事务与线程池重建，但尚未重新测量；大规模导入应使用预期的只读 NFS 挂载并复测。

2026-11-10 AISHELL-1 导入性能复测：把 `data_aishell` 7,176 条（3,610,677ms）复制到本地 ext4 `/var/tmp/asr-aishell1-local`，用独立库 `asr_eval_import_perf` 和 `tools/bench_import.py` 导入，**11.60s 完成，618 items/s，版本 ready**。说明导入代码路径与分批/manifest 本身满足 `<25s` 目标；当前机器没有挂载 NFS，正式 NFS 验收仍待提供只读导出后复跑。记录见 `evidence/import-perf-2026-11-10.md`。

优化手段：一次读取同时算 sha256 与 WAV 头（不再 `wave.open` 二次打开）、单导入任务复用 16 线程池、每 500 条批量提交；扫描时每 50 条报告一次进度。
结论（初步）：**代码路径在本地 ext4 上已达标（7,176 条 11.60s，618 items/s），作为当前导入性能的初步结论。** KeSpeech 2,230s 与 AISHELL-1 9p 593s 的瓶颈是 9p 小文件读取，不是导入逻辑。按项目决定，暂不基于该结果继续优化；真实部署到只读 NFS 后，再以实际耗时判断是否需要优化。若按 1.16GB 全量读取估算，25s 至少需要约 50MB/s 的有效读取吞吐，且小文件元数据延迟也需计入。

### 9.4 黄金样例

用本机真实数据：

```
/mnt/f/.../aishell1/data_aishell
/mnt/f/.../AISHELL-3
/mnt/f/.../kespeech
/mnt/f/.../alimeeting/Test_Ali/Test_Ali_far
/mnt/f/.../方言ASR测试/MagicData-*
/mnt/f/.../方言ASR测试/APY*
/mnt/f/.../方言ASR测试/客家话
```

---

## 10. 验收

1. 六类标准语料 Importer 都能导入，客家话 SRT/TXT 有专用导入器；AliMeeting 一次导入生成 `near`+`far` 两个 subset（primary=near），APY/DataTang 生成 `phone`+`mic`（primary=phone）。
2. 每个 subset 的 `capabilities` 与人工核对一致（尤其 AliMeeting far 的 `overlap_ratio≈16.2%`）。
3. 同一数据集导入两次（换版本号），`manifest_hash` 相同。
4. 改一条参考文本后生成新版本，`diff` 能准确报出 `text_changed=1`。
5. 1 万条导入 < 30s；列表 < 300ms。
6. 断点续跑后条数与一次性导入一致。
7. 路径越界（`../`）被拒绝。

---

## 11. 契约与风险

| 下游 | 需要什么 |
|---|---|
| M3 | `dataset_version.capabilities`、`item_count`、时长分布 |
| M4 | `item.duration_ms`、`audio_asset` / `audio_variant` 路径 |
| M5 | `ref_text`、`ref_segment`、`speaker_meta` |
| M6 | `buckets` 用于分桶 |

| # | 风险 | 处理 |
|---|---|---|
| 1 | 数据集标注格式与预期不符 | 预览 + 黄金样例先行 |
| 2 | 8 声道/双声道下混方式影响结果 | `audio_profile` 进版本，报告标注 |
| 3 | 大文件导入慢 | 懒转码；mp4 单独排队 |
| 4 | 参考文本繁简 | 保持原样，归一化在 MetricProfile 里做 |
| 5 | 开源数据集许可 | 不对外分发音频；报告只引用路径 |
