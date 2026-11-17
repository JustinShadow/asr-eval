# 实现细则（对设计 v1.0 的补充）

> 回答 5 个疑问，并补到"可直接开工"的粒度。
> 阅读顺序建议：§1 DER → §2 导入 → §3 能力 → §4 实测 → §5 执行

---

## 1. 为什么不直接用 `md-eval.pl`

### 1.1 结论先说

**不是不用，是不把它当默认引擎。** 正确分工是：

| 角色 | 工具 | 何时用 |
|---|---|---|
| 生产默认（通用） | `pyannote.metrics` | 一般场景的 DER |
| **AliMeeting 官方口径** | `dscore` | 官方基线仓库即用 dscore 计算 DER；`alimeeting_official` 口径默认用它 |
| **验收基准** | `md-eval.pl` | CI 门禁，与 dscore / pyannote 三方对照 |
| 正式对外口径 | `dscore` 或 `md-eval.pl`（可切换） | 报告标注 scorer |

> 官方 AliMeeting baseline 的 speaker track README 明确写：`We use toolkits dscore to get the DER results`，且只使用第 0 声道。因此 AliMeeting 口径必须支持 `scorer=dscore`。

### 1.2 为什么生产不用它当默认

| # | 问题 | 具体影响 |
|---|---|---|
| 1 | **文件契约，不是 API** | 每次打分要落 `ref.rttm` + `hyp.rttm` + `uem`，起子进程，解析 stdout。我们一个 worker 进程要连续算上千个 case，文件 I/O 和进程创建开销 ×1000 |
| 2 | **没有逐时间片的错误归属** | 它只给汇总（`MISS/FA/CONF` 秒数）。Badcase 页要"哪一段、哪两个人混了"，得自己再算一遍帧级归属——等于两套实现 |
| 3 | **SA-DER 变体要自己构造** | `-1` 只关闭最优映射；身份标签怎么贴、未匹配怎么归类，仍要平台侧实现 |
| 4 | **依赖面** | 容器里多一个 Perl + SCTK 脚本树；它不是库，没有版本语义、没有 API 稳定性承诺，升级靠替换文件 |
| 5 | **出错难定位** | 子进程非零退出 + 文本 stderr，无法结构化重试；批量任务里定位"哪一条坏了"成本高 |
| 6 | **不可单元测试** | 无法在 pytest 里对 10 行构造数据做断言，只能靠端到端对比 |

`pyannote.metrics` 的对应优势：进程内调用、返回 `DER/MISS/FA/CONF` 结构化、支持 UEM、可被 pytest 直接断言。

### 1.3 但 md-eval 是不可替代的"裁判"

它的价值不在跑得快，而在**别人认**。所以把它放在两个位置：

**① CI 一致性门禁（必做）**

```
对 AliMeeting 20 段：
  ref.rttm  ← TextGrid 转换器生成
  hyp.rttm  ← 我们的 pyannote 输出转换回 RTTM（保证输入一致）

  md-eval.pl -c 0.25 -r ref.rttm -s hyp.rttm -u uem
  pyannote.metrics.DiarizationErrorRate(collar=?, skip_overlap=False)

  断言：|ΔDER| < 0.2pp 且 MISS/FA/CONF 各自 |Δ| < 0.2pp
```

门禁不过 → 不允许发布新 scorer 版本。

**② 正式报告模式**

```jsonc
// metric_profile.der
{ "scorer": "pyannote",   // pyannote | md_eval
  "collar_ms": 250, "collar_semantics": "half_width",
  "skip_overlap": false, "uem": "full" }
```

`scorer=md_eval` 时走子进程，输出与 pyannote 完全一致的 JSON 结构，报告页标注 `scorer: md-eval.pl (NIST)`。

### 1.4 参数映射（容易踩的坑）

| 概念 | md-eval.pl | pyannote.metrics | 映射 |
|---|---|---|---|
| collar | `-c 0.25` = **边界两侧各 0.25s**（总 0.5s） | `collar=0.25` = **以边界为中心、总宽 0.25s**（±0.125s） | **pyannote 要传 `0.5` 才等价于 md-eval 的 `0.25`** |
| 重叠 | 默认参与计分 | 默认 `skip_overlap=True` | 必须显式 `skip_overlap=False` |
| UEM | 需显式给文件 | 可由 `Annotation` 推导 | 统一按 `[0, duration]` 生成 UEM |
| 映射 | 匈牙利（`-1` 关闭） | 匈牙利 | 一致 |
| 帧长 | 10ms | 内部自适应 | 结果差异 < 0.1pp |

> 这张表就是 §1.3 门禁要验证的东西。**先跑门禁，再信数字。**

### 1.5 顺带说清 SA-DER

`sa_der` **不用 md-eval 的 `-1`**，实现更直接：

```python
# 1) 把 hypothesis 的簇标签替换成匹配到的身份
for seg in hyp_segments:
    ident = spk_voiceprints.get(seg.spk, {}).get("match_info", {}).get("matched_id")
    seg.label = ident or f"__unknown_{seg.spk}"     # 未匹配进唯一 unknown 桶

# 2) 参考侧本来就用真实身份（SPK8005 / G0003）

# 3) 用 DER 算法，但**禁用最优映射**，直接按标签比对
der = fixed_mapping_der(ref, hyp)                   # 复用同一套帧级算法
```

`SA-DER − DER` = 命名环节引入的额外错误。这在 pyannote 上只需替换"映射函数"，不需要第二套算法。

---

## 2. 数据集怎么导入

### 2.1 五个阶段（状态机）

```
created ──► scanning ──► parsing ──► persisting ──► analyzing ──► ready
   │            │            │             │             │
   └────────────┴────────────┴─────────────┴─────────────┴──► failed
                                                          └──► ready_with_errors
```

| 阶段 | 做什么 | 进度分母 | 典型耗时（1 万条） |
|---|---|---|---|
| `scanning` | 遍历目录、识别文件配对、算 `item_key` | 文件数 | 5~15s（NFS 慢则 30s） |
| `parsing` | 解析标注 → `RawItem`（文本/分段/说话人/分桶） | 条目数 | 10~30s |
| `persisting` | 算 audio sha256 + duration、写 `COPY` | 条目数 | 10~30s |
| `analyzing` | 算数据集能力（标点覆盖率/分段率/重叠率/说话人数） | 条目数 | 2~5s |
| `ready` | 写 `stats`、`manifest`、能力 JSON | — | — |

> 音频**不在导入时转码**。首次被评测用到时才生成 `16k/mono` variant（内容寻址缓存）。

### 2.2 用户操作（向导 6 步）

```
① 选类型      自动识别（读目录特征）或手动指定
                 └─ 识别失败时给出候选：「看起来像 MagicData（找到 AUDIOINFO.txt）」
② 填根目录    服务器路径浏览器（只读挂载点白名单）
③ 参数        语言 / split 映射（train|dev|test|all）/ 是否跳过无参考条目
④ 预览        ★ 扫描前 50 条，展示：
                 item_key | 时长 | 参考文本(截断) | 说话人 | 分桶 | 是否存在问题
                 └─ 这一步能提前发现 90% 的解析错误
⑤ 命名+版本   数据集名 + 版本号（默认 v1.0）
⑥ 导入        后台任务，SSE 推进度；完成后弹「质量报告」
```

### 2.3 质量报告（导入结束后必须看）

```
✔ 总条目        10,000
✔ 成功           9,982
⚠ 跳过（无参考）      12        → 列出前 20 条
✖ 失败                6        → 按错误码分组
─────────────────────────────────────────────
标点覆盖率        78.4%   → 标点指标可评
分段时间戳覆盖    100%    → DER 可评
说话人标签覆盖    100%    → 命名可评（需注册音频）
重叠语音占比      16.2%
时长 P50 / P95    4.1s / 28.6s
采样率分布        44.1kHz 1,204 · 16kHz 8,778
```

失败率 > 1% → 版本状态置 `ready_with_errors`，需人工确认才能用于评测。

### 2.4 错误码（导入期）

