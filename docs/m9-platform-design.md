# M9 · 平台基础详细设计（轻量版 v2）

> 修订原因：原方案按"服务器级平台"设计（9 个容器、MinIO + Redis + nginx、25G 内存上限），对一个 **3~5 人、局域网、先在本机验证** 的评测工具过重。
> 本版目标：**2~3 个容器、2~3G 内存、一条命令起停、备份就是一个目录**。
> 业务逻辑不变（M1~M8），只换基础设施。

---

## 0.0 实施阶段范围（2026-11-04 起）

当前只做**本机开发**，以下内容**推迟到功能稳定后**：

| 项 | 当前 | 上线前 |
|---|---|---|
| Jenkins / CI | 不接；本地 `make test` | 配 Jenkinsfile + 真机 Job |
| 备份 / 恢复 | 不做；数据可丢 | `asrctl backup` + 演练 |
| 外部 nginx / TLS | 不配；`http://localhost:8080` | 域名反代 + SSE 关缓冲 + Cookie 安全模式 |
| S3 后端 | 只留接口 | 按需启用 |

代码里保留接口与配置项（`ObjectStore`、`COOKIE_SECURE`、`TRUSTED_PROXIES`），但**不实现部署脚本**，避免现在过度设计。

---

## 0. 修订摘要

| 项 | 原方案 | 轻量版 | 影响 |
|---|---|---|---|
| 容器数 | 9 | **2（lite）/ 3（standard）** | 部署、排障、启动时间 |
| 内存上限 | ~25G | **2.75G / 4.5G** | 本机可跑 |
| 对象存储 | MinIO（+2G） | **本地目录** `data/objects/` | 少一个服务、备份更简单 |
| 队列/租约 | Redis + arq | **PostgreSQL**（SKIP LOCKED + 租约表） | 少一个服务、少一种故障 |
| 反向代理 | nginx | **FastAPI 直接服务静态资源** | 少一层；SSE 不再有缓冲坑 |
| 事件广播 | Redis Pub/Sub | **PG LISTEN/NOTIFY** | 少一个服务 |
| TLS | 必须 | **默认 HTTP（局域网）**，可选 Caddy | 本机零配置 |
| 磁盘 | 1TB SSD | **256GB 够用** | 音频不落盘 |

**保留不变**：PostgreSQL、只读 NFS、JWT/LDAP、结构化日志、备份恢复、审计。

---

## 1. 决策记录

| # | 决策 | 理由 |
|---|---|---|
| D-M9-1 | **默认 2 容器**：`db` + `app`；重评分时加 `worker` | 本机 Docker 一条命令；服务器同一套 |
| D-M9-2 | **去掉 MinIO 容器**，默认本地目录；保留 `ObjectStore` SPI 与 **`S3ObjectStore` 实现**（不默认部署） | 3~5 人、年增 20~50GB，本地盘足够；以后要多机/托管再切 S3 |
| D-M9-3 | **去掉 Redis**，队列/租约/事件/幂等全部落 PostgreSQL | PG 已是唯一真相与单点，不再引入第二个有状态服务 |
| D-M9-4 | **去掉 nginx**，uvicorn 直接服务 SPA 与 API | 局域网无 TLS 需求；SSE 无代理缓冲问题 |
| D-M9-5 | 队列用 **`job` 表 + `FOR UPDATE SKIP LOCKED`** | 万级任务足够；重试/延时用字段表达 |
| D-M9-6 | 设备租约用 **`device_lease` 单行 + 过期时间** | 原子抢锁，心跳续期，进程崩溃自动过期 |
| D-M9-7 | 跨进程事件用 **`LISTEN/NOTIFY`**，单进程内直接分发 | 无 Redis 也能让 worker 的事件到达 API |
| D-M9-8 | 默认 **HTTP**；域名由**外部 nginx** 反代（不在本 Compose 内）；需要 TLS 时在 nginx 终止 | 本机零配置；复用现有运维体系 |
| D-M9-9 | 备份 = `pg_dump` + `tar data/` + `secrets/` | 一个目录、可校验、可演练 |
| D-M9-10 | 音频仍**只读挂 NFS**，不复制、不进对象目录 | 存储成本与一致性 |
| D-M9-11 | 中文/英文/时间码字体随镜像打包 | 本机与服务器渲染一致 |
| D-M9-12 | 保留 `asrctl`，命令减少到 6 个 | 运维动作可重复 |

