# M10 · 工具与门禁详细设计

> 目标：**一次 push 到可合并 ≤ 15 分钟**；坏改动在合并前被拦住，而不是在评测结果里被发现。
> 原则：门禁只覆盖"会真坏"的地方——口径、协议、契约、性能；不追求覆盖率数字。

---

## 0.0 当前阶段（2026-11-04 起）

**先实现功能，再接 CI。**

- 不配置 Jenkins；`Jenkinsfile` 作为后续参考保留，不在当前 DoD 内
- 测试在本地跑：`make test`、`make e2e`，结果贴 PR/记录
- 真机测试手动执行，脚本固化，功能完成后接 Jenkins Job
- 阶段门禁仍然有效：**本地全绿才进入下一阶段**

### 本机 E2E 实现记录（2026-11-06）

- 默认 `make e2e` 使用 `deploy/compose.test.yml` 启动 tmpfs PostgreSQL，迁移后写入一次性合成数据、测试账号与假设备快照。
- Playwright 启动本机 FastAPI、WebSocket 假设备和 Vite；Run 用户故事经过真实登录、Benchmark/设备查询、Run 创建、后台转写、自动 CER 评分与详情读数；设备用户故事验证台账与部署摘要可见，并覆盖旧部署快照缺少可选字段的兼容路径。
- Playwright 共 11 条用例，其中 10 条默认运行、1 条真机用例需显式配置；异常凭据、外部跳转拦截和弹窗键盘关闭作为补充回归。
- 默认测试结束后删除临时数据库容器及音频目录，不访问开发数据库、外部数据集或真机；真机模式仍使用同一隔离数据库。
- 当前 Codex Browser 工具未向会话提供，故使用 Playwright 管理的 WSL Chromium 完成浏览器验证。

### 真机 E2E（显式启用）

- 通过 `ASR_E2E_REAL_DEVICE_NAME/HOST/PORT/PLATFORM/KIND/SSH_USER/SSH_PORT` 配置一台真实设备，并提供 `ASR_E2E_AUDIO_PATH` 与 `ASR_E2E_TRANSCRIPT`；数据集、Benchmark 和 Run 名称可用对应 `ASR_E2E_*_NAME` 覆盖。
- 用例先执行 `probe_identity` 并验证部署变化自动排入的声纹自检成功，再创建 Run；源音频只读，测试副本放入 E2E 临时数据目录，etsSTT 暂存到 `/data/voiceprint`、网关暂存到 `/mnt/voiceprint`，任务结束后删除暂存文件。
- 若不应改写用户级 `~/.ssh/known_hosts`，调用方可将已核验的临时 known_hosts 路径设为 `ASR_E2E_SSH_KNOWN_HOSTS`。`tools/e2e.sh` 仅在该变量存在时为本次 E2E 子进程启用严格校验的 SSH/SFTP wrapper；文件内不保存设备指纹或密钥。
- `tools/probe_etsstt.py` 可独立执行 205 / gateway 的 WS 分离探测与可选 A/B 声纹自检；命令保存的 JSON 摘要不含 embedding，且会清理设备暂存文件。
- 2026-11-06 验收：205 `etsstt` 真机使用 AISHELL-1 单条真实 WAV，完整登录、身份采集、声纹自检、Run 识别及 CER 评分；Playwright 9/9 通过，Run 详情 `CER n=1` 且样本行可见。验收后 E2E 数据库、音频目录及设备暂存音频均已清理。
- 2026-11-07 复测：205 使用与 600 相同的 AISHELL-1 `BAC009S0901W0288.wav` 再跑真实设备 E2E；识别参考一致、CER=0，Run 用时 3,128ms；A/B 声纹自检均 192 维并成功匹配。Playwright 11/11 通过，临时数据库、音频副本和远端声纹文件均已清理。
- 2026-11-07 验收：502 ARM 网关真机使用 KeSpeech 单条真实 WAV，SSH 身份采集、声纹暂存/提取/匹配、Run 识别、自动评分和 Badcase 全链路通过；参考文本“去年六月的饥荒今年恐难再演”，识别文本“去年六月的饥荒，今年恐难再演。”，CER=0，Run 用时 876ms。Playwright 11/11 通过；E2E PostgreSQL 和音频副本自动清理，网关声纹暂存文件验收后确认已删除。
- 2026-11-07 验收：600「宇视0007」使用 AISHELL-1 单条真实 WAV，临时转发 ASR、系统信息和 root SSH；身份检查、声纹自检、Run 识别、CER 与 Badcase 全链路通过，A/B 声纹均 192 维，设备匹配置信度 0.8394 / 0.6543，CER=0（23 字），Run 用时 3,473ms。Playwright 11/11 通过；E2E PostgreSQL、音频副本、SSH agent、隧道和临时 known_hosts 自动清理，远端 A/B 声纹暂存文件确认删除，开发数据库未改动。
- 2026-11-07 SSH 重试：600 root key 登录及 Ed25519 指纹复核通过；临时 SSH 转发后用真实 AISHELL-1 测试音频执行 `bench_aishell_matrix.py --limit 1`，1/1 成功、CER=0（0/13），TTFT=1,229ms、finalize=1,474ms、`rtf_dev=0.319`。未写数据库；开发库仍为 1,160 条 item、0 个有效租约，临时 known_hosts 与隧道均已清理。
- 2026-11-07 本地门禁：`make test` 339 passed，`make lint` 中 Ruff、格式检查与 mypy 全绿；默认 `make e2e` 10 passed、1 条需显式配置的真机用例按预期跳过。集成测试连接独立 `asr_eval_test` PostgreSQL；开发库在验证后仍为 1,160 条 item、0 个有效租约。
- 2026-11-07 进程恢复 E2E：`make e2e-recovery` 在独立 `asr_eval_test` schema 内强杀并重启 app，等待 PostgreSQL 120 秒租约自然过期后 Run 自动恢复；Run、target、Job、Evaluation 全部成功，1 条 CaseResult、0 个活动租约。临时 schema、数据目录和假设备进程均已清理。