| code | 含义 | 处理 |
|---|---|---|
| `E_AUDIO_MISSING` | 标注有、音频无 | 跳过 + 报告 |
| `E_AUDIO_DECODE` | 无法解码 / 非 PCM WAV | 跳过；提示需 ffmpeg |
| `E_REF_MISSING` | 音频有、参考无 | 跳过（可配 `allow_missing_ref=true`） |
| `E_REF_EMPTY` | 参考去空白后为空 | 跳过 |
| `E_DUP_KEY` | `item_key` 重复 | 跳过后者 + 报告 |
| `E_SEGMENT_INVALID` | `end <= start` | 修正为 `start+1ms` + 告警 |
| `E_SEGMENT_OVERFLOW` | `end > duration + 200ms` | 截断 + 告警 |
| `E_ENCODING` | 非 UTF-8 且无法自动识别 | 失败（拒绝静默乱码） |

### 2.5 Manifest（可复现的锚点）

导入结束写 `manifest.jsonl.gz` + `manifest.meta.json` 到本地对象目录：

```jsonc
// manifest.meta.json
{
  "dataset": "aishell1", "version": "v1.0",
  "importer": "aishell1", "importer_version": "1.0.0",
  "root": "/mnt/data/aishell1", "params": {"lang": "zh", "split": "all"},
  "item_count": 9982, "error_count": 6,
  "manifest_hash": "sha256:...",       // 排序后的 (item_key|audio_sha|ref_sha) 拼接
  "capabilities": { "...见 §3.2..." }
}
```

```jsonc
// manifest.jsonl 一行
{"item_key":"BAC009S0002W0122",
 "audio":{"path":".../BAC009S0002W0122.wav","sha256":"...","bytes":131884,"duration_ms":4120,"sr":16000,"ch":1},
 "ref":{"verbatim":"而 对 楼市 成交 抑制 作用 最 大 的 限 购","sha256":"...","itn":null},
 "segments":[{"s":0,"e":4120,"spk":"S0002","text":"而对楼市成交抑制作用最大的限购","skip":false,"overlap":false}],
 "speakers":{"S0002":{"gender":"M"}},
 "buckets":{"dialect":"mandarin","scene":"read"},
 "lang":"zh"}
```

**用途**：版本 diff、跨环境重建、审计"这个数字是哪份题算出来的"。

### 2.6 增量与重复导入

| 场景 | 行为 |
|---|---|
| 同 `(dataset, version)` 再导入 | `409`，提示换版本号或加 `?replace=true` |
| 同一 dataset 追加目录 | 生成 `v1.1`，`diff` 展示增/删/改 |
| 导入中断 | 支持 `resume`（按 `item_key` 断点） |
| 删除版本 | 若被 Benchmark 引用 → 拒绝，需先删 Run 或标记 `archived` |

### 2.7 新增数据集的成本

```
app/importers/
├── __init__.py          # 注册表
├── base.py              # RawItem / Seg / Importer 协议
├── aishell1.py  aishell3.py  kespeech.py
├── alimeeting.py  magicdata.py  datatang.py  srt_clip.py
```

新增一个 = 一个文件 + 一个 `register()`，约 80~150 行。核心代码零改动。

---

## 3. 能力怎么确认 / 可评指标怎么算出来

**核心答案：能力是"两侧的交集"，必须在开跑前显式算出来给用户看。**

```
可评指标 = f( 数据集能力 , 设备能力 , 口径开关 )
```

任何一侧缺失 → `N/A` + 原因码，**不是 0 分，也不是悄悄不显示**。

### 3.1 设备能力：声明 + 观测

`capabilities.punctuation` **不可信**（见 §4.1 实测：声明 `false`，实际 100% 带标点）。所以分两层：

```jsonc
// deployment.capabilities
{
  "declared": {                       // init 响应里的 capabilities
    "streaming": true, "vad": true, "itn": true,
    "punctuation": false,             // ← 实测不可信，仅记录
    "hotwords": {"supported": true, "max_count": 150},
    "languages": ["zh","en","yue", ...]
  },
  "observed": {                       // 首次接入时用固定探测音频实跑一次
    "segments": true,                 // 输出是否带 start/end
    "speakers": true,                 // 是否带 spk
    "punctuation_output": true,       // 输出是否含标点（关键）
    "naming": true,                   // 是否回显 matched_id
    "usage_field": true,              // 是否返回 usage.inference_ms
    "max_audio_ok_sec": 1800,         // 实测可跑多长
    "probed_at": "2026-11-02T...",
    "probe_version": "probe-1"
  },
  "slots": 3
}
```

**探测怎么做**：内置两段固定素材（随平台发布）

| 素材 | 内容 | 验证 |
|---|---|---|
| `probe_single_10s.wav` | 单说话人，参考含 `，。？` | 标点输出、usage 字段、TTFT |
| `probe_two_spk_20s.wav` | 2 人，约 8% 重叠 | `spk` / `global_spk_list` / DER 可行性 |
| `probe_vp_A.wav` / `probe_vp_B.wav` | 两人注册音频 | `matched_id` 回显 |

触发时机：设备首次接入、`deployment.uid` 变化、手动"重新探测"。

### 3.2 数据集能力：导入时算出来

```python
@dataclass
class DatasetCaps:
    has_text: bool
    text_ratio: float          # 有参考文本的条目占比
    punct_any_ratio: float     # 参考含任意标点的条目占比
    punct_sentence_ratio: float# 分段以 。！？ 结尾的比例（标点可评性依据）
    itn_ratio: float           # 有 itn_expected 的占比
    segment_ratio: float       # 有时间戳分段的占比
    speaker_ratio: float       # 分段里带说话人的占比
    overlap_ratio: float       # 参考中重叠语音时长 / 总语音时长
    duration_p50_ms: int
    duration_max_ms: int
    speaker_count_max: int
    langs: list[str]
    distinct_speakers: int
```

实测值（本机数据集）：

| 数据集 | punct_ratio | segment_ratio | speaker_ratio | overlap | 结论 |
|---|---|---|---|---|---|
| AISHELL-1 | 0% | 0%（只补整段） | 0% | — | 只有 CER |
| AISHELL-3 | 0% | 0% | 0% | — | 只有 CER |
| KeSpeech | 0% | 0% | 0% | — | 只有 CER |
| AliMeeting | 100% | 100% | 100% | 16.2% | CER+标点+DER+命名 |
| MagicData 沪语 | 100% | 100% | 100% | 有 | 全指标 |
| MagicData 湘语 | 100% | 100% | 100% | 有 | 全指标 |
| APY 粤语 | 90% | 100% | 100% | 有 | 全指标（需 ≥ 阈值） |
| APY 四川话 | **0%** | 100% | 100% | 有 | 标点 N/A |
| 客家话 | 视文稿 | 视文稿 | 0% | — | 冒烟 |

> 注意 **APY 四川话**：有分段有说话人，但**参考完全无标点** → 标点指标必须 N/A。这就是为什么能力要按数据集算，不能按"数据集类型"拍脑袋。

### 3.3 指标可用性规则表（可直接编码）

```python
RULES = {
  "cer":       [("dataset.text_ratio", 0.95), ("device", "any")],
  "wer":       [("dataset.text_ratio", 0.95), ("dataset.lang", "en")],
  "punct_f1":  [("dataset.punct_supported", True), ("device.observed.punctuation_output", True)],
  "itn_acc":   [("dataset.itn_ratio", 0.95),   ("device.declared.itn", True)],
  "der":       [("dataset.segment_ratio", 0.95), ("dataset.speaker_ratio", 0.95),
                ("device.observed.speakers", True)],
  "sa_der":    [("dataset.segment_ratio", 0.95), ("dataset.speaker_ratio", 0.95),
                ("device.observed.naming", True)],
  "naming_acc":[("dataset.segment_ratio", 0.95), ("dataset.speaker_ratio", 0.95),
                ("device.observed.naming", True), ("benchmark.enrollment", "complete")],
  "speaker_eer":[("dataset.segment_ratio", 0.95), ("dataset.speaker_ratio", 0.95),
                 ("device.observed.naming", True), ("benchmark.enrollment", "complete")],
  "rtf_dev":[("device.observed.usage_field", True)],
  "ttft_ms":   [("device.declared.streaming", True)],
  "npu_aicore":[("device.kind", "etsstt")],
}
```

