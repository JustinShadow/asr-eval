# AISHELL-1 7,176 条导入性能复测（2026-11-10）

> 环境：WSL2 / Linux，Python 3.13，PostgreSQL 16（Docker，`asr-eval-db`）。
> 语料：AISHELL-1 `data_aishell/wav/test` 7,176 条，3,610,677ms 参考时长。
> 数据库：独立 `asr_eval_import_perf`，脚本建表，不写开发库。

## 1. 存储条件

| 条件 | 路径 | 说明 |
|---|---|---|
| **本地 ext4** | `/var/tmp/asr-aishell1-local` | 从 `/mnt/f` 9p 复制后的本地副本，1.1GB |
| 9p（历史） | `/mnt/f/.../data_aishell` | 之前隔离导入耗时 593.49s，缓存状态未控 |
| NFS | **未挂载** | 当前 `/etc/fstab` 无 NFS，`/mnt/asr-data` 不存在，无法执行正式 NFS 验收 |

## 2. 命令

```bash
# 独立基准库
docker exec asr-eval-db psql -U asr -d postgres \
  -c "CREATE DATABASE asr_eval_import_perf OWNER asr"

# 本地 ext4 导入
cd backend
uv run python ../tools/bench_import.py \
  --root /var/tmp/asr-aishell1-local \
  --source-type aishell1 --limit 7176 \
  --database-url 'postgresql+psycopg://asr:asr@127.0.0.1:5432/asr_eval_import_perf' \
  --data-dir /tmp/asr-import-perf-data \
  --save /tmp/asr-import-perf-ext4.json
```

## 3. 结果

```json
{
  "database": "asr_eval_import_perf",
  "source_type": "aishell1",
  "root": "/var/tmp/asr-aishell1-local",
  "limit": 7176,
  "elapsed_s": 11.603153538000697,
  "item_count": 7176,
  "total_duration_ms": 36106777,
  "status": "ready",
  "items_per_s": 618.452559168365
}
```

- **11.60s / 7,176 条 / 618 items/s**，低于设计目标 `<25s`；
- 说明导入代码路径、分批提交、指纹线程池与 manifest 生成本身不是瓶颈；
- 与历史 9p 593.49s、KeSpeech 2,230s 的差异来自存储小文件读取带宽。

## 4. 结论

**代码路径在本地 ext4 上达标（7,176 条 11.60s），作为当前导入性能的初步结论。** 按项目决定，暂不基于该结果继续优化导入代码；真实部署到只读 NFS 后，再以现场耗时判断是否需要优化。正式 NFS 复测建议在固定缓存与数据库状态下复跑同一命令；预计 25s 目标要求有效读取吞吐约 ≥50MB/s 且小文件元数据延迟可控。

## 5. 清理

验收后删除 `asr_eval_import_perf` 库、`/var/tmp/asr-aishell1-local` 与临时 manifest 目录；
开发库 `asr_eval` 未写入。