---

## 0. 决策记录

| # | 决策 | 理由 |
|---|---|---|
| D-M10-1 | **单仓库**（backend / web / tools / deploy / docs） | 契约、迁移、前端类型同一次提交，避免版本漂移 |
| D-M10-2 | Python 用 **uv** 管理；Node 用 **pnpm** | 锁文件可复现、安装快 |
| D-M10-3 | 后端 **ruff + mypy**，前端 **eslint + tsc** | 只上两个检查器，减少"工具打架" |
| D-M10-4 | **PR 门禁 < 15 分钟**；重的放 nightly | 门禁太慢就会被绕过 |
| D-M10-5 | 真设备测试只在 **nightly / 手动**，PR 用 **Mock 设备** | CI 机器访问不到 192.168 网段 |
| D-M10-6 | DER/CER 用 **golden 样例 + 外部实现交叉验证** | 自研指标必须和 `md-eval.pl` / `dscore` 对齐 |
| D-M10-7 | 性能基线**只做夜间回归**，阈值 ±20% | 共享 CI 机器噪声大，卡太紧会天天红 |
| D-M10-8 | 覆盖率只卡 **M5 打分 / M1 解析** ≥ 80% | 其它模块用行为测试，不追数字 |
| D-M10-9 | 密钥/大文件/音频**永不入库**；用 `gitleaks` + 体积检查 | 一次泄露/一次 500MB 提交就够痛 |
| D-M10-10 | 发布 = 打 tag + 构建镜像 + 迁移检查；**不回滚数据库** | 迁移 expand/contract，镜像可回退 |
| D-M10-11 | **中文注释**：公开接口写 docstring，协议/算法/魔法数字写"为什么" | 内部项目，降低接手成本 |
| D-M10-12 | 前端每页按 **frontend-design → shadcn → composition → react-best-practices → web-design-guidelines** 顺序开发 | 技能进入流程，不是写完再补 |
| D-M10-13 | **Conventional Commits** + 小步提交；commitlint 校验 | 出错可回退，历史可读 |
| D-M10-14 | 阶段门禁：**上一阶段测试全绿才进入下一阶段** | 防止"最后补测试" |
| D-M10-15 | CI 用 **Jenkins**；无 registry，镜像 `docker save/load` | 内网现状 |
| D-M10-16 | Python **3.13** | 更新、仍有一年+ 生态支持 |

---

## 1. 仓库结构

```
asr-eval/
├── backend/
│   ├── app/
│   │   ├── api/            # 路由（薄层）
│   │   ├── core/           # 配置、鉴权、事件、存储
│   │   ├── domain/         # 实体与状态机（M1~M6）
│   │   ├── adapters/       # etsstt / gateway / fake
│   │   ├── scoring/        # 指标插件（M5）
│   │   ├── workers/        # 队列、scheduler、执行器
│   │   └── main.py
│   ├── migrations/         # Alembic
│   ├── tests/
│   │   ├── unit/
│   │   ├── integration/
│   │   ├── golden/
│   │   ├── fakes/          # Mock 设备、时钟、UUID
│   │   └── conftest.py
│   └── pyproject.toml
├── web/
│   ├── src/                # 结构见 M8
│   ├── e2e/                # Playwright
│   └── package.json
├── tools/
│   ├── probe_etsstt.py     # 真机探测（已有）
│   ├── bench_stream.py     # 30ms 分片基线
│   ├── compare_der.py      # dscore vs md-eval
│   ├── make_fixtures.py    # 生成/裁剪测试音频
│   └── check_openapi.py
├── deploy/
│   ├── compose.yml         # lite
│   ├── compose.standard.yml
│   ├── compose.test.yml    # CI 用 PG
│   └── nginx.sample.conf
├── docs/                   # 本套设计
├── Jenkinsfile
├── Makefile
└── .pre-commit-config.yaml
```