`0.95` 是默认阈值（可配）。低于阈值但仍 > 0 → 状态 `partial`，报告里标注覆盖率，用户自行决定。

### 3.4 解析器与预览 API

```http
POST /api/v1/runs/preview
{ "benchmark_id": "...", "deployment_ids": ["...", "..."] }
```

```jsonc
{
  "benchmark": { "version_hash": "ab12...", "item_count": 10000,
                 "total_duration_ms": 41200000 },
  "targets": [
    {
      "deployment_id": "d1",
      "label": "网关A · 502 · w8a8",
      "eta_ms": 5340000,
      "metrics": [
        {"name":"cer",   "status":"ok"},
        {"name":"punct_f1","status":"ok"},
        {"name":"der",   "status":"ok"},
        {"name":"naming_acc","status":"blocked",
         "reason":"enrollment_missing", "detail":"缺 4 个说话人的注册音频"},
        {"name":"itn_acc","status":"n/a","reason":"no_itn_reference"},
        {"name":"ttft_ms","status":"ok"}
      ],
      "warnings": ["该设备 3 路并发，ETA 按 3 路估算"]
    }
  ]
}
```

**UI 呈现**：这就是"开跑前的指标矩阵"。

```
┌ 网关A · 502 · w8a8 ────────────────────────────── ETA 1h29m ─┐
│ ✔ CER   ✔ 标点 F1   ✔ DER   ⚠ 命名(缺声纹 4/4)   — ITN(无参考) │
│ [去准备声纹 →]                                                │
└──────────────────────────────────────────────────────────────┘
```

### 3.5 为什么不是"导入后自动知道"

因为可评性同时取决于：
- 这份**参考**有没有标点（数据集侧）
- 这台**设备**输出有没有标点（设备侧）
- 这次**口径**有没有开关（配置侧）

三者任一不满足，指标就是 `N/A`。所以必须做成"开跑前解析 + 显式展示"，而不是偷偷算或者静默跳过。

---

## 4. 标点与真实输出：实测结论

### 4.1 已确证（基于本机 400 条真实产物）

```
样本 400 条，含标点 400 条（100%）
标点分布 {'。':13392, '，':17850, '？':3812, '！':1161, '、':1019, '；':79, '：':50}
```

而 `init` 响应里报的是：

```json
"capabilities": {"streaming": true, "vad": true, "itn": true, "punctuation": false}
```

**结论**：
1. 设备**确实输出标点**，标点指标完全可评；
2. 但 `capabilities.punctuation` **不可作为门控依据**——必须用 `observed.punctuation_output`（实跑一次看输出）；
3. 参考侧也不是都有标点（AISHELL / KeSpeech / 四川话 APY 都没有），所以数据侧也要独立判定。

这三条直接决定了 §3.1/§3.2 的双层能力设计。

### 4.2 已执行（2026-11-02，`192.168.65.181` / platform 205 / model `etsstt`）

> 历史备注（2026-11-10 已废弃）：旧方案通过 `prepare` 修改 `SSFConfig.yaml` 把 8701 改为 `0.0.0.0`。当前平台**不再修改设备文件**，改用 SSH 本地转发访问设备回环 8701（M2 §5.3.2）。

| # | 待验证 | 结果 |
|---|---|---|
| 1 | 分离后 `sentence.spk` / `global_spk_list` 形状 | ⚠️ **`sentences[].spk` 不存在**；说话人只在 `global_spk_list` |
| 2 | 传声纹后 `matched_id` 回显 | ✅ 回显，且带 `confidence` |
| 3 | `usage.inference_ms` 与 wall 差 | ✅ 可信；`total_cost_ms ≈ wall`，比 `inference_ms` 高约 25% |
| 4 | 长音频 | ✅ 120s 一次跑通（RTF 0.37）；30min 未测 |
| 5 | 网关容器可写 | 未测（本机是 205，非 502） |

**关键实测数据**

```
单句 4.2s：inference 1024ms → rtf_dev 0.244；wall 1284ms → rtf_e2e 0.306
120s 4人：inference 44779ms → rtf_dev 0.373；wall 46227ms → rtf_e2e 0.385

标点：400/400 条输出含标点（声明 punctuation=true，本次一致）
分离：global_spk_list 长度 79 = 78 句 + 1 个句末标记；取值 [0,1,2,3]
声纹：192 维，提取耗时 0.54~0.98s/条
命名：4 簇命中 SPK8005/6/7/8，confidence 0.70 / 0.69 / 0.62 / 0.55
      第 5 簇未匹配（19 句，混了 4 个人）→ 拒识率 25.4%
近似 DER（collar 0.25 单侧、排除重叠帧）= 8.58%
CER（同 120s 窗口、去标点）= 30.6%
```

### 4.3 协议实现必须处理的 6 个坑（不处理会算错）

| # | 坑 | 现象 | 正确处理 |
|---|---|---|---|
| 1 | **`transcript.final.text` 不是转写结果** | 实测 `final.text == "。"`，`final.sentences == [{text:"。",start:3775,end:3775}]`（句末标记） | 转写文本 = **累加所有 `transcript.segment.sentences`**；`final` 只用来取 `global_spk_list` / `spk_voiceprints` / `usage` |
| 2 | **`sentences[].spk` 可能不存在** | 78 句全部没有 `spk` 字段 | 过程中的 `spk` 会实时变化，不参与最终映射；最终说话人**只**取 `final.global_spk_list` 按句位置对齐 |
| 3 | **`global_spk_list` 长度 = 句数 + 1** | 78 句 → 79 个元素 | 前 N 个对应 N 个真实句子；第 N 个是句末标记。对齐时**不要**用 `list[-N:]` |
| 4 | **`streaming=false` 时不发 partial** | 本机 `capabilities.streaming=false`，无 `transcript.partial` | `ttft_ms` 记 `N/A`；`finalize_ms` 仅在 1x 发送模式下有意义（burst 模式下等于积压排空时间） |
| 5 | **`usage.rtf` ≠ `inference_ms/duration`** | 0.3753 vs 0.3732 | 自己算：`rtf_dev = inference_ms / audio_duration_ms`；`rtf_e2e = (end→final)/duration` |
| 6 | **RTF 随音频变长而升高** | 4.2s→0.244，120s→0.373 | 报表**按时长桶**呈现，禁止只报一个总 RTF |

### 4.4 命名评测的真实形态（据此定指标）

实测簇→参考说话人混淆矩阵：

```
簇  matched_id  conf    参考分布
0   SPK8005     0.701   {SPK8005: 22}                    ← 纯
1   None         —       {SPK8005:10, 8006:4, 8008:3, 8007:1}  ← 陌生人桶，混了4人
2   SPK8008     0.553   {SPK8008: 8, SPK8007: 2, SPK8005: 2}
3   SPK8007     0.618   {SPK8007: 7}                     ← 纯
4   SPK8006     0.694   {SPK8006: 12}                    ← 纯
```

结论：
1. **命中即高精度**（3/4 簇纯度 100%），错认率仅 4.2%
2. **拒识率 25.4%**，全部落在"陌生人桶"，而桶里混了 4 个人 → 说明引擎有 low-confidence 合并行为
3. 因此**单一命名准确率没有意义**，必须出 `FA / FR / 阈值曲线`；`speaker_match_threshold` 必须进 MetricProfile
4. 同一个 session 跑 `clustering` 与 `matching` 得到 **4 簇 vs 5 簇**，两种模式的 DER 不可混比 → 模式也要进口径

### 4.5 探测脚本（已生成，可直接跑）