---

## 2. 两个部署档位

### 2.1 lite（默认，本机 / 小服务器）

```
┌──────────────────────────────┐
│ app  (uvicorn :8080)         │
│  API + SPA + scheduler       │
│  worker（同进程后台任务）      │
└──────────────┬───────────────┘
               │
        ┌──────▼──────┐
        │ postgres 16 │
        └─────────────┘
        data/objects/（本地目录）
        /mnt/asr-data（NFS 只读，可选）
```

- 2 个容器：`db`、`app`
- 适合：本机验证、3 台设备、每天 ≤ 20 个 Run
- 评分任务在 app 内的进程池执行（并发 2），不阻塞 API 事件循环
- 不需要 Docker 也能跑：本机装 PG，`uv run uvicorn` + `uv run worker`

### 2.2 standard（服务器，可选）

```
        ┌──────────┐
        │  app     │  API + scheduler
        └────┬─────┘
             │
   ┌─────────┼──────────┐
┌──▼───┐  ┌──▼────┐  ┌──▼─────┐
│ db   │  │worker │  │ caddy  │（可选 TLS）
└──────┘  └───────┘  └────────┘
```

- 3~4 个容器：`db`、`app`、`worker`、可选 `caddy`
- 评分/导入移到独立 worker，app 只做 API 与调度
- 仍然没有 Redis / MinIO / nginx

### 2.3 资源

| 服务 | lite 限额 | standard 限额 | 实际 RSS 量级 |
|---|---|---|---|
| postgres | 1G | 2G | 200~500M |
| app | 768M | 1G | 250~450M |
| worker | （进程池） | 1.5G | 300~800M |
| caddy | — | 128M | 20M |
| **合计** | **~1.8G** | **~4.6G** | **0.8~1.8G** |

磁盘：代码 + 依赖 1G，对象 20~50G/年，PG 5~10G/年 → **256GB 足够**。

---

## 3. 配置与密钥

### 3.1 配置

单文件 `data/config.env`（0600），`docker compose --env-file` 加载：

```env
APP_ENV=prod
APP_VERSION=0.1.0
PUBLIC_BASE_URL=http://localhost:8080

DATABASE_URL=postgresql+psycopg://asr:******@db:5432/asr_eval
DATA_DIR=/data
STORAGE_BACKEND=local           # local | s3（s3 为可选后端，默认不启用）
# S3_ENDPOINT=...
# S3_BUCKET=asr-eval
# S3_ACCESS_KEY_FILE=/data/secrets/s3_access_key
# S3_SECRET_KEY_FILE=/data/secrets/s3_secret_key
COOKIE_SECURE=false             # 走 HTTPS 时设 true
TRUSTED_PROXIES=127.0.0.1,::1   # 外部 nginx 的地址
NFS_ROOT=/mnt/asr-data

AUTH_MODE=ldap                  # ldap | local（本机验证可先用 local）
LDAP_SERVER_URL=ldaps://ldap.intra:636
LDAP_USER_BASE_DN=ou=people,dc=intra,dc=corp
LDAP_BIND_DN=cn=svc-asr-eval,ou=service,dc=intra,dc=corp
# LDAP_BIND_PASSWORD 由本机密钥管理/运行环境注入，不写入仓库
LDAP_USER_DN_TEMPLATE=cn={username},ou=people,dc=intra,dc=corp  # 目录不提供服务账号搜索时使用
LDAP_START_TLS=true
LDAP_CONNECT_TIMEOUT_S=5
LDAP_CA_CERT_FILE=/path/to/ldap-ca.pem
LDAP_TLS_VALID_NAMES='["ldap.intra"]'
# P0 本机临时兼容：LDAP_START_TLS=false + LDAP_ALLOW_INSECURE=true（仅 APP_ENV=dev）

WORKER_INLINE=true              # lite：评分在 app 进程池
SCORE_CONCURRENCY=2
INFER_CONCURRENCY=3

SESSION_TTL=30m
REFRESH_TTL=7d
LOG_LEVEL=info
```

