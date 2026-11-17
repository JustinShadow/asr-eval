# M7 · API 与鉴权详细设计

> 范围：对外 REST 契约、SSE、异步任务、鉴权（LDAP + JWT）、授权（RBAC）、审计、OpenAPI 流水线。
> 上游：M1 数据集 / M2 设备 / M3 评测定义 / M4 执行 / M5 打分 / M6 报告。
> 约束：单机部署、局域网、3~5 人、前后端分离、CI 也要能触发评测。

---

## 0. 决策记录

| # | 决策 | 理由 |
|---|---|---|
| D-M7-1 | **LDAP 是默认登录方式**；登录请求可选择 LDAP 或本地账号；未指定时沿用 `AUTH_MODE` | 内部用户使用目录账号，本机管理员仍可主动选择本地账号 |
| D-M7-2 | access token **JWT 30min**（内存持有）+ refresh token **7d**（HttpOnly Cookie） | 短 token 降低泄露面，refresh 避免频繁输密码 |
| D-M7-3 | refresh **一次性轮换 + 复用检测**（token family） | 被窃取的 refresh 一旦被使用即整族失效 |
| D-M7-4 | 授权用**权限字符串**（`run:create`），角色只是权限包 | 端点只声明所需权限，改角色映射不动代码 |
| D-M7-5 | SSE 用**一次性 stream ticket**，不用 `?token=` | EventSource 不能设 header；把长期 token 放 URL 会进日志 |
| D-M7-6 | 所有异步操作统一为 **`job` 资源**，返回 `202 + Location` | 导入/探测/报告/声纹自检共用一套进度与取消语义 |
| D-M7-7 | 写操作支持 **`Idempotency-Key`**（创建类）；配置类用 **`If-Match` ETag** | 防重复点、防并发覆盖 |
| D-M7-8 | 错误体固定 `{code, message, detail, trace_id}`；`code` 为机读常量 | 前端可分支、日志可聚合 |
| D-M7-9 | 分页**只用游标**，不提供 offset | 万级 items 深翻页不退化 |
| D-M7-10 | OpenAPI 3.1 由 FastAPI 生成，CI 做**破坏性变更门禁** | 契约先行，前端不被后端阻塞 |
| D-M7-11 | 审计日志**与业务写同事务**（仅管理/运维动作） | 不能出现"操作成功但没记录" |
| D-M7-12 | 报告 HTML 以**下载 + sandbox** 方式提供，不在主域内联渲染 | 转写文本是用户数据，防存储型 XSS |
| D-M7-13 | 角色扩为 **viewer / tester / engineer / admin** 四档，另支持**按人授权（grant）** | 项目组内"只看结果"和"要跑评测"的人远多于"维护数据集"的人 |
| D-M7-14 | 对象级规则：`run:cancel` 限本人（engineer/admin 不限）、`report:delete` 限创建者或 admin | 防止互删/互停 |
| D-M7-15 | CI 门禁**预留契约**，阈值做成 `gate_policy` 配置，不写死在代码 | 阈值要边跑边调，不能每次改代码发版 |
| D-M7-16 | 新增**用户级事件流** `GET /events`，只发 `resource.changed` 变更信号 | 列表页/设备状态无法靠单对象流感知他人操作 |
| D-M7-17 | 配置类 `PATCH` **强制** `If-Match`，缺失返回 `428`；不匹配 `412` 并回传当前对象 | 不静默覆盖；前端能做差异确认 |
| D-M7-18 | refresh 轮换加 **30s 幂等宽限**：宽限期内旧 token 返回同一个新 token | 多标签页并发刷新不能误判"复用" |
| D-M7-19 | 设备列表/详情返回 **`lease.holder`**（谁、哪个 Run、何时释放） | 前端能直接说"被谁占用"，不用猜 |
| D-M7-20 | 编辑中提示用 **advisory presence**（PG 表 TTL，不锁） | 3~5 人团队，硬锁等待比冲突更烦 |
| D-M7-21 | `run:delete` 仅 admin 可用；只允许删除终态 Run，评分仍在排队/执行或报告仍引用评分时拒绝删除 | 防止运行中断、评分 worker 丢失目标与不可变报告失去来源 |

> 已确认：D-M7-2 / D-M7-3（refresh 走 HttpOnly Cookie）、D-M7-15（门禁预留 + 配置化）。

---

## 1. 职责与边界

### 1.1 负责

- HTTP 契约：URL、方法、状态码、请求/响应 schema、错误码
- 认证：LDAP bind、JWT 签发/校验/刷新/登出、service token
- 授权：权限模型、端点声明、对象级前置校验（设备空闲、Run 归属）
- 传输：SSE 协议、分页、幂等、ETag、限流、CORS/安全头
- 契约治理：OpenAPI 产物、前端类型生成、破坏性变更检测

### 1.2 不负责

- 业务状态机（在 M1~M6 的服务层）
- 调度与租约（M4）
- 指标计算（M5）
- 前端路由与状态（M8）

### 1.3 关键约束

- **API 层不写业务规则**：只做参数校验、权限判断、调用 service、序列化
- **所有时间用 RFC 3339 UTC**（`2026-11-03T10:22:31Z`），前端本地化
- **所有 ID 用 UUIDv4 字符串**，不暴露自增主键（`case_result.id` 等内部 ID 仅在明细里出现）
- **列表接口不返回大字段**（`metrics`、`detail`、`raw` 一律走详情）
- **删除是软删或不可变**：Dataset/DatasetVersion/Evaluation/Report 一旦被引用不可物理删除

---

## 2. 认证

### 2.1 登录流程

```
SPA                     API                        LDAP
 │ POST /auth/login       │                          │
 │ {username,password,    │                          │
 │  auth_mode?}           │                          │
 ├───────────────────────►│                          │
 │                        │ auth_mode=ldap: bind/search
 │                        ├─────────────────────────►│
 │                        │ auth_mode=local: verify DB hash
 │  ◄── 200               │                          │
 │ access_token (JSON)    │                          │
 │ Set-Cookie: refresh   │                          │
```

- 用户不存在与密码错误**返回同一错误** `E_AUTH_INVALID`（防枚举）
- 登录页默认选择 LDAP；用户可显式切换为本地账号，所选方式由 `auth_mode` 传入登录请求。未传 `auth_mode` 时沿用服务端 `AUTH_MODE`，兼容旧客户端。
- 选择 LDAP 且目录不可达 → `503 E_AUTH_BACKEND_DOWN`，**不**自动降级；用户可主动切换到本地账号。break-glass 仍是单独的 LDAP 故障应急路径。
- 选择本地账号时，仅校验 `app_user.password_hash` 与账户状态，不访问 LDAP；角色沿用本地账户配置。
- 登录成功写 `audit_log(action='auth.login')`，失败写 `auth.login_failed`（含来源 IP，不含密码）
- 默认采用服务账号搜索用户 DN 并读取组。目录仅提供固定 DN 规则时，可用 `ldap_user_dn_template` 直接 bind；该模式不读取组，未配置其他组信息时按默认 `viewer` 角色处理。两种正常模式都必须使用通过 CA 校验的 LDAPS 或 StartTLS。
- `ldap_tls_valid_names` 仅用于证书 SAN/CN 与连接地址不同但已明确核验的目录部署；它只调整证书名称校验，不能替代 CA 信任链校验。
- P0 本机临时兼容例外：仅 `APP_ENV=dev` 且显式 `LDAP_ALLOW_INSECURE=true` 时允许 `LDAP_START_TLS=false` 的明文 LDAP；生产与预发配置始终拒绝明文 bind。该例外存在密码被链路观察者读取的风险，修复目录证书后应关闭。

### 2.2 Token 模型

**Access token（JWT，HS256）**