**依赖方向**：`api → domain → core`；`adapters/scoring` 实现 `domain` 的协议；`workers` 调 `domain`。禁止 `domain → api`。

---

## 2. 开发环境

### 2.1 版本

| 项 | 版本 |
|---|---|
| Python | **3.13** |
| PostgreSQL | 16 |
| Node | 20 LTS |
| pnpm | 9 |
| Docker | 24+（本机验证） |

### 2.2 首次启动

```bash
git clone … && cd asr-eval
cp .env.example .env
make setup          # uv sync + pnpm install + 启动 db
make migrate
make dev            # api + worker（热重载）
make web            # 前端 dev server
```

`make setup` 必须 **5 分钟内**完成（不含镜像拉取）。

### 2.3 本地服务

```bash
docker compose -f deploy/compose.yml up -d db
# app 直接跑在宿主机，便于调试
uv run uvicorn app.main:app --reload --port 8080
```

---

## 3. 代码质量

### 3.1 后端

```toml
# pyproject.toml
[tool.ruff]
line-length = 100
target-version = "py313"

[tool.ruff.lint]
select = ["E","F","I","B","UP","SIM","RUF","ASYNC","S"]
ignore = ["S101"]          # assert 允许在测试

[tool.mypy]
python_version = "3.13"
strict = true
plugins = ["pydantic.mypy"]
```

- 只对 `app/core`、`app/domain`、`app/scoring` 开 `strict`
- `app/api` 允许少量 `Any`（序列化层）
- 测试不查类型

### 3.2 前端

```json
{
  "scripts": {
    "lint": "eslint src --max-warnings=0",
    "typecheck": "tsc --noEmit",
    "test": "vitest run",
    "e2e": "playwright test"
  }
}
```

规则重点：

- 禁止页面直接 `import from "recharts"`（只允许 `shared/charts`）
- 禁止硬编码颜色（只允许 `var(--token)`）
- 禁止 `any`（`@typescript-eslint/no-explicit-any`）
- 未使用变量直接报错

### 3.3 pre-commit

```yaml
repos:
  - repo: local
    hooks:
      - id: ruff
        entry: uv run ruff check --fix
        language: system
        types: [python]
      - id: ruff-format
        entry: uv run ruff format
        language: system
        types: [python]
      - id: eslint
        entry: pnpm -C web lint
        language: system
        pass_filenames: false
      - id: gitleaks
        entry: gitleaks protect --staged
        language: system
```

只跑"快"的检查；类型检查放 CI，避免每次提交等 30 秒。

### 3.4 注释与文档字符串（中文）

代码首先是给人读的。约定：

| 位置 | 要求 |
|---|---|
| 模块 / 类 / 公开函数 | **中文 docstring**，写清用途、参数、返回、异常 |
| 状态机 / 协议适配 / 指标算法 | **必须写"为什么"**，并标注来源（设计文档章节或实测结论） |
| 魔法数字 | 必须命名 + 注释来源（如 `COLLAR_SECONDS = 0.25  # AliMeeting 官方口径`） |
| 复杂分支 | 一行说明触发条件，不逐行翻译代码 |
| 标识符 | 保持英文；注释用中文 |

```python
def compute_der(ref_rttm: Path, hyp_rttm: Path) -> float:
    """计算说话人 DER。

    使用 dscore 口径：collar 0.25s、不跳过重叠语音。
    与 md-eval.pl 的差值需 ≤ 0.2pp（见 M5 §5.5）。

    Args:
        ref_rttm: 参考标注。
        hyp_rttm: 系统输出。

    Returns:
        DER，取值 [0, 1]。
    """
```

**禁止**：

- 注释掉的旧代码（用 git 找回）
- `# 设置 x 为 1` 这类复述
- 与实现不符的过期注释（改代码必须同步改注释）
- 大段英文注释（内部项目，中文优先）

TODO 统一格式：`# TODO(zhangsan, 2026-12): 支持 8k 采样率`。

### 3.5 前端开发与技能使用

前端页面不是"照着图写"，按固定流程走：

1. **读设计**：`m6-report-ui-design.md`（视觉/场景）+ `m8-frontend-design.md`（路由/数据/组件）
2. **定方案**：用 `frontend-design` 明确这一屏的信息层级与唯一视觉焦点
3. **选组件**：用 `shadcn` 先搜现有组件，再决定是否新增；不手写已有组件
4. **定 API**：用 `vercel-composition-patterns` 设计组合式组件，禁止布尔属性堆叠
5. **写实现**：用 `vercel-react-best-practices` 检查瀑布、包体积、重渲染
6. **自检**：用 `web-design-guidelines` 跑可访问性/键盘/对比度检查
7. **留证**：PR 附 1440 与 1024 两张截图，标注改了什么

PR 里必须能回答：

- 颜色是否只用了令牌（没有 `#xxxxxx`、没有 `blue-500`）
- 图表是否只经 `ChartSpec`（没有页面直接 import Recharts）
- 键盘能否完成主路径（Tab / 方向键 / Enter / Esc）
- 空态、错态、加载态是否都有
- 是否尊重 `prefers-reduced-motion`

