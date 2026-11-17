#!/usr/bin/env python3
"""隔离导入性能基准（M1 §9.3/§10）。

只对专用 perf/test 数据库建表并导入指定语料；源音频只读，manifest 写到临时 data_dir。
用于比较 9p / NFS / 本地 ext4 的导入耗时，不写开发库。

用法：
    cd backend && uv run python ../tools/bench_import.py \
      --root /mnt/f/.../data_aishell --source-type aishell1 --limit 7176 \
      --database-url postgresql+psycopg://asr:asr@127.0.0.1:5432/asr_eval_import_perf \
      --data-dir /tmp/asr-import-perf-data --save /tmp/import-perf.json
"""

import argparse
import json
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "backend"))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.domain import datasets as datasets_domain  # noqa: E402
from app.domain.models import Base, Dataset  # noqa: E402
from app.importers.aishell1 import Aishell1Importer  # noqa: E402
from app.importers.aishell3 import Aishell3Importer  # noqa: E402
from app.importers.kespeech import KespeechImporter  # noqa: E402

IMPORTERS = {
    "aishell1": Aishell1Importer,
    "aishell3": Aishell3Importer,
    "kespeech": KespeechImporter,
}


def _database_name(url: str) -> str:
    """从 SQLAlchemy URL 取数据库名，用于拒绝开发库误写。"""
    return url.rsplit("/", 1)[-1].split("?", 1)[0]


def _run(args: argparse.Namespace) -> dict[str, Any]:
    """建表、导入并返回耗时与条目统计。"""
    database_name = _database_name(args.database_url)
    if not any(marker in database_name for marker in ("perf", "test", "e2e")):
        raise SystemExit(f"拒绝写入非 perf/test/e2e 数据库：{database_name}；请使用独立基准库")
    root = args.root.expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"语料根目录不存在：{root}")
    data_dir = args.data_dir.expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    # 让 _run_import 的 manifest 与白名单检查使用本次临时目录/根目录。
    datasets_domain.get_settings = lambda: SimpleNamespace(  # type: ignore[assignment]
        data_dir=data_dir,
        import_allowed_roots=[str(root)],
    )

    engine = create_engine(args.database_url, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    dataset = Dataset(name=f"import-perf-{uuid.uuid4().hex[:10]}", source_type=args.source_type)
    session.add(dataset)
    session.commit()
    started = time.perf_counter()
    try:
        version = datasets_domain._run_import(
            session,
            dataset,
            root=root,
            limit=args.limit,
            version="perf-v1",
            created_by="perf",
            importer=IMPORTERS[args.source_type](),
        )
        elapsed_s = time.perf_counter() - started
    finally:
        session.close()
        engine.dispose()

    return {
        "database": database_name,
        "source_type": args.source_type,
        "root": str(root),
        "limit": args.limit,
        "elapsed_s": elapsed_s,
        "item_count": version.item_count,
        "total_duration_ms": version.total_duration_ms,
        "status": version.status,
        "items_per_s": version.item_count / elapsed_s if elapsed_s > 0 else None,
    }


def main() -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="隔离导入性能基准")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--source-type", choices=sorted(IMPORTERS), required=True)
    parser.add_argument("--limit", type=int, default=7176)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--save", type=Path)
    parser.add_argument("--keep-data", action="store_true", help="保留 manifest 临时目录")
    args = parser.parse_args()
    if args.limit < 1:
        raise SystemExit("--limit 必须大于 0")

    temp_dir: str | None = None
    if args.data_dir is None:
        temp_dir = tempfile.mkdtemp(prefix="asr-import-perf-")
        args.data_dir = Path(temp_dir)
    try:
        result = _run(args)
    finally:
        if temp_dir is not None and not args.keep_data:
            shutil.rmtree(temp_dir, ignore_errors=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.save is not None:
        args.save.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