```bash
# 基础：30ms、1× 实时探测分离 + 标点 + usage
cd backend
uv run python ../tools/probe_etsstt.py \
  --url ws://192.168.133.2:30025/v1/audio/transcription/realtime \
  --model qwen3-asr-1.7b \
  --audio "/mnt/f/测试数据/测试文件/AI测试素材/开源数据集/alimeeting/Test_Ali/Test_Ali_far/audio_dir/R8002_M8002_MS802.wav" \
  --diarize --max-spks 4 --mode stream --chunk-ms 30 --input-rate 1

# 如需验证声纹匹配，追加 --voiceprints；etsSTT 还需配置 SSH 暂存：
# 205 的 8701 只监听设备回环地址，先按已核验的设备指纹建立本机 SSH 转发到 18701。
uv run python ../tools/probe_etsstt.py \
  --url ws://127.0.0.1:18701/v1/audio/transcription/realtime \
  --model etsstt --voiceprints --kind etsstt --platform 205 \
  --ssh-host 192.168.65.181 --ssh-user etsme
```

脚本直接打印 JSON 摘要，包含消息类型计数、`final_text`、`global_spk_list`、带时间戳的识别分段、能力观测、usage、按公式计算的 `rtf_dev`、TTFT / finalize 和安全匹配摘要。`--save PATH` 可选保存相同结果；摘要不包含声纹向量。

### 4.6 网关容器可写性检查（一条命令）

```bash
ssh ictuser@192.168.133.2 \
  'sudo -n kubectl exec -n ns-meagt deploy/etsllm -- sh -c "mount | grep -E \" /data|/opt/etsme|/tmp\""'
```

期望看到 `hostPath` 挂载。若有 → 用宿主路径推送；若只有 `/tmp` → 走 stdin 直写。

---

## 5. 评测怎么进行

### 5.1 用户看到的完整序列

```
① 选 Benchmark（数据集版本 + 口径 + 参数）
        ↓
② 勾选设备（可多选，跨形态）
        ↓
③ 【自动】能力预览     ← 3.4 的 preview
        显示：每台设备能评什么、不能评什么、为什么、ETA、要不要准备声纹
        ↓
④ 点「开始评测」
        ↓
⑤ 状态机自动流转，无需人工干预
        probing(3~10s) → preparing(声纹, 可选) → running → scoring → completed
        ↓
⑥ 完成即出结果（自动跳转结果页）
```

### 5.2 状态机与产物

| 阶段 | 做什么 | 失败处理 | 产物 |
|---|---|---|---|
| `probing` | 并行探测所有 deployment，冻结 fingerprint | 单台失败 → 该 target `failed`，其余继续 | `deployment` 行 |
| `preparing` | 仅当有命名/SA-DER：确保注册音频向量齐备 | 单台容器不可写 → 该 target 命名 `N/A`，不阻塞 | `voiceprint` 行 |
| `running` | 分片推理，落 `case_result` + `hypothesis` | 单条失败重试 2 次；连续 5 条熔断该 target | `case_result` |
| `scoring` | 按 profile 逐 case 打分 + 聚合 + bootstrap | 单条打分异常 → 记 `score_error`，不中断 | `case_result.metrics`、`metric_value` |
| `completed` / `partial` | 生成报告 | `partial` = 有失败但覆盖 ≥ 90% | 报告快照 |

**"转写结束直接出结果吗"** → 是。`scoring` 是自动的，1 万条约 30~60s，用户只看到"打分中"一闪而过。

### 5.3 指标是自动还是手动

**默认全自动**，理由：打分是离线纯函数，几乎零成本（1 万条 < 60s），没必要让用户选。

但有三个例外，必须显式：

| 例外 | 原因 | 交互 |
|---|---|---|
| **命名 / SA-DER / EER** | 需要声纹注册，有额外成本（推送 + 提取，约 8s/人/设备） | 预览阶段提示「缺 4 个声纹」，给「去准备」入口 |
| **ITN** | 依赖参考有 ITN 对照 | 无参考 → 自动 `N/A`，不给开关 |
| **快速冒烟** | 只想看 CER | `POST /runs` 可传 `"metrics": ["cer"]` |

**展示层面**可勾选：结果页支持隐藏某些指标列，但不影响计算。

### 5.4 API 序列（可直接对接）

```http
# 1. 预览（前端在勾选设备后自动调用，debounce 300ms）
POST /api/v1/runs/preview
{ "benchmark_id": "b1", "deployment_ids": ["d1","d2"] }

# 2. 创建
POST /api/v1/runs
{ "name": "网关回归 11-02",
  "benchmark_id": "b1",
  "deployment_ids": ["d1","d2"],
  "metrics": null,                  // null=全部可用；或 ["cer","der"]
  "campaign_id": null }
→ { "run_id": "r1", "status": "created" }

# 3. 订阅进度
GET /api/v1/runs/r1/events        (text/event-stream)
event: phase   data: {"phase":"running","progress":0.31,"eta_ms":3200000}
event: target  data: {"deployment_id":"d1","done":3120,"failed":2,"total":10000}
event: phase   data: {"phase":"completed"}

# 4. 结果
GET /api/v1/runs/r1/matrix
GET /api/v1/runs/r1/buckets
GET /api/v1/runs/r1/cases?status=failed&cursor=...
```

### 5.5 结果页的"三档"呈现

```
┌ 结论条 ──────────────────────────────────────────────┐
│ CER 6.82% (n=9,982)  ↓0.42pp*   DER 12.3% (n=20)      │
│ 标点 F1 0.79         ITN —     命名 Acc 88.1%          │
└───────────────────────────────────────────────────────┘
  ↑ 有数字的点开看：样本量 / 口径版本 / 置信区间 / 显著性
  ↑ N/A 的点开看：原因码 + 一句话解释 + 怎么才能评

┌ 分桶矩阵 ─────────────────────────────────────────────┐
│ 方言桶 / 时长桶 / 重叠桶 … 每桶都有 n 与 CI            │
└───────────────────────────────────────────────────────┘

┌ Badcase ─────────────────────────────────────────────┐
│ 可按指标筛选（CER 高的 / DER 混的 / 命名错的）          │
└───────────────────────────────────────────────────────┘
```

### 5.6 ETA 的算法（用户最关心"要跑多久"）

```
每个 target：
  T_infer = Σ(item.duration × rtf_estimate) / slots
  T_fixed = probe(5s) + voiceprint(n_spk × 8s，仅首次) + scoring(30s) + 报告(5s)
  ETA     = T_infer + T_fixed

rtf_estimate 优先级：
  1. 该 deployment 的历史 median rtf_dev（同 stream_mode、同并发档）
  2. 同 model_key 的全局 median
  3. 保守默认 0.25
```

跑起来后用实测值滚动修正，SSE 每 5s 推一次更新后的 ETA。

---

### 5.7 流式与非流式：两套时延指标（不可混比）

**实测事实**

| 部署 | `capabilities.streaming` | init 传 `streaming:true` | partial | 首个 segment |
|---|---|---|---|---|
| `192.168.20.36` / `asr-b`（RK3588 w8a8） | **true** | — | ✅ 有（1260s 音频 14 条） | — |
| `192.168.65.181` / `etsstt`（205） | **false** | 传了被忽略 | ❌ 0 条 | **19.3s**（30s 音频） |

**结论（修正用户描述）**

1. 不是"两个模型"，是**同一服务的两种工作模式**，由 `init.streaming` 切换；
2. 能否真正流式，取决于**该设备部署的模型构建**是否具备 streaming 能力（`capabilities.streaming`）；
3. capability 为 false 时传 `streaming:true` 无效，服务端直接忽略（实测无 partial、无报错）；
4. **partial 粒度 ≈ 客户端发送块大小**：旧数据用 30s 块，partial 覆盖区间正是 `[0,30000]`、`[30000,60000]`…；因此测 TTFT 必须用小分片，否则 TTFT 下限被分片大小吃掉。

**模式判定（Run 启动时自动）**

```python
mode = "streaming" if any(non_empty_partial) else "batch"
# 不能只信 capabilities.streaming：声明 true 但实际不出 partial，仍判 batch
```

**流式指标**

