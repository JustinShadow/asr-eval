#!/usr/bin/env bash
# 本机 P0 备份/恢复演练：隔离数据库 + 临时数据目录，不触碰开发库与真实 data/。
# 用法：bash tools/backup_restore_drill.sh
# 依赖：Docker、docker compose、tar（与 M9 §9.4 的正式演练流程一致，只是库和目录是临时的）。
set -Eeuo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/deploy/compose.yml"
DB_NAME="asr_eval_restore_drill"
WORK_DIR="$(mktemp -d /tmp/asr-eval-backup-drill.XXXXXX)"
DATA_DIR="$WORK_DIR/data"
BACKUP_DIR="$WORK_DIR/backup"

cleanup() {
  local code=$?
  docker exec asr-eval-db psql -U asr -d postgres -c \
    "DROP DATABASE IF EXISTS $DB_NAME" >/dev/null 2>&1 || true
  rm -rf "$WORK_DIR"
  exit "$code"
}
trap cleanup EXIT INT TERM

for command in docker tar; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "缺少命令：$command" >&2
    exit 1
  }
done

echo "[1/7] 启动本机 PostgreSQL…"
docker compose -f "$COMPOSE_FILE" up -d db >/dev/null
for _ in {1..30}; do
  docker exec asr-eval-db pg_isready -q -U asr -d postgres >/dev/null 2>&1 && break
  sleep 1
done
docker exec asr-eval-db pg_isready -q -U asr -d postgres

echo "[2/7] 创建隔离演练库并写入样本…"
docker exec asr-eval-db psql -U asr -d postgres -c "DROP DATABASE IF EXISTS $DB_NAME" >/dev/null
docker exec asr-eval-db psql -U asr -d postgres -c "CREATE DATABASE $DB_NAME OWNER asr" >/dev/null
docker exec -i asr-eval-db psql -q -U asr -d "$DB_NAME" <<'SQL'
create table drill_sample (id integer primary key, note text not null);
insert into drill_sample values (1, 'backup-ok');
SQL

mkdir -p "$DATA_DIR/objects" "$BACKUP_DIR"
printf 'object-payload\n' >"$DATA_DIR/objects/demo.txt"
printf 'dummy-credentials-key\n' >"$DATA_DIR/credentials.key"
tar -C "$DATA_DIR" -czf "$BACKUP_DIR/data.tar.gz" .

echo "[3/7] 执行 pg_dump（custom 格式）…"
docker exec asr-eval-db pg_dump -U asr -d "$DB_NAME" -Fc >"$BACKUP_DIR/db.dump"

echo "[4/7] 生成 SHA256SUMS…"
(cd "$BACKUP_DIR" && sha256sum db.dump data.tar.gz >SHA256SUMS)

echo "[5/7] 模拟丢失：删表并清空数据目录…"
docker exec -i asr-eval-db psql -q -U asr -d "$DB_NAME" -c "DROP TABLE drill_sample"
rm -rf "$DATA_DIR"
mkdir -p "$DATA_DIR"

echo "[6/7] 恢复数据库与数据目录…"
docker exec -i asr-eval-db pg_restore -U asr -d "$DB_NAME" --no-owner <"$BACKUP_DIR/db.dump"
tar -C "$DATA_DIR" -xzf "$BACKUP_DIR/data.tar.gz"

echo "[7/7] 校验恢复结果…"
row="$(docker exec -i asr-eval-db psql -tA -U asr -d "$DB_NAME" -c "select note from drill_sample where id = 1")"
test "$row" = "backup-ok"
test -f "$DATA_DIR/objects/demo.txt"
test "$(cat "$DATA_DIR/objects/demo.txt")" = "object-payload"
test -f "$DATA_DIR/credentials.key"
(cd "$BACKUP_DIR" && sha256sum -c SHA256SUMS >/dev/null)

echo
echo "备份恢复演练通过：数据库行与 data/ 文件均已恢复，SHA256SUMS 校验通过。"
echo "备份目录样本：$BACKUP_DIR"