### 3.2 密钥

本机开发配置放在被 Git 忽略的 `backend/.env`，由 `make dev` 加载；E2E 和单元测试不加载该文件，仍使用各自显式配置的隔离环境。LDAP 用户密码仅在登录请求中传递，不保存到配置文件。

```
data/secrets/
├── jwt_key              # 32B，JWT HS256
├── credentials_key      # Fernet，设备密码/SSH 私钥/cloud API Key/refresh 宽限 token
├── ldap_bind            # 目录只读账号密码
└── database_url         # 含 PG 密码（可选，覆盖 env）
```

- 目录 0700，文件 0600
- `credentials_key` **必须进备份**；丢失后设备凭据不可解，refresh 宽限行在 30 秒后自然失效
- 本机验证可用 `AUTH_MODE=local` + `asrctl user add`，不依赖 LDAP

### 3.3 配置校验

启动时 pydantic-settings 校验：

- 必填缺失 / 类型错 / URL 不可解析 → 退出码 78
- `AUTH_MODE=ldap` 但 LDAP 不可达 → 启动成功，`/readyz` 报 `degraded`（不阻塞查看历史数据）
- `DATA_DIR` 不可写 → 直接退出

---

## 4. PostgreSQL 承担的四件事

### 4.1 业务数据（不变）

M1~M7 的表结构不变；连接池每进程 10，`max_connections=50` 足够。

### 4.2 任务队列

```sql
create table job (
  id bigserial primary key,
  kind text not null,                 -- infer_case | score_eval | import_version | export_report
  payload jsonb not null,
  status text not null default 'queued',  -- queued|running|done|failed|cancelled
  priority int not null default 0,
  run_at timestamptz not null default now(),
  attempts int not null default 0,
  max_attempts int not null default 3,
  locked_by text,
  locked_at timestamptz,
  last_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index job_ready on job (priority desc, id) where status = 'queued';
```

领取（原子，多 worker 安全）：

```sql
with next as (
  select id from job
  where status = 'queued' and run_at <= now()
  order by priority desc, id
  limit 1
  for update skip locked
)
update job j
set status = 'running', locked_by = $1, locked_at = now(), attempts = attempts + 1
from next
where j.id = next.id
returning j.*;
```

- 可见性超时：`locked_at < now() - interval '10 min'` 且未完成 → 重新入队
- 重试：`attempts < max_attempts` → `status='queued'`, `run_at=now()+backoff`
- 取消：`status='cancelled'`，worker 在处理间隙检查

> 为什么不用 arq：任务量万级、单机、只需"领任务/重试/延时"三种语义；一张表 + 200 行代码，少一个 Redis 依赖。原 Q6 决策作废。

### 4.3 设备租约

```sql
create table device_lease (
  device_id uuid primary key references device(id) on delete cascade,
  run_target_id uuid not null,
  holder text not null,
  acquired_at timestamptz not null default now(),
  expires_at timestamptz not null
);
```

抢占：

```sql
insert into device_lease (device_id, run_target_id, holder, expires_at)
values ($1,$2,$3, now() + interval '120 seconds')
on conflict (device_id) do update
  set run_target_id = excluded.run_target_id,
      holder = excluded.holder,
      acquired_at = now(),
      expires_at = excluded.expires_at
  where device_lease.expires_at < now()
returning *;
```

