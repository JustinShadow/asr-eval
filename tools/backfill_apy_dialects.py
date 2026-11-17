"""回填 APY/DataTang 数据集的方言标签。

历史导入时 `_language()` 只看 ACT，闽语包的 ACT 是“漳州/泉州”地点，
导致 language 落回 zh、且没有 dialect 分桶。本脚本按数据集名/包路径
重新推断语言与方言，并同步 item.lang、item.buckets、subset/version 能力。

默认 dry-run；加 --apply 才写库。
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.db import get_session_factory
from app.domain.models import AudioAsset, Dataset, DatasetSubset, DatasetVersion, Item
from app.importers.apy import infer_dialect


def _update_capabilities(
    capabilities: dict[str, object],
    language_counts: Counter[str],
    dialect_counts: Counter[str],
) -> dict[str, object]:
    """把 language/dialect 计数写回能力快照，保留其它字段。"""
    updated = dict(capabilities)
    updated["langs"] = sorted(language_counts)
    raw_counts = updated.get("bucket_counts")
    bucket_counts: dict[str, object] = dict(raw_counts) if isinstance(raw_counts, dict) else {}
    bucket_counts["language"] = dict(language_counts)
    if dialect_counts:
        bucket_counts["dialect"] = dict(dialect_counts)
    else:
        bucket_counts.pop("dialect", None)
    updated["bucket_counts"] = bucket_counts
    return updated


def backfill(db: Session, *, apply: bool) -> int:
    datasets = db.scalars(select(Dataset).order_by(Dataset.name)).all()
    changed = 0
    for dataset in datasets:
        versions = db.scalars(
            select(DatasetVersion).where(DatasetVersion.dataset_id == dataset.id)
        ).all()
        for version in versions:
            items = db.scalars(select(Item).where(Item.dataset_version_id == version.id)).all()
            if not items:
                continue
            language, dialect = infer_dialect(Path(dataset.name))
            if language is None and items:
                asset = db.get(AudioAsset, items[0].audio_asset_id)
                if asset is not None:
                    language, dialect = infer_dialect(Path(asset.uri))
            if language is None or dialect is None:
                continue
            language_counts: Counter[str] = Counter()
            dialect_counts: Counter[str] = Counter()
            subset_items: dict[object, list[Item]] = {}
            for item in items:
                buckets = dict(item.buckets or {})
                buckets["language"] = language
                buckets["dialect"] = dialect
                item.lang = language
                item.buckets = buckets
                language_counts[language] += 1
                dialect_counts[dialect] += 1
                subset_items.setdefault(item.subset_id, []).append(item)

            version.capabilities = _update_capabilities(
                version.capabilities or {}, language_counts, dialect_counts
            )
            for subset_id, members in subset_items.items():
                subset = db.get(DatasetSubset, subset_id)
                if subset is None:
                    continue
                subset_languages = Counter(
                    str((member.buckets or {}).get("language") or language) for member in members
                )
                subset_dialects = Counter(
                    str((member.buckets or {}).get("dialect") or dialect) for member in members
                )
                subset.capabilities = _update_capabilities(
                    subset.capabilities or {}, subset_languages, subset_dialects
                )
            changed += len(items)
            print(
                f"{dataset.name} {version.version}: {len(items)} 条 → "
                f"language={language}, dialect={dialect}"
            )
    if apply:
        db.commit()
        print(f"已提交，共更新 {changed} 条 item")
    else:
        print(f"dry-run，共计划更新 {changed} 条 item；加 --apply 写库")
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="写入数据库")
    args = parser.parse_args()
    with get_session_factory()() as db:
        backfill(db, apply=args.apply)


if __name__ == "__main__":
    main()