```json
{
  "iss": "asr-eval",
  "aud": "asr-eval-web",
  "sub": "u_8f3c…",
  "name": "zhangsan",
  "role": "engineer",
  "auth_method": "ldap",
  "perms": ["run:create", "dataset:write", "..."],
  "sid": "s_2b91…",
  "jti": "t_5a0e…",
  "iat": 1793700000,
  "exp": 1793701800
}
```

| 项 | 取值 |
|---|---|
| 算法 | HS256，密钥 32B，来自 `secrets/jwt.key`（0600，不进镜像） |
| 有效期 | 1800s |
| 校验 | 签名 + `exp` + `iss` + `aud` + `sid` 未撤销 |
| 放在哪 | 仅内存（不写 localStorage），页面刷新先调 `/auth/refresh` |
| 撤销 | 登出/改权限时写 `user_session.revoked_at`，30min 内自然过期 + 立即失效 |

> `perms` 冗余进 token 是为了省一次查询；**权限变更后强制失效该用户所有 session**，避免旧 token 继续生效。

**Refresh token（不透明随机串）**

| 项 | 取值 |
|---|---|
| 形态 | 32B 随机，base64url，**只在 Cookie 里** |
| Cookie | HTTP：`asr_refresh`；HTTPS：`__Host-refresh`。均 `HttpOnly; SameSite=Lax; Path=/api/v1/auth`；`Secure` 仅在 HTTPS 下加 |
| 存储 | 只存 `sha256(token)`，不存明文 |
| 有效期 | 7d，滑动（每次刷新重置） |
| 轮换 | 每次 `/auth/refresh` 签发新 token，旧的立即作废 |
| 复用检测 | 旧 token 被再次使用 → 该 `family_id` 全部作废 + 审计 `auth.refresh_reuse` |
| 多标签宽限 | 轮换后 **30s 内**旧 token 再次出现 → 幂等返回同一新 token，**不判复用**（PG `refresh_grace`，新 token 加密存储 30s） |

> 多标签页会并发刷新。没有宽限期时，第二个标签会被判"复用"，整族失效、用户被踢下线。宽限期 + 前端 Web Locks 双保险。

**为什么 Cookie 是最佳实践（同源 SPA）**

| 方案 | XSS 可窃取 | CSRF 风险 | 说明 |
|---|---|---|---|
| refresh 存 localStorage | ✅ 可窃取，等于长期账号 | 无 | 最差 |
| refresh 存内存 | 刷新即丢失，体验差 | 无 | 不可行 |
| **HttpOnly Cookie + Path 限定** | ❌ 脚本读不到 | 需防 | 当前选择 |

三道防线：

1. `HttpOnly` + `Path=/api/v1/auth`：JS 读不到，且只在刷新/登出时随请求发送；
2. `SameSite=Lax`：跨站 POST 不带 Cookie，挡住绝大多数 CSRF；
3. `Origin` 校验：`/auth/refresh` 与 `/auth/logout` 校验 `Origin` 必须等于站点源，不匹配直接 403。

> 不用 `SameSite=None`：前后端同源反代，没有跨站需求；一旦设 None 就必须再上 CSRF token，复杂度翻倍。

```sql
create table user_session (
  id uuid primary key default gen_random_uuid(),   -- sid
  username text not null,
  role text not null,
  auth_method text not null default 'local', -- local / ldap / break_glass；随 refresh 保留本次登录来源
  refresh_hash char(64) not null unique,
  family_id uuid not null,
  user_agent text,
  ip inet,
  created_at timestamptz not null default now(),
  last_used_at timestamptz not null default now(),
  expires_at timestamptz not null,
  revoked_at timestamptz
);
create index session_user on user_session (username, created_at desc);
create index session_family on user_session (family_id);
```

### 2.3 端点

```
POST   /api/v1/auth/login            {username, password} → 200 {access_token, expires_in, user}
POST   /api/v1/auth/refresh          Cookie → 200 {access_token, expires_in, user}
POST   /api/v1/auth/logout           Cookie → 204（撤销当前 session）
GET    /api/v1/auth/me               200 {user, permissions, ldap_groups, auth_method}
GET    /api/v1/auth/sessions         200 当前用户的活动会话
DELETE /api/v1/auth/sessions/{sid}   204（改密码后"退出其他设备"）
POST   /api/v1/auth/stream-ticket    200 {ticket, expires_in}
```

`POST /auth/login` 响应：

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs…",
  "token_type": "Bearer",
  "expires_in": 1800,
  "user": {
    "id": "u_8f3c…",
    "username": "zhangsan",
    "display_name": "张三",
    "role": "engineer",
    "permissions": ["dataset:read", "dataset:write", "run:create", "…"]
  }
}
```

LDAP 后端不可用时，错误体 `detail.break_glass_available` 只返回是否同时启用了 break-glass、配置了本地账号与哈希；不返回账号名或凭据。登录时将真实认证方式写入会话与 access token，refresh 后保持不变；`GET /auth/me.auth_method` 为 `ldap`、`local`、`break_glass` 或 `service_token`，前端据此显示应急会话告警。

`Set-Cookie: asr_refresh=…; HttpOnly; SameSite=Lax; Path=/api/v1/auth; Max-Age=604800`（HTTPS 时用 `__Host-` 前缀并加 `Secure`）

### 2.4 Stream ticket（SSE 专用）

EventSource 无法设置 `Authorization`，因此：

```
POST /api/v1/auth/stream-ticket
→ {"ticket": "st_9f2c…", "expires_in": 60}

