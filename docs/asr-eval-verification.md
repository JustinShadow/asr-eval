# 待验 5 项 · 验证报告（v2 · 更正版）

> 2026-11-02 ｜ `192.168.65.181`（205 / etsstt）、`192.168.133.2`（502 网关 / qwen3-asr-1.7b / Ascend 310P）
> **v1 的"长音频流式不 final"结论是错的**，原因是测试过程中进程被强杀导致引擎卡死。重启后在干净状态下全部复验通过，本版为准。

---

## 结论速览

| # | 项 | 结果 |
|---|---|---|
| 1 | 502 网关 3 路并发 | ✅ **3 路可用**，含 3×30min；算力只放大 1.3~1.4x，单流 RTF 翻倍但仍 <1 |
| 2 | 30min / 长音频 | ✅ **非流式、流式都通过**；时延与耗时符合预期 |
| 3 | 网关容器可写 | ✅ `/mnt` PVC 可写，声纹+命名闭环通过 |
| 4 | 昇腾版本字段 | ✅ driver 25.2.0 / ascendhal 7.35.23 / 310P-24G-arm64 |
| 5 | 客家话正式集 | ✅ 无可公开获取的评测集 → 自建 |

---

## 1. 3 路并发

### 1.1 短音频（10s，1x，20ms 分片）

| 并发 | 成功率 | `ttft` | `finalize` | `rtf_dev` | 吞吐（audio-s/s） |
|---|---|---|---|---|---|
| 1 | 1/1 | 986ms | 1245ms | 0.115 | 0.87 |
| 2 | 2/2 | 1012ms | 1383ms | 0.126 | 1.71 |
| 3 | 3/3 | 1041ms | 3371ms | 0.320 | 2.18 |

### 1.2 长音频（300s，burst 50x）

| 并发 | 成功率 | `ttft` | `finalize` | `rtf_dev` | 吞吐 |
|---|---|---|---|---|---|
| 1 | 1/1 | 2.6s | 33.4s | 0.116 | 7.6 |
| 3 | 3/3 | 9.8s | 78s | 0.2475 | 10.0 |

### 1.3 3 路 × 30min（burst 50x）—— 最接近真实业务

```
3/3 成功
wall 458.7s（7.6 分钟）
rtf_dev 0.243 / 0.243 / 0.243
ttft 7.55 / 7.63 / 7.59s
finalize 396 / 400 / 399s
吞吐 5400s / 458.7s = 11.8 audio-s/s（单路为 8.23）
```

### 1.4 结论

1. **功能上 3 路完全可用**，含每路 30 分钟长录音，全部返回完整转写；
2. **算力不是线性放大**：短音频约 2.5x，长音频只有 **1.3~1.4x**——单 NPU 时间片调度，不是真并行；
3. 单流 `rtf_dev` 从 0.116 涨到 0.243，**仍远小于 1**，实时场景 3 路可持续跟播；
4. `ttft` 从 2.6s 涨到 7.6s（burst 下主要反映排队），实时 1x 下影响较小。

**外推（按 3 路实测 rtf 0.243）**

| 单路音频 | 1 路耗时 | 3 路并发耗时 |
|---|---|---|
| 30min | 3.6min | 7.6min |
| 1h | 7.3min | 15.3min |
| 3h | 21.9min | 45.8min |

> 结论：**"数小时录音 × 3 路"在算力上可行**（3 小时约 46 分钟出结果），但必须把这条外推写进 ETA 模型。

---

## 2. 长音频（含 30min）

### 2.1 非流式 burst

```
1800s / 30s 分片 / 50x
inference 213.96s → rtf_dev 0.1189
wall 220.96s，partial 覆盖 1800000/1800000ms
```

### 2.2 流式 burst

```
1800s / streaming:true / 30s 分片 / 50x
partial 58 条 + segment 251 条 + final 1 条
inference 214.35s → rtf_dev 0.1232
wall 218.66s
```

### 2.3 流式 1x 实时

| 音频 | partial | segment | final | finalize | 结论 |
|---|---|---|---|---|---|
| 30s | 25 | 4 | ✅ | 1.13s | 通过 |
| 60s | 70 | 6 | ✅ | 0.90s | 通过 |
| 90s | 114 | 9 | ✅ | 0.48s | 通过 |
| 120s | 176 | 10 | ✅ | 1.26s | 通过 |
| 300s | 425 | 26 | ✅ | 1.73s | 通过 |

### 2.4 v1 为什么判错

**时间线（`960a4f21`，300s / 1x / 20ms 分片）**

```
19:28:09  connected
19:29:11  Received audio duration: 60000 ms
...       每 ~61s 上报一次，说明客户端按 1x 正常推流
19:33:18  Received audio duration: 300000 ms
19:33:18  Recv AudioEnd
          ↓ 之后 10 分钟，引擎再无任何 segment 完成
19:43:08  WebSocket error: Protocol(ResetWithoutClosingHandshake)   ← 被工具 900s 超时强杀
19:43:08  release lock
```