- 返回 0 行 = 设备被占用，读出 holder 返回 `409 E_DEVICE_BUSY`
- 心跳每 30s `update ... set expires_at=now()+120s where device_id=$1 and holder=$2`
- 进程崩溃 → 120s 后自动可被抢占
- **不自动重启设备**（Q8 不变）

### 4.4 幂等 / SSE 票据 / 限流

| 需求 | 存储 | 说明 |
|---|---|---|
| `Idempotency-Key` | `idempotency_key(user, key, request_hash, response, status, created_at)` | 24h 清理 |
| SSE ticket | `sse_ticket(hash, user, expires_at, used_at)` | 60s，一次性 |
| refresh 宽限 | `refresh_grace(old_hash, new_token_enc, expires_at)` | 30s，token 用 `credentials_key` 加密 |
| 限流 | **进程内令牌桶** | 单 API 进程；重启清零，可接受 |
| 会话撤销 | `user_session.revoked_at`（已有） | 不再需要 Redis key |

清理任务每小时跑一次：过期幂等键、ticket、宽限、`job` 历史。

---

## 5. 本地对象存储

### 5.1 目录

```
data/
├── objects/
│   ├── raw/{run_id}/{case_result_id}.json.gz      # 设备原始响应
│   ├── reports/{report_id}/report.html
│   ├── reports/{report_id}/report.xlsx
│   └── exports/{job_id}/...
├── tmp/                                            # 导入中间文件，1 天
└── secrets/
```

- 单文件 ≤ 50MB；报告单份 ≤ 20MB
- 写入用"临时文件 + rename"，避免半截文件
- 路径校验：`realpath` 必须在 `data/objects` 下

### 5.2 ObjectStore SPI

```python
class ObjectStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None: ...
    def open(self, key: str) -> BinaryIO: ...
    def delete(self, key: str) -> None: ...
    def delete_prefix(self, prefix: str) -> int: ...
    def signed_url(self, key: str, ttl: int = 300) -> str: ...
```

- `LocalObjectStore`（默认）：`signed_url` 返回 `/api/v1/files/{key}?exp=...&sig=...`（HMAC-SHA256）
- `S3ObjectStore`（已实现，默认不启用）：配置 `STORAGE_BACKEND=s3` 即切换；业务代码只依赖 `ObjectStore` 接口
- 两套实现共用同一套 key 规则，切换时用 `asrctl storage migrate` 搬运对象
- 前端 `<audio>` / 下载链接直接用签名 URL，不需要额外 header

### 5.3 清理

```bash
asrctl gc --raw-days 90 --exports-days 7 --tmp-days 1
```

- 报告不自动删（业务资产），由用户/保留策略处理
- 删除前先删索引行，再删文件；失败留待下次

---

## 6. 事件与实时

| 场景 | 机制 |
|---|---|
| 同一进程内（API 自身的 mutation） | 进程内 `EventBus`（asyncio） |
| worker → API | `pg_notify('asr_events', json)`；API 每个进程 `LISTEN` |
| SSE 断线续传 | 进程内环形缓冲 500 条 / 10 分钟；超出则 `resync` |
| 用户级事件流 | 同一通道，按 `user` 过滤 |

- 单 API 进程时无需 NOTIFY，但统一走同一总线，代码只有一条路径
- NOTIFY payload ≤ 8KB：只发 `{type,id,version,actor,at}`，不发全量
- API 重启 → 客户端 `resync`，不做持久化回放

> 取舍：SSE 缓冲不落盘，API 重启后正在看的页面会重新拉一次数据。这是可接受的，不引入 Redis。

---

## 7. 进程模型

### 7.1 lite（默认）

| 组件 | 并发 | 说明 |
|---|---|---|
| uvicorn | 1 进程 | 3~5 用户足够；保证进程内事件总线简单 |
| scheduler | 后台协程 | 每 2s 扫描 `run_target`，生成 `job` |
| infer 执行 | asyncio，最多 3 并发 | 设备是瓶颈，I/O 等待 |
| score 执行 | `ProcessPoolExecutor(max_workers=2)` | 避免 GIL 阻塞；DER CPU 密集 |
| import | 后台线程 1 个 | 避免与评分抢 CPU |