GET /api/v1/runs/{id}/events?ticket=st_9f2c…
```

- PG `sse_ticket(hash, user, expires_at, used_at)`，**60s，用一次即删**
- 只授予读权限；ticket 泄露最多看 60s 的只读流
- 连接建立后，流的最长生命周期 30min，过期由前端重新取 ticket 重连

### 2.5 Service token（CI）

```
POST   /api/v1/admin/service-tokens   {name, scopes, expires_at} → 201 {token}  # 仅此一次返回明文
GET    /api/v1/admin/service-tokens
DELETE /api/v1/admin/service-tokens/{id}
```

- 形态 `ase_<id>_<secret>`，存 `sha256(secret)`
- scopes 只能是 `run:create, run:read, evaluation:read, report:create, report:read`
- **不能**有 `device:ops`、`admin:*`
- 用于 CI：建 Run → 轮询 `GET /runs/{id}` → 取 `GET /evaluations/{id}/summary` → 失败退出码

### 2.6 Break-glass 管理员

| 项 | 取值 |
|---|---|
| 开关 | `AUTH_BREAK_GLASS=1`（默认 0） |
| 账号 | `LOCAL_ADMIN_USER` + `LOCAL_ADMIN_HASH`（argon2id） |
| 限制 | 仅当 LDAP 返回 503 时可登录；每次登录审计 `auth.break_glass`；错误 detail 仅暴露启用布尔值，登录后 `/auth/me.auth_method=break_glass`，UI 顶部常驻告警 |
| 关闭 | 立即删除所有本地 session |

本地用户登录由登录页的 `auth_mode=local` 主动选择，不依赖 `AUTH_BREAK_GLASS`；其会话记录为 `auth_method=local`。`AUTH_BREAK_GLASS` 仅控制 LDAP 不可用时的应急登录。

---

## 3. 授权（RBAC + 按人授权）

### 3.1 角色重新定义（按真实使用人群）

项目组的实际分工不是"工程师 / 只读"两档，而是四类人：

| 角色 | 是谁 | 典型动作 | 关键约束 |
|---|---|---|---|
| `viewer` 观察者 | 项目组关心质量的成员、主管、外组旁听 | 看报告、看对比、看 Badcase、导出 | **默认角色**，LDAP 未命中任何组即此档 |
| `tester` 测试执行 | 测试工程师、日常跑回归的人 | 选基准 → 建 Run → 等结果 → 看结论 → 生成报告 | **不能**改数据集/设备/口径；只能取消自己发起的 Run |
| `engineer` 评测工程师 | 模型工程师、数据工程师 | + 建数据集版本、建设备、写口径与基准、维护说话人库 | 不能删数据集、不能动设备运维 |
| `admin` 管理员 | 平台负责人（1~2 人） | + 删除、设备凭据与重启、审计、service token、用户授权 | 高危动作需二次确认 |

设计原则：**能跑评测的人远多于能改数据的人**。把 `run:create` 从 engineer 里拆出来，避免为了跑一次回归就给人开数据集写权限。

### 3.2 权限矩阵

| 权限 | viewer | tester | engineer | admin |
|---|:--:|:--:|:--:|:--:|
| `meta:read` | ✅ | ✅ | ✅ | ✅ |
| `dataset:read` `device:read` `benchmark:read` `run:read` `evaluation:read` `report:read` `speaker:read` | ✅ | ✅ | ✅ | ✅ |
| `run:create` | — | ✅ | ✅ | ✅ |
| `run:cancel`（本人） | — | ✅ | ✅ | ✅ |
| `run:cancel`（他人） | — | — | ✅ | ✅ |
| `run:delete` | — | — | — | ✅ |
| `evaluation:create`（手动评分/换口径） | — | ✅ | ✅ | ✅ |
| `evaluation:cancel` | — | ✅ | ✅ | ✅ |
| `report:create` | — | ✅ | ✅ | ✅ |
| `report:delete`（本人） | — | ✅ | ✅ | ✅ |
| `report:delete`（他人） | — | — | — | ✅ |
| `dataset:write`（建/导入/重分析） | — | — | ✅ | ✅ |
| `device:write`（建设备/改地址） | — | — | ✅ | ✅ |
| `benchmark:write` / `metric_profile:write` | — | — | ✅ | ✅ |
| `benchmark:delete` / `evaluation:delete` / `metric_profile:delete` | — | — | — | ✅ |
| `speaker:write`（说话人/注册音频/提向量） | — | — | ✅ | ✅ |
| `dataset:delete` / `device:delete` | — | — | — | ✅ |
| `device:ops`（restart） | — | — | — | ✅ |
| `audit:read` | — | — | — | ✅ |
| `admin:users` / `admin:tokens` / `admin:system` | — | — | — | ✅ |

### 3.3 按人授权（grant）

组映射解决 90%，剩下 10% 不该逼人升角色：

> "小李是测试，但他也维护粤语测试集" —— 不该给他 engineer（会连带拿到设备写权限）。

```sql
create table user_grant (
  id uuid primary key default gen_random_uuid(),
  username text not null,
  permission text not null,           -- dataset:write
  scope jsonb not null default '{}',  -- 可选：{"dataset_id":"…"} 限定对象
  reason text,
  granted_by text not null,
  granted_at timestamptz not null default now(),
  expires_at timestamptz,             -- 支持临时授权
  revoked_at timestamptz,
  unique (username, permission, (scope->>'dataset_id'))
);
```

规则：

- **生效权限 = 角色权限 ∪ 未过期 grant**（只加不减）
- grant **不能**授予 `admin:*`、`device:ops`——这两类只能靠角色变更
- 带 `expires_at` 的授权到期自动失效，无需人工回收
- 前端 `/auth/me` 返回**合并后的权限列表**，不返回"角色 + grant"让前端自己算
- 任一变更 → 撤销该用户全部 session，强制重新登录（最长 30min 内自然生效）

### 3.4 用户与角色来源

```sql
create table app_user (
  username text primary key,
  display_name text,
  email text,
  ldap_dn text,
  role text not null default 'viewer',
  role_source text not null default 'ldap',   -- ldap|manual
  active boolean not null default true,
  last_login_at timestamptz,
  created_at timestamptz not null default now()
);
```

- 首次登录自动建行（`role` 取 LDAP 映射结果）
- admin 可手工把某人 `role_source` 改为 `manual` 并指定角色（覆盖 LDAP，记审计）
- 离职处理：`active=false` + 撤销全部 session；其历史 Run/报告保留（`created_by` 是文本，不因用户删除而断链）

### 3.5 对象级规则（service 层，不在装饰器）

| 动作 | 规则 | 错误 |
|---|---|---|
| `run:cancel` | 本人创建，或 engineer/admin | `403 E_NOT_OWNER` |
| `run:resume` | 同上 | `403 E_NOT_OWNER` |
| `run:delete` | 仅 admin，且 Run 已进入终态；评分仍在排队/执行或报告引用评分时拒绝 | `409 E_RUN_ACTIVE` / `409 E_EVALUATION_ACTIVE` / `409 E_RUN_REFERENCED` |
| `report:delete` | 创建者本人，或 admin | `403 E_NOT_OWNER` |
| `dataset:delete` | 无 DatasetVersion 被 Run 引用 | `409 E_VERSION_IN_USE` |
| `benchmark:delete` | 无 Run 引用；有引用时只能归档 | `409 E_BENCHMARK_IN_USE` |
| `evaluation:delete` | 终态评分；报告引用或活动报告任务时拒绝 | `409 E_EVALUATION_ACTIVE` / `409 E_EVALUATION_REFERENCED` |
| `metric_profile:delete` | 无 Benchmark 引用 | `409 E_METRIC_PROFILE_IN_USE` |
| `device:ops` | 设备无 running target 且无有效租约 | `409 E_DEVICE_BUSY` |
| `device:ops` | `expected_fingerprint_uid` 与当前一致 | `409 E_FINGERPRINT_CHANGED` |
| 创建 Run | 目标设备可达（或显式允许 offline） | `409 E_TARGET_OFFLINE` |
| 创建 Run | 引用的 benchmark 未归档 | `409 E_BENCHMARK_ARCHIVED` |

> 权限判断（能不能做）与状态判断（现在能不能做）分开报错：`403` 是权限，`409` 是状态。前端据此给不同文案。

**设备占用摘要**（`GET /devices`、`GET /devices/{id}` 返回）：

```json
"lease": {
  "holder_run_id": "r_91e…",
  "holder_run_name": "回归 11-03 · near",
  "holder_user": "zhangsan",
  "acquired_at": "2026-11-04T09:00:00Z",
  "expires_at": "2026-11-04T10:31:00Z"
}
```

无占用时 `lease: null`。前端据此显示"被谁占用到几点"，而不是只给一个红色圆点。

### 3.6 权限如何影响交互（前端契约）

`GET /auth/me` 返回扁平权限数组，前端**只判断权限，不判断角色**：

```json
{
  "user": {"username": "lisi", "display_name": "李四", "role": "tester"},
  "permissions": ["dataset:read", "run:create", "run:cancel:own", "evaluation:create",
                  "report:create", "report:delete:own", "speaker:read", "…"]
}
```

| 场景 | 做法 | 原因 |
|---|---|---|
| 关键动作不可用（新建 Run、手动评分、生成报告） | **显示但禁用**，hover 说明"需要 tester 权限，请联系管理员" | 隐藏会让人以为功能不存在 |
| 破坏性/高危动作（删除、设备重启） | **直接隐藏** | 减少误触，不需要"看见了但不能点"的挫败 |
| 只能操作自己的对象 | 他人 Run 的"取消"禁用并注明"仅创建者可取消" | 避免点了才报错 |
| 数据集/基准为空且无写权限 | 空状态写"还没有可用基准，请联系评测工程师创建" | 给出下一步，不是死路 |
| 权限变更 | 后端撤销 session；前端收到 401 后静默刷新一次，仍失败则提示"权限已变更，请重新登录" | 避免旧权限继续操作 |

**管理员页面**（`admin:users`）：

```
用户            角色       来源     额外授权              最近登录
张三 zhangsan   engineer   LDAP     —                     11-03 09:12
李四 lisi       tester     LDAP     dataset:write(粤语集)  11-02 18:40
王五 wangwu     viewer     manual   run:create(至 12-31)   10-28 14:02
```

- 角色下拉：viewer / tester / engineer / admin
- "额外授权"用 Command 多选，带到期时间
- 所有变更写审计，并在页面底部给出"该用户当前有效权限"预览

---

## 4. 通用约定

### 4.1 URL 与方法

- 前缀 `/api/v1`，资源复数、kebab-case
- 子资源最多两层：`/runs/{id}/evaluations`，再深用顶层 ID
- 动作型端点用子路径：`/runs/{id}/cancel`、`/devices/{id}/probe`
- 幂等动作可重复调用（`cancel` 已取消 → `204`，不报错）

### 4.2 状态码

| 码 | 用途 |
|---|---|
| 200 | 查询/更新成功 |
| 201 | 创建成功（带 `Location`） |
| 202 | 已接受异步任务（带 `Location: /jobs/{id}`） |
| 204 | 删除/动作成功，无体 |
| 304 | ETag 未变 |
| 400 | 参数格式错误 |
| 401 | 未认证 / token 过期（`WWW-Authenticate: Bearer`） |
| 403 | 已认证但无权限 |
| 404 | 资源不存在 |
| 409 | 状态冲突（设备忙、Run 已终态、幂等键冲突） |
| 410 | 快照已失效 |
| 422 | 语义校验失败（字段合法但组合不合法，如 `strip` + 无参考标点） |
| 429 | 限流（带 `Retry-After`） |
| 500 | 未预期错误 |
| 503 | 依赖不可用（LDAP/DB/本地存储/设备网关） |

### 4.3 错误体

```json
{
  "code": "E_DEVICE_BUSY",
  "message": "设备 205-B 正在执行 Run「回归 11-03」，无法启动新的探测。",
  "detail": {
    "device_id": "d_2f…",
    "holder_run_id": "r_91…",
    "lease_expires_at": "2026-11-03T10:31:00Z"
  },
  "trace_id": "01J9Z8Q7X3"
}
```

- `code` 全局唯一、稳定、可枚举；新增只追加
- `message` 直接可展示（中文），**不拼接技术栈信息**
- `detail` 结构化，前端可用来做"跳到占用者"这类操作
- `trace_id` 与日志、审计对齐

### 4.4 分页

```
GET /api/v1/dataset-versions/{id}/items?limit=50&cursor=eyJjIjoiMjAyNi0xMS0wMyIsImkiOjEyMzQ1fQ
```

```json
{
  "items": [ … ],
  "next_cursor": "eyJjIjoiMjAyNi0xMS0wMyIsImkiOjEyMzk1fQ",
  "has_more": true
}
```

- 游标 = base64(`{sort_value, id}`)，服务端不透明
- `limit` 默认 50，上限 200
- **不返回 total**（万级 count 太贵）；需要总数时用 `/stats` 或 `X-Total-Estimate`
- 排序字段白名单，禁止任意 `order_by`

### 4.5 幂等

创建类端点支持：

```
POST /api/v1/runs
Idempotency-Key: 7d2f9c1e-…
```

- PG `idempotency_key(user, key, request_hash, response, status, created_at)`，保留 24h
- 同 key 同 body → 返回原响应（200/201，带 `Idempotent-Replay: true`）
- 同 key 不同 body → `409 E_IDEMPOTENCY_CONFLICT`
- 必用场景：`POST /runs`、`POST /reports`、`POST /datasets/{id}/versions`

### 4.6 并发控制（ETag）

可变配置（Device、MetricProfile、Benchmark 草稿）：

```
GET /api/v1/devices/{id}  →  ETag: "d-7f3c2a"
PATCH /api/v1/devices/{id}
If-Match: "d-7f3c2a"
→ 200 新 ETag  /  412 E_PRECONDITION_FAILED
```

- ETag = 行 `version`（自增整数）+ 关键字段哈希
- 内容未变 → `304`
- **缺失 `If-Match` → `428 E_PRECONDITION_REQUIRED`**（服务端强制，防止"读旧写新"）
- **不匹配 → `412 E_PRECONDITION_FAILED`**，`detail` 必带：

```json
{
  "current": { "...": "服务端当前完整对象" },
  "current_etag": "b-222",
  "changed_fields": ["sampling", "buckets"],
  "updated_by": "lisi",
  "updated_at": "2026-11-04T09:12:00Z"
}
```

- `force=true&reason=...` 覆盖：仍须提供 `If-Match`；仅配置类（Benchmark / MetricProfile / Device 元信息 / Dataset 元信息）且**调用者具备该资源的写权限**时可绕过旧 ETag。原因必填并写入同事务审计。不为"覆盖"单设角色规则
- Run/Evaluation/Report **永不接受 force**（状态与快照不可被覆盖）

### 4.7 其它头

| 头 | 方向 | 说明 |
|---|---|---|
| `X-Request-Id` | 双向 | 客户端可传，服务端必回；无则生成 |
| `trace_id` | 响应体 | 与 `X-Request-Id` 同值 |
| `Retry-After` | 429/503 | 秒 |
| `Deprecation` / `Sunset` | 响应 | 弃用端点 |
| `Content-Encoding: gzip` | 响应 | JSON > 1KB 自动压缩 |

### 4.8 限流

| 范围 | 限额 | 存储 |
|---|---|---|
| `POST /auth/login` | 5 次/分钟/IP + 10 次/分钟/用户名 | 进程内滑动窗口 |
| 全局（按用户） | 600 次/分钟 | 进程内令牌桶 |
| SSE 并发流 | 5 条/用户 | 进程内计数 |
| 报告生成 | 10 次/小时/用户 | 进程内计数 |

超限返回 `429` + `Retry-After`。

### 4.9 空值与"不可评"

- 指标不可评：`{"value": null, "status": "na", "reason": "no_sentence_punctuation"}`
- **禁止**用 `0`、`-1`、`""` 表示缺失
- 可选字段缺省用 `null`，不省字段（前端类型稳定）

---

## 5. 资源与端点

### 5.1 总表

| 资源 | 端点 | 权限 |
|---|---|---|
| 认证 | `POST /auth/login` | 公开 |
| | `POST /auth/refresh` | Cookie |
| | `POST /auth/logout` | 登录 |
| | `GET /auth/me` | 登录 |
| | `POST /auth/stream-ticket` | 登录 |
| 数据集 | `GET /datasets` | `dataset:read` |
| | `POST /datasets` | `dataset:write` |
| | `GET /datasets/{id}` | `dataset:read` |
| | `POST /datasets/{id}/preview` | `dataset:write` |
| | `POST /datasets/{id}/versions` | `dataset:write` → 202 |
| | `GET /datasets/{id}/versions` | `dataset:read` |
| | `GET /dataset-versions/{id}` | `dataset:read` |
| | `GET /dataset-versions/{id}/subsets` | `dataset:read` |
| | `DELETE /dataset-versions/{id}` | `dataset:delete` |
| | `GET /dataset-versions/{id}/diff?against=` | `dataset:read` |
| | `GET /dataset-versions/{id}/items` | `dataset:read` |
| | `GET /dataset-versions/{id}/capabilities` | `dataset:read` |
| | `GET /items/{id}` / `/items/{id}/audio` | `dataset:read` |
| | `POST /dataset-versions/{id}/reanalyze` | `dataset:write` → 202 |
| 设备 | `GET /devices` | `device:read` |
| | `POST /devices` | `device:write` |
| | `PATCH/DELETE /devices/{id}` | `device:write` / `device:delete` |
| | `GET /devices/{id}/credential` | `device:read`，只返回凭据类型与配置布尔值 |
| | `PUT/DELETE /devices/{id}/credential` | `device:write`，强制 `If-Match` |
| | `POST /devices/{id}/probe?mode=` | `device:write` → 202 |
| | `POST /devices/{id}/voiceprint-check` | `device:write` → 202 |
| | `GET /devices/{id}/deployments` | `device:read` |
| | `GET /deployments/{id}` | `device:read` |
| | `GET /devices/{id}/health` | `device:read` |
| | `POST /devices/{id}/ops/{action}` | `device:ops` |
| | `GET /devices/{id}/ops/history` | `device:read` |
| 评测定义 | `GET /metric-profiles` | `metric_profile:read` |
| | `POST /metric-profiles` | `metric_profile:write` |
| | `POST /metric-profiles/validate` | `metric_profile:read` |
| | `GET /metric-profiles/templates` | `metric_profile:read` |
| | `GET /benchmarks` | `benchmark:read` |
| | `POST /benchmarks` | `benchmark:write` |
| | `GET /benchmarks/{id}` | `benchmark:read` |
| | `POST /benchmarks/preview` | `benchmark:read` |
| | `POST /benchmarks/{id}/archive` | `benchmark:write` |
| | `DELETE /benchmarks/{id}` | `benchmark:delete`（无 Run 引用） |
| | `DELETE /metric-profiles/{id}` | `metric_profile:delete`（无 Benchmark 引用） |
| | `GET /benchmarks/{id}/availability?deployment_id=` | `benchmark:read` |
| 执行 | `POST /runs/preview` | `run:create` |
| | `POST /runs` | `run:create` |
| | `GET /runs` | `run:read` |
| | `GET /runs/{id}` | `run:read` |
| | `POST /runs/{id}/cancel` | `run:cancel`（本人；engineer/admin 不限） |
| | `POST /runs/{id}/resume` | `run:cancel`（同上） |
| | `DELETE /runs/{id}` | `run:delete`（终态 Run、无活动评分；admin） |
| | `GET /runs/{id}/events` | `run:read`（ticket） |
| | `GET /runs/{id}/targets` | `run:read` |
| | `GET /runs/{id}/cases` | `run:read` |
| 评分 | `GET/POST /runs/{id}/evaluations` | `evaluation:read/create` |
| | `GET /evaluations/{id}` | `evaluation:read` |
| | `POST /evaluations/{id}/cancel` | `evaluation:cancel` |
| | `DELETE /evaluations/{id}` | `evaluation:delete`（终态，无报告引用） |
| | `GET /evaluations/{id}/events` | `evaluation:read`（ticket） |
| | `GET /evaluations/{id}/summary` | `evaluation:read` |
| | `GET /evaluations/{id}/matrix` | `evaluation:read` |
| | `GET /evaluations/{id}/buckets` | `evaluation:read` |
| | `GET /evaluations/{id}/cases` | `evaluation:read` |
| | `GET /evaluations/{id}/cases/{case_id}` | `evaluation:read` |
| 对比/报告 | `POST /compare` | `evaluation:read` |
| | `GET /compare/available` | `evaluation:read` |
| | `GET /reports` | `report:read` |
| | `POST /reports` | `report:create` |
| | `GET /reports/{id}` | `report:read` |
| | `GET /reports/{id}/html` / `/xlsx` | `report:read` |
| | `DELETE /reports/{id}` | `report:delete`（本人；admin 不限） |
| 声纹 | `GET /speakers` | `speaker:read` |
| | `GET /speakers/{id}/enrollments` | `speaker:read` |
| | `GET /speakers/{id}/enrollments/{enrollment_id}/audio` | `speaker:read`，私有缓存禁用的音频流 |
| | `POST /speakers` | `speaker:write` |
| | `POST /speakers/{id}/enrollments` | `speaker:write` |
| | `POST /speakers/{id}/extract?deployment_id=` | `speaker:write` → 202 |
| | `GET /speakers/{id}/vectors` | `speaker:read` |
| 异步任务 | `GET /jobs/{id}` / `/jobs/{id}/events` | 创建者或 admin |
| 实时 | `GET /events`（用户级流） | 登录（ticket） |
| | `POST /presence` · `DELETE /presence` | 登录 |
| 元数据 | `GET /meta/enums` | `meta:read` |
| 审计 | `GET /audit-logs` | `audit:read` |
| 系统 | `GET /healthz` / `/readyz` / `/version` | 公开（不含敏感信息） |
| | `GET /system/status` | `admin:system` |
| 门禁 | `GET /evaluations/{id}/gate?policy_id=` | `evaluation:read` |
| | `GET/POST /gate-policies`、`PATCH /gate-policies/{id}` | `admin:system` |
| 管理 | `GET/PATCH /admin/users` | `admin:users` |
| | `POST /admin/users/{username}/grants`、`DELETE …/grants/{id}` | `admin:users` |
| | `/admin/service-tokens` | `admin:tokens` |

### 5.2 关键请求/响应

**创建 Run**

```http
POST /api/v1/runs
Idempotency-Key: 7d2f9c1e-2a44-4b0e-9c31-6f0a1d2e3b4c
```

```json
{
  "benchmark_id": "b_4a1c…",
  "name": "回归 11-03 · near",
  "kind": "accuracy",
  "targets": [
    {"deployment_id": "dp_71c…", "subset": "near"},
    {"deployment_id": "dp_92f…", "subset": "near"}
  ],
  "auto_score": true,
  "lanes": 1
}
```

```json
201 Created
Location: /api/v1/runs/r_91e…
{
  "id": "r_91e…",
  "status": "created",
  "snapshot": {"benchmark_hash": "…", "item_count": 9982, "profile_hash": "…"},
  "targets": [
    {"id": "rt_1…", "deployment_id": "dp_71c…", "subset": "near", "status": "pending"}
  ],
  "created_at": "2026-11-03T10:20:00Z"
}
```

校验失败（同集合内设备不可用）返回 `409`，`detail` 列出每一个目标的原因，前端一次展示全部冲突，不让用户逐个试。

**Run 详情（列表轻量 / 详情完整）**

```json
{
  "id": "r_91e…",
  "name": "回归 11-03 · near",
  "status": "running",
  "progress": {"done": 3120, "total": 9982, "pct": 31.3},
  "eta_ms": 1840000,
  "eta_source": "rolling",
  "targets": [
    {
      "id": "rt_1…",
      "device": {"id": "d_2f…", "name": "205-B"},
      "deployment": {"id": "dp_71c…", "quant": "fp16", "soc": "…"},
      "status": "running",
      "stats": {"ok": 3100, "failed": 4, "cached": 16, "timeout": 0},
      "rtf_dev": 0.243,
      "abort_reason": null
    }
  ],
  "evaluations": [
    {"id": "ev_3c…", "status": "queued", "trigger": "auto"}
  ]
}
```

**SSE**

```
GET /api/v1/runs/r_91e…/events?ticket=st_9f2c…
```

```
id: 1042
event: run.progress
data: {"run_id":"r_91e…","done":3120,"total":9982,"pct":31.3,"eta_ms":1840000}