> 技能不是"写文档时用一次"，而是每个页面的开发步骤；PR 模板里保留这份清单。

---

## 4. 测试分层

| 层 | 工具 | 范围 | 目标时长 |
|---|---|---|---|
| 单元 | pytest / vitest | 纯函数、状态机、格式化 | < 60s |
| 集成 | pytest + PG | 仓储、迁移、队列、租约、API | < 5min |
| 契约 | `make openapi-check` | 端点/schema 变更 | < 30s |
| 密钥 | `make secrets-check`（gitleaks） | 凭据/私钥泄露 | < 1min |
| 提交信息 | `make commitlint` | Conventional Commits | < 30s |
| 组件 | vitest + MSW | 数字渲染、矩阵键盘、空/错态 | < 60s |
| E2E | Playwright + Compose | 登录 → 建 Run → 看结果 | < 8min |
| Golden | `make golden` | 指标口径 | < 2min |
| 真机 | `make matrix`（`tools/bench_matrix.py`） | 协议、长音频、并发 | nightly 20min |
| 性能 | bench_stream | ttft/finalize/rtf | nightly 30min |

### 4.1 单元测试重点

- M5：CER/WER、标点 F1、ITN、DER/SA-DER、命名、bootstrap（固定 seed）
- M1：六个 Importer 的字段映射、错误码、路径校验
- M4：状态机、租约抢占、条件变化中断、ETA 滚动
- M7：权限矩阵、ETag、幂等、游标

### 4.2 集成测试

`compose.test.yml` 只起 PG：

```yaml
services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_PASSWORD: test
    ports: ["55432:5432"]
    tmpfs: /var/lib/postgresql/data
```

每个测试用事务回滚或独立 schema；**不 mock 数据库**。

必测：

- 迁移从空库到 head，再插入种子数据
- `job` 领取/重试/可见性超时
- `device_lease` 并发抢占（10 个协程抢同一设备，只有 1 个成功）
- API 鉴权/权限/错误码
- SSE 事件序列（用 TestClient 流式读）

---

## 5. Golden 样例（口径门禁）

### 5.1 目录

```
backend/tests/golden/
├── cer/
│   ├── basic.json          # ref/hyp/expected
│   ├── sc_strip.json       # <sc> 处理
│   └── empty_ref.json
├── punct/
├── itn/
├── der/
│   ├── sample_2spk.rttm
│   ├── sample_2spk.ref.rttm
│   └── expected.json
└── naming/
```

### 5.2 判定

| 指标 | 方式 | 阈值 |
|---|---|---|
| CER/WER | 与手算值比对 | 完全相等（1e-6） |
| 标点 F1 | 固定样例 | 完全相等 |
| ITN | 规则样例 | 完全相等 |
| DER | 与 `md-eval.pl` 比对 | **≤ 0.2pp** |
| SA-DER | 与 `dscore` 比对 | ≤ 0.2pp |
| 命名准确率 | 固定混淆矩阵 | 完全相等 |
| bootstrap CI | 固定 seed | 完全相等 |

> DER 比对脚本 `tools/compare_der.py`：同一份 RTTM 分别跑 `dscore` 与 `md-eval.pl`，输出差值；CI 里跑纯 Python 版，nightly 跑 `md-eval.pl`（需要 perl）。

### 5.3 样例来源

- 公开数据集片段（AliMeeting / AISHELL）**只放 5~10 秒、单说话人、已匿名**
- 单文件 ≤ 1MB，总计 ≤ 20MB
- 大音频不进仓库；nightly 从 NFS 取
- 每个样例带 `source` 与 `license` 字段

---

## 6. Mock 设备

### 6.1 为什么

PR 机器访问不到 `192.168.65.181` / `192.168.133.2`，但协议坑必须被测到。

### 6.2 实现

`tests/fakes/device_server.py`：一个 FastAPI/uvicorn 小服务，按脚本回放帧。

必须覆盖的真实坑：

| 坑 | Mock 行为 |
|---|---|
| `final.text` 恒为 `"。"` | 回放该帧 |
| `sentences[].spk` 缺失 | 只给 `global_spk_list`，长度 = 句数 + 1 |
| 流式 partial 抖动 | 按 30ms 分片回放，带随机 lag |
| 会话强杀卡死 | 测试里验证 `close()` 被调用；不测"强杀" |
| 30 分钟长音频 | 回放压缩时间戳的 1800s 会话 |
| 超时/断连 | 注入 `engine_overloaded`、半途断开 |

### 6.3 使用

```python
@pytest.fixture
def fake_device():
    with FakeDevice(script="streaming_30min") as d:
        yield d
```

真机测试：

```python
@pytest.mark.device
def test_real_gateway_smoke():
    ...
```

CI 默认 `-m "not device"`；nightly runner 带网段访问权限。

---

## 7. 性能基线

### 7.1 指标

