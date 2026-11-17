# 设计方案一致性审计与修正记录

> 2026-11-04：按用户要求从 P0 起重新对照 `docs/`（M1~M10）逐模块审计已完成代码，修正不一致后再按设计方案顺序推进。
> 门禁：`make test`（43 passed）+ `make lint` 全绿后才提交。

## 1. 审计范围与结论

四个并行审计：M1+M3、M2+M4、M5+M7、M6+M8（前端）。总体结论：
主干口径（CER micro、JWT 形态、权限矩阵、probe 指纹、多目标 Run/SSE 骨架、视觉令牌）与文档一致；
但契约层普遍"实现落后于文档"，已按下表修正。

## 2. 已修正（本轮提交）

| 模块 | 文档依据 | 修正内容 | 提交 |
|---|---|---|---|
| M5 scoring | §2.1/§5.2 | `cer()` 返回 S/D/I 分解；`<sc>` 剥离（strip_sc 参数）；逐样本嵌套结构 `{cer:{status,value,s,d,i,n}}` | bf4eff5 |
| M4 runner | §5.4/§5.6/§5.10 | 连接错误重试 2 次（1s/3s）；连续 5 条失败熔断；overload 中断 target 并标 needs_restart；设备占用 target 保持 pending；failed/auto_score=false 不评分；空参考落 `na(empty_reference)`；覆盖率分母=计划 case 数（≥0.95 ok / partial） | 50fd646 |
| M7 auth | §2.1/§2.2/§4.2/§4.7 | refresh 复用检测（整族失效+审计）、30s 幂等宽限（Fernet 加密暂存）、7d 滑动过期；access token 校验 sid 撤销；422/500 统一错误体；X-Request-Id 中间件；登录成败审计（迁移 0007：audit_log/refresh_grace） | b88e4cc |
| M2 device | §5.3.1/§6/§2.3 | probe_lite 租约联动（被独占 409）；init 超时重试 1 次；duration_ms 口径；realtime_slots 默认（网关 3/整机 1） | 1a9b215 |
| M4 runner | §4-§6/§5.9 | ETA 四级取值（deployment→model_key→kind→fallback）+ 时长桶 + 创建首估 + 每 10 条滚动修正（剔 warm-up，按桶 median）；cancel/resume（限本人/只补缺失）；case_result 落 server_ms/rtf_dev；迁移 0009 | d39cb73 |
| M4 runner | §5.8 / D-M4-6 | 执行使用冻结 deployment；有身份元数据的目标在派发前核验，执行中每 5 分钟巡检 SN/UID；变化时通过协议 close 优雅中断，保留已完成样本、不自动评分；新增 Run 终态、SSE 事件和前端指纹横幅/逐样本查看；迁移 0030 | 本轮 |
| M3 benchmark | §2/§5.3/§5.6/§7 | subsets JSONB 多选（缺省 primary）；version_hash（幂等复用）；采样按 subset 独立执行；未 ready 409（迁移 0008） | 6790f75 |
| M3 benchmark | §2.1/§3/§5/§7 | MetricProfile 名称版本唯一且引用后不可变；版本 hash 忽略展示阈值；可用性解析覆盖数据/设备能力、口径开关、覆盖阈值与声纹注册前置；基准预览返回确定性抽样条数、时长、桶分布、缺口和设备矩阵；ETag 更新、归档、权限与数据表格 UI | 本轮 |
| M1 dataset | §5.5/§5.6/§5.7/§8、M7 §5.1 | capabilities 按参考文本、分段、说话人和 ITN 覆盖实算；重叠率按多人并发时间并集 / 说话人标注总时长计算；APY/MagicData 按设计强制关闭标点能力；新增版本能力查询，返回加权汇总和逐 subset 能力；manifest.jsonl.gz+meta 与 manifest_hash（重复导入同 hash，已验证）；pair_key（APY）；E_DUP_KEY 保留首条+错误分组报告；RefText(kind=itn) 契约位 | 550556d / 本轮 |
| M7 gate | §12 / D-M7-15 / D-M7-17 | PostgreSQL 门禁策略表与迁移 0025；绝对阈值、基线差值、覆盖率及缺失策略判定；策略 CRUD、幂等创建、If-Match、事务审计和权限声明 | 本轮 |
| M7 资源矩阵 | §5.1/§9.4、M2 §5.5/§6、M9 §5.2 | 补齐部署详情、设备安全删除、说话人档案/注册音频/声纹提取与向量查询；本地 ObjectStore 原子写入；声纹按 enrollment/deployment 缓存 192 维向量，失败单次重试并与设备任务互斥；迁移 0035 | 本轮 |
| M7 OpenAPI 契约 | §8.1/§8.2 | 导出 `docs/openapi.json` 与当前基线，生成 `web/src/api/schema.d.ts`；新增 `make openapi` 和基于生成 schema 的 `openapi-fetch` 请求底座，沿用统一 token、trace、401 刷新和错误解析 | 本轮 |
| M7 job | §6 / D-M7-6 / §7 | 统一 Job 表与迁移 0026；导入、设备探测、声纹自检和报告生成返回 `202 + Location`，支持幂等键、任务查询、对象级取消与一次性 ticket SSE；比较页和 Run 详情页轮询报告 Job 并展示阶段 | 本轮 |
| M6 对比结论带 | M6 §5.2、`m6-report-ui-design.md` §1.4、`docs/v2.html` | 增加 sticky 五指标结论带，取所选基线 Evaluation 的总体 `metric_value`，保留状态、样本数与 CI；总体值缺失时显示 `—`；CER 差异与区间统一按百分点展示 | 本轮 |
| M8 对比托盘 | M8 §4/§7.2、`docs/v2.html` | 托盘按 v2 固定为 46px，有内容才吸底；使用 `localStorage`（`asr-eval:tray`），兼容迁移旧 `sessionStorage`；最多 4 个 Evaluation，缺评分与满额均有中文 toast；支持鼠标/键盘排序、首项基线、对比 URL 深链及 Esc 收起/展开 | 本轮 |
| M8 Run 矩阵抽屉 | M8 §5.1/§9.4/§12 | 抽屉显示 CER 分布与最多 20 条最差样本；参考文本只按 verbatim 计数，末档覆盖 ≥60%；原生模态支持焦点限制/归还、Enter/Esc、空/错/重试态并防止过期请求覆盖；API 非成功响应统一抛出 ApiError | 本轮 |
| M8 声纹库 | M8 §9.11、M7 §5.1、`docs/v2.html` | 说话人卡片与 deployment 向量覆盖状态、注册 WAV 上传/试听、私有音频流与权限矩阵、多设备提取 Job 进度入口；1440/1024 交互、Axe 与构建验收通过 | 本轮 |
| M8 管理中心 | M8 §9.13、M7 用户/Token/门禁契约、`docs/v2.html` | 用户角色与授权管理、一次性 Service Token、If-Match 门禁策略、数据库/存储/NFS/设备/版本/队列状态；新增受保护的系统状态端点；1440/1024 Playwright、Axe 与全量测试验收通过 | 本轮 |
| M8 SSE | M8 §5.4/§6.3/§6.4、M7 §7 | 接入用户级与 Run/Evaluation/Job 资源流；一次性票据重连携带 `last_event_id`，服务端兼容请求头续传；进度事件直写缓存，用户事件合并失效；增加退避重连、resync 对账、连接状态提示及断流轮询回退 | 本轮 |
| M7 元数据 | M1 §3.3/§7.2、M7 §5.1 | 新增 `GET /api/v1/meta/enums`，提供中文语言/方言/性别/年龄段/split/指标/原因码映射，声明 `meta:read`；匿名请求返回 401；权限与 OpenAPI 契约加入静态门禁 | 本轮 |
| M1 参考标注 | §3.1/§4/§7.1、M5 §5.5 | `ref_segment` 增加 `skip/overlap`；新增版本级 `speaker_meta`；AISHELL-3、AliMeeting、APY、MagicData 产出完整说话人/分段标注，DER 与命名过滤 skip 段 | 本轮 |
| M1 样本 API | §3.2/§3.3、M7 §4.4 | 新增 `GET /items/{id}` 完整参考标注详情；版本样本列表支持 split/bucket/文本检索与不透明游标，不返回 total | 本轮 |
| M1 版本生命周期 | §3.2/§3.3、M7 §3.5/§6 | 删除版本时阻止 Benchmark/活动任务引用、同事务记录审计并清理受管 manifest；新增异步 `reanalyze` Job，从已落库文本/分段重算 subset 与版本能力 | 本轮 |
| M1 音频变体与录音条件 | §3.3/§5.4/§5.6、§6 | 新增 `AudioVariant` 懒生成缓存；Runner 与样本音频接口按 subset profile 使用 16k 单声道；AliMeeting far 保留源文件并运行时取 ch0；APY 同时导入 phone/mic 并按 session/domain 配对；AISHELL-3 重采样为 16k；迁移 0028 修正历史 profile | 本轮 |
| M1 导入边界与性能 | §5.1/§5.2/§6/§8/§9.3/§10 | 新增客家话 MP4+SRT/TXT 导入，TXT 按内容识别字幕时码，音轨抽取至受管 16k mono 缓存并保留源路径；DataTang 映射 LHD 导入器；坏音频跳过并分组报告；分段越界修正；失败阈值按失败样本计数；补齐 1 万条导入与列表性能验收 | 本轮 |
| M2 探测素材 | §附录 A | 按用户授权制作 A/B 两段单人注册音频和 10 秒双人重叠音频；附逐段参考文本、生成脚本、声线/响度/哈希清单，不使用开源数据集录音 | 本轮 |
| M2 声纹协议 | §3.2/§5.2.1/§5.6、`asr-eval-verification.md` §3 | 新增设备侧声纹提取适配器，校验 192 维有限数值向量；转写 init 支持 matching 模式、声纹组、阈值和最大说话人数 | 本轮 |
| M2 声纹链路自检 | §5.2.1/§5.4/§6、M7 §6/§7 | 新增 etsstt SFTP 与网关 exec-cat/kubectl cp 暂存、远端 SHA-256 校验和 30 分钟缓存；新增声纹自检 Job，验证 A/B 192 维提取及至少一个置信度大于 0.5 的 matching 回显并更新 `observed.naming`；首次部署或 UID 变化时空闲自动排队；设备行锁串行化 probe/自检创建，Run 租约拒绝活动 probe/自检；支持幂等创建和任务取消；迁移 0029 | 本轮 |
| M2 身份采集 | §2.4/§5.1.1/§5.3.1/§7.1 | 新增 SSH/kubectl 固定只读版本命令及 `/v1/models`、`/v1/system/info` 采集；解析 SN、平台、模型、量化与 SoC，按 UID upsert 部署快照；版本采集部分失败时降级；Run 租约期间允许 identity 并拒绝 lite；SN 唯一冲突保护 | 本轮 |
| eval badcase | M8 §9.7 / §9.7.1、M6 §2.5 | 逐样本列表/明细/对齐/音频 API；新增 CER/语言/错误类型筛选及 URL 深链、路径/泳道图例/键帽细节、WaveSurfer 就绪与错误态 | 本轮 |
| M8 权限 UI | M8 §8 / §3.2 | `/auth/me` 权限缓存复用；各页面路由守卫、缺权限 403、侧栏与全局导航裁剪；统一 `Can` / `useCan`，按策略禁用并说明或隐藏操作；登录切换清理旧用户缓存 | 本轮 |