| 指标 | 定义 | 聚合 |
|---|---|---|
| `ttft_ms` | `t(首个非空 partial) − t(首块音频发出)` | median / P95 |
| `ttft_init_ms` | `t(首个非空 partial) − t(init 响应)` | 辅助 |
| `partial_interval_ms` | 相邻 partial 到达间隔 | median / P95 |
| `partial_lag_ms` | 每条 partial：`已发送音频时长 − partial.end_ms` | median / P95 / max |
| `partial_coverage` | `Σ(partial 覆盖时长) / 音频总时长` | 每 case |
| `partial_stability` | `1 − 被 covered_until 作废的 partial 数 / 总 partial 数` | 每 run |
| `first_segment_ms` | `t(首个 segment) − t(首块音频)` | median |
| `finalize_ms` | `t(final) − t(最后一块音频发出)` | median / P95 |
| `rtf_dev`（流式） | `usage.inference_ms / usage.audio_duration_ms` | median / P95 |

**非流式指标**

| 指标 | 定义 | 聚合 |
|---|---|---|
| `ttfa_ms` | `t(首个 segment) − t(首块音频)` ← 非流式唯一的"首个输出"信号 | median / P95 |
| `rtf_dev`（非流式） | `usage.inference_ms / usage.audio_duration_ms` | median / P95 |
| `rtf_e2e` | `(t_final − t_last_chunk) / duration`（仅 burst） | median |
| `finalize_ms` | `t(final) − t(最后一块音频)` | median / P95 |

**五条硬规则**

1. 流式与非流式的时延指标**必须分列，禁止合并成一个数**；`rtf_dev` 可同名但必须标 `mode`，缺失方标 `N/A(device_not_streaming)`。
2. `ttft_ms` 只在 `mode=streaming` 时存在；batch 下**不得**用 wall 或 ttfa 冒充。
3. 流式测量必须 `stream_input_rate=1.0`（1x）；burst 只测吞吐，不测 TTFT。
4. 测 TTFT 用 `chunk ≤ 100ms`，**业务真实口径 30ms**；测吞吐用大块（30s）。实测 1s 分片会把 TTFT 从 0.976s 压成 0.076s 的假值。
5. 准确率（CER/标点/ITN/DER/命名）**只用最终拼接文本**；partial 仅用于时延与稳定性。

**为什么必须分开（实测）**

- 非流式 30s 音频：首个 segment 在 **19.3s** 才出现，用户已等掉 2/3 时长；
- 流式 1260s 音频：首条 partial 在 **2.1s**。

若把两者放进同一列"首个输出时间"，非流式必然被吊打，但这个差距**不代表识别质量**。

**分片大小对 TTFT 的影响（实测，502 网关 / `qwen3-asr-1.7b`，10s 音频，1x 发送）**

| chunk | partial 数 | `ttft` | partial 间隔 median | 说明 |
|---|---|---|---|---|
| 20ms | 15 | 0.980s | 659ms | ✅ |
| **30ms** | 15 | **0.976s** | 649ms | ✅ **业务真实分片（口径基准）** |
| 100ms | 15 | **0.962s** | 611ms | ✅ 可用 |
| 200ms | 15 | 0.656s | 605ms | ⚠️ 偏低，需固定 |
| 1s | 10 | **0.076s** | 1019ms | ❌ **假值**：整秒一次性送达 |
| 10s | 1 | 0.963s | — | ❌ 只剩 1 条 partial |

**结论**：
1. `chunk ≤ 100ms` 时 TTFT 稳定在 **0.96~0.98s**，可复现；
2. `chunk=1s` 会测出 76ms 的**假首字延迟**（比真实值低一个数量级）；
3. partial 间隔由**服务端内部步长**（~600ms）决定，不随分片继续变小；
4. 因此 perf 协议必须固定 `chunk_ms = 20`，并写进 Run 快照。

**能力探测可行性（实测）**

| 设备 | `init` 连续 3 次 `declared.streaming` | 实际 partial | 一致性 |
|---|---|---|---|
| `192.168.65.181` / `etsstt` | false / false / false | 0 条 | ✅ |
| `192.168.133.2` / `qwen3-asr-1.7b` | true / true / true | 有（15 条/10s） | ✅ |

→ **`init` 返回的 `capabilities.streaming` 是稳定且可信的**，可直接用于设备管理探测（不同于 `punctuation`，那个不可信）。但仍建议记录 `observed`，因为成本只有一次 10s 探测。

**`usage.rtf` 到底是什么（9169 条真实样本拟合）**

**结论：`usage.rtf = inference_ms / 有效语音时长`，不是 `inference_ms / 音频总时长`。**

| 样本 | 音频 | `inference_ms` | `usage.rtf` | `inf/rtf`（隐含分母） | 句时长合计 |
|---|---|---|---|---|---|
| `14a4dbf0` | 1260s | 265s | 0.789 | **336s** | 276 句 × ~1.2s ≈ 331s |
| `ffbeaa28` | 1269s | 437s | 0.707 | **618s** | 509 句 ≈ 611s |

同期客户端实测 span：`14a4dbf0 = 269s`（≈ `inference_ms` 265s），证明 **`inference_ms` 才是真实处理时间**，`usage.rtf` 只是把分母换成了"语音时长"。

全量统计（9169 条）：

```
usage.rtf ÷ (inference_ms / audio_ms)   中位数 1.11 · P90 1.72 · 最大 11.03
>2 倍的样本 535 条（5.8%），全部是长音频 / 低语音占比
```

另外 `query.status.avg_rtf` 是**正确**的（实测 0.625 vs `inf/dur` 0.601），与文档定义"处理时长 / 音频时长"一致。

**平台口径固定为**

| 指标 | 公式 | 用途 |
|---|---|---|
| `rtf_e2e`（主） | `(首块音频 → final) / audio_duration` | 跨设备横评，端到端可复现 |
| `rtf_dev`（辅） | `inference_ms / audio_duration` | 隔离设备算力 |
| ~~`usage.rtf`~~ | — | **禁用**：分母是语音时长 |
| `rtf_speech`（可选） | `inference_ms / Σ句时长` | 只在"纯算力"分析时用，必须标注口径 |

> 附带事实：`usage` 整个字段**未在 Apifox 接口文档中定义**（同源导出的 `etsstt实时转写接口文档.txt` 没有 usage 章节），属于服务端实现细节。凡是文档没定义的字段，一律不进对外口径。

### 5.8 流式指标必须独立成任务（`run.kind = perf`）

**为什么不能和准确率评测混在一次 Run 里**

| # | 原因 | 量化 |
|---|---|---|
| 1 | 1x 发送意味着**墙上时间 = 音频时长** | 1 万条 × 平均 5s ≈ 14 小时，还没算处理 |
| 2 | 需要按时长分层抽样 + 多次重复 + 多并发档 | 组合爆炸，必须控样本量 |
| 3 | 需要**设备独占**，否则并发干扰使数字不可信 | 与准确率 Run 的调度目标直接冲突 |
| 4 | 准确率要吞吐（burst），时延要实时（1x） | 一次 Run 不可能两种节奏 |
| 5 | 聚合方式不同 | CER 用 micro；时延用 median/P95 |

**任务类型**

```
run.kind = accuracy | perf
```

| 维度 | `accuracy` | `perf` |
|---|---|---|
| 发送节奏 | burst（尽可能快） | **1x 实时** |
| 分片 | 30s（吞吐优先） | **30ms**（业务真实） |
| 采样 | 全量或按桶 | **按时长分层，每桶 N 条** |
| 重复 | 1 | **≥3，取 median** |
| 并发 | 按 slot 尽量满 | **固定档位 1..slots 扫描** |
| 指标 | CER/标点/ITN/DER/命名 | ttft/partial_interval/lag/finalize/rtf/成功率 |
| 设备占用 | 可排队共享 | **独占**（运行期间不接受其他 Run） |
| 结果复用 | 可复用 hypothesis | **不可复用**（节奏不同，结果不同） |

**perf Run 执行协议**

```
1. 预热：每个并发档先跑 1 条并丢弃（避免首包受模型加载影响）
2. 分层抽样：按时长桶各取 N 条（默认 N=30）
      <5s / 5~15s / 15~60s / 1~5min / >5min
3. 逐档执行：concurrency ∈ {1..slots}
      每档开 concurrency 条独立 WS 连接，各自 init、各自发送
4. 重复 repeat 次（默认 3），每档取 median 与 P95
5. 产出「并发-时延-吞吐-成功率」四联表
```

