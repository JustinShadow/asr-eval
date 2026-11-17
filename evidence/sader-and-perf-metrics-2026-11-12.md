# SA-DER 超过 100% 与性能指标解释（2026-11-12）

## 1. SA-DER 为什么会超过 100%

DER / SA-DER 的公式：

```text
value = (miss_frames + fa_frames + conf_frames) / ref_frames
```

分母是**参考语音帧**，分子包含：

- Miss：参考有、假设没有；
- FA：假设有、参考没有（包括重叠区多出来的假设说话人）；
- Conf：映射后仍不一致。

FA 和 Conf 可以叠加，因此分子可能大于分母，DER/SA-DER **不是严格 ≤100%**。

但 `112` 这条评测的 100.19% 不是正常的说话人错误：

```text
ref_frames  140754
miss_frames   4535
fa_frames      267
conf_frames 136219
value         100.19%
```

设备返回了 `spk_voiceprints`，但每个 speaker 只有 `voiceprint`，
**没有 `match_info`**：

```json
{
  "0": {"voiceprint": {...}},
  "1": {"voiceprint": {...}}
}
```

旧逻辑把这种“没有匹配结果”当成 `matched_id=None`，SA-DER 用固定映射时
把所有假设 speaker 都映射到 `unknown`，几乎每一帧都记成 Conf，
于是得到 100.19%；命名也全记 FR、显示 0%。

这时的正确状态应是 `na(no_voiceprint_match)`，而不是 100%。

### 已修复

`_score_run` 现在要求**至少一个实际 matched_id**：

- 全部为 `None` → SA-DER / 命名 `na(no_voiceprint_match)`；
- 部分匹配 → 只用实际匹配的 speaker，未匹配的按 unknown 处理；
- 占位符 `spk/-1/unknown` 且参考多说话人 → `na(no_diarization_output)`。

历史 Evaluation 已落库的 SA-DER 不会自动改；重新评分/重新执行后才会变成 na。

## 2. 性能指标含义

| 指标 | 公式 | 含义 |
|---|---|---|
| `rtf_dev` 设备实时因子 | `inference_ms / audio_ms` | 设备模型推理耗时相对音频时长；不含网络、上传、会话建立 |
| `rtf_e2e` 端到端实时因子 | `wall_ms / audio_ms` | 从调用到拿到最终结果的总耗时；burst/batch 有效 |
| `rtf_realtime` 实时模式因子 | `finalize_ms / audio_ms` | 1x 实时流模式下，音频结束到 final 的收尾耗时占音频时长 |
| `overhead_ratio` 设备开销比 | `(total_cost_ms - inference_ms) / audio_ms` | 设备上报总成本中除推理外的开销（VAD、标点、调度等）占音频时长 |
| `throughput` 吞吐 | `Σaudio_ms / Σwall_ms` | 每秒实际处理多少秒音频（x realtime），排除缓存样本 |

## 3. 为什么没有首字延迟和最终结果延迟

`112` 是**准确率 Run（accuracy）**，执行模式是 burst：

- `ttft_ms`：只在 `mode=stream` 且设备发送 `transcript.partial` 时记录；
  burst 关闭 partial，所以 `na(no_partial_timing)`；
- `finalize_ms`：只在 `mode=stream` 时从 `audio.end` 到 `final` 计算；
  burst 没有实时收尾，所以 `na(no_realtime_timing)`；
- `rtf_realtime`：只在 1x/realtime 模式且有 finalize 时计算，
  所以 `na(no_realtime_results)`；
- `overhead_ratio`：需要设备 `usage.total_cost_ms` 和 `inference_ms`，
  缺失时 `na(cost_data_unavailable)`。

要得到 TTFT 与 finalize：

1. 新建 **性能评测 Run**（`kind=perf`）；
2. 口径选择 `perf_30ms`（`mode=stream`、`chunk_ms=30`、1x）；
3. 设备支持流式 partial；执行后即可得到 `ttft_ms`、`finalize_ms`、
   `rtf_realtime`。

## 4. 报告与旧快照

报告是生成时快照：

- 旧报告仍显示旧的 SA-DER 100.19% 和重复 subset；
- 重新生成报告后，指标去重、设备/版本信息、硬件资源表会生效；
- 旧 Run 没有硬件资源采样，报告会显示“该 Run 未采集硬件资源”；
  需要重新执行 Run 才有 CPU/内存/NPU/温度数据。