验证证据：
- 集成冒烟：宽限幂等（同新 refresh cookie）、31s 后复用 `E_REFRESH_REUSE`+整族失效、access 立即 401、E_VALIDATION 错误体、X-Request-Id 回显；
- 数据验证：AISHELL-1 重导入 50 条 ×2 → manifest_hash 一致（`263bb7ef…`），punct_sentence_ratio=0.0（正确）；
- 旧数据兼容：matrix/badcase 对旧扁平 metrics 用 coalesce 回退。
- Job 验收：旧导入任务成功/失败记录在隔离 PostgreSQL schema 完成迁移转换；导入、设备探测、报告生成、任务查询、创建者/管理员权限、取消、SSE ticket 和幂等创建测试通过。
- 报告 Job 验收：`make test` 198 passed，`make lint` 全部通过；`npm run build` 通过，`npm run lint` 通过（保留仓库既有告警）。Playwright 模拟 API 验证比较页与 Run 详情导出流程、阶段提示、完成导航、离开页面后停止前端轮询；1440px 与 1024px 页面无控制台或页面异常，Axe 两页均为 0 violation。
- 元数据契约验收：`make test` 200 passed（包含数据库集成验证标签与匿名 401、静态权限检查和 OpenAPI 路径）；`make lint` 通过。
- 能力查询验收：`make test` 203 passed；定向验证版本汇总、subset 独立值、主 subset 标记、404、`dataset:read` 权限和 OpenAPI 路径；`make lint` 通过。
- 能力计算验收：用户提供的 AliMeeting `Test_Ali_far` 20 场真实导入为 `ready`；实际结果 `segment_ratio=1.0`、`speaker_ratio=1.0`、`overlap_ratio=0.162`，与 M1 验收值相符。补充区间并集、重复说话人区间及标点覆盖规则单测；全量 `make test` 206 passed，`make lint` 通过。
- 参考标注验收：全量 `make test` 207 passed，`make lint` 通过；真实 MagicData 沪语首条扫描得到 449 个分段、6 个 skip、2 位说话人，真实 APY 粤语首条得到 264 个分段、2 位说话人。集成测试验证 APY 的 item.speakers、ref_segment 时间戳及 speaker_meta 字段均已落库。
- 样本 API 验收：全量 `make test` 210 passed，`make lint` 通过；集成验证详情中的 verbatim/ITN、skip/overlap、speaker_meta，列表的筛选、游标续页、非法游标和不存在版本响应。
- 版本生命周期验收：全量 `make test` 213 passed，`make lint` 通过；验证无引用版本删除与审计、Benchmark 引用阻止删除、reanalyze 返回 202 Job 并重建 segment/speaker/ITN/overlap/punctuation 能力。
- 音频变体验收：全量 `make test` 216 passed，`make lint` 通过；ffmpeg 集成验证 mono_mix/mono_ch0 输出均为 16k 单声道并命中缓存、样本音频接口默认采用 subset profile；APY phone/mic 共用 pair_key；真实 APY mic 与 AliMeeting far 音频转换成功；0028 迁移离线 SQL 检查通过。
- 探测素材验收：全量 `make test` 217 passed，`make lint` 通过；独立素材测试验证 3 个 WAV 的 16kHz/16bit/mono、6/6/10 秒时长、SHA-256、响度差≤2dB、说话人各≥3秒、中段 800ms 重叠及逗号/句号/问号。
- 声纹协议验收：全量 `make test` 219 passed，`make lint` 和 mypy 通过；Mock HTTP 验证设备路径、model、timestamp_ranges 及 192 维响应；Mock WS 验证 init matching 字段和 matched_id 解析。
- 声纹链路自检验收：远端暂存、`kubectl cp` Pod 解析、上传摘要校验、matching 成败、deployment 能力回写、ProbeRun、30 分钟缓存、幂等重放、设备租约拒绝、活动探测/自检期间阻止 Run 抢租约、UID 变化自动排队及 queued/running 取消均有测试覆盖；定向测试 14 passed，全量 `make test` 228 passed，`make lint` 和 mypy 通过；迁移 0029 离线 SQL 确认创建 `staging_probe` 并推进版本号。
- 身份采集验收：覆盖 RKNN 型号键解析、Ascend system/info 字段、etsllm/get_version 版本采集、网关 kubectl 目标、版本部分失败降级，以及 Run 租约期间 identity 允许 / lite 拒绝；全量 `make test` 233 passed，`make lint` 和 mypy 通过。
- 条件变化中断验收：身份变化时发送 WebSocket `close`、停止当前 target、保留已完成 case、不创建 Evaluation；部分版本字段缺失时不误报变化。全量 `make test` 235 passed，`make lint` 通过；前端 `npm run build` / `npm run lint` 通过；Playwright 验证中断筛选、旧/新指纹、已完成样本，1440/1024 无横向溢出，axe 0 violation、无页面异常；0030 离线迁移 SQL 检查通过。
- M6 对比页验收：所选基线的 CER、标点 F1、DER、命名准确率与设备 RTF 展示在 sticky 结论带，缺失聚合不拼算；CER Δ 与 CI 使用 pp。Playwright 模拟接口验证五项读数及配对 URL 状态，1440/1024 无横向溢出、axe 0 violation、无页面异常。
- M8 对比托盘验收：Playwright 验证旧 sessionStorage 迁移、最多 4 项及满额提示、无评分提示、刷新持久化、鼠标与键盘排序、首项基线、对比 URL、清空和 Esc 收起/展开；1440×1000、1024×900 均无横向溢出或页面异常，axe 0 violation。`npm run build` 与 `npm run lint` 通过（保留既有告警）；主 JS gzip 220.95KB，符合 M8 ≤250KB 预算。
- M8 Run 矩阵抽屉验收：后端集成测试覆盖 24 条样本、按 CER 降序取 20 条、verbatim 去重及 ≥60% 开口分桶；Playwright 验证 Enter 打开、失败后重试、20 条链接、直方图零值、Esc 关闭和焦点归还/Tab 限制。1440×1000、1024×900 均无横向溢出、页面异常或 axe 违规；全量 `make test` 245 passed，`make lint` 通过；前端 build 主 JS gzip 222.13KB，lint 通过（保留既有告警）。
- 前端字号归一验收：页面与 shadcn 基础组件统一使用 `--type-*` CSS 尺度变量及可合并字号工具类；常用小字归并到 11px，主尺度覆盖 11/12/13/15/20/29px，保留 v2 明确的 22px 页面标题、13.5px 小节标题及 9/9.5px 图表微标注。Playwright 在 1440×1000、1024×900 实测标题/小节/KPI/正文/抽屉标题及小号按钮为 22/13.5/29/11/15/11px，无横向溢出、page error 或 axe 违规；`npm run build` 通过，主 JS gzip 222.31KB；`npm run lint` 通过（保留既有告警）。
- M8 Badcase 工作台验收：筛选 API 支持 CER 严格阈值、语言、替换/删除/插入及 bucket 条件，前端将 CER/语言/错误类型写入 URL；修正 WaveSurfer 必须解码完成后才显示就绪，并展示解码失败态。全量 `make test` 247 passed；`make lint` 通过。Playwright 模拟接口验证筛选请求、筛选后选中项、J/K、1-9 打标、长音频通读/概览；1440×1000、1024×900 波形均正确显示、无横向溢出或页面/控制台错误，Axe 0 violation；`npm run build` 通过，主 JS gzip 222.36KB，`npm run lint` 通过（保留仓库既有告警）。
- M8 权限 UI 验收：Playwright 验证只读权限直达受限路由时保留 URL 并给出可访问返回入口、侧栏裁剪、无权限探测操作禁用与键盘提示、授权后 Run/设备操作启用；构造“admin 角色但只有只读权限”和“viewer 角色但有明确写权限”确认只根据权限数组判定。1440×1000 与 1024×900 无横向溢出，Axe 0 violation，无页面/控制台错误；`npm run build` 通过，主 JS gzip 223.83KB；`npm run lint` 通过（保留仓库既有告警）。
- M8 Run 列表验收：API 增加 benchmark/device/status/q 游标筛选、稳定复合游标、筛选 facets、目标/最新评分/CER/耗时/ETA 摘要；前端 URL 筛选、IntersectionObserver 续页与虚拟滚动、可键盘打开整行、批量选择已评分 Run 加入托盘、新建 Benchmark 子集与多设备向导。Run 创建校验 Benchmark 与数据版本、归档状态和 subset。新增 3 个 PostgreSQL 集成用例；全量 `make test` 250 passed，`make lint`（含 mypy）通过，`npm run build` 与 `npm run lint` 通过（保留既有告警）。Playwright 验证筛选 URL/API、续页、批量加入（含无评分提示）、创建请求与详情导航；1440×1000、1024×900 无页面横向溢出，深色 Axe 0 violation，无页面/控制台错误。
- M8 Run 详情验收：支持选择历史 Evaluation 并将 `eval` 写入 URL，摘要/矩阵/分桶/样本/下钻共用评分版本；总览呈现五项指标、CI/Δ 与目标排名，分桶支持时长/语言/方言/场景/性别/划分/来源及 BH 校正，横评可加入托盘，日志使用持久化时间戳并单独说明缺少时间的历史失败样本。新增 `CaseResult.created_at` 可空迁移（旧记录保留 null）及时间线/评分选择集成覆盖。全量 `make test` 251 passed，`make lint`（含 mypy）通过；Playwright 验证五页签、历史评分 URL/API、维度切换、托盘、Badcase 与失败事件；1440×1000 浅色、1024×900 深色均无横向溢出、页面/控制台错误，五页签 Axe 0 violations。`npm run build` 与 `npm run lint` 通过（lint 保留仓库既有告警）；主 JS gzip 232.24KB，低于 250KB 预算。视觉复核去除总览重复矩阵，并将错误文字令牌微调至满足 ≥4.5:1 对比度。
- M1 导入边界验收：真实客家话目录扫描 2 个 TXT 字幕，分别解析 211/345 个时间段；FFmpeg 集成验证 MP4 抽轨产物为受管 16kHz 单声道 WAV、字幕时码入库，损坏媒体跳过并写入 `E_AUDIO_DECODE`；1 万条样本导入与首屏查询分别通过 `<30s`、`<300ms` 门槛；失败率按失败样本计算，修正型分段告警不抬高失败比例。全量 `make test` 245 passed，`make lint` 与 mypy 通过。
- M3 基准与口径验收：MetricProfile 版本、引用后不可变、哈希和可用性原因码有单元/集成覆盖；基准预览、分层采样、桶统计、设备矩阵、ETag 更新与归档通过集成测试。全量 `make test` 270 passed，`make lint`（ruff、format、mypy）通过。前端 `npm run build` 通过，主 JS gzip 197.38KB；Playwright 验证预览更新与失效、保存、归档、创建口径新版本，1440×1000 浅色及 1024×768 深色无横向溢出，5 个页面状态 Axe 0 violation、无页面/控制台错误；`npm run lint` 通过，保留 5 条既有告警。
- M4 Run 调度与预览验收：创建前按 target 展示指标可用性、设备队列位置及 P50/P90 ETA；Run 写入 PostgreSQL `job` 队列，固定 worker 有界派发、启动恢复，设备独占且同设备子集顺序执行、不同设备并行；支持流式 timing、perf lanes、取消/续跑和运行期 ETA 校准。全量 `make test` 280 passed，`make lint`（ruff、format、mypy）通过，PostgreSQL 恢复测试覆盖 interrupted Run/target/Job 的状态回收与重新派发。前端 `npm run build`、`npm run lint` 通过（lint 保留 5 条既有告警，build 有既有大 chunk 提示）；Playwright 模拟 API 验证准确率/性能切换、3 路预览与创建请求、详情跳转，1440×1000 与 1024×768 无页面横向溢出，Axe 0 violations、无页面/控制台错误。Browser 插件不可用，按 `@Build Web Apps` QA 流程使用本机 Playwright；截图：`/tmp/asr-eval-m4-run-preview-1440.png`、`/tmp/asr-eval-m4-run-preview-1024.png`。
- M7 资源与契约验收：部署详情、设备删除约束/审计、说话人注册和声纹提取/缓存通过 PostgreSQL 集成测试；声纹适配器异常会按 M2 §6 重试一次。service token 实测创建 Run 并读取自动评分 CER=0 的结论；三标签页并发刷新均获得同一新 refresh cookie 且 access token 仍有效；运维审计核对操作者、IP、命令、退出码与健康结果。`make openapi` 导出 OpenAPI 3.1 并由 `openapi-typescript` 生成零错误，迁移在隔离 schema 中 upgrade head → downgrade 0034 → upgrade head 成功。全量 `make test` 290 passed、`make lint` 通过；前端 `npm run build` 通过，`npm run lint` 保留 5 条既有告警，build 保留既有大 chunk 提示。Jenkins/CI 接线与失败退出脚本仍按 `AGENTS.md` 的 P0 范围推迟。
- M5 数字近似信号与测试隔离（2026-11-07）：正式 ITN 无金标仍为 N/A；新增 `numeric_cer`，按 CER editops 汇总数字/数词字符误差，写入逐样本、Evaluation、target 和时长桶，并将 scorer 版本推进到 `cer-4`。集成测试覆盖 spoken/written 数词差异。集成套件统一读取 `TEST_DATABASE_URL`，且在测试收集阶段拒绝未标记为 `test`/`e2e` 的数据库。隔离 PostgreSQL 上 `make test` 314 passed，`make lint`（Ruff、格式、mypy）通过；显式指向开发库 `asr_eval` 的安全拒绝检查通过。
- M8 Run 详情数字读数验收（2026-11-07）：CER 主读数下增加数字相关 CER 近似值及样本量，保留五项 KPI 主布局；对比矩阵以中文名称显示该指标，并按 CER 的“越低越好”规则着色。Playwright 假设备主路径验证数值和键盘打开 Badcase，1440×1000 / 1024×900 无横向溢出、Axe 0 violation、无页面异常；`npm run lint` 0 errors（5 条既有 warning），`npm run build` 通过；`make e2e` 9 passed、真实设备用例 1 skipped。截图：`/tmp/asr-eval-run-detail-1440.png`、`/tmp/asr-eval-run-detail-1024.png`。
- M6 报告数字指标展示（2026-11-07）：单次与组合 HTML 报告以中文指标标签呈现 `numeric_cer`，按百分比显示数值和置信区间；XLSX 保留 0–1 数值并通过单元格百分比格式展示，维持数值可计算性。报告模板版本升至 `report-2`；单元测试覆盖标签、HTML 百分比、XLSX XML、样式和值。
- M9 本机诊断（2026-11-07）：`/readyz` 增加可选 NFS 与磁盘水位，`/version` 返回数据库 schema 与代码 migration head；新增只读 `./asrctl doctor`，不执行迁移。`make test` 319 passed，`make lint`（Ruff、格式、mypy）通过；临时 PostgreSQL 升级到 head 后真实 API 自检全绿。当前开发库仍为 `0035_speaker_voiceprints`，代码 head 为 `0036_device_credentials`，doctor 正确报告待迁移；未触碰开发库数据。

