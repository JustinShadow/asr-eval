# ASR 评测平台 · 当前方案总览

> 一页看懂"现在设计成什么样了"。细节见三份子文档（文末索引）。
> 所有标 ✅ 的结论都来自 2026-11-02 的真实设备实测，不是推演。

---

## 1. 一句话

**用"不可变的考卷 × 不可变的考生"生成一次评测运行，把推理产物与评分彻底分开**——改口径不用重跑设备，换设备不用重建数据集。

---

## 2. 核心模型：四层解耦 + 六实体

```
不可变资产 ──► 不可变考生 ──► 可缓存产物 ──► 纯函数评分
DatasetVersion  Deployment     Hypothesis    MetricProfile
```

| 实体 | 一句话 | 可变性 |
|---|---|---|
| `DatasetVersion` | 冻结的考卷：音频 + 参考答案 + 时间轴 + 标签 | 不可变 |
| `Deployment` | 冻结的考生：设备 + 固件 + 服务 + 模型 + 量化 + 能力 | 不可变 |
| `Benchmark` | 考卷 + 评分口径 + 抽样规则 | 不可变 |
| `Run` | 一次评测 = Benchmark × Deployment[]，`kind=accuracy/perf` | 结果不可变 |
| `Hypothesis` | 一次推理的产物，内容寻址 | 不可变 |
| `Score` | 打分结果，可随时换口径重算 | 可重算 |

**三个身份**

```
content_uid     = sha256(item_key|audio_sha|ref_sha|lang)[:24]      # 题目身份
deployment_uid  = sha256(platform|bigversion|etsllm|accel|model_key|model_version|slots)[:16]
benchmark_hash  = sha256(sorted(content_uid[])|profile_hash|sampling|params)[:16]
```

三者相同 → 结果可比；任一不同 → UI 必须显式提示。

---

## 3. 系统架构

```
接入   React SPA · OpenAPI · CI · LDAP
应用   FastAPI（dataset / benchmark / run / report / device / speaker）
执行   Scheduler ─► SlotPool ─► Adapter SPI ─► 设备
                  └► Scorer ─► Metric Plugin ─► Aggregator
数据   PostgreSQL（元数据/结果/队列/租约）
       本地对象目录（raw/报告） + 只读 NFS（原始音频）
```

前端：React 18 + TS + Vite + Tailwind + shadcn/ui + TanStack；图表栈 = **CSS Grid（矩阵）+ Recharts（柱/线）+ 自定义 SVG（轨道/slope/泳道/波形）**，不引入 ECharts。详见 `m6-report-ui-design.md` §4。

单机 Docker Compose（lite）：`postgres + app`，2 个容器；重评分时可加 `worker`。

---

## 4. 三条主数据流

```
① 导入   原始目录 → scan → manifest → PG(item/ref_segment) + NFS 引用
② 跑评   Benchmark × Deployment[] → Run → Hypothesis（可缓存）→ Score
③ 出报   矩阵 / 分桶 / 回归 / Badcase → HTML / XLSX
```

**两条入口，一张表**：数据集页发起横评；设备页发起回归。报表只是 pivot 方向不同。

---

## 5. 设备接入

| platform | kind | 部署 | 转写端点 | 并发 | 流式 |
|---|---|---|---|---|---|
| 205 | `etsstt` | 普通 | `ws://:8701/...` | 1 | 依模型构建（实测 false） |
| 600 | `etsstt` | 普通 | 同上 | 1 | 依模型构建 |
| 502 | `gateway` | k3s 容器 | `ws://:30025/...` | **3** | ✅ true（实测） |
| — | `cloud` | 托管 | WSS / HTTP | 1 | 依服务 |

**能力探测两档（已实测可行）**

| 档位 | 动作 | 耗时 | 产出 |
|---|---|---|---|
| `probe_lite` | WS 连接 + `init` + 关闭 | ~200ms | `declared` 能力（`streaming` 连续 3 次稳定、与行为 2/2 一致） |
| `probe_full` | + 10s 探测音频，30ms 分片、1x | ~12s | `observed.partials / ttft / partial_step_ms` |

**声纹（命名评测的前置）**

```
SFTP 推注册音频 → POST /v1/audio/voiceprints（file_path 为设备侧路径）
   → 192 维向量 → init.voiceprints[] → sentence 匹配 → matched_id + confidence
```

网关（502）需三级降级：共享挂载 → `kubectl cp` → `kubectl exec -i ... cat >`。

---

## 6. 指标：两种模式、两套时延

### 6.1 准确率指标

| 指标 | 实现 | 说明 |
|---|---|---|
| `cer` / `wer` | `rapidfuzz` | AliMeeting 用 FIFO `<sc>` 拼接（M2MeT 官方口径） |
| `punct_p/r/f1` | 字序列对齐后比位置+类型 | 参考无标点 → N/A |
| `itn_acc` | `WeTextProcessing` 实体级 | 无 ITN 参考 → N/A |
| `der` | `pyannote.metrics`，collar 0.25 单侧、不跳重叠 | md-eval 作 CI 门禁 |
| `sa_der` | 固定身份映射（禁用最优映射） | 分离+命名联合质量 |
| `naming_acc` / `fa` / `fr` / `eer` | `matched_id` + `confidence` | 阈值必须进口径 |

