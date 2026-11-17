# AGENTS.md

ASR 模型评测平台。后端已起步，前端未开始。**当前阶段：P0 本机开发**（不接 Jenkins、不做备份、不配外部 nginx/TLS）。

## 先读哪里

- 设计与口径：`docs/`（M1~M10）。指标、协议、状态机以文档为准。
- 可执行事实：`Makefile`、`backend/pyproject.toml`、`backend/app/`。两者冲突时以代码/配置为准，并回头修文档。

## 命令

```bash
make setup        # uv sync
make dev          # http://localhost:8080
make test         # pytest
make e2e          # 临时 PostgreSQL + 本地假设备，验证设备台账与登录至 Run 评分主路径
make lint         # ruff + format check + mypy
make fmt          # 自动修复
make openapi-check # OpenAPI 与 baseline 一致性门禁
make drill-backup  # 隔离库备份/恢复演练
make golden        # CER 黄金样例回归
make secrets-check # gitleaks 密钥扫描（Docker）
make commitlint    # Conventional Commits 校验
make matrix-check  # 3 设备 × 3 数据集计划与样本校验
make matrix        # 真机矩阵（需 tools/matrix_plan.json）
make punct-205     # 205 标点 F1（AliMeeting near 100 段）
make gate          # 全量本地门禁
```

- uv 在 `~/.local/bin/uv`（未加入 PATH），Makefile 已处理。
- 前端：`export PATH="$HOME/.local/node/bin:$PATH"; cd web && npm run dev`（5173，`/api` 代理 8080）
- 前端构建：`cd web && npm run build`
- `make e2e` 使用独立临时数据库与本地假设备；不会写开发数据库或访问真机。
- 单测单个用例：`cd backend && ~/.local/bin/uv run pytest tests/test_health.py::test_healthz -q`
- 运行服务：`cd backend && ~/.local/bin/uv run uvicorn app.main:app --reload --port 8080`
- 健康检查：`/healthz`、`/readyz`、`/version`（不在 `/api` 下）
- **不要用系统 Python 3.14**；版本固定在 `backend/.python-version`（3.13）。
- 装新依赖用 `uv add`；PyPI 直连很慢，先设 `UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple`。

## 目录边界

```
backend/app/api/      路由，只做校验/鉴权/序列化
backend/app/core/     配置、日志、存储等基础设施
backend/app/domain/   实体与状态机（待建，按 M1~M6）
backend/app/adapters/ 设备协议实现（待建）
backend/app/scoring/  指标插件（待建）
backend/tests/        unit / integration / golden（逐步补齐）
docs/                 设计文档
web/                  前端（Vite + React + Tailwind + shadcn）
```

依赖方向：`api → domain → core`；`adapters/scoring` 实现 domain 协议。禁止反向。

## 代码约定

- **中文注释**：公开模块/类/函数写中文 docstring；协议坑、算法口径、魔法数字必须写"为什么"并注明文档章节。
- 标识符保持英文。禁止注释掉的旧代码；TODO 用 `# TODO(name, 2026-12): …`。
- `ruff` 显式忽略 `RUF001/2/3`：中文全角标点是有意的，不要"修复"成半角。
- `mypy strict` 已开启；新增模块不要用 `# type: ignore` 绕过，除非写明原因。
- 后端 `package = false`，`app` 通过 `pythonpath=["."]` 导入；不要改成 src 布局。
- 提交 `uv.lock`；不要手改锁文件。

## Git

- Conventional Commits：`feat|fix|refactor|perf|test|docs|build|ci|chore(revert)(scope): 摘要`。
- scope 用：`api/auth/scoring/dataset/device/runner/report/web/deploy/ci/docs`。
- 小步提交：能启动、能跑测试、能回退就提交；重构前先提交可用状态。
- 不要提交密钥、音频、`data/`、`.env`、`node_modules`、构建产物。
- 阶段门禁：`make test` + `make lint` 全绿才进入下一阶段，不允许"先往下写、最后补测试"。

## 领域硬约束（最容易写错）

