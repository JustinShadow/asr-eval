# DER 说话人来源收口：只信 final 的 global_spk_list

日期：2026-11-13
涉及：`backend/app/adapters/etsstt.py`

## 问题

设备在转写过程中持续下发 `transcript.segment`，句子里的 `spk` 会随着
diarization 聚类过程实时变化；只有 `transcript.final.global_spk_list`
才是全量句子重排后的最终标签。旧逻辑在 `global_spk_list` 缺失或长度
不符时保留过程中的 `spk`，可能把中间态当成最终说话人，DER 随之偏移。

## 规则

1. `_collect_sentences` 收集句子时**不读** `spk` / `speaker`，统一写占位符 `spk`；
   过程中的 `spk` 只用于 `observed.speakers_in_sentence` 能力观测。
2. `_apply_sentence_speakers` 用 final 的 `global_spk_list` **覆盖**每个句子的
   说话人；长度必须等于句数，或等于句数 + 1（末尾哨兵）。
3. 长度不符或没有 final 标签时**不猜**：全部保留 `spk`，让 DER 走
   `na(no_diarization_output)`（参考多说话人时），而不是输出误导性数值。
4. final 重发完整句子时整体替换累积句子，避免过程中的旧时间戳与新标签错位。
5. 转写文本按去重后的句子重建：部分固件会在 final 重复全部 segment，
   直接拼接消息文本会把同一句算两次，CER 虚高。

## 验证

### 单元测试（`backend/tests/test_etsstt.py`）

- `test_intermediate_sentence_spk_is_ignored`：句子带 `spk="1"`，收集后仍是 `spk`；
- `test_final_global_speakers_override_intermediate_spk`：过程中 `spk=0`，
  final `[1,2,99]` 覆盖为 `1/2`；
- `test_global_speaker_labels_are_not_guessed_on_length_mismatch`：长度不符返回 False；
- `test_final_repeated_sentences_do_not_duplicate_text`：final 重复句子时文本只有一份；
- `test_final_sentences_win_when_timestamps_are_refined`：final 微调时间戳后
  分段与标签仍一致。

### 真机（502 网关 `qwen3-asr-1.7b`）

素材：`R8002_M8003_MS803` 第 7.33s–125.33s（118s，参考含 4 个说话人）。

```text
模式            burst + diarization(max_num_spks=6)
消息            init 1 / partial 107 / segment 14 / final 1
句子            57 / 56（两次执行，分段轻微非确定性）
global_spk_list 57 / 56（全部 0）
分段            57 / 56（speaker 全部 0）
文本            555 / 554 字，无重复
```

说明对齐按“全部句子（含标点句）”计数成立；该窗口模型只聚出 1 个簇，
不能用来验收多人分离质量，但足以验证标签来源与对齐。

### 真机多说话人复核（502 网关 `qwen3-asr-1.7b`）

素材：`R8002_M8003_MS803` 第 120s–420s（300s，参考含 4 个说话人），
burst + `max_num_spks=6`：

```text
句子数(含零时长)      206
final 标签数          206
标签取值              {0: 114, 1: 49, 2: 34, 3: 9}
分段数                206
分段 speaker          {0: 114, 1: 49, 2: 34, 3: 9}
文本长度              1378 字
usage                 inference 63.6s / audio 300s = rtf 0.2308
```

分段 speaker 的分布与 final 标签**逐项一致**，且全部来自 final 标签集合；
因为该设备句子本身没有 `spk` 字段，这组结果只能由 final
`global_spk_list` 逐句对齐产生。

### 报文结构复核

同音频前 60 秒原始抓包（`/tmp/opencode/raw-gateway-R8002_M8003_60s.json`）：

- 7 个 `transcript.segment` 共 26 个唯一句子，句子字段只有 `start/end/text`，
  没有任何 `spk`；
- `transcript.final.global_spk_list` 恰好 26 个元素。

即：该设备不提供句级 `spk`，说话人只能来自 final 的逐句列表；旧逻辑
能出标签也是靠这份列表，而不是中间态。

## 对已有结果的影响

- 已完成的 Run 不自动改写：`Hypothesis.segments` 是当时的快照。
- 若历史 Run 的设备报文含过程中 `spk` 且 final 列表缺失/不等长，旧快照
  可能使用了中间态标签；需要重新执行（或重新转写）才会按新规则得到
  `spk` 占位与 `na(no_diarization_output)`。