**引擎侧日志（关键）**

```
19:28:15  stt_iterator.rs:818  Segment processing start: 100ms -> 5100ms,   duration 5000ms
19:28:25  stt_iterator.rs:818  Segment processing start: 10130ms -> 14330ms, duration 4200ms
          ↓ 两条都没有对应的 "Segment processed"，之后 285s 音频再没有派发任何 segment
```

**根因分解**

| 层 | 事实 |
|---|---|
| 直接原因 | 测试工具的 900s **硬超时**把探针进程 SIGKILL，TCP 发 RST（`ResetWithoutClosingHandshake`） |
| 为什么还在等 | 探针的 `--timeout 900` 是**每条消息**的接收超时，`AudioEnd` 之后一直在等 `final`，总时长撞上工具超时 |
| 引擎为什么不出 `final` | 有两条 segment 被派发但**从未完成**，迭代器随后卡住，不再派发后续 segment |
| 为什么之后一直 overload | 卡住的推理没有返回，槽位不释放；`release lock` 只释放了会话锁，没释放推理资源 |

**这不是"音频太长"**：同一段 300s 音频在重启后的干净状态下 1x 跑通（425 partial / 26 segment / final，finalize 1.73s）。属于**低概率的流式管线卡死**，触发条件与"持续高频小分片（20ms × 15000 帧）"相关，非确定性。

**对平台的含义**

1. 客户端要有**独立的 finalize 看门狗**（`max(30s, 2×rtf×duration)`），不能依赖"等消息"的超时；
2. 看门狗到点 → 发 `close` 帧、等对端关闭、记 `finalize_timeout`，**绝不 SIGKILL**；
3. 该卡死需要厂商修；平台侧只能"检测 + 隔离 + 标记设备需重启"。

### 2.6 30ms 分片复测（业务真实口径）

业务录音转写实际是 **30ms 一块**，因此流式评测统一按 30ms 口径复测：

| 场景 | 分片 | 结果 |
|---|---|---|
| 10s × 1 路，1x | 30ms | 15 partial / 1 segment / final；`ttft 0.976s`、`finalize 1.323s`、partial 间隔 649ms |
| 300s × 1 路，1x | 30ms | 425 partial / **26 segment（start 26 = processed 26）** / final；`rtf_dev 0.1125`、`finalize 1.653s`、覆盖 299.97s/300s |
| 30s × 3 路，1x | 30ms | 3/3 成功；`rtf_dev 0.145~0.152`、`finalize 2.28s`、`ttft 11.4s`（该段起始有静音） |

**30ms vs 20ms 对照（同一 10s 音频）**

| 分片 | ttft | finalize | partial 间隔 |
|---|---|---|---|
| 20ms | 0.980s | 1.295s | 662ms |
| **30ms** | **0.976s** | 1.323s | 649ms |

差异 < 1%，**30ms 完全等价且更贴近业务**。

**对"20ms 高频小帧"假设的修正**：30ms × 一万帧的 300s 会话稳定通过，说明不是"高频小帧必然卡死"；那次卡死仍是**未定位的低概率竞态**，需要厂商按请求号排查。平台侧按 30ms 业务口径执行，同时保留看门狗与熔断。

### 2.7 30ms 性能基线（可入库）

脚本：`scripts/perf_baseline.py` ｜ 参数：180s 音频 · 30ms 分片 · 1x · 每档 2 轮 · 无预热

| 并发 | 成功率 | ttft med/P95 (ms) | partial 间隔 (ms) | lag med/P95 (ms) | finalize med/P95 (ms) | rtf_dev | 吞吐 (audio-s/s) |
|---|---|---|---|---|---|---|---|
| 1 | 2/2 | 968 / 970 | 644 | 30 / 45 | 635 / 636 | 0.11 | 0.98 |
| 2 | 4/4 | 1008 / 1026 | 647 | 60 / 90 | 695 / 712 | 0.12 | 1.95 |
| 3 | 6/6 | 1018 / 1066 | 653 | 90 / 120 | 1275 / 1295 | 0.23 | 2.91 |

**解读**

1. **首字几乎不掉**：968 → 1018ms（+5%），3 路交互体验基本不变；
2. **lag 极小**（30~120ms）→ 1x 下 3 路完全跟得上，无累积积压；
3. **尾延迟翻倍**：635 → 1275ms，但仍 <1.5s；
4. **rtf_dev 0.11 → 0.23**，设备仍有 4 倍余量；
5. 吞吐 0.98 → 2.91（2.97x）**接近线性**，但这是 1x 节奏决定的（每路本来就是 1x），**不能用来判断算力上限**；算力上限看 `rtf_dev` 与 burst 吞吐。

**口径说明（本轮修正的两个度量错误）**

- `partial_lag` 必须用「当前已发送音频时长 − partial.end_ms」；早先误用「总时长 − end_ms」，得到的 170s 是"剩余音频"，无意义。
- 1x 模式的吞吐必须按「每轮」计算：`level × audio / median(wall)`；早先按"全部轮次求和"算，导致数值翻倍。