- 设备 `final.text` 恒为 `"。"`；真实文本在 `sentences[]`。**说话人只信 `transcript.final` 的 `global_spk_list`**：转写过程中的 `sentences[].spk` 会实时变化，只能用于能力观测，不能进入最终映射；`global_spk_list` 与全部句子（含零时长标点句）按位置对齐，长度 = 句数 或 句数 + 1（末尾哨兵）。
- `usage.rtf` 分母是语音时长，**不要用**；性能口径见 M4/M5。
- 流式会话必须优雅 `close()`；强杀会让引擎卡死。平台**不自动重启设备**。
- 设备整机独占；Run 内默认单路，只有 `perf` 任务可显式开多路。设备或模型在执行中变化 → 中断 Run。
- CER：`<sc>` 默认剥离；DER：dscore、collar 0.25、不跳重叠；AliMeeting far 取 ch0；APY/MagicData 关闭标点。
- 报告主体是 **Evaluation**，不是 Run；`phone/mic` 按 `pair_key` 配对，`near/far` 只聚合；分桶显著性默认 BH 校正。
- 图表栈：CSS Grid + Recharts + 自定义 SVG，**不引入 ECharts**；页面只经 `ChartSpec`。

## 前端（开始 web/ 时）

按顺序使用技能，不是写完再补：

1. `frontend-design`：这一屏的信息层级与唯一视觉焦点
2. `shadcn`：先搜/用现有组件，CLI 添加
3. `vercel-composition-patterns`：组合式组件，禁止布尔属性堆叠
4. `vercel-react-best-practices`：瀑布、包体积、重渲染
5. `web-design-guidelines`：键盘、对比度、空/错/加载态

- 视觉令牌与用色规则：`docs/m6-report-ui-design.md` §1（浅色默认、墨蓝只表交互、绿/红只表结论、6px 圆角、无阴影）。
- 页面结构/路由/数据层：`docs/m8-frontend-design.md`；组件只用 `shared/ui`，颜色只用 CSS 变量。
- 字体：Instrument Sans + Noto Sans SC（子集化）+ IBM Plex Mono（仅时间码/ID）。
- PR 附 1440/1024 截图；必须通过键盘主路径与 axe 检查。

## 数据与运行环境

- 只用 PostgreSQL（后续接入）；**不要用 SQLite 分叉**。队列/租约/事件都设计在 PG，无 Redis。
- 对象存本地 `data/objects/`，经 `ObjectStore` 接口；音频只读挂 NFS，不拷贝。
- 本机已有 Docker 与 PostgreSQL 容器；`make db-up` 起库，`make migrate` 建表。Node 仍未安装。
- 真机网段 `192.168.65.x`、`192.168.133.x` 可达；单测不得访问真机，真机测试单独跑。
- 205 的 ASR 只监听设备回环 `127.0.0.1:8701`：先运行 `tools/device_tunnel.sh etsme@192.168.65.181 18705 127.0.0.1 8701`，计划里用 `127.0.0.1:18705`；不要改设备绑定或暴露到局域网。
- 备份 `credentials.key` 是后续一等公民；现在不做备份，但不要把密钥写进仓库。

## 已推迟

Jenkins/CI、备份恢复、外部 nginx/TLS、S3 实现、Prometheus。代码保留接口与配置项，但不要现在实现部署脚本。

## 当前进度

