# 0162 批量长音频发送观测（2026-11-11）

## 目的

在 0162（205 / etsstt，`192.168.65.181:8701`，平台经 SSH 本地转发）上，
用 APY / MagicData 目录的真实长音频验证批量（burst）发送是否会被设备反压阻塞，
以及是否出现中途断连、超时或异常结束。

- 不修改设备文件；只读取 `/mnt/md0/log/services/etsllm/etsstt.log`。
- 观测脚本：`/tmp/opencode/long_audio_probe.py`（包装 websockets，记录 send/recv 耗时）。
- 执行参数：`mode=burst`、`chunk_ms=1000`、`input_rate=1.0`、
  `disable_speaker_diarization=true`、单次 recv 超时 120s（与平台默认一致）。

## 样本

| 来源 | 文件 | 时长 | 采样 |
|---|---|---|---|
| MagicData-沪语 | `A0002_S0003_0_G0003_G0004.wav` | 1800.1s | 16k mono |
| APY-四川话 | `2_175_001.wav` | 1593.3s | 16k mono |
| AliMeeting（对照） | `R8004_M8005_MS803_ch0_16k.wav` 截 600s | 600.0s | 16k mono |

> APY-粤语的 `tra_mic.wav` 是 44.1k 双声道，适配器会拒绝；本轮改用 APY-四川话 16k mono。

## 结果

| 样本 | 发送字节 | send 调用 | 发送总耗时 | 单次 send 最大 | 慢发送(>250ms) | 收包 | partial / segment | 最终 |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| 60s AliMeeting | 1.92MB | 63 | 16.3ms | 0.46ms | 0 | 61 | 54 / 5 | final ✔ |
| 300s AliMeeting | 9.60MB | 303 | 74.2ms | 0.47ms | 0 | 307 | 283 / 22 | final ✔ |
| 600s AliMeeting | 19.20MB | 602 | 154.1ms | 0.65ms | 0 | 620 | 577 / 41 | final ✔ |
| 1800s MagicData | 57.60MB | 1803 | 453.5ms | 0.86ms | 0 | 1800 | 1601 / 197 | final ✔ |
| 1593s APY | 50.99MB | 1596 | 376.2ms | 0.76ms | 0 | 1579 | 1425 / 152 | final ✔ |

处理耗时与 RTF：

| 样本 | wall | 设备 usage RTF | 最大瞬时 RTF | 最大 Lag | 会话结束 |
|---|---:|---:|---:|---:|---|
| 600s AliMeeting | 176.3s | 0.2917 | — | ~550s→9.6s | normal |
| 1800s MagicData | 485.3s | 0.2784 | 0.42 | 1,730,149ms | normal |
| 1593s APY | 447.9s | 0.2853 | 0.55 | 1,546,320ms | normal |

`max_recv_wait` 分别为 3.6s（MagicData）与 4.3s（APY），远小于 120s。
recv 类型只出现 `init / transcript.partial / transcript.segment / transcript.final`，
无 `error` 消息。

## 设备日志

- MagicData：`cc04a311-735c-4e3b-b495-3cff8eb444a8`
  - `15:13:28.830` 建立，`15:21:33.636` `Realtime session finished normally`
  - 197 个 segment，0 条 `STT error / session error`
- APY：`70054357-a312-41ab-af6f-3251c6f6f91d`
  - `15:21:39.508` 建立，`15:29:06.910` `Realtime session finished normally`
  - 152 个 segment，0 条 `STT error / session error`
- 全日志 `WARN` 计数为 0。
- 日志中的 `Client disconnected` / `Broken pipe` 全部来自每次测试后用
  `probe_lite` 做健康检查：init 成功后立即关闭连接，共 4 行/次；
  不是转写会话异常。

## 结论

1. **发送没有阻塞**：57.6MB / 1800s 音频在 0.45s 内全部 `send` 完成，
   单次最大 0.86ms，没有超过 250ms 的慢发送。
   设备会先把音频收下并缓冲，再按 RTF≈0.28 处理。
2. **没有异常结束**：三类样本均收到 `transcript.final`，
   `error=None`、`server_errors=[]`，设备侧会话均 `finished normally`。
3. **Lag 会接近整段时长**：burst 会先把整段音频送入设备，
   1800s 样本的 Lag 峰值约 1730s，随后持续下降。
   这是整段预缓冲，不是死锁；设备 16GB 内存、测试后可用约 9.9GB。
4. 更长的音频（小时级）会继续放大整段缓冲；如后续要跑超长音频，
   建议在平台侧限制单条音频上限，或改成窗口化发送，避免设备内存随音频线性增长。

## 复现

```bash
cd backend
PYTHONPATH=$PWD PROBE_DIARIZATION=0 PROBE_TIMEOUT_S=120 \
  ~/.local/bin/uv run python /tmp/opencode/long_audio_probe.py \
  "/mnt/f/.../MagicData-沪语（吴语）/WAV/A0002_S0003_0_G0003_G0004.wav" \
  "/mnt/f/.../APY200501001-四川话/APY200501001/data/2_175/2_175_001.wav"
```

日志：

```bash
ssh etsme@192.168.65.181 \
  "grep 'Realtime session finished\\|Realtime session established\\|STT error\\|Broken pipe' \
   /mnt/md0/log/services/etsllm/etsstt.log"
```