| 指标 | 采集 | 阈值 |
|---|---|---|
| ttft | 首帧延迟（30ms 分片） | 基线 ±20% |
| partial lag | partial 与音频时间差 | ≤ 200ms |
| finalize | 最后一帧到 final | 基线 ±20% |
| rtf_dev | 设备侧 | < 1.0 |
| 单 case 内存 | RSS 峰值 | < 200MB |

基线存 `bench/baseline.json`，带机器指纹（CPU 型号、核数）。

### 7.2 执行

```bash
# nightly / 手动
uv run python tools/bench_stream.py --device 192.168.65.181:8701 \
  --audio tests/data/30min.wav --chunk-ms 30 --repeat 3
```

默认扫描 1 到 `--max-concurrency`；长稳态验收可通过 `--min-concurrency N --max-concurrency N` 固定在目标并发档，避免重复压测较低档位。

- 取 3 次中位数
- 与基线比：`abs(delta) / baseline > 0.20` → 失败并输出对比表
- 机器指纹不同 → 只记录不判定（避免换机器天天红）

---

## 8. CI 流水线（Jenkins）

### 8.1 约束

| 项 | 现状 | 应对 |
|---|---|---|
| CI | 局域网 **Jenkins** | 用 `Jenkinsfile`，不依赖 GitHub Actions |
| Docker registry | **没有** | 镜像在 Jenkins 构建后用 `docker save` 归档；部署端 `docker load` |
| PyPI 镜像 | **没有** | 直连 pypi.org + uv 缓存；必要时预下载 wheel 包 |
| Node / pnpm | 可安装（内网可出网） | 锁定版本 + pnpm store 缓存 |
| 设备网段 | 192.168.65.x / 192.168.133.x 可通 | PR 用 Mock；真机放独立 Job |

### 8.2 阶段

| # | 阶段 | 命令 | 阻塞 | 预算 |
|---|---|---|---|---|
| 1 | 静态检查 | `ruff check` + `ruff format --check` + `gitleaks` | ✅ | 40s |
| 2 | 类型 | `mypy app` | ✅ | 60s |
| 3 | 前端静态 | `pnpm lint` + `tsc --noEmit` | ✅ | 90s |
| 4 | 单元 | `pytest -m "not integration and not device"` | ✅ | 2min |
| 5 | 契约 | `make openapi-check`（导出 + 基线比对 + 生成类型） | ✅ | 40s |
| 6 | 集成 | `compose.test.yml` + `pytest -m integration` | ✅ | 5min |
| 7 | 前端测试 | `vitest run` | ✅ | 60s |
| 8 | 构建 | `docker build` + `vite build` | ✅ | 3min |
| 9 | E2E 冒烟 | `playwright test`（Compose 起 app+db） | ✅ | 8min |
| 10 | Golden | `pytest tests/golden` | ✅ | 2min |

PR 总时长目标 **≤ 15 分钟**；1~7 可并行。

### 8.3 Jenkinsfile

```groovy
pipeline {
  agent { label 'asr-eval' }          // 需 Docker + Node 20 + uv
  options {
    timeout(time: 30, unit: 'MINUTES')
    buildDiscarder(logRotator(numToKeepStr: '30'))
    disableConcurrentBuilds()
  }
  environment {
    UV_CACHE_DIR   = "${WORKSPACE}/.cache/uv"
    PNPM_HOME      = "${WORKSPACE}/.cache/pnpm"
    APP_TAG        = "asr-eval/app:${env.GIT_COMMIT.take(8)}"
  }
  stages {
    stage('静态与类型') {
      parallel {
        stage('ruff')     { steps { sh 'uv run ruff check . && uv run ruff format --check .' } }
        stage('mypy')     { steps { sh 'uv run mypy app' } }
        stage('前端静态') { steps { sh 'pnpm -C web lint && pnpm -C web typecheck' } }
      }
    }
    stage('单元')   { steps { sh 'uv run pytest -m "not integration and not device" --junitxml=reports/unit.xml' } }
    stage('契约')   {
      steps {
        sh 'uv run python -m app.openapi export > reports/openapi.json'
        sh 'npx openapi-diff docs/openapi.baseline.json reports/openapi.json --fail-on-breaking'
        sh 'npx openapi-typescript reports/openapi.json -o web/src/api/schema.d.ts'
        sh 'git diff --exit-code web/src/api/schema.d.ts'
      }
    }
    stage('集成') {
      steps {
        sh 'docker compose -f deploy/compose.test.yml up -d db'
        sh 'uv run pytest -m integration --junitxml=reports/integration.xml'
      }
      post { always { sh 'docker compose -f deploy/compose.test.yml down -v' } }
    }
    stage('前端测试') { steps { sh 'pnpm -C web test -- --run' } }
    stage('构建') {
      steps {
        sh 'docker build -t "$APP_TAG" .'
        sh 'pnpm -C web build'
      }
    }
    stage('E2E') {
      steps {
        sh 'docker compose -f deploy/compose.yml up -d --build'
        sh 'pnpm -C web e2e'
      }
      post { always { sh 'docker compose -f deploy/compose.yml down -v' } }
    }
    stage('Golden') { steps { sh 'uv run pytest tests/golden' } }
    stage('归档镜像') {
      when { branch 'main' }
      steps {
        sh 'mkdir -p dist && docker save "$APP_TAG" | gzip > "dist/app-${GIT_COMMIT.take(8)}.tar.gz"'
        archiveArtifacts artifacts: 'dist/*.tar.gz', fingerprint: true
      }
    }
  }
  post {
    always {
      junit 'reports/*.xml'
      archiveArtifacts artifacts: 'reports/openapi.json', allowEmptyArchive: true
    }
  }
}
```

