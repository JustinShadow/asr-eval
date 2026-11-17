# 本地开发入口；uv 默认装在用户目录，避免依赖 PATH
UV ?= $(HOME)/.local/bin/uv
NODE_BIN ?= $(firstword $(wildcard $(HOME)/.local/node/bin $(HOME)/.nvm/versions/node/*/bin))

.PHONY: setup dev test test-integration e2e e2e-recovery lint fmt migrate db-up db-down p0-check openapi openapi-sync openapi-check drill-backup golden secrets-check commitlint matrix matrix-check punct-205 gate

setup:
	cd backend && $(UV) sync

dev:
	cd backend && set -a && { [ ! -f .env ] || . ./.env; } && set +a && $(UV) run uvicorn app.main:app --reload --port 8080

test:
	cd backend && $(UV) run pytest

e2e:
	PATH="$(NODE_BIN):$$PATH" UV_BIN="$(UV)" bash tools/e2e.sh

e2e-recovery:
	cd backend && $(UV) run python ../tools/e2e_process_recovery.py

migrate:
	cd backend && { if [ -z "$${DATABASE_URL+x}" ] && [ -f .env ]; then set -a && . ./.env && set +a; fi; } && $(UV) run alembic upgrade head

db-up:
	docker compose -f deploy/compose.yml up -d db

db-down:
	docker compose -f deploy/compose.yml down

P0_PASSWORD ?= admin12345
p0-check:
	cd backend && $(UV) run python ../tools/p0_check.py --password '$(P0_PASSWORD)'

openapi:
	cd backend && $(UV) run python -m app.openapi export > ../docs/openapi.json
	cd web && PATH="$(HOME)/.local/node/bin:$$PATH" npm run api:types

openapi-sync: openapi
	cp docs/openapi.json docs/openapi.baseline.json

openapi-check:
	$(UV) run --project backend python tools/check_openapi.py docs/openapi.json docs/openapi.baseline.json

drill-backup:
	bash tools/backup_restore_drill.sh

golden:
	cd backend && $(UV) run pytest tests/golden -q

GITLEAKS_IMAGE ?= zricethezav/gitleaks:latest
secrets-check:
	docker run --rm -v "$(CURDIR):/repo" $(GITLEAKS_IMAGE) detect --source=/repo --no-git --config=/repo/.gitleaks.toml --redact

COMMIT_FROM ?= HEAD~1
COMMIT_TO ?= HEAD
commitlint:
	PATH="$(NODE_BIN):$$PATH" npx --yes @commitlint/cli@20.5.3 --from $(COMMIT_FROM) --to $(COMMIT_TO) --verbose

MATRIX_PLAN ?= tools/matrix_plan.json
matrix:
	cd backend && $(UV) run python ../tools/bench_matrix.py --plan ../$(MATRIX_PLAN)

matrix-check:
	cd backend && $(UV) run python ../tools/bench_matrix.py --plan ../tools/matrix_plan.example.json --validate-only

PUNCT_NEAR_ROOT ?= /mnt/f/测试数据/测试文件/AI测试素材/开源数据集/alimeeting/Test_Ali/Test_Ali_near
PUNCT_PORT ?= 18705
punct-205:
	cd backend && $(UV) run python ../tools/bench_punctuation.py --near-root '$(PUNCT_NEAR_ROOT)' --host 127.0.0.1 --port $(PUNCT_PORT) --model etsstt --limit 100

gate: test lint golden openapi-check secrets-check e2e commitlint

lint:
	cd backend && $(UV) run ruff check . && $(UV) run ruff format --check . && $(UV) run mypy app

fmt:
	cd backend && $(UV) run ruff format . && $(UV) run ruff check --fix .