id: 1043
event: target.aborted_condition_changed
data: {"run_id":"r_91e…","target_id":"rt_1…","reason":"fingerprint_changed","field":"sn"}

: heartbeat
```

**矩阵**

```json
{
  "evaluation_id": "ev_3c…",
  "rows": [
    {
      "deployment_id": "dp_71c…",
      "label": "网关A · w8a8",
      "cells": [
        {"metric": "cer", "subset": "near", "value": 0.0682, "n": 9982,
         "ci_low": 0.0660, "ci_high": 0.0700, "status": "ok", "reason": null},
        {"metric": "punct_f1", "subset": "near", "value": 0.79, "n": 9982, "status": "ok"},
        {"metric": "der", "subset": "near", "value": null, "n": 0,
         "status": "na", "reason": "no_speaker_labels"},
        {"metric": "name_acc", "subset": "near", "value": 0.881, "n": 72, "status": "ok"}
      ]
    }
  ],
  "comparability": {"comparable": true, "blockers": []}
}
```

不可比时：

```json
"comparability": {
  "comparable": false,
  "blockers": [
    {"code": "E_PROFILE_MISMATCH", "message": "口径不一致：default_zh_1.0 vs default_zh_1.1",
     "evaluation_ids": ["ev_3c…", "ev_44…"]}
  ]
}
```

---

## 6. 异步任务（job）

### 6.1 模型

```sql
create table job (
  id uuid primary key default gen_random_uuid(),
  kind text not null,              -- import|probe|voiceprint_check|report|export|reanalyze
  ref_type text not null,          -- dataset_version|device|report|evaluation
  ref_id text not null,
  status text not null default 'queued',  -- queued|running|succeeded|failed|cancelled
  progress jsonb not null default '{}',
  result jsonb,
  error_code text, error_detail jsonb,
  created_by text,
  created_at timestamptz not null default now(),
  started_at timestamptz, finished_at timestamptz
);
create index job_ref on job (ref_type, ref_id, created_at desc);
create index job_status on job (status, created_at);
```

### 6.2 契约

```
POST /datasets/{id}/versions → 202
Location: /api/v1/jobs/j_77a…

