# DER 全链路人工校验包（29b96b18 / R8002_M8003_MS803）

## 产物

- 完整解析数据：`/tmp/opencode/verify-29b96b18-R8002_M8003_MS803.json`
- 可视化校验页：`/tmp/opencode/verify-29b96b18-R8002_M8003_MS803.html`
- 同音频前 60s 原始报文：`/tmp/opencode/raw-gateway-R8002_M8003_60s.json`

## 样本

```text
Run        AliMeeting-Far-DER-gateway
case       2131
item       R8002_M8003_MS803
音频       /mnt/f/.../Test_Ali_far/audio_dir/R8002_M8003_MS803.wav
时长       2,042,126 ms
参考       4 个说话人，1126 段
模型解析   2 个说话人，1316 段
映射       0 → N_SPK8011；1 → N_SPK8009
DER        46.94%
```

## 链路示例

原始 `transcript.segment`（60s 抓包）：

```json
{"type":"transcript.segment",
 "sentences":[
   {"text":"大家早上好，","start":7390,"end":8490},
   {"text":"今天把大集把大家呢召集过来呢，","start":8830,"end":11460}
 ],
 "global_spk_list":[-1,-1],
 "covered_until":11}
```

`transcript.final`：

```json
{"type":"transcript.final",
 "global_spk_list":[0,0,0,0,0,0],
 "usage":{"audio_duration_ms":60000,"inference_ms":11183,
          "total_cost_ms":12519,"rtf":0.21618}}
```

平台处理：

1. `sentences[].text` 拼接为转写文本；
2. `start/end` 由 `_time_ms` 解析；
3. 全部句子（含零时长标点句）按顺序与 `global_spk_list` 对齐；
   过程中的 `sentences[].spk` 不参与最终映射（会实时变化）；
   长度不符时不猜，保留 `spk` 占位并让 DER 输出 `na(no_diarization_output)`；
4. 重建 `Hypothesis.segments[{speaker,start_ms,end_ms,text}]`；
5. `RefSegment` 与 `Hypothesis.segments` 进入 `speaker.der()`；
6. DER 用 10ms 帧、±250ms collar、最优映射，计算 miss/fa/conf。

## DER 核对

```text
ref_frames   140547
miss_frames   38619
fa_frames       226
conf_frames   27122
DER = (38619+226+27122)/140547 = 46.94%
```

独立 pyannote（collar=0.5，等价 ±250ms）该样本 46.99%，差异 0.05pp。

## 结论

- 模型实际只输出 2 个 speaker，平台没有把 4 个 speaker 合并；
- 参考 `N_SPK8010`、`N_SPK8012` 没有独立假设 speaker，导致 Conf/Miss；
- 高 DER 是模型 diarization 能力问题，不是解析或评分错误。