**时长预算**

```
T ≈ Σ(样本音频时长) × repeat × 并发档数 / 平均并发度
例：120 条 × 平均 8s × 3 次 × 3 档 / 2 ≈ 72 分钟
```

**缓存键必须带节奏参数**

```
cache_key = sha256(deployment_uid|content_uid|params_hash|mode|pace|chunk_ms|concurrency)
```

否则会把 burst 结果当成 1x 结果复用，数字直接错。

**报告呈现**

```
并发度   ttft(med/P95)   partial间隔   rtf_dev   成功率   吞吐
1        0.98s / 1.12s   659ms         0.120        100%     1.00x
2        1.35s / 1.88s   702ms         0.168        100%     1.83x
3        1.92s / 2.61s   811ms         0.236         98%     2.41x
```

> 这张表才是"这台设备能不能扛 3 路"的答案；单路数字回答不了。

---

### 5.9 `rtf_e2e` / `rtf_dev` 的定义、合理性与边界

**精确定义（时间点必须一致）**

```
t0 = 首块音频发出
t1 = 收到 transcript.final
D  = usage.audio_duration_ms        （音频总时长）

rtf_dev  = usage.inference_ms / D
rtf_e2e  = (t1 − t0) / D
overhead = (usage.total_cost_ms − usage.inference_ms) / D
```

**各自包含什么**

| 指标 | 包含 | 不包含 |
|---|---|---|
| `rtf_dev` | 设备侧前处理 + 模型推理 + 后处理（VAD/标点/ITN/分离，视开关） | 网络、客户端节奏、排队 |
| `rtf_e2e` | 网络 + 排队 + 设备处理 + 客户端发送节奏 | — |
| `overhead` | 前后处理 + 排队 | 模型推理 |

**有效性条件（关键）**

| 指标 | 有效条件 | 失效条件 |
|---|---|---|
| `rtf_dev` | 已 warmup；并发固定；功能开关固定 | 冷启动；并发变化；开关变化 |
| `rtf_e2e` | **仅 burst 发送** | **1x 发送时恒 ≥ 1，无区分度** |
| `overhead` | 仅 burst | 1x 时被 pacing 淹没 |

**实测证据（同一台 205 / `etsstt`）**

| 场景 | 音频 | pace | `rtf_dev` | `rtf_e2e` | 说明 |
|---|---|---|---|---|---|
| 单句 | 4.2s | burst | 0.244 | 0.306 | 固定开销 ~258ms，占 6% |
| 会议·无分离 | 120s | burst | 0.272 | 0.278 | 开销 0.7% |
| 会议·有分离 | 120s | burst | **0.384** | 0.390 | **分离 +41%** |
| 30s | 30s | 1x | 0.267 | **1.034** | 被 pacing 钳住 |
| 网关 10s | 10s | 1x | 0.120 | **1.150** | 被 pacing 钳住 |

**两条结论**

1. **`rtf_e2e` 在 1x 下没有区分度**：设备 `rtf_dev` 从 0.120 到 0.267 差 2.2 倍，`rtf_e2e` 都是 1.03~1.15。因为发送本身就要花掉 D 秒，端到端不可能小于 1。→ **1x 模式禁止用 `rtf_e2e` 做横评**，改用 `ttft` / `lag_max` / `tail` / `rtf_dev`。
2. **`rtf_dev` 不是"模型性能"，是"部署计算性能"**：它随功能开关变化巨大（分离开关 +41%），随长度变化（4.2s 0.244 → 120s 0.373），随并发变化（待测）。所以必须声明"在什么开关、什么并发下的 rtf_dev"。

**分解模型（burst）**

```
wall ≈ T_queue + T_pre + T_infer + T_post + T_net
rtf_dev  = T_infer / D
rtf_e2e  = wall / D
overhead = (wall − T_infer) / D
```

短音频 `overhead` 占比高（4.2s 时 6%），长音频趋近 0.6~0.7% → **必须分时长桶**。

**应该怎么用（回答三类问题）**

| 问题 | 用哪个 | 条件 |
|---|---|---|
| 这个模型在这台设备上算得快不快 | `rtf_dev` | 并发=1、warmup、功能开关写明 |
| 用户实际等多久 | `ttft`（流式）/ `ttfa`（非流式）+ `tail` | 1x |
| 能不能扛 3 路 | `throughput`（audio-s/s）+ `rtf_dev@c=3` + 成功率 | burst + 并发扫描 |
| 量化/编译有没有收益 | 同设备 `rtf_dev` 不同 deployment 对比 | 同开关、同并发 |
| 硬件差异 | 同 model_key 跨设备 `rtf_dev` 对比 | 同开关、同并发 |

**命名建议（避免"模型性能"误导）**

| 现名 | 建议 | 原因 |
|---|---|---|
| `rtf_dev` | `rtf_compute` | 强调是设备计算耗时比，不是模型理论性能 |
| `rtf_e2e` | `rtf_wall` + 标注 `pace=burst` | 1x 下无意义 |
| — | `rtf_realtime` = `tail / D` | 1x 下的"跟播能力" |

> 一句话：**`rtf_dev` 回答"设备算得多快"，`rtf_e2e` 回答"端到端多久"；只有 burst 下两者可比，1x 下只能看 ttft/tail/lag。**

---

## 附录 A · 指标计算规格

### A.1 CER

```
输入：ref.verbatim → normalize(profile) → ref_norm
      hyp.text     → normalize(profile) → hyp_norm
      （AliMeeting: join="concat", separator="<sc>", order="by_start"）

计算：ops = rapidfuzz.distance.Levenshtein.editops(ref_norm, hyp_norm)
      S = count(replace), D = count(delete), I = count(insert)
      cer = (S+D+I) / len(ref_norm)

边界：
  len(ref_norm)==0 且 len(hyp)==0 → 跳过（不计入分母）
  len(ref_norm)==0 且 len(hyp)>0  → D 记 len(hyp)，n=0，不参与 micro
  失败/超时                        → 不计入分母，单独计 failure_rate

聚合：micro = Σ(S+D+I) / Σlen(ref_norm)     ← 主口径
      macro = mean(item cer)
```

### A.2 标点 F1

```
1. 先做字序列对齐（去掉标点后的 ref/hyp），得到 ref→hyp 的字符级映射
2. 对每个参考标点位置 p（字符索引 + 类型），检查 hyp 在映射后的同位置是否有标点
3. 位置对且类型对 → TP；参考有 hyp 无 → FN；hyp 有参考无 → FP
4. P = TP/(TP+FP), R = TP/(TP+FN), F1 = 2PR/(P+R)

边界：
  ref 无标点 → N/A（不是 F1=0）
  连续标点按类型严格比对（。≠，）
  另报 sentence_end_acc：句末标点（。？！）单独准确率
```

### A.3 DER / SA-DER

```
输入：ref_segments（含 skip/overlap）→ 过滤 skip 区间
      hyp_segments（sentence.spk）
      uem = [0, audio_duration]
      collar = profile.der.collar_ms（语义见 §1.4）
      skip_overlap = profile.der.skip_overlap

计算：pyannote.metrics.DiarizationErrorRate(
        collar=collar_seconds, skip_overlap=skip_overlap,
        metric="diarization_error_rate", accumulate=False)
      → {der, false_alarm, missed_detection, confusion, total}

SA-DER：把 hyp 标签替换为 matched_id（未匹配 → __unknown_{label}），禁用最优映射

边界：
  ref_segments 为空 → N/A
  hyp 无 spk → N/A（reason=device_no_diarization）
  overlap 占比 > 0 时必须显式声明 skip_overlap
```

### A.4 命名准确率

```
输入：ref_segments[i].speaker（真实身份）
      hyp_segments[i].spk → spk_voiceprints[spk].match_info.matched_id

sentence_acc = #{matched_id == ref.speaker} / #{hyp 有 spk 的句}
cluster_acc  = Σ(簇时长 × 该簇映射正确?) / Σ簇时长
FA = matched_id 存在但 ≠ ref.speaker      （错认）
FR = matched_id 为空                       （未识别）

EER：把 confidence 当分数，扫 threshold 0~1，
     算 (FA/miss 率, 正确接受率)，取交叉点
```

