# 3 设备 × 3 数据集矩阵 Runbook（2026-11-10）

> 目标：用一份计划 JSON 在 AISHELL-1 / AISHELL-3 / KeSpeech 上执行 3 台设备的只读 CER 矩阵。
> 约束：音频只读、不写数据库、不复制源文件；每台设备内部严格单路，数据集之间串行。

## 1. 准备计划

```bash
cp tools/matrix_plan.example.json tools/matrix_plan.json
```

按现场修改 `tools/matrix_plan.json`：

- `devices[]`：`name`、`host`、`port`、`model`、`mode`（`burst` 或 `stream`）。
  - 205 的 ASR 只监听设备回环 `127.0.0.1:8701`，先建 SSH 本地转发到本机 `18705`，计划里写 `127.0.0.1:18705`、模型 `etsstt`、`burst`；
  - 502 直连 `192.168.133.2:30025`，模型 `qwen3-asr-1.7b`，`stream`；
  - 600 通常经 SSH 隧道映射到本机 `127.0.0.1:18702`，模型 `asr-b`，`stream`。

205 隧道：

```bash
# 终端 A：保持前台运行；已核验指纹时加 ASR_TUNNEL_KNOWN_HOSTS=/path/to/known_hosts
tools/device_tunnel.sh etsme@192.168.65.181 18705 127.0.0.1 8701

# 终端 B：确认本地端口已监听
ss -ltn | grep 18705
```

不要通过改设备绑定地址或 `socat`/反向代理把 8701 暴露到局域网；SSH 本地转发最小权限且不改变设备服务。
- `datasets[]`：`name`、`kind`（`aishell1` / `aishell3` / `kespeech`）、`root`、`subset`、`limit`。
  - AISHELL-1：`root/data_aishell`，`subset=test`，读 `transcript/aishell_transcript_v0.8.txt` 与 `wav/test/S*/*.wav`；
  - AISHELL-3：`root/AISHELL-3`，`subset=test`，读 `test/content.txt` 与 `test/wav/<speaker>/*.wav`；原始音频多为 44.1k，工具会转成临时 16k 缓存，源文件只读；
  - KeSpeech：`root` 下的方言目录，wav/txt 同名配对。

`tools/matrix_plan.json` 已加入 `.gitignore`，避免设备地址进入仓库。

## 2. 只校验样本（不连设备）

```bash
make matrix-check
```

2026-11-10 在本地真实目录上的结果：

```text
[OK] AISHELL-1 (aishell1) 100 条样本
[OK] AISHELL-3 (aishell3) 100 条样本
[OK] KeSpeech (kespeech) 100 条样本
[OK] 3 台设备、3 个数据集计划可执行
```

## 3. 执行矩阵

```bash
make matrix
# 等价于：
cd backend && uv run python ../tools/bench_matrix.py \
  --plan ../tools/matrix_plan.json --save /tmp/asr-matrix.json
```

输出为“数据集 × 设备”表，列包含成功率、micro CER、参考字符数、TTFT/finalize median/P95、`rtf_dev`。

说明：

- 单条失败只记入该设备该数据集的失败数，不中断其他设备/数据集；
- 205 隧道未建立时，该设备 3 个数据集均为失败，不会伪造 CER；先按 §1 建隧道；
- 502 过载时同样只记录失败，便于保留证据后等设备恢复；
- `--save` 的 JSON 只含安全摘要，不含原文、音频或声纹。

## 3.1 205 标点 F1（AliMeeting near 100 条）

```bash
cd backend
uv run python ../tools/bench_punctuation.py \
  --near-root "/mnt/f/测试数据/测试文件/AI测试素材/开源数据集/alimeeting/Test_Ali/Test_Ali_near" \
  --host 127.0.0.1 --port 18705 --model etsstt --limit 100 \
  --save /tmp/asr-205-punct-100.json
```

工具按 TextGrid 区间从原 WAV 裁切 16k 临时片段，源音频只读；输出 micro CER、标点 P/R/F1 和句末准确率。
命名需要逐说话人声纹注册，当前输出 `na(no_voiceprint_enrollment)`，不伪造准确率。

## 4. 结果归档

- 将命令输出与 `/tmp/asr-matrix.json` 摘要追加到 `evidence/`（不要提交音频）；
- 三个数据集、三台设备全部成功后才算 P1 “3 设备 × 3 数据集横评出榜”通过；
- 若某台设备不可达，在记录中写明检查时间、设备与错误，不得用部分成功冒充完整矩阵。