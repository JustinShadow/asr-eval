# ASR 评测平台

自研硬件 ASR 模型的内部评测平台。当前处于 **P0 本机开发收口阶段**：M1~M8 功能主干已实现，正在补齐真机长项、性能复测与工程化门禁。

## 运行

```bash
make setup     # 安装后端依赖（Python 3.13 + uv）
make db-up     # 启动 PostgreSQL（Docker）
make migrate   # 建表
cd backend && uv run python -m app.cli user add \
  --username admin --password 'admin12345' --role admin
make dev       # http://localhost:8080
make test          # 单元 + PostgreSQL 集成测试
make lint          # Ruff + format + mypy
make e2e           # 隔离 PostgreSQL + 假设备，验证登录到 Run 评分主流程
make e2e-recovery  # SIGKILL 后租约过期与 Run 自动恢复
make p0-check      # 健康/登录/设备/数据集/最近 Run 的 CER（密码可用 P0_PASSWORD 覆盖）
make openapi-check # 后端契约与 docs/openapi.baseline.json 是否一致
make openapi-sync  # 确认契约变更后同步 baseline（会重生成前端类型）
make drill-backup  # 隔离库 + 临时 data/ 的备份恢复演练
make golden        # CER 黄金样例回归
make secrets-check # gitleaks 密钥扫描（Docker）
make commitlint    # Conventional Commits 校验（默认 HEAD~1..HEAD）
make matrix-check  # 校验 3 设备 × 3 数据集计划与样本可读性
make matrix        # 按 tools/matrix_plan.json 执行真机矩阵
make punct-205     # 205 标点 F1：AliMeeting near 100 段（需 18705 隧道）
make gate          # 全量本地门禁：test/lint/golden/openapi/secrets/e2e/commitlint
```

205 的 ASR 只监听设备回环地址，真机测试前先建 SSH 本地转发：

```bash
tools/device_tunnel.sh etsme@192.168.65.181 18705 127.0.0.1 8701
# 另一个终端使用 127.0.0.1:18705；详见 docs/matrix-runbook.md
```
```

WSL/Linux 一键启动前后端（首次运行会同步后端依赖、安装缺失的前端依赖，并执行数据库迁移）：

```bash
./tools/dev-up.sh
```

需要 Docker、uv 和 Node.js。启动后按 `Ctrl+C` 停止前后端；PostgreSQL 保持运行。

登录页默认使用 LDAP，也可切换到平台本地账号；切换方式会随登录请求发送，LDAP 与本地用户都能使用。未提供认证方式的旧 API 客户端沿用 `backend/.env` 的 `AUTH_MODE`（默认 `local`）。一键脚本会在首次启动且数据库里没有 `admin` 时提示设置本地管理员密码，不会覆盖已有账户。

前端：

```bash
export PATH="$HOME/.local/node/bin:$PATH"
cd web && npm install
npm run dev     # http://127.0.0.1:5173，/api 代理到 8080
```

健康检查：

```bash
curl http://localhost:8080/healthz
curl http://localhost:8080/readyz
curl http://localhost:8080/version
```

## 目录

```
backend/    FastAPI + PostgreSQL（M1~M7、M9 基础设施）
web/        前端（Vite + React + Tailwind + shadcn）
docs/       设计文档（M1~M10）与验收记录
tools/      开发、探测、矩阵与恢复脚本
deploy/     本机 PostgreSQL / 测试 Compose
```

## 当前状态

- [x] 仓库骨架、配置、健康检查、数据库迁移
- [x] 本地账号与 LDAP 登录、JWT/refresh、四角色 RBAC、Service Token、审计
- [x] 设备探测、SSH/kubectl 身份采集、部署快照、凭据、声纹自检、租约与运维动作
- [x] 205 / 502 / 600 真机接入；AISHELL-1 单路 100 条 CER 基线
- [x] 7 类 Importer：AISHELL-1、AISHELL-3、KeSpeech、AliMeeting near/far、APY phone/mic、MagicData、SRT/TXT
- [x] 数据集版本、预览、能力、Diff、manifest、断点续跑、重新分析
- [x] MetricProfile / Benchmark、分层采样、分桶、设备 × 子集可用性
- [x] Run 队列、设备独占、取消/续跑、ETA、身份变化中断、进程崩溃恢复、perf 多路
- [x] 评分：CER、数字相关 CER、标点 F1、ITN、DER/SA-DER、命名、EER、CI 与 BH 校正
- [x] 报告快照、对比、门禁策略、Badcase、SSE 实时事件流
- [x] 前端 21 个页面与 1440/1024、Axe 可访问性记录
- [x] 本地门禁：`make test`、`make lint`、`make golden`、`make e2e`、`make e2e-recovery`、`make openapi-check`、`make secrets-check`、`make commitlint`

### 仍待收口

- [x] AISHELL-1 7,176 条导入（本地 ext4）：11.60s，作为初步结论；暂不继续优化
- [ ] 真实部署到只读 NFS 后复测耗时，再决定是否优化（本机无 NFS 挂载）
- [ ] 3 设备 × 3 数据集平台内横评；205 标点/命名 100 条
- [x] 205 标点 F1：AliMeeting near 100 段，99/100 成功，F1 0.694；命名需声纹注册，记 na
- [x] 基准/终态评分删除入口；网关 SSH 凭据编辑；205 无 `/v1/models` 时身份与 lite 探测降级
- [x] 设备检测合并为一个按钮：空闲跑身份+能力，Run 占用时自动只检查身份
- [x] 205/600 回环 ASR 自动走平台侧 SSH 本地转发；不再修改设备 SSFConfig
- [x] 0162 真机自动检测通过：full 12.5s、无 warning、版本字段完整
- [x] 网关设备添加时必须配置 SSH 用户/端口与密码或私钥，与 etsSTT 一致
- [x] 评分口径增加删除入口（未被 Benchmark 引用时可删）
- [x] 官方/预设口径改为锁定 evaluator；UI 只展示实际生效参数
- [x] 口径支持关闭 CER / 性能指标；跨数据集评测组（RunGroup）
- [x] 502 三路 × 5 分钟（1×、300s，3/3 成功）
- [ ] 502 三路 × 30 分钟（未经允许不得使用 502，暂停待授权）
- [ ] 600 三路 5/30 分钟；600 量化/SoC 快照
- [x] 本机隔离库备份/恢复演练（`make drill-backup`）
- [ ] 两容器 Compose、30 分钟并发 P95 与内存读数
- [ ] Jenkins / golden / gitleaks / commitlint / nightly 性能（按 AGENTS 推迟）

## 约定

- Python 3.13，uv 管理依赖
- 中文注释：公开接口写 docstring，协议/算法写"为什么"
- Conventional Commits：`feat(scope): 摘要`
- 测试通过再进入下一阶段