GET /api/v1/jobs/j_77a…
{
  "id": "j_77a…",
  "kind": "import",
  "ref": {"type": "dataset_version", "id": "dv_12…"},
  "status": "running",
  "progress": {"phase": "parse", "scanned": 12000, "parsed": 9982, "errors": 3,
               "total": 12000, "eta_ms": 42000},
  "created_at": "2026-11-03T10:00:00Z"
}
```

- 成功：`result = {resource_id, url}`
- 失败：`error_code` + `error_detail`，并保留 `progress` 现场
- 取消：`POST /jobs/{id}/cancel`（仅 `queued/running` 且 kind 支持）
- 清理：`succeeded` 保留 7d，`failed` 保留 30d

---

## 7. SSE 协议

### 7.1 连接

```
GET /api/v1/runs/{id}/events?ticket=…
Accept: text/event-stream
```

响应头：

```
Content-Type: text/event-stream
Cache-Control: no-cache, no-transform
X-Accel-Buffering: no          # 仅在加可选反代时生效；默认直连无此问题
Connection: keep-alive
```

### 7.2 帧格式

```
id: <递增整数>
event: <类型>
data: <单行 JSON>

```

- `id` 每条流内单调递增，用于 `Last-Event-ID` 续传
- `event` 用点号命名：`run.created`、`target.progress`
- `data` **必须单行**（JSON 不换行），否则要 `data:` 多行
- 心跳：每 15s 发 `: heartbeat` 注释帧（防代理超时）

### 7.3 续传

浏览器重连带 `Last-Event-ID`，服务端从进程内环形缓冲补发（重启后 `resync`）：

```
# 进程内环形缓冲：每流保留最近 500 条 / 10 分钟
# 超过缓冲或进程重启 → 发 resync，前端重拉
```

超过缓冲 → 发 `event: resync` + 前端重新拉一次 `GET /runs/{id}`（不假装连续）。

### 7.4 事件全集

| 流 | 事件 |
|---|---|
| Run | `run.created` `run.running` `run.completed` `run.partial` `run.failed` `run.aborted` `run.cancelled` |
| Run | `target.started` `target.progress` `target.finished` `target.aborted_condition_changed` |
| Run | `eta.updated` `case.failed` |
| Evaluation | `evaluation.queued` `evaluation.scoring` `evaluation.progress` `evaluation.completed` `evaluation.failed` |
| Job | `job.progress` `job.succeeded` `job.failed` |
| Device | `device.health.changed` `device.overloaded` |

### 7.5 结束与错误

- 终态事件后服务端主动 `event: end` 并关闭
- 认证失效 → 关闭流，前端重取 ticket；连续 3 次失败降级为轮询（5s）
- 单用户 >5 条流 → `429`，不静默丢弃

### 7.6 用户级事件流 `/events`

资源流只覆盖"当前打开的对象"；跨页面的变更需要一条用户级流。

```
GET /api/v1/events?ticket=…
```

```json
{"type":"run","id":"r_91e…","version":12,"actor":"lisi","label":"回归 11-03 · near","at":"2026-11-04T09:12:00Z"}
```

事件类型：`run.changed` `evaluation.changed` `device.changed` `job.changed` `report.changed` `config.changed` `permissions.changed`。

规则：

- **只发信号，不发全量**（客户端按 type 失效对应 query key）
- 同一对象 **500ms 内合并**（服务端节流），避免进度事件风暴
- `actor` 用于前端决定是否静默（自己触发的不弹提示）
- 实现用 **PG `LISTEN/NOTIFY`**（channel `asr_events`）；API 进程 `LISTEN` 后分发给本地 SSE 订阅者
- 断线重连 → `resync`，客户端失效全部活跃查询
- 与资源流去重：同一对象在资源流已有细节时，用户流只做失效，不重复 toast

### 7.7 编辑中提示（presence）

```
POST   /api/v1/presence   {"type":"benchmark","id":"b_1"}
DELETE /api/v1/presence   {"type":"benchmark","id":"b_1"}
```

- 心跳 30s，PG `presence` 表 TTL 90s，按 `(type,id,user)` 记录
- 查询：`GET /benchmarks/{id}` 响应附 `editors: [{username, display_name, last_seen_at}]`
- **advisory only**：不阻断保存，不做锁
- 页面隐藏/离开时删除；异常退出靠 TTL 自愈

---

## 8. OpenAPI 与前端契约

### 8.1 产物

| 产物 | 位置 | 用途 |
|---|---|---|
| `openapi.json` | `/api/openapi.json` | 运行时；生产环境需登录 |
| `docs` | `/api/docs`（Swagger） | 仅 `admin:system` 或非生产 |
| TS 类型 | `web/src/api/schema.d.ts` | `openapi-typescript` 生成，提交入库 |
| 客户端 | `web/src/api/client.ts` | `openapi-fetch`，统一注入 token/trace |
| 基线 | `docs/openapi.baseline.json` | CI diff 用 |

### 8.2 生成与校验

```bash
# 后端
python -m app.openapi export > docs/openapi.json
npx openapi-typescript docs/openapi.json -o web/src/api/schema.d.ts