### A.5 性能

```
rtf_dev = usage.inference_ms / usage.audio_duration_ms
rtf_e2e   = (audio.end 发出 → transcript.final) / duration
ttft_ms    = (init 响应 → 首个非空 partial)
finalize_ms    = (最后一块音频 → transcript.final)

聚合：median + P95；按时长桶（<10s / 10~60s / 1~5min / >5min）
```

---

## 附录 B · 原因码（N/A 的唯一解释来源）

| code | 文案 | 触发 |
|---|---|---|
| `no_reference_text` | 该数据集没有参考文本 | `text_ratio < 0.95` |
| `no_sentence_punctuation` | 参考缺少句末标点（只有句中逗号不算） | `punct_supported=false` |
| `no_itn_reference` | 参考无 ITN 对照 | `itn_ratio < 0.95` |
| `no_segment_timeline` | 参考无时间戳分段 | `segment_ratio < 0.95` |
| `no_speaker_labels` | 参考无说话人标签 | `speaker_ratio < 0.95` |
| `device_no_speaker_output` | 设备输出不含说话人 | `observed.speakers = false` |
| `device_no_punctuation_output` | 设备输出不含标点 | `observed.punctuation_output = false` |
| `device_no_naming` | 设备未回显声纹匹配 | `observed.naming = false` |
| `enrollment_missing` | 缺说话人注册音频 | `benchmark.enrollment = incomplete` |
| `staging_unavailable` | 设备容器不可写，无法推送注册音频 | `Staging.prepare()` 三级全失败 |
| `audio_too_long` | 音频超过设备上限 | `max_audio_sec` |
| `unsupported_language` | 设备不支持该语言 | `languages` 不含 |
| `insufficient_coverage` | 有效样本不足 | `scored / planned < 阈值` |

前端规则：**任何 `N/A` 都必须能点开看到 code + 一句话 + 补救建议**，禁止只显示 `—`。

---

## 附录 C · 开工验收清单

按顺序做完，方案才算落地：

- [x] 2026-11-07：`tools/probe_etsstt.py --diarize` 在 205 真机跑通；探测文本句有 `spk`，final `global_spk_list` 长度为 8，另有一个末尾哨兵；未传声纹时标签均为 `0`，双人分离质量不通过。
- [x] 2026-11-07：`tools/probe_etsstt.py --diarize --voiceprints` 在 205 真机跑通；提取 A/B 均为 192 维，分别以 0.8394 / 0.6543 回显 `matched_id`，超过 0.5 门槛；SFTP 暂存文件已清理。
- [x] 2026-11-07：网关 `kubectl exec ... mount` 确认 `/mnt/voiceprint` 在 `/mnt` 的可写挂载上，etsllm 容器可见
- [x] 2026-11-07：md-eval / pyannote 在 AliMeeting 20 场对照，汇总与逐场最大差值均为 0.000000pp
- [x] 2026-11-07：AISHELL-1 隔离导入 7,176 条，版本 `ready`；能力汇总与 manifest 文件校验通过。耗时 593.49s，缓存状态未控，性能需与既有热缓存结果分开看
- [x] 2026-11-07：AliMeeting far 20 段隔离导入通过；`punct_sentence_ratio=1.0`、`overlap_ratio=0.162`，8 声道原音频、`mono_ch0` profile、manifest 均验证
- [x] 2026-11-08：分设备、单路完成 AISHELL-1 `test` 100 样本矩阵，样本 ID 与顺序一致：205 `etsstt`、502 `qwen3-asr-1.7b`、600 `asr-b` 均 100/100；三台设备的准确率与时延数据见下表。此项验收各设备单路识别；跨设备并行时 502 曾返回 `engine_overloaded`，并发稳定性仍未通过验收。
- [x] 2026-11-07：502 网关 1/2/3 路并发曲线完成，30ms / 1× / 每档 3 次重复，9/9 测量会话与 6/6 预热会话成功

### 2026-11-07 设备协议观测

用内置 `assets/probe/probe_two_spk_10s.wav` 对 502 网关 `qwen3-asr-1.7b` 只读执行 WebSocket 分离探测：收到了 9 个 `transcript.segment` 句子，句子字段只有 `start/end/text`；final 有 9 个 `global_spk_list` 标签，全部为 `0`，无句级 `spk`，final 的 `sentences` 为空。适配器支持标签数等于分段数或多一个末尾哨兵；本次报文落在前者。该结果证明标签字段可接收和对齐，但双人探测素材只得到一个簇，不能据此验收双人分离质量。205 设备 `192.168.65.181:8701` 当时拒绝 WebSocket 连接，未完成该设备探测。

同日后续通过已核验 SSH 指纹建立临时转发后，完成 205 `etsstt` 真机补测。设备 ASR 仅监听回环地址 `127.0.0.1:8701`，因此从本机转发到 `18701`。不带声纹的 `--diarize` 探测输出 8 个全局标签（均为 `0`），7 条有效带时间分段；双人素材仍聚成单簇，分离质量不通过。matching 探测得到标签 `[0, 0, 1, 1, 1, 0, 1, 1]`，并分别回显 `A=0.8394`、`B=0.6543`。205 与 502 的裸 `start/end` 均实测为整数毫秒；适配器现将整数和 `*_ms` 解析为毫秒、裸浮点解析为秒，并新增回归测试。

新增的 `tools/probe_etsstt.py` 使用 M2 适配器跑 30ms / 1× 流式探测，记录消息类型、`final.text`、`global_spk_list`、分段、usage、TTFT 和 finalize；匹配摘要主动剔除向量。205 真机实测 TTFT=1,055ms、finalize=1,312ms，A/B 均提取到 192 维。SFTP 暂存经 SHA-256 校验，结束后两条 `/data/voiceprint` 临时文件均确认不存在；设备没有有效 Run 租约，E2E 未写开发数据库。`rtf_dev` 按 `inference_ms / audio_duration_ms` 计算，不使用设备 `usage.rtf`。

### 2026-11-07 600 真机与 502 并发基准

600「宇视0007」经已核验的 root SSH key 建立临时本机隧道访问回环 ASR 服务，模型键为 `asr-b`。使用 AISHELL-1 测试集 `BAC009S0901W0288.wav`（11,107ms，SHA-256 `659db1cb6fb05c04e1c9ad003852e46087fab343ef16739115ca331ff5d57071`）跑流式分离探测：11 个 partial、3 个带时间戳分段、final 收尾成功；与同 ID 参考文本按项目 CER 归一口径比较为 CER=0.00（0/23）。TTFT=1,181ms、finalize=1,195ms、`rtf_dev=0.3183`（3,535/11,106ms）。随后通过 M10 真机 E2E 完成身份检查、A/B 声纹暂存/提取/匹配和真实 Run 评分；声纹均 192 维、置信度分别 0.8394 / 0.6543，Run CER=0、n=1、耗时 3,473ms，Playwright 11/11 通过。E2E 数据库和音频副本自动清理，开发库 `item` 数保持 1,160、活动租约为 0；临时 SSH agent、隧道、known_hosts 与远端 `/data/voiceprint` 两个样本文件均已清理。

同一条 AISHELL-1 样本的三设备冒烟结果：205 完整 Run CER=0（n=1，3,128ms），600 完整 Run CER=0（n=1，3,473ms）；502 的 `tools/probe_etsstt.py` 流式协议探测文本与参考一致，CER=0，TTFT=1,184ms、finalize=212ms、`rtf_dev=0.0471`。205/600 声纹自检均成功，502 此处仅做识别协议读数；这组单样本冒烟不替代尚未完成的多样本三设备矩阵。

### 2026-11-07–08 AISHELL-1 100 条真机矩阵