### 6.2 性能指标（`run.kind=perf`，独立任务）

| 指标 | 公式 | 有效条件 |
|---|---|---|
| `ttft_ms` | 首块音频 → 首个非空 partial | 流式 + 1x |
| `ttfa_ms` | 首块音频 → 首个 segment | 非流式 |
| `partial_interval_ms` / `partial_lag_ms` | 相邻间隔 / 已发送−partial.end | 流式 |
| `finalize_ms` | 最后一块音频 → final | 任意 |
| `rtf_dev` | `inference_ms / audio_duration_ms` | 需 warmup、固定开关与并发 |
| `rtf_e2e` | `(首块音频 → final) / duration` | **仅 burst** |
| `rtf_realtime` | `finalize_ms / duration` | 1x 跟播能力 |
| `throughput` | `Σduration / Σwall` | burst + 并发扫描 |

### 6.3 口径矩阵（数据集 × 指标）

| 数据集 | CER | 标点 | ITN | DER | 命名 |
|---|---|---|---|---|---|
| AISHELL-1 / AISHELL-3 / KeSpeech | ✅ | ✗ | ✗ | ✗ | ✗ |
| AliMeeting（官方 Test，20 段 / 10.75h） | ✅ | ✅ | ✗ | ✅ | ✅ |
| MagicData 沪 / 湘 / 赣 | ✅ | ❌ 句末标点率 0.6~12% | 视标注 | ✅ | ✅ |
| APY 粤 / 川 / 闽 | ✅ | ❌ 统一按不支持 | 视标注 | ✅ | ✅ |
| APY 四川话 | ✅ | **✗**（参考无标点） | ✗ | ✅ | ✅ |

---

## 7. 执行：一次评测长什么样

```
选 Benchmark → 勾设备 → 【自动】能力预览 → 开始
   → probing → preparing(声纹) → running → completed
   → 自动创建 Evaluation → scoring → 评分完成
```

- **能力预览**先告诉你"每台设备能评什么、缺什么、为什么"，再决定跑不跑
- 指标默认**全算**（打分零成本），展示层可选；只有声纹有额外成本
- 设备**整机独占**；Run 内**默认单路**。`accuracy` 用 burst，`perf` 用 1x + 30ms 分片，仅 `perf` 可显式开启多路并发
- 失败：单条重试 2 次；连续 5 条熔断；`no slot` 立即熔断；支持断点续跑

---

## 8. 关键实测结论（决定实现细节）

### 8.1 协议坑（必须封在 Adapter 里）

| # | 事实 | 正确处理 |
|---|---|---|
| 1 | `final.text` 恒为 `"。"` 或空 | 转写文本 = 累加 `transcript.segment.sentences` |
| 2 | `sentences[].spk` 可能不存在且过程中会变 | 过程中的 `spk` 只作观测；最终说话人只取 final `global_spk_list[i]` 位置对齐 |
| 3 | `global_spk_list` 长度 = 句数 + 1 | 末尾是句末标记，不能用 `[-N:]` |
| 4 | `streaming=false` 无 partial | `ttft` 记 N/A，不得用 wall 冒充 |
| 5 | `usage.rtf` 分母是**语音时长** | 禁用；用 `inference_ms / audio_duration_ms` |
| 6 | RTF 随长度升高 | 必须分时长桶 |

### 8.2 能力声明可信度

| 字段 | 可信性 | 依据 |
|---|---|---|
| `streaming` | ✅ 可信 | 两设备连续 3 次一致，与行为 2/2 吻合 |
| `punctuation` | ❌ 不可信 | 声明 false，实际 400/400 输出含标点 |
| `usage` | ⚠️ 部分可信 | `inference_ms` 与客户端 span 吻合；`rtf` 口径不同 |

### 8.3 测量有效性

```
chunk 30ms  → ttft 0.976s   ✅（业务口径）
chunk 20ms  → ttft 0.980s   ✅
chunk 100ms → ttft 0.962s   ✅
chunk 1s    → ttft 0.076s   ❌ 假值（低 13 倍）
```

```
分离开→关：rtf_dev 0.384 → 0.272（-29%）
```

→ 性能数字必须声明：**音频集、分片、节奏、并发、功能开关、warmup**。

### 8.4 参考量级（用于回归判断）

| 场景 | 结果 |
|---|---|
| AliMeeting 120s 四人（近似 DER） | ~8.6%（collar 0.25 单侧，排除重叠帧） |
| 同段 CER | 30.6% |
| 命名 | 准确 70.4% / 错认 4.2% / 拒识 25.4% |

---

## 9. 分期