# CI 门禁
npx openapi-diff docs/openapi.baseline.json docs/openapi.json --fail-on-breaking
```

**破坏性变更**（CI 失败）：删端点、删字段、改类型、加必填字段、收紧枚举、改状态码语义。
**允许**：加端点、加可选字段、加枚举值（前端需容错）、加响应头。

### 8.3 约定

- 所有端点声明 `operationId`（前端函数名）
- 所有枚举用字符串常量 + `x-enum-display-name`（中文名），前端直接取
- 所有时间 `format: date-time`
- 所有金额/比率用 `number`，比率范围 `[0,1]`；展示层再乘 100
- 错误响应统一引用 `Error` schema
- 分页响应统一 `Page<T>` schema

---

## 9. 审计与安全

### 9.1 审计

```sql
create table audit_log (
  id bigserial primary key,
  at timestamptz not null default now(),
  actor text not null,              -- username 或 token id
  actor_role text,
  action text not null,             -- device.ops.restart / dataset.delete / auth.login
  target_type text, target_id text,
  outcome text not null,            -- ok|denied|failed
  ip inet, user_agent text,
  request_id text,
  detail jsonb                     -- 变更前后摘要、命令、备份路径
);
create index audit_at on audit_log (at desc);
create index audit_actor on audit_log (actor, at desc);
create index audit_target on audit_log (target_type, target_id, at desc);
```

**必须审计**：登录/登出/失败、refresh 复用、权限变更、设备运维、删除、报告生成/导出、service token 创建/删除、break-glass、**强制覆盖（force）**。

**不审计**：普通查询（量太大）；查询失败的 403 记录 `outcome=denied` 但只保留 30 天。

**不写入**：密码、token 明文、Authorization 头、音频内容、转写全文。

### 9.2 传输与头

```nginx
add_header X-Content-Type-Options nosniff always;
add_header X-Frame-Options DENY always;
add_header Referrer-Policy no-referrer always;
add_header Content-Security-Policy "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'" always;
add_header Strict-Transport-Security "max-age=31536000" always;   # 有 TLS 时
```

- CORS：生产**同源**（nginx 反代），不开放跨域；开发允许 `http://localhost:5173`
- 报告 HTML：`Content-Disposition: attachment` + `Content-Security-Policy: sandbox allow-popups`，独立路径 `/api/v1/reports/{id}/html`，不进 SPA 域
- 音频：返回 `302` 到本地存储签名 URL（5min），由 API 流式返回，不占内存