新增 `tools/bench_aishell_matrix.py`：从 F 盘 AISHELL-1 `test` split 仅读取音频和参考文本，不复制音频、不写数据库；205 使用 burst，502/600 使用 30ms 实时流，每台设备内部严格单路。首轮并行矩阵中 205 `etsstt` 和 600 `asr-b` 均完成 100/100，micro CER 均为 1.14%（16/1,403）；600 TTFT median/P95 为 1,141/1,222ms、finalize 为 1,629/2,189ms、`rtf_dev` 为 0.364/0.439。205 使用 burst 模式，不报告流式 TTFT/finalize。502 首轮 100 条均被设备以 `engine_overloaded` 拒绝；之后冷却并改为单设备、单路运行 100 条，曾成功 100/100，micro CER=1.43%（20/1,403），TTFT median/P95=1,125/1,231ms、finalize=966/1,437ms、`rtf_dev`=0.197/0.289。

2026-11-07 20:41 再执行三设备并行矩阵（相同 `test` split 100 条，205 burst、502/600 stream）。205 与 600 分别 100/100，micro CER 均为 1.14%（16/1,403）；205 `rtf_dev` median/P95=0.239/0.246，600 TTFT=1,148/1,239ms、finalize=1,638/2,167ms、`rtf_dev`=0.358/0.438。502 仅 22/100 成功，其余 78 条均收到 `engine_overloaded`；成功子集 CER=1.12%（3/267），样本不完整，不能作为准确率验收结果。此前 502 单设备 100 条结果通过，但本次三设备并行矩阵仍未通过。矩阵后对 502 进行 1 条低负载可用性复测，仍收到 `engine_overloaded`，因此停止继续请求；需在设备服务恢复后重新完成矩阵。该命令只读 F 盘样本、未访问数据库；开发库复核仍为 1,160 条 item、0 个有效租约。临时 SSH 隧道、known_hosts 和工作目录均已清理。

2026-11-07 21:02 只读排查 502 高阶网关：使用 `ictuser` key 登录（Ed25519 与本机既有 known_hosts 指纹匹配），`ns-meagt` 下 `etsllm`、`mebox`、`broadcast` 均为 1/1 Ready。`etsllm` Pod 当前 Running、restartCount=1；上次容器以 OOMKilled / exit 137 结束于 2026-10-29 06:58:15 UTC。采样时 Pod 使用 27,594Mi 内存（limit 28Gi）和 3,002m CPU，节点使用 42,820Mi / 67% 内存、3,553m / 14% CPU。单样本复测仍收到 `engine_overloaded`；随后只读 GET `/v1/models` 返回 HTTP 200，目录中存在 `qwen3-asr-1.7b`。模型目录接口可用不代表推理槽位可用。高内存占用与此前 OOM 同时存在，但只读观测不能证明它们就是当时槽位拒绝的直接原因。未查看容器日志，未停止会话、重启 Pod 或修改设备；这是该时点的状态快照，恢复确认后的复测见下。

2026-11-08 收到 502 服务恢复确认后复测：先后两条单样本初始化探测超时；隔离执行完整 100 条后全部成功、失败数为 0。复测前 Deployment 均 1/1 Ready，`etsllm` 使用 15,029Mi / 28Gi，节点内存使用率 47%；`/v1/models` 返回 200 且包含目标模型。100 条完成后的只读快照仍为 1/1 Ready，`etsllm` 使用 26,362Mi / 28Gi，节点使用 41,609Mi / 65% 内存、458m / 1% CPU；该任务后没有再发送识别请求。本次为单设备单路验收，不代表跨设备并发能力。

| 设备 | 模型 | 模式 | 成功数 | micro CER | TTFT median/P95 (ms) | finalize median/P95 (ms) | `rtf_dev` median/P95 |
|---|---|---|---:|---:|---:|---:|---:|
| 205 | `etsstt` | burst | 100/100 | 1.14% (16/1,403) | — | — | 0.239/0.246 |
| 502 | `qwen3-asr-1.7b` | stream, 30ms | 100/100 | 1.21% (17/1,403) | 1,129/1,222 | 417/548 | 0.077/0.101 |
| 600 | `asr-b` | stream, 30ms | 100/100 | 1.14% (16/1,403) | 1,148/1,239 | 1,638/2,167 | 0.358/0.438 |

三台设备使用同一 `test` split 的 100 个样本；205/600 数据来自 2026-11-07 矩阵，502 数据来自本次隔离复测。仅从 F 盘读取音频和参考文本，没有复制源音频或写入数据库。验证前后开发库均为 1,160 条 item、0 个有效设备租约；临时报告、脚本和 SSH 隧道已清理。

600 SSH key 复测：使用 root key 与一次性 known_hosts（Ed25519 指纹再次匹配已确认值），经临时本机隧道对上述 AISHELL-1 `test` split 单条样本重跑 `asr-b` 流式识别，1/1 成功、CER=0（0/13），TTFT=1,229ms、finalize=1,474ms、`rtf_dev=0.319`、wall=5,914ms。源音频只读，测试未连接数据库；复测后开发库仍为 1,160 条 item、0 个有效租约，临时 known_hosts 与 SSH 隧道已清理。此单样本记录仅作为 600 的重连复测证据；三设备 AISHELL-1 100 条单路矩阵已按本节前述结果完成。

502 网关 `qwen3-asr-1.7b` 使用同一条真实 WAV，以 `stream / 1× / chunk_ms=30` 每档预热 1 次、重复 3 次，结果如下。`rtf_dev` 为设备 `inference_ms / audio_duration_ms`，`audio-s/s` 为并发波次实际处理吞吐。

| 并发 | TTFT median/P95 (ms) | finalize median/P95 (ms) | RTF_dev median/P95 | 成功率 | 吞吐 (audio-s/s) |
|---:|---:|---:|---:|---:|---:|
| 1 | 1184 / 1215 | 207 / 207 | 0.047 / 0.047 | 3/3，预热 1/1 | 0.96 |
| 2 | 1218 / 1246 | 296 / 340 | 0.060 / 0.062 | 6/6，预热 2/2 | 1.89 |
| 3 | 1248 / 1292 | 376 / 397 | 0.079 / 0.085 | 9/9，预热 3/3 | 2.81 |

同日更早一次相同 30ms、每档 3 次的扫描中，3 路仅 6/9 成功，失败连接收到没有 `code/message` 字段的 init error。之后 1 次短测和上述完整复测均通过；因此记录为本次 3 路曲线验收通过，同时保留“设备曾出现瞬时 init 拒绝”的观察。M10 规定的 AISHELL-1 100 条 CER 基线现已按设备单路完成（见上表）；205 的 100 条标点/命名评测及 502/600 的 3 路持续 5 分钟验收仍未执行。此基准新增 `tools/bench_stream.py`，失败连接输出安全的服务器错误字段、预热结果和观测耗时；预热或测量失败都会使命令以非零退出。

### 2026-11-07 数据集模块验收

- AliMeeting far 20 条：隔离 PostgreSQL 导入 `ready`，20/20 条、16,358 个参考分段、0 解码错误、8 声道，subset=`far`、profile=`mono_ch0`；能力 JSON 的句末标点率为 1.0、重叠率为 0.162，manifest hash=`76804879e9f823e7ec04eb89b5179e5ba10ede000f83542e8fa1c239281cd309`。原始 WAV 共约 9.93 GB，导入耗时 372.75s。
- AliMeeting far 导入前预览：20/20 有效、无问题，2,165ms；WAV 元数据通过容器头读取，避免为预览完整读取 9.93 GB 音频。预览与导入统一按毫秒截断。
- `make test`：323 passed；`make lint`：Ruff、格式检查、mypy 全绿。
- 本模块验证使用独立临时 PostgreSQL；开发库 item 数保持 1,160，临时对象目录和测试库均已清理。
- DER 四引擎对照：项目帧级算法、dscore、md-eval.pl、pyannote 在 20 场 AliMeeting far 上均为 1.400805%，最大汇总和逐场差值均为 0.000000pp；假设覆盖 miss/FA/confusion，collar=±250ms，重叠计分。使用固定 dscore commit `e02f949ac6592279300a2c33d03daf9e0c12fd27`，源 TextGrid 与 WAV 仅读；临时代码目录已清理。
