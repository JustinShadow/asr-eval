# DER 说话人标签对齐问题排查（2026-11-11）

## 结论

`AliMeeting-DER-3576` 的 DER 57.29%、Conf 38.36% 不可信：设备实际输出了
4 个说话人，但适配器没有把 `global_spk_list` 对齐到转写分段，导致所有
Hypothesis segment 的 `speaker` 都是占位符 `"spk"`，DER 只能把全部假设
映射到一个参考说话人。

## 数据流

### 参考侧

- AliMeeting far：TextGrid 每个 tier（`N_SPK8005` …）→ `RefSegment`；
- 每个 item 保存参考分段的 `speaker/start_ms/end_ms`；
- DER / SA-DER 用这些分段做 10ms 帧比较。

### 设备侧

205 的 `transcript.segment/final` 报文包含：

```json
{
  "sentences": [{"text": "...", "start": 0, "end": 1000, "spk": 0}],
  "global_spk_list": [0, 0, 1, 1, ...]
}
```

- `global_spk_list` 是**逐句**说话人标签（含标点句、零时长句）；
- 部分句子带 `spk`，部分只有 `global_spk_list`；
- 适配器 `_collect_segments` 负责把句子转成带 speaker 的分段。

## 根因

1. `_collect_segments` 只保留 `end > start` 且有文本的句子；标点句/零时长句
   被丢弃。AliMeeting 长会议最终 `global_spk_list` 有 1206 个标签，
   而分段只有 1175 个，旧的 `_apply_global_speakers` 校验长度不匹配后
   **直接放弃全部标签**，所有分段保持 `"spk"`。
2. `speaker = str(sentence.get("spk") or ... )` 把合法的说话人 `0` 当成缺失。
3. 全部 `"spk"` 时仍继续计算 DER，得到一个“看似有效”的高 DER。

300s 样本可复现：

```text
旧分段：145 个，global_spk_list 148 个 → 长度不匹配 → 全部 "spk"
```

## 修复

`backend/app/adapters/etsstt.py`：

- 新增 `_collect_sentences`：收集全部句子（含零时长标点句），按
  `(start,end,text)` 去重，保留显式 `spk`（包括 0）；
- 新增 `_apply_sentence_speakers`：按句子顺序对齐 `global_spk_list`，
  `len == sentences + 1` 时去掉句末哨兵；长度不符不猜测；
- `_coerce_global_labels`：兼容固件把标签序列化为字符串的形态；
- 最终从**全部句子**重建 `segments`，不再只依赖非零时长分段。

`backend/app/domain/runs.py`：

- 若参考有多个说话人、但假设 speaker 只有 `spk/-1/unknown`，
  DER / SA-DER / 命名标记 `na(no_diarization_output)`，不再输出误导性数值。

`backend/app/api/evaluations.py`：
- speaker-analysis 对这类历史数据返回不可用，并说明原因。

## 验证

用 0162 直接跑 300s AliMeeting（diarization=True）：

```text
修复前：145 个 segment，speaker 全部为 "spk"
修复后：145 个 segment，speaker 分布 {0:52, 1:25, 2:43, 3:25}
global_spk_list 长度 148
```

新增单测：

- `_apply_sentence_speakers` 对齐零时长标点句；
- `_sentence_speaker` 保留 `spk=0`；
- `_coerce_global_labels` 解析字符串/拒绝截断字符串；
- DER `collect_errors` 区间。

## 历史结果

旧 Run 的 Hypothesis segments 已经落库为 `"spk"`，**重新评分不会修复**
（评分只读已存的 Hypothesis）。需要修复后**重新执行 Run**，才能得到
可用的 DER/Conf 与说话人分析。

```text
make test          404 passed
make lint          All checks passed
```