### 7.2 standard

- `app`：uvicorn + scheduler，不执行重任务
- `worker`：领取 `job`，执行 infer / score / import；`SCORE_CONCURRENCY=2`
- 优雅退出：停止领任务 → 等待当前 ≤ 60s → 释放租约 → 退出
- **流式会话必须 `close()`**：强杀会让引擎卡死（M2 已定）

### 7.3 启动顺序

P0 本机开发使用显式迁移，不在应用启动时自动改库：

```
make db-up → make migrate → make dev
```

`make migrate` 使用已导出的 `DATABASE_URL`；未设置时读取 `backend/.env`，再退回本机默认数据库。执行前应确认目标库。
容器部署时由 app 自动迁移并加 advisory lock 属于后续部署方案，不在当前 P0 实现范围。

---

## 8. 数据库迁移

- Alembic，单 head
- P0 通过 `make migrate` 显式执行 `alembic upgrade head`
- P0 app 启动不自动迁移；`make migrate` 失败时 app 不会替用户改用另一数据库
- 规则不变：expand / contract；大表索引用 `CONCURRENTLY`
- 自动迁移开关与容器启动锁未在 P0 实现；生产部署阶段再落实

---

## 8.5 构建与发布（无镜像仓库）

内网没有 Docker registry，镜像通过 tar 传递：

```bash
# Jenkins：构建并归档
docker build -t asr-eval/app:<sha> .
docker save asr-eval/app:<sha> | gzip > dist/app-<sha>.tar.gz

# 部署机：加载并切换
gunzip -c app-<sha>.tar.gz | docker load
sed -i 's|^APP_TAG=.*|APP_TAG=asr-eval/app:<sha>|' data/config.env
docker compose up -d
```

- compose 里写 `image: ${APP_TAG}`，不写 `build:`
- 回滚 = 加载上一个 tar + 改 `APP_TAG` + `up -d`
- 迁移只做 expand，旧镜像仍可启动
- 构建产物保留最近 10 个（Jenkins 归档）

---

## 9. 备份与恢复

### 9.1 目标

| 项 | lite | standard |
|---|---|---|
| RPO | 24h | 24h |
| RTO | 1h | 2h |
| 保留 | 日备 7、周备 4 | 日备 14、周备 8、月备 6 |

### 9.2 内容

```
backup/20261104-0200/
├── db.dump              # pg_dump -Fc
├── objects.tar.gz       # data/objects
├── config.tar.gz        # config.env + secrets/
├── version.json
└── SHA256SUMS
```

一条命令：

```bash
asrctl backup --dest /backup/asr-eval
```

脚本要点：

```bash
pg_dump -Fc > db.dump
tar -C "$DATA_DIR" -czf objects.tar.gz objects
tar -C "$DATA_DIR" -czf config.tar.gz config.env secrets
sha256sum * > SHA256SUMS
```

### 9.3 恢复

```bash
asrctl restore --from /backup/asr-eval/20261104-0200
```

顺序：**secrets → db → objects → 启动 → doctor**。
先恢复 secrets，否则设备密码解不开。

### 9.4 演练

- 每月一次，恢复到临时目录 + 临时库
- 验收：打开一个历史 Run 的 Badcase、下载报告、设备探测正常
- 记录到 `ops/drills/`；本机可运行 `make drill-backup` 做隔离库 + 临时 data/ 的流程演练

---

## 10. 可观测性

| 端点 | 检查 |
|---|---|
| `/healthz` | 进程存活 |
| `/readyz` | PG、`DATA_DIR` 可写、可选 NFS 探针、磁盘水位；未配置 NFS 不影响本机就绪 |
| `/system/status` | 上述 + 队列长度、运行中 Run、最近备份时间、设备在线数 |
| `/version` | 应用版本、数据库 schema 版本和当前代码 migration head |