## 3. 待修正（按优先级，下一步执行）

**后端**
1. M4：~~ETA 四字段+创建预估+运行修正~~ ✅（d39cb73）；~~cancel/resume 端点~~ ✅；~~RunTarget 冻结 deployment_uid；hypothesis 表与 timing 列~~ ✅（D-M4-11/12 实测：锁定 409、同音频多 subset 只转写 1 次）；~~条件变化中断与展示~~ ✅；~~持久队列、限流 worker、同设备子集串行/跨设备并行、流式 timing、性能路数及创建预览~~ ✅（全量门禁 280 passed，本轮）。
2. M1：~~异步 import_task + 断点续跑 + ready_with_errors；E_PATH_ESCAPE、E_ENCODING；preview/diff 端点~~ ✅（c40c069/3968fde/1911eaf）；~~版本能力查询、样本详情/音频、版本删除与重新分析、APY phone/mic 配对及音频变体~~ ✅；~~客家话字幕/MP4、错误质量报告与性能验收~~ ✅（本轮，M1 §10）。
3. ~~M5：metric_value 补 run_target_id/scope/bucket 列与矩阵响应结构统一~~ ✅（matrix/buckets 端点实测）
4. M7：~~SSE stream-ticket（D-M7-5）~~ ✅（实测一次性语义）；~~E_REFRESH_INVALID 与 audit 查询端点~~ ✅；~~D-M7-15 门禁策略~~ ✅；~~`GET /meta/enums` 元数据标签接口~~ ✅；~~`GET /dataset-versions/{id}/capabilities`~~ ✅；~~D-M7-6 统一导入、设备探测、声纹自检和报告 Job~~ ✅；~~§5.1 端点矩阵与 §14 本地验收项~~ ✅。Jenkins/CI 接线及失败退出脚本按 `AGENTS.md` P0 范围推迟。
5. ~~M3：MetricProfile 迁移到文档 config 结构与七个预置模板；BUCKET_DIMS 注册表~~ ✅（b96432b，11 单测）