要点：

- `disableConcurrentBuilds()`：共享一台机器，避免集成测试互相踩库
- 集成测试用 **tmpfs 的 PG**，结束 `down -v`
- `APP_TAG` 用 commit 短 SHA，不用 `latest`
- 没有 registry：**镜像 tar 作为构建产物归档**，部署时 `docker load`

### 8.4 部署（无 registry）

```bash
# Jenkins 归档产物：dist/app-<sha>.tar.gz
scp dist/app-<sha>.tar.gz asr@server:/tmp/
ssh asr@server '
  gunzip -c /tmp/app-<sha>.tar.gz | docker load
  cd /opt/asr-eval && sed -i "s|APP_TAG=.*|APP_TAG=asr-eval/app:<sha>|" .env
  docker compose up -d
'
```

- 服务器不需要访问外网/registry
- 回滚 = `docker load` 上一个 tar + 改 `APP_TAG`

### 8.5 真机 Job（独立）

PR 不跑真机；单独建 `asr-eval-device` Job：

```groovy
// 触发：每天 02:00 或手动
// agent label: asr-eval-lan（能访问 192.168.65.x / 192.168.133.x）
sh 'uv run pytest -m device --device-env=lan --junitxml=reports/device.xml'
```

覆盖：

| 设备 | 场景 |
|---|---|
| 205（etsstt，非流式） | 100 条 CER + 标点 + 命名 |
| 502（网关，流式） | 100 条 + 3 路并发 5 分钟 |
| 600 | 同上，作为对照 |
| 长音频 | 30 分钟流式 / 非流式各 1 次 |
| DER | AliMeeting near/far 各 20 段，与 `md-eval.pl` 对比 |

若 Jenkins agent 不在设备网段：在开发机手动跑同一脚本，把 `device.xml` 与截图上传 Jenkins。

### 8.6 依赖获取

```bash
# Python（无镜像，直连 pypi）
uv sync --frozen

# 若 CI 机器不能出网：提前在可出网机器下载
uv export --format requirements-txt --no-hashes > requirements.txt
pip download -r requirements.txt -d vendor/wheels
# CI: pip install --no-index --find-links vendor/wheels -r requirements.txt
```

- `uv.lock` 必须提交
- pnpm 用 `--frozen-lockfile`，store 目录缓存
- Docker 基础镜像在 Jenkins 预拉，避免每次构建都出网

## 9. 覆盖率与质量阈值

| 模块 | 行覆盖 | 说明 |
|---|---|---|
| `scoring/` | ≥ 85% | 指标算错比 UI 崩了严重 |
| `dataset/importers/` | ≥ 80% | 六类数据集字段映射 |
| `workers/` | ≥ 70% | 状态机、租约、重试 |
| `api/` | ≥ 60% | 路由薄，主要靠集成测 |
| `web/` | 不设 | 行为测试为主 |

覆盖率**只降不升时失败**（与上次基线比），避免一次调低阈值永远达标。

---

## 10. 安全与合规门禁

| 检查 | 工具 | 失败条件 |
|---|---|---|
| 密钥扫描 | `gitleaks` | 任何命中 |
| 大文件 | `check-added-large-files` | > 5MB |
| 依赖漏洞 | `pip-audit` / `pnpm audit` | high/critical |
| 镜像漏洞 | `trivy`（nightly） | critical 且有修复 |
| 许可证 | `pip-licenses` | GPL/AGPL 进入分发 |
| 路径穿越 | 单元测试 | 已有用例 |

---

## 11. 分支与发布

### 11.1 分支与提交

- trunk-based：`main` + 短分支（≤ 2 天）
- PR 必须：CI 绿 + 1 人 review + 无未解决评论

**提交节奏**

- 每完成一个"可运行的小切片"就提交：能启动、能跑测试、能回退
- 重构前先提交当前可用状态
- 每天结束不留未提交改动（至少 WIP 分支）
- 禁止一次提交跨多个模块、超过 500 行（迁移/生成文件除外）

**Conventional Commits**

```
<type>(<scope>): <subject>

<body 可选，中文说明为什么>
<footer 可选，BREAKING CHANGE / Refs>
```