### 9.3 敏感字段

| 字段 | 处理 |
|---|---|
| LDAP 密码 | 只用一次 bind，不落库、不落日志 |
| 设备密码/SSH 私钥/cloud API Key | Fernet 加密（Q7）；API 只返回凭据类型和 `has_password` / `has_ssh_key` / `has_api_key` 布尔值 |
| refresh token | 只存 hash |
| service token | 只存 hash，创建时返回一次 |
| `data/secrets/credentials_key` | Fernet 密钥文件，权限 0600；备份时必须与数据库一起保留 |

日志脱敏中间件：`password|token|secret|authorization|cookie` → `***`。

### 9.4 上传

- 注册音频：`POST /speakers/{id}/enrollments` 直接 multipart 上传到 API（≤50MB），落 `data/objects`，返回 enrollment id
- 数据集：**不走上传**，只填 NFS 根路径，服务端按白名单根校验（`E_PATH_ESCAPE`）
- 大小上限：单文件 50MB，单次批量 200 个

---

## 10. 关键流程

### 10.1 登录 → 调用 → 刷新

```
1. POST /auth/login           → access(30m) + refresh cookie(7d)
2. 任意请求 Authorization: Bearer <access>
3. 401 E_TOKEN_EXPIRED
4. POST /auth/refresh（自动，单飞） → 新 access + 新 refresh
5. 重放原请求（仅一次）
6. refresh 失败 → 清状态，跳登录
```

前端拦截器要求：**并发 401 只触发一次刷新**，其余请求排队等待。

### 10.2 CI 触发评测

```
1. POST /admin/service-tokens（人工，一次性）→ ase_…
2. POST /runs (Idempotency-Key) → 201
3. 轮询 GET /runs/{id} 每 10s 或 SSE
4. 终态：completed/partial → GET /evaluations/{id}/summary
5. GET /evaluations/{id}/gate → pass/fail/not_configured（见 §12）
6. 判定：fail 或（not_configured 且自带阈值超限）→ exit 1
```

### 10.3 设备运维（高危）

```
1. GET /devices/{id} → ETag + fingerprint.uid + 是否有 running target
2. POST /devices/{id}/ops/restart
   {"confirm": true, "expected_fingerprint_uid": "…", "reason": "网关无响应"}
3. 校验：权限 + 设备空闲 + 指纹未变（防误操作到另一台）
4. 202 + job；审计 detail 记录命令与输出摘要
```

`expected_fingerprint_uid` 不匹配 → `409 E_FINGERPRINT_CHANGED`。

---

## 11. 错误码总表（API 层）

| code | HTTP | 说明 |
|---|---|---|
| `E_AUTH_INVALID` | 401 | 用户名或密码错误 |
| `E_AUTH_BACKEND_DOWN` | 503 | LDAP 不可达 |
| `E_TOKEN_EXPIRED` | 401 | access 过期 |
| `E_TOKEN_INVALID` | 401 | 签名/格式/aud 错误 |
| `E_SESSION_REVOKED` | 401 | 会话被撤销 |
| `E_REFRESH_REUSE` | 401 | refresh 复用，整族失效 |
| `E_FORBIDDEN` | 403 | 权限不足 |
| `E_NOT_OWNER` | 403 | 非创建者，不能操作他人 Run/报告 |
| `E_BENCHMARK_ARCHIVED` | 409 | 引用的基准已归档 |
| `E_GATE_POLICY_INVALID` | 422 | 门禁规则非法（未知指标/区间倒置） |
| `E_NOT_FOUND` | 404 | 资源不存在 |
| `E_VALIDATION` | 400 | 参数格式错误 |
| `E_SEMANTIC` | 422 | 语义组合非法 |
| `E_CONFLICT` | 409 | 通用状态冲突 |
| `E_DEVICE_BUSY` | 409 | 设备被占用 |
| `E_TARGET_OFFLINE` | 409 | 目标设备不可用 |
| `E_RUN_TERMINAL` | 409 | Run 已终态 |
| `E_FINGERPRINT_CHANGED` | 409 | 指纹与预期不符 |
| `E_IDEMPOTENCY_CONFLICT` | 409 | 同 key 不同 body |
| `E_PRECONDITION_REQUIRED` | 428 | 配置类 PATCH 未带 `If-Match` |
| `E_PRECONDITION_FAILED` | 412 | ETag 不匹配（附当前对象） |
| `E_VERSION_IN_USE` | 409 | 版本被 Run 引用 |
| `E_REPORT_EXPIRED` | 410 | 快照失效 |
| `E_RATE_LIMITED` | 429 | 限流 |
| `E_DEPENDENCY_DOWN` | 503 | DB/本地存储不可用 |
| `E_INTERNAL` | 500 | 未预期错误 |

> 业务域错误码（`E_AUDIO_MISSING` 等）在各自模块定义，API 层原样透传，不重编号。

---

## 12. 基线门禁（契约预留，阈值配置化）

### 12.1 原则

- **代码里没有阈值**：所有阈值存 `gate_policy.rules`，管理员在平台内改，改完立即生效；
- **一期只留契约**：端点与表先建好，默认 `enabled=false`，返回 `not_configured`，CI 自行判断；
- **不阻断评测**：门禁只做判定，不改变 Run/Evaluation 的状态。

### 12.2 数据结构

```sql
create table gate_policy (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  benchmark_id uuid references benchmark(id) on delete cascade,  -- null = 全局默认
  enabled boolean not null default false,
  rules jsonb not null default '[]',
  on_missing text not null default 'skip',   -- skip|fail：没有基线/不可评时怎么算
  created_by text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
```

`rules` 形态：

```json
[
  {"metric": "cer", "subset": "near", "op": "<=", "value": 0.070,
   "scope": "absolute", "label": "CER 不高于 7.0%"},
  {"metric": "cer", "subset": "near", "op": "<=", "value": 0.003,
   "scope": "delta_vs_baseline", "label": "相对基线回退不超过 0.3pp"},
  {"metric": "name_acc", "subset": "near", "op": ">=", "value": 0.85, "scope": "absolute"},
  {"metric": "rtf_dev", "subset": "near", "op": "<=", "value": 0.30, "scope": "absolute"},
  {"metric": "coverage", "subset": "near", "op": ">=", "value": 0.98, "scope": "absolute"}
]
```

### 12.3 端点

```
GET  /api/v1/gate-policies
POST /api/v1/gate-policies
PATCH /api/v1/gate-policies/{id}
GET  /api/v1/evaluations/{id}/gate?policy_id=&baseline_evaluation_id=
```