**前端（P1 地基 → P2）**
1. ~~数据层地基：TanStack Query + qk 规范；401 单飞刷新+重放~~ ✅（e37e818）；~~URL 即状态（zod）~~ ✅（lib/url.ts，RunsPage 筛选 / RunDetail ?tab=，实测）
2. ~~AppShell 三件套~~ ✅（⌘K cmdk 面板、运行中指示器由用户事件流更新且断线 10s 回退、用户菜单+退出，浏览器实测）
3. ~~Badcase §9.7 重写~~ ✅；~~1-9 打标~~ ✅；~~说话人泳道/三视图~~ ✅（ref_segment + 分段归因 + 通读/概览，34min 实测）
4. ~~字号归一 11/12/13/15/20/29~~ ✅（type-* 角色、v2 标题与微标注例外已验收）；~~Run 目标×指标矩阵下钻抽屉~~ ✅（本轮）；~~托盘 localStorage+dnd-kit 排序+基线~~ ✅（本轮）
5. M8 总览、声纹库、管理中心与 SSE 实时事件流均已完成验收：总览覆盖近 7 天 CER 配对变化、显著回退、运行中任务、设备健康、报告和按权限显示的快捷入口；声纹库覆盖注册音频试听、deployment 向量状态和多设备提取任务；管理中心覆盖用户授权、Service Token、门禁策略与系统状态；SSE 覆盖用户/资源事件、缓存同步、续传与断线回退。

## 4. 执行顺序（严格）

先完成 §3 后端 1-5（M4 ETA 与取消优先，阻塞 P2 性能指标），随后前端地基，再 Badcase §9.7。
每完成一项：`make test`+`make lint` 全绿 → Conventional Commit → 本表勾销。