- [x] 骨架、配置、健康检查、lint/test
- [x] 数据库 schema + Alembic + 本地登录
- [x] 设备探测（205 / 502 lite）
- [x] 设备身份采集（SSH / 版本）
- [x] AISHELL-1 导入 1,000 条
- [x] KeSpeech 导入（方言桶）
- [x] AISHELL-3 导入（性别/年龄/口音桶）
- [x] AliMeeting near 导入（far 待 P2）
- [x] APY phone 导入（mic 待 P2）
- [x] 单设备执行与 CER
- [x] 全量 1,000 条验收（CER 1.75%）
- [x] 前端骨架：登录、AppShell、Run 列表、Run 详情
- [x] M8 页面：总览、声纹库、管理中心
- [x] M8 SSE：用户事件流与 Run/Evaluation/Job 资源流、断线续传和轮询回退均已验收
- [x] M10 本机 E2E：真实登录、创建 Run、假设备识别、自动评分与 CER 读数
- [x] M10 真机 E2E：502 ARM 网关 + KeSpeech 真实样本，身份采集、声纹自检匹配、Run CER=0
- [x] M7 LDAP：临时 PostgreSQL 完成真实 LDAP 登录与 `/auth/me` 验证
- [x] 资源生命周期：设备自助登记、Run/Dataset 整组删除、报告删除与引用保护
- [x] 删除/登录/导入进度回归：终态与活动评分、报告引用、基准引用、`auth_mode`、`eta_ms`
- [x] OpenAPI 契约门禁：`make openapi-check` + 单测基线一致性
- [x] 本机备份恢复演练：`make drill-backup`（隔离库与临时 data/）
- [x] 205 标点 F1：AliMeeting near 100 段，99/100 成功，CER 5.74%，标点 F1 0.694；命名需声纹注册，记 na
- [x] 资源删除入口：基准（无 Run 引用）与终态评分（无报告引用）支持删除，含审计与前端确认
- [x] 网关设备可保存 SSH 凭据用于身份采集/健康检查；205 `/v1/models` 不可用时 identity/lite 降级到 `get_version`/`etsstt` 模型键
- [x] 设备详情“身份检查 + 能力探测”合并为“检测设备”：`mode=auto` 空闲跑 full、Run 租约时降为 identity
- [x] 205/600 回环 ASR 改用平台侧 SSH 本地转发（`ssh_tunnel.py`）；移除会修改设备 SSFConfig 的 `prepare` 运维动作
- [x] 0162 真机 `mode=auto` 检测通过：full 12.5s、warnings=[]、大版本 3.2.671、模型版本 1.0.2，设备 online
- [x] 网关设备添加时与 etsSTT 一致，必须选择 SSH 密码或私钥并填写 SSH 用户/端口
- [x] 评分口径列表增加删除入口：未被 Benchmark 引用时可删，引用时 `409 E_METRIC_PROFILE_IN_USE`
- [x] 口径 evaluator 化：官方/预设（AliMeeting、perf_30ms、cer_only）锁定；新增 `/metric-profiles/evaluators`；DER collar/skip_overlap 已接入；UI 隐藏未实现参数
- [x] 口径开关完整化：`cer.enabled`/`perf.enabled` 生效；移除 `cer.join/separator`、`naming.mode/granularity`、`perf.repeat`
- [x] 跨数据集评测组：`RunGroup` + `POST /runs/groups` + `/run-groups/:id` 汇总（按 ref_len 加权 CER）
- [x] APY/DataTang 方言识别：包路径优先于 ACT，闽语包写 `minnan/闽南语`；历史 47 条 item 已回填
- [x] MagicData 方言统一：`wuu/xiang/gan` + `沪语/湘语/赣语`；历史 36 条 item 已回填
- [x] 单条音频实时进度：partial/segment → `RunTarget.stats.live` / `Run.progress.live`，前端显示当前音频百分比
- [x] 评分分析：总览指标明细、DER 错误区间接口与“说话人”页签、Badcase 参考/识别对比与 CER 可见性
- [x] DER 说话人来源收口：只信 final `global_spk_list`（过程中 `spk` 不参与映射），按全部句子（含零时长标点句）对齐；长度不符保留占位符并输出 `na(no_diarization_output)`；final 重发句子整体替换、文本按去重句子重建
- [x] 报告去重与设备信息：单 subset 不再重复 all/far；报告含数据集/版本/设备/部署
- [x] 设备硬件资源：Run 期间每 15s 采样 CPU/内存/NPU/温度，详情「资源」页签与报告硬件资源表

### 当前阻塞（需要真机或外部环境）

- [ ] 3 设备 × 3 数据集平台内横评：205（SSH 隧道）+ 502 已完成 2 设备 × 3 数据集 × 20 条（全部 20/20），205 另完成 3 × 100 条；仍缺 600 的 SSH 目标/地址
- [x] 502 三路 × 5 分钟（1×、300s）：3/3 成功，TTFT 1182/1221ms，RTF 0.176/0.178
- [ ] 502 三路 × 30 分钟：用户要求未经允许不得使用 502，暂停待授权
- [ ] 600 三路 5/30 分钟；600 量化/SoC 快照
- [x] AISHELL-1 7,176 条导入（本地 ext4）：11.60s、618 items/s、ready；作为初步结论，暂不继续优化
- [ ] 真实部署到只读 NFS 后复测耗时，再决定是否优化（本机无 NFS 挂载）
- [ ] 两容器 Compose、30 分钟并发 P95 与内存读数
- [ ] Jenkins / golden / gitleaks / commitlint / nightly 性能（按阶段推迟）