```json
{
  "status": "fail",
  "policy": {"id": "gp_1…", "name": "近场回归门禁", "enabled": true},
  "checks": [
    {"metric": "cer", "subset": "near", "op": "<=", "threshold": 0.070,
     "observed": 0.0682, "result": "pass", "label": "CER 不高于 7.0%"},
    {"metric": "cer", "subset": "near", "op": "<=", "threshold": 0.003,
     "observed": 0.0041, "delta": 0.0041, "result": "fail",
     "baseline": {"evaluation_id": "ev_44…", "value": 0.0641},
     "label": "相对基线回退不超过 0.3pp"},
    {"metric": "der", "subset": "near", "op": "<=", "threshold": 0.20,
     "observed": null, "result": "skip", "reason": "no_speaker_labels"}
  ],
  "failed_count": 1,
  "skipped_count": 1
}
```

- `status ∈ pass | fail | not_configured`
- 未配置策略 → `200 {"status":"not_configured","checks":[]}`（**不是错误**，CI 可按自己的阈值判断）
- `on_missing=skip`：不可评的指标跳过；`fail`：视为失败（用于严格门禁）

### 12.4 CI 用法

```
1. POST /runs → 轮询/SSE 到终态
2. GET /evaluations/{id}/gate?policy_id=gp_1
3. status=pass → exit 0；fail → exit 1 并打印失败项
4. status=not_configured → 回退到 CI 自带阈值，或直接 exit 0（可配置）
```

阈值上线后，**CI 脚本不用改**，只需在平台把策略 `enabled=true`。

### 12.5 一期落地范围

| 项 | 一期 | 二期 |
|---|---|---|
| 表结构与端点 | ✅ 建好，返回 `not_configured` | — |
| 策略配置 UI | 只读列表 + JSON 查看 | 可视化规则编辑器 |
| 判定逻辑 | 实现（绝对阈值 + 相对基线） | 分桶级门禁、趋势门禁 |
| 与报告联动 | 报告顶部显示门禁结果徽标 | 历史门禁趋势 |

---

## 13. 测试点

### 13.1 单元

- JWT 签发/校验：过期、错 aud、错签名、缺 sid
- 权限装饰器：每个端点声明的权限与矩阵一致（**用表驱动测试遍历路由**）
- 游标编解码：非法游标 → 400；跨排序字段 → 400
- 幂等：同 key 同 body / 同 key 异 body / 过期 key
- ETag：并发 PATCH 只有一个成功

### 13.2 集成

- LDAP mock：用户不存在、密码错、组缺失、组多命中取最高
- refresh 轮换 + 复用检测（旧 token 再用 → 全族失效）
- refresh **30s 宽限**：宽限期内旧 token 返回同一新 token，不触发复用
- 配置类 `PATCH` 缺 `If-Match` → 428；旧 ETag → 412 且 `detail.current` 完整
- 用户级事件流：同一对象 500ms 合并；断线 resync
- SSE：断线续传（`Last-Event-ID`）、缓冲溢出触发 `resync`、心跳
- 限流：第 6 次登录 429
- 审计：运维动作与业务写同事务（模拟写审计失败 → 业务回滚）

### 13.3 安全

- 越权矩阵：4 角色 × 全部写端点（表驱动）
- grant 叠加：tester + `dataset:write` 能导入但不能删；过期后立即失效
- grant 禁止项：`admin:users`、`device:ops` 不可被 grant（只能改角色）
- 对象级：tester 取消他人 Run → 403；取消本人 → 200
- 越权矩阵：engineer 调 `device:ops` / `admin:*` → 403
- service token 调 `device:ops` → 403
- 报告 HTML 含 `<script>` → 下载后不执行（sandbox）
- 门禁：未配置 → `not_configured`；`on_missing=fail` + 不可评指标 → `fail`
- 日志中不出现密码/token（正则扫描）

### 13.4 性能

- `GET /runs?limit=50` P95 < 80ms（1 万 Run）
- `GET /evaluations/{id}/matrix` P95 < 150ms（50 目标 × 8 指标）
- SSE 单流 1000 事件无丢失、内存平稳

---

## 14. 验收

- [x] OpenAPI 3.1 可导出，`openapi-typescript` 生成零错误（`make openapi`）
- [x] 所有路由都有权限声明（`test_mounted_application_routes_have_auth_policy`；静态测试通过，本机阶段不接 Jenkins/CI）
- [x] 角色矩阵测试覆盖 4 角色与全部写端点（`test_role_matrix_covers_every_write_permission`、`test_every_mutation_route_has_an_exact_permission_or_auth_policy`）
- [x] grant 生效/过期/禁用项三类用例通过（grant 集成测试覆盖授权、到期、撤销和禁止项）
- [x] `GET /evaluations/{id}/gate` 未配置时返回 `not_configured`，service token 流程可读取结论
- [x] 登录 → 刷新 → 登出 → 复用检测全链路通过
- [x] SSE 断线 30s 后重连能补齐事件（集成测试实际等待 30 秒）
- [x] 运维动作审计可查到操作者、IP、命令、结果
- [x] 报告 HTML 默认作为附件下载，并带 `sandbox` CSP
- [ ] CI service token 跑通"建 Run → 取结论 → 失败退出"（本机 API 已验证建 Run 与评分结论读取；CI runner/失败退出待 Jenkins 阶段）
- [x] 双端并发编辑同一配置：一方 412，另一方 200，强制覆盖须带原因且审计可查 force（Device / MetricProfile / Benchmark）
- [x] 三标签页并发刷新不误判复用，三个会话均保持登录
- [x] 设备列表能显示 `lease.holder`（Run、持有人、预计释放时间；Playwright + Axe 验收）

---

## 15. 风险与已确认

### 15.1 风险

| # | 风险 | 缓解 |
|---|---|---|
| 1 | LDAP 组结构未知（`memberOf` 或 `groupOfNames`） | 上线前用真实账号 `ldapsearch` 确认；适配层支持两种 |
| 2 | 用户改名/离职后 JWT 仍有效 | session 按 username 关联；离职 `active=false` + 撤销全部 session |
| 3 | 可选反代（Caddy/nginx）缓冲 SSE | 配置 `flush_interval -1` / `proxy_buffering off`；默认直连无代理 |
| 4 | 报告 HTML 内联资源过大 | 单报告上限 20MB；超限改为外部资源 ZIP |
| 5 | grant 泛滥导致权限失控 | grant 页面显示"有效权限预览"；每季度审计一次过期未回收项 |

### 15.2 已确认

| 事项 | 结论 |
|---|---|
| refresh 存放 | **HttpOnly Cookie**（`SameSite=Lax` + Path 限定 + Origin 校验；HTTPS 时加 `__Host-`/`Secure`） |
| 角色模型 | **viewer / tester / engineer / admin** 四档 + 按人 grant |
| CI 门禁 | **契约预留**，阈值做成 `gate_policy` 配置，一期返回 `not_configured` |
| P0 LDAP 登录实测（2026-11-07） | 临时 PostgreSQL 上以 `AUTH_MODE=ldap` 登录成功，`GET /auth/me` 返回 `auth_method=ldap`、默认 `viewer`；DN 模板模式不读取组，LDAP 群组数为 0。仅在 dev 显式允许明文 LDAP 时验证 |
| P0 LDAP 真服务复核（2026-11-07） | 使用本机 LDAP 配置与指定账号再次走真实 `POST /api/v1/auth/login`、`GET /api/v1/auth/me`，均返回 200，`auth_method=ldap`、群组数为 0；验证只创建 `asr_eval_test` 内独立临时 schema，结束后删除，未连接开发库 |

### 15.3 仍待定（不阻塞 M8）

1. LDAP 组 DN 的真实结构（需要 IT 提供一条样例）。
2. 是否需要"一人多角色"（当前是单角色 + grant，够用）。
3. 门禁是否要按 subset 分别配置（当前 `rules` 已带 `subset` 字段，天然支持）。
