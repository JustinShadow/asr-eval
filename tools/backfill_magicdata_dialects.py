"""回填 MagicData 数据集的统一方言标签。

历史导入把 `buckets.dialect` 写成完整目录名（如 `MagicData-沪语（吴语）`）。
本脚本统一为 `沪语/湘语/赣语`，语言代码保持 `wuu/xiang/gan`。

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
from app.importers.magicdata import infer_dialect


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
            language, dialect = infer_dialect(dataset.name)
            if language == "zh":
                # 数据集名没有方言关键字时，用音频路径兜底；仍未知则跳过。
                asset = db.get(AudioAsset, items[0].audio_asset_id)
                if asset is not None:
                    hinted_language, hinted_dialect = infer_dialect(asset.uri)
                    if hinted_language != "zh":
                        language, dialect = hinted_language, hinted_dialect
            if language == "zh":
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