| type | 用途 |
|---|---|
| `feat` | 新功能 |
| `fix` | 修 bug |
| `refactor` | 不改行为的重构 |
| `perf` | 性能优化 |
| `test` | 测试 |
| `docs` | 文档 |
| `build` | 构建/依赖 |
| `ci` | Jenkins/脚本 |
| `chore` | 杂项（含 WIP） |
| `revert` | 回滚 |

scope 用模块名：`api`、`scoring`、`dataset`、`runner`、`web/runs`、`deploy`。

示例：

```
feat(runner): 支持设备条件变化时中断 Run

设备 sn/uid 在执行中变化时，当前 Run 置 aborted_condition_changed，
并保留已完成样本供评分。见 M4 §5.8。

Refs: #42
```

**校验**：

```bash
# commit-msg hook
npx commitlint --edit "$1"
```

```js
// commitlint.config.js
export default {
  extends: ["@commitlint/config-conventional"],
  rules: {
    "subject-max-length": [2, "always", 72],
    "scope-enum": [2, "always", [
      "api","auth","scoring","dataset","device","runner","report",
      "web","deploy","ci","docs"
    ]]
  }
};
```

- 分支名：`feat/runner-abort-on-change`、`fix/web-sse-reconnect`
- 合并用 squash，保留 PR 标题为最终提交信息

### 11.2 发布

```bash
make release VERSION=0.3.0
# 1. 全量测试
# 2. 更新 CHANGELOG
# 3. 构建镜像，tag 0.3.0 + git sha
# 4. 记录 digest 到 deploy/compose.yml
# 5. git tag v0.3.0
```

发布前检查：

- [ ] 迁移可从上一版本升级（在 CI 用旧 schema + 新代码跑一次）
- [x] 2026-11-07：运行 `make openapi` 并同步 `docs/openapi.baseline.json`；`docs/openapi.json` 与 baseline 字节一致。
- [x] 2026-11-10：新增 `tools/check_openapi.py` 与 `make openapi-check` / `make openapi-sync`；`test_openapi_contract_matches_committed_baseline` 在单测中强制基线与当前契约一致，避免契约漂移静默通过。
- [ ] `asrctl doctor` 在 staging 通过
- [ ] 回滚方案确认（上一个 digest 可用）

---

## 12. 阶段门禁（P0~P4）

**规则：上一阶段退出条件全绿，才进入下一阶段。** 不允许"先往下写，最后补测试"。

| 阶段 | 进入条件 | 退出条件（全部满足） |
|---|---|---|
| **P0 打通** | 设计冻结 | ① `make test` 全绿；② 登录 + 设备探测通过；③ AISHELL-1 导入 1,000 条；④ 205 真机跑通 CER；⑤ 手工抽查 20 条与人工一致 |
| **P1 可用** | P0 退出 | ① 3 设备 × 3 数据集横评出榜；② SSE 进度不丢；③ E2E 用户故事与页面回归全通过；④ 备份/恢复演练一次（2026-11-10 本机隔离库演练通过，见 `evidence/backup-restore-drill.md`） |
| **P2 完整** | P1 退出 | ① AliMeeting near/far 出 DER/SA-DER/命名；② 与 `md-eval.pl` 差值 ≤ 0.2pp；③ 声纹向量按 device 物化；④ 命名 EER 有报告 |
| **P3 决策** | P2 退出 | ① 性能双 RTF + 并发档位；② 回归显著性 p 值正确；③ Badcase 键盘流 10 条/分钟；④ LDAP 登录 + 四角色 |
| **P4 打磨** | P3 退出 | ① 全量 E2E + 可访问性检查；② 备份恢复演练；③ `asrctl doctor` 全绿；④ 文档与实现一致 |

每个阶段的检查命令：

```bash
make test          # 单元 + 集成
make e2e           # Playwright 用户故事主路径与页面回归
make gate          # 当前阶段专属检查（脚本在 tools/gates/）
make device-smoke  # 真机（P0 起每阶段至少一次）
```

**例外**：确需并行时，由项目负责人在 PR 里写明"跳过哪条、何时补"，并在下一阶段开始前补跑；不允许静默跳过。

> 2026-11-10 状态：备份/恢复已在隔离库+临时 data/ 上演练通过；矩阵脚本已支持 AISHELL-1/AISHELL-3/KeSpeech。205 直连 8701 不通，但可通过 SSH 隧道 `tools/device_tunnel.sh` 访问；205 + 502 已完成 2 设备 × 3 数据集 × 20 条联跑（全部 20/20）。完整 3 设备 × 3 数据集仍缺 600 的 SSH 目标/地址。记录见 `evidence/gate-2026-11-10.md`。

---

## 13. 完成定义（每个模块）

一个模块算"完成"，必须同时满足：

1. 单元 + 集成测试通过；口径模块有 golden 样例
2. OpenAPI 变更已生成类型并提交
3. 迁移可正向执行；无破坏性变更
4. 关键路径有结构化日志与错误码
5. 设计文档对应章节已更新
6. `asrctl doctor` 通过

---

## 14. 验收