### 2.5 由此得到的三条硬规则

| # | 规则 |
|---|---|
| 1 | **禁止强杀转写进程**；超时必须走 WS `close` 帧并等待对端关闭（graceful shutdown） |
| 2 | 会话看门狗：`audio.end` 后超过 `max(30s, 2×rtf_est×duration)` 无 final → 主动 close + 记 `finalize_timeout` |
| 3 | 连续出现 `engine_overloaded` 且**当前无活跃会话** → 标记设备「需重启」，平台不自动重启 |

**撤销 v1 的错误结论**：准确率**可以**用流式模式（流式同样产出 segment + final），但必须记录 `mode`，且同一 Run 内不得混用。

---

## 3. 网关容器可写且 etsllm 可见

```
/mnt → PVC mebox-data-pvc（topolvm，rw）；/opt/etsme/etc/ssf 同卷
写入：kubectl exec -i deploy/etsllm -- sh -c 'cat > /mnt/voiceprint/SPK8005.wav'
校验：容器内 md5 dee41f707f79fa956935639ad6ceb88f == 本机
声纹：POST /v1/audio/voiceprints → 192 维，0.86s
命名：簇 0 → matched_id=SPK8005，confidence=0.7009
```

宿主侧也有 PVC 挂载（`/var/lib/kubelet/pods/<pod-uid>/volumes/kubernetes.io~csi/pvc-.../mount`），但 pod uid 会变，不作默认。

**结论**：`RemoteStaging` 默认用 `exec-cat`，降级 `kubectl cp` → hostPath 动态解析。

---

## 4. 昇腾版本字段

```ini
# /usr/local/Ascend/driver/version.info
Version=25.2.0
ascendhal_version=7.35.23
Innerversion=V100R001C21SPC008B208
```

```jsonc
// get_version：无模型版本键、platform 为空
// etsllm --version：1.4.49
// GET /v1/models：{"id":"qwen3-asr-1.7b","extra_info":{"accel":"cann"}}
// GET /v1/system/info：
"system": {"platform":"502","version":"3.2.412"},
"spec": {"features":{"ai":{"support-model-type":"gjwg-310p-24g-arm64"}}}
```

| 字段 | 来源 | 值 |
|---|---|---|
| `platform` | system/info 兜底 | 502 |
| `accel` | version.info + models | `{ascend_driver:"25.2.0", ascendhal:"7.35.23", innerversion:"V100R001C21SPC008B208", runtime:"cann"}` |
| `soc` | `support-model-type` | 310P / 24G / arm64 |
| `model_key` | `invocation_name` | `qwen3-asr-1.7b` |
| `model_version` | 无 | `null`（不伪造） |

> Ascend 没有 RKNN 那种 `model-xxx-w8a8_rk3588` 版本键，**量化信息无法自动获得**，只能人工备注或从模型仓库读取。

---

## 5. 客家话

| 资源 | 规模 | 状态 |
|---|---|---|
| 本地现有 | 2 段 / 34.2min / SRT 文稿 / 无说话人标签 | 仅冒烟 |
| HF `formospeech/hat_asr` | 1181.7h / 137GB | **private_and_closed，Eval=0h** |
| FSR-2023/2025 | ~80h / 6 腔 | 评测集仅发参赛队 |
| 数据堂 / MagicData | 商业 | 需采购 |
| ModelScope SenseVoice_Hakka | 2231 条 | 社区集，无评测协议 |

**结论**：不存在可公开获取、带评测划分的客家话 ASR 基准。一期用现有 34.2min 冒烟；正式集自建 300~500 条（简体汉字、≥20 说话人、带标点；命名另采 30s/人）。

---

## 6. 对方案的修改（更正版）

| 模块 | 修改 |
|---|---|
| `Deployment.capabilities.observed` | 新增 `max_stream_sec`（实测可用时长）与 `realtime_slots`（实测并发上限） |
| 会话管理 | 优雅关闭 + 看门狗 + `finalize_timeout` 日志 |
| 熔断 | `engine_overloaded` 且无活跃会话 → 设备「需重启」 |
| ETA | 3 路并发按 `rtf ≈ 2×单路` 估算（实测 0.243 vs 0.116） |
| 准确率 Run | **流式/非流式均可**，必须记录 `mode`，同一 Run 不混用（撤销 v1 的"只走非流式"） |
| `RemoteStaging` | 默认 `exec-cat` |
| 指纹 | Ascend 解析规则；`model_version=null` |
| 数据集 | 客家话一期冒烟 |

---

## 7. 剩余待办

| # | 项 | 说明 |
|---|---|---|
| 1 | 2h 长录音 soak | 现有最长 34min；建议夜间跑 1 次 2h 单路 + 3 路各 1h |
| 2 | 3 路并发 repeat=3 | 当前每档 1 轮 |
| 3 | 优雅关闭是否能完全避免引擎卡死 | 需专门做一次"close vs reset"对照实验 |
| 4 | 客家话正式集 | 立项 |