| 阶段 | 周 | 交付 |
|---|---|---|
| P0 | 1 | PG schema、Adapter SPI、`RemoteStaging`、AISHELL-1 导入、CER |
| P1 | 3 | 6 类 Importer、Benchmark、Run、矩阵、SSE |
| P2 | 2 | 说话人模式、DER/SA-DER/命名/EER、md-eval 口径验收 |
| P3 | 2 | perf Run（双 RTF / 并发档 / 分层抽样）、回归显著性、Badcase、LDAP |
| P4 | 持续 | 前端打磨、导出、CI 门禁、客家话扩充 |

---

## 10. 文档索引

| 文件 | 内容 |
|---|---|
| `asr-eval-overview.md` | 一页总览（本文件） |
| `asr-eval-design-review.md` | 全量评审与 12 项决策 |
| `asr-eval-platform-design.md` | 总体设计：领域模型、架构、存储、分期 |
| `asr-eval-implementation.md` | 实现细则：DER 引擎、导入状态机、实测记录、两套时延、任务拆分、验收清单 |
| `asr-eval-verification.md` | 5 项待验的实测报告 |
| `asr-eval-uiux.md` | 前端 IA、页面规格、交互模式 |
| `m1-dataset-design.md` | M1 数据接入：Importer、subset、版本、官方口径 |
| `m2-device-design.md` | M2 设备与部署：探测、指纹、暂存、声纹自检 |
| `m3-benchmark-design.md` | M3 评测定义：MetricProfile、Benchmark、采样、可用性 |
| `m4-runner-design.md` | M4 执行引擎：Run/Evaluation、租约、ETA、中断 |
| `m4-eta-design.md` | ETA 基线派生与滚动估算 |
| `m5-scoring-design.md` | M5 打分聚合：指标实现、CI、覆盖率 |
| `m6-report-design.md` | M6 报告与对比：快照、配对、显著性、导出 |
| `m6-report-ui-design.md` | 报告页面设计：五类场景视图、视觉方向、图表栈 |
| `m7-api-auth-design.md` | M7 API 与鉴权：REST、SSE、job、LDAP/JWT/RBAC、审计、OpenAPI |
| `m8-frontend-design.md` | M8 前端：路由、数据层、组件树、权限驱动 UI、性能与测试 |
| `m9-platform-design.md` | M9 平台基础：部署拓扑、配置密钥、迁移、备份恢复、可观测性、运维手册 |
| `m10-tooling-design.md` | M10 工具与门禁：仓库结构、测试分层、golden 样例、Mock 设备、性能基线、CI、发布 |
| `tools/probe_etsstt.py` | 设备探测脚本（能力 / 流式 / 分离 / 命名 / 时延） |

---

## 11. 待验 5 项 → 已验（详见 `asr-eval-verification.md`）

| # | 项 | 结果 |
|---|---|---|
| 1 | 502 网关 3 路并发 | ✅ **3/3 成功，含 3×30min**；算力只放大 1.3~1.4x，单流 rtf 0.116→0.243（仍 <1） |
| 2 | 30min / 长音频 | ✅ **非流式与流式均通过**；1800s 流式 58 partial + 251 segment + final |
| 3 | 网关容器可写 | ✅ `/mnt` PVC 可写，`exec -i cat` 写入 md5 一致，声纹+命名闭环通过 |
| 4 | 昇腾版本字段 | ✅ driver 25.2.0 / ascendhal 7.35.23 / `gjwg-310p-24g-arm64`；无模型版本键 |
| 5 | 客家话正式集 | ✅ 无可公开获取的评测集（HAT 为 closed 且 eval=0h）→ 自建 |

**由此得出的三条硬规则**

1. **禁止强杀转写进程**；超时必须优雅 close。强杀会让引擎卡在"只收 AudioEnd 不产 segment"，后续全部 `engine_overloaded`。
2. 会话看门狗：`audio.end` 后超过 `max(30s, 2×rtf×duration)` 无 final → 主动 close 并记 `finalize_timeout`。
3. 3 路并发 ETA 按 `rtf ≈ 2×单路` 估算；`engine_overloaded` 且无活跃会话 → 设备标记「需重启」，平台不自动重启。

**30ms 性能基线（180s × 2 轮，可入库）**

| 并发 | 成功率 | ttft med/P95 | lag med/P95 | finalize med/P95 | rtf_dev |
|---|---|---|---|---|---|
| 1 | 2/2 | 968 / 970 ms | 30 / 45 ms | 635 / 636 ms | 0.11 |
| 2 | 4/4 | 1008 / 1026 ms | 60 / 90 ms | 695 / 712 ms | 0.12 |
| 3 | 6/6 | 1018 / 1066 ms | 90 / 120 ms | 1275 / 1295 ms | 0.23 |

> 3 路下首字仅 +5%、lag <120ms、rtf_dev 0.23（仍有 4 倍余量）；尾延迟翻倍但仍 <1.5s。