- [ ] 新机器 `make setup && make test` 一次通过（Python 3.13）
- [ ] PR 从 push 到可合并 ≤ 15 分钟（Jenkins）
- [x] 2026-11-10：CER 黄金样例门禁 `make golden`（`backend/tests/golden/test_cer_golden.py`），固定 P0 20 条抽查，micro CER=0.008、macro=0.0076923；口径漂移会直接失败。
- [x] 2026-11-10：`make openapi-check` + `test_openapi_diff_detects_removed_response_field`；删除响应字段时本地差异检查会报告接口变更。
- [x] 2026-11-10：`make secrets-check` 使用 `.gitleaks.toml`；仓库扫描 0 leaks，临时假 `api_key` 文件可稳定触发 `leaks found: 1`。
- [x] 2026-11-10：`make commitlint` 使用 `.commitlintrc.json`；最新提交通过，非法提交信息被拒绝。
- [ ] 故意让 ttft 变慢 30% → nightly 性能检查失败
- [x] 2026-11-07：`backend/tests/test_fake_device_server.py` 验证假设备返回 `final.text="。"`、句子缺少 `spk`，且 `global_spk_list` 含末尾哨兵（长度为句数 + 1）。
- [x] 2026-11-08：205 / 502 / 600 真机 AISHELL-1 `test` 集单路 100 条 CER 基线均完成，成功率均为 100%；设备指标见 [三设备矩阵记录](asr-eval-implementation.md)。此记录不覆盖 205 标点/命名、502/600 三路持续 5 分钟或 30 分钟长音频。
- [x] 2026-11-08：`make e2e` Playwright 10 passed、1 条默认真机用例按预期跳过；Run 详情与设备台账 axe 均 0 violations，Badcase 标签页/设备 Run 链接键盘 Enter、弹窗 Escape 均通过。1440×1000 和 1024×900 截图在 `/tmp`，两页均无横向溢出；测试使用临时 PostgreSQL 与本地假设备。
- [x] 2026-11-10：新增 `tools/bench_matrix.py` 与 `tools/matrix_plan.example.json`，支持 AISHELL-1/AISHELL-3/KeSpeech × 多设备；`make matrix-check` 在真实本地目录校验 3 个数据集各 100 条样本通过。205 通过 SSH 隧道完成 3×100 条。
- [x] 2026-11-10：205 标点 F1（AliMeeting near 100 段）：99/100 成功，CER 5.74%，标点 F1 0.694，句末准确率 93.3%；命名因无声纹注册记 na。
- [x] 2026-11-10：502 三路 × 5 分钟（1×、300s）：3/3 成功，TTFT 1182/1221ms，RTF 0.176/0.178。三路 30 分钟因用户要求暂停使用 502 未执行。
- [ ] 真机 Job 在 205 / 502 / 600 上通过（含 30 分钟与 3 路并发）
- [ ] 每个阶段退出检查全绿后才进入下一阶段
- [x] 2026-11-07：抽查 `core/ldap.py`、`adapters/etsstt.py`、`scoring/cer.py`；AST 核对公开模块/类/函数及公开方法均有中文 docstring，设备协议坑和 CER 口径注释均引用 M2/M4/M5 章节。
- [ ] 前端 PR 附两张截图 + 通过 axe/键盘检查

---

## 15. 明确不做

- 100% 覆盖率、变异测试
- 每个 PR 跑真机
- 每个 PR 跑 30 分钟长音频
- UI 像素级快照（只对矩阵/Badcase 两页做）
- 自建 CI 集群

---

## 16. 风险与待确认

| # | 风险 | 缓解 |
|---|---|---|
| 1 | Jenkins 机器没有 Docker/Node/uv | 预装并固化到 agent 镜像；`Jenkinsfile` 里显式检查版本 |
| 2 | 无 registry，镜像靠 tar 传递 | 归档到 Jenkins；部署脚本校验 sha256 |
| 3 | 无 PyPI 镜像，依赖拉取慢/断 | `uv.lock` + 缓存；必要时 vendor wheels |
| 4 | 真机 Job 与 PR Job 抢机器 | 分开 label：`asr-eval` / `asr-eval-lan` |
| 5 | golden 样例版权 | 只用 5~10 秒片段 + 记录 license |
| 6 | 阶段门禁被绕过 | 门禁写进 Jenkins Job；例外必须书面记录 |

**已确认**

| 项 | 结论 |
|---|---|
| CI | 局域网 **Jenkins** |
| 镜像仓库 | 无；`docker save/load` + 归档 |
| PyPI / npm | 无镜像，内网可出网；锁文件 + 缓存 |
| Node / pnpm | 可安装，锁定 Node 20 / pnpm 9 |
| 真机网段 | 192.168.65.x / 192.168.133.x 均可通，独立 Job 跑 |
| Python | **3.13** |

**仍待确认**

1. Jenkins agent 是否已装 Docker、Node 20、uv？没有的话谁负责装？
2. 真机 Job 用哪台 agent（是否在设备网段）？
3. 构建产物保留多久、放哪台存储？