`./asrctl doctor` 只读检查 `/healthz`、`/readyz` 和 `/version`；数据库未就绪、数据库 schema 落后于代码 head 或磁盘/NFS 检查异常时返回非零退出码。

日志：JSON 单行到 stdout；Docker `json-file` 50MB×5；审计进 PG。

告警（一期）：

- 磁盘 > 85% 警告，> 95% 阻断导入/报告
- 备份失败（`asrctl backup` 非零）
- 队列积压 > 50 且 10 分钟未下降
- 设备离线 > 10 分钟
- 无 webhook 时只在健康页显示

---

## 10.5 外部 nginx 反代约定

nginx 由运维统一维护，**不在本项目的 Compose 内**。需要满足：

```nginx
location / {
    proxy_pass         http://127.0.0.1:8080;
    proxy_http_version 1.1;
    proxy_set_header   Host              $host;
    proxy_set_header   X-Real-IP         $remote_addr;
    proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header   X-Forwarded-Proto $scheme;

    client_max_body_size 50m;
}

# SSE：关缓冲、放宽超时
location ~ ^/api/v1/(events|runs/.+/events|evaluations/.+/events|jobs/.+/events)$ {
    proxy_pass         http://127.0.0.1:8080;
    proxy_http_version 1.1;
    proxy_set_header   Host $host;
    proxy_buffering    off;
    proxy_cache        off;
    proxy_read_timeout 1h;
    add_header         X-Accel-Buffering no;
}
```

应用侧：

- uvicorn 启动参数 `--proxy-headers --forwarded-allow-ips=127.0.0.1`
- 只信任 `TRUSTED_PROXIES` 列表里的 `X-Forwarded-*`
- 审计与限流取 `X-Forwarded-For` 的第一个可信地址
- 若 nginx 终止 TLS：`COOKIE_SECURE=true`，refresh cookie 用 `__Host-` 前缀
- 若纯 HTTP：`COOKIE_SECURE=false`，cookie 用 `asr_refresh`（`__Host-` 要求 Secure，HTTP 下会被浏览器丢弃）

> 这是唯一需要和运维对齐的地方：**SSE 必须关 `proxy_buffering`**，否则进度会"攒一批再吐"。

## 11. 安全（局域网口径）

| 项 | 做法 |
|---|---|
| 网络 | 只监听 `0.0.0.0:8080`；建议防火墙限定办公网段 |
| 认证 | LDAP 或本地账号；JWT + refresh |
| TLS | 默认关闭；需要时加 `caddy` profile，自动证书或内部 CA |
| 密钥 | `data/secrets/` 0600，不进镜像、不进 Git |
| 容器 | 非 root、只读根文件系统、`cap_drop: ALL` |
| 路径 | 对象目录与 NFS 根都做 `realpath` 白名单 |
| 审计 | 登录、运维、删除、授权、force 覆盖 |
| 时间 | 依赖宿主机 NTP；偏移 > 5s 告警 |

> 明确风险：HTTP 下 token 在局域网内明文传输。若办公网不可信，开 Caddy profile。

---

## 12. 运维手册（精简）

```bash
./asrctl doctor        # 自检（在仓库根目录运行）
asrctl backup          # 备份
asrctl restore --from  # 恢复
asrctl gc              # 清理
asrctl user add        # 本地账号（无 LDAP 时）
asrctl requeue --run   # 重建某 Run 的任务
```

| 现象 | 处置 |
|---|---|
| 页面打不开 | `docker compose ps`；`/readyz` |
| Run 卡住 | `asrctl doctor`；检查 `job` 表 running 超时；`requeue` |
| 设备全离线 | 从宿主机 `curl` 设备端口；检查路由；不自动重启 |
| 磁盘满 | `asrctl gc`；清理 `data/tmp`；扩容 |
| 队列堆积 | 看 worker 是否存活；提高 `SCORE_CONCURRENCY`（不超过核数） |
| 迁移失败 | 看 app 日志；修复后重启；不要手工改 schema |

