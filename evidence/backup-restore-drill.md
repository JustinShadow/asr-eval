# 本机备份/恢复演练记录（M9 §9.4）

> 日期：2026-11-10
> 环境：WSL2 / Linux，PostgreSQL 16（`deploy/compose.yml` 的 `asr-eval-db`），Docker Compose。

## 目的

在不触碰开发库 `asr_eval` 与真实 `backend/data/` 的前提下，验证 M9 §9.2/§9.3 的备份恢复顺序：
**数据库 pg_dump → data/ 打包 → 模拟丢失 → 恢复 → 校验**。

## 执行命令

```bash
make drill-backup
# 等价于
bash tools/backup_restore_drill.sh
```

脚本使用独立临时库 `asr_eval_restore_drill` 和临时数据目录，结束后删除临时库与目录。

## 演练步骤

1. 创建隔离库，写入 `drill_sample(id=1, note='backup-ok')`。
2. 在临时 `data/` 下写入 `objects/demo.txt` 与 `credentials.key`。
3. `pg_dump -Fc` 导出 `db.dump`，`tar -czf` 打包 `data.tar.gz`，生成 `SHA256SUMS`。
4. 删除表并清空数据目录，模拟丢失。
5. `pg_restore --no-owner` 恢复数据库，`tar -xzf` 恢复数据目录。
6. 校验行值、对象文件、凭据文件与 SHA256SUMS。

## 结果

```text
[7/7] 校验恢复结果…

备份恢复演练通过：数据库行与 data/ 文件均已恢复，SHA256SUMS 校验通过。
```

## 说明

- 这是本机 P0 的流程演练，不替代上线前的正式 `asrctl backup/restore` 与备份落点/保留周期配置。
- 演练脚本只依赖 `docker exec` 内的 `pg_dump/pg_restore`；正式部署应使用宿主机 PostgreSQL 客户端或备份机工具链。
- 恢复顺序按 M9 §9.3：secrets → db → objects → 启动 → doctor；本次用临时文件验证了 secrets/objects 与数据库同目录恢复。