---

## 13. 验收

- [ ] 上线前：本机 `docker compose up -d`，2 个容器，3 分钟内可登录
- [ ] 上线前：内存占用（`docker stats`）稳定在 **1.5G 以内**（3 设备并发）
- [x] 2026-11-07：本机完整流程无需 MinIO / Redis / nginx；`make e2e` 使用隔离 PostgreSQL 和本地假设备通过登录、建 Run、识别与评分
- [ ] 30 分钟音频 + 3 路并发，API 仍可响应（P95 < 300ms）
- [ ] 上线前（当前阶段明确推迟）：备份只有一个目录；恢复后历史报告可打开
- [x] 2026-11-10：本机隔离库 `asr_eval_restore_drill` + 临时 data/ 完成 `pg_dump -Fc` / `pg_restore` 与 `tar` 恢复演练，行值、对象文件、凭据文件及 SHA256SUMS 全部校验通过；脚本 `tools/backup_restore_drill.sh`，记录见 `evidence/backup-restore-drill.md`
- [x] 2026-11-07：`make e2e-recovery` 创建 Run 并在本地假设备 final 屏障处 `SIGKILL` app；PostgreSQL 中 Run/target/Job 与设备租约保持持久。等待租约按 PG 时间自然过期（TTL 120s）后重启 app，Run 自动恢复并完成：Run/target completed、Job succeeded、Evaluation completed、1 条 CaseResult、0 个活动租约。验收使用 `asr_eval_test` 唯一 schema 与临时目录，结束后清理。
- [x] 2026-11-07：本机 `AUTH_MODE=local` 可登录；临时 PostgreSQL 上切换 `AUTH_MODE=ldap` 并使用指定 LDAP 测试账号完成 `/auth/login` 与 `/auth/me`
- [x] 2026-11-07：开发库迁移至 `0036_device_credentials`；`./asrctl doctor`、`/healthz` 与 `/readyz` 全绿，原有 2 台设备记录仍在

### 当前 P0 验收边界

`deploy/compose.yml` 按当前 P0 只启动 PostgreSQL；应用通过 `make dev` 在宿主机运行。M9 §0.0 明确暂不实现部署脚本，因此包含 app 容器的两容器 Compose 验收留到上线前。Run 恢复与租约到期已通过模块级测试和真实 app 进程 `SIGKILL` / 重启 E2E；30 分钟并发及资源占用读数仍待验收。备份恢复按阶段约定推迟，不列为当前 P0 缺口。

---

## 14. 风险与待确认

| # | 风险 | 缓解 |
|---|---|---|
| 1 | 单 PG 既做业务又做队列，压力集中 | 数据量小；慢查询日志 + 索引；必要时再加 Redis（SPI 不变） |
| 2 | 进程池评分与 API 同机抢 CPU | lite 限 2 并发；standard 拆 worker |
| 3 | 本地对象目录随容器丢失 | `data/` 必须是宿主机卷；备份含 objects |
| 4 | NOTIFY 8KB 限制 | 只发变更信号，不发全量 |
| 5 | HTTP 明文 | 办公网不可信时开 Caddy |

**已确认**

| 项 | 结论 |
|---|---|
| 本机验证 | **Docker Compose**（`db + app`） |
| 对象存储 | 默认本地目录；**保留 S3 接口与实现**，不默认部署 |
| 网络 | 默认 HTTP；域名由**外部 nginx** 反代，nginx 不在 Compose 内 |
| 镜像分发 | 无 registry，Jenkins 构建后 `docker save` / 部署机 `docker load` |
| Python | 3.13 |

**仍待确认**

1. 备份落点（NAS 路径）与保留周期——需要运维提供。
2. 外部 nginx 是否终止 TLS；若终止，`COOKIE_SECURE=true` 并改用 `__Host-` 前缀。
3. 设备网段与 Jenkins agent 的网络关系（真机 Job 放哪台 agent）。
