"""数据集样本详情与游标分页接口测试（M1 §3.3、M7 §4.4）。"""

import os
import time
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, insert
from sqlalchemy.orm import Session, sessionmaker

from app.domain.models import (
    AudioAsset,
    Base,
    Dataset,
    DatasetSubset,
    DatasetVersion,
    Item,
    RefSegment,
    RefText,
    SpeakerMeta,
)

TEST_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://asr:asr@127.0.0.1:5432/asr_eval_test",
)
pytestmark = pytest.mark.integration


@pytest.fixture()
def db() -> Iterator[Session]:
    """使用集成测试 PostgreSQL 准备样本详情数据。"""
    engine = create_engine(TEST_URL, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _sample_rows(db: Session) -> tuple[DatasetVersion, list[Item]]:
    """创建三条可筛选、可分页的样本及完整参考标注。"""
    suffix = uuid.uuid4().hex[:10]
    dataset = Dataset(name=f"样本接口-{suffix}", source_type="aishell3")
    db.add(dataset)
    db.flush()
    version = DatasetVersion(dataset_id=dataset.id, version="v1", status="ready")
    db.add(version)
    db.flush()
    subset = DatasetSubset(dataset_version_id=version.id, name="all", is_primary=True)
    asset = AudioAsset(sha256=uuid.uuid4().hex.ljust(64, "0"), uri="/missing/sample.wav")
    db.add_all([subset, asset])
    db.flush()
    items: list[Item] = []
    for index, (split, gender, text_value) in enumerate(
        [("test", "F", "你好世界"), ("test", "M", "你好北京"), ("train", "F", "再见北京")]
    ):
        item = Item(
            dataset_version_id=version.id,
            subset_id=subset.id,
            item_key=f"sample-{index}",
            content_uid=uuid.uuid4().hex[:24],
            audio_asset_id=asset.id,
            split=split,
            duration_ms=3000,
            lang="zh",
            speakers=[f"S{index}"],
            buckets={"gender": gender},
        )
        db.add(item)
        db.flush()
        db.add_all(
            [
                RefText(
                    item_id=item.id,
                    kind="verbatim",
                    lang="zh",
                    text=text_value,
                    sha256=uuid.uuid4().hex[:16],
                ),
                RefText(
                    item_id=item.id,
                    kind="itn",
                    lang="zh",
                    text=f"ITN-{text_value}",
                    sha256=uuid.uuid4().hex[:16],
                ),
                RefSegment(
                    item_id=item.id,
                    seq=0,
                    speaker=f"S{index}",
                    start_ms=100,
                    end_ms=900,
                    text=text_value,
                    skip=index == 0,
                    overlap=index == 1,
                ),
                SpeakerMeta(
                    dataset_version_id=version.id,
                    speaker_id=f"S{index}",
                    gender=gender,
                    age="B",
                    accent="north",
                    meta={"source": "fixture"},
                ),
            ]
        )
        items.append(item)
    db.commit()
    return version, items


def _token(client: TestClient) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin12345"},
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def test_item_detail_returns_references_segments_and_speaker_meta(
    client: TestClient, db: Session
) -> None:
    _version, items = _sample_rows(db)
    response = client.get(
        f"/api/v1/items/{items[0].id}",
        headers={"Authorization": f"Bearer {_token(client)}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ref_text"] == "你好世界"
    assert payload["itn_text"] == "ITN-你好世界"
    assert payload["segments"] == [
        {
            "seq": 0,
            "speaker": "S0",
            "start_ms": 100,
            "end_ms": 900,
            "text": "你好世界",
            "skip": True,
            "overlap": False,
        }
    ]
    assert payload["speaker_meta"]["S0"] == {
        "gender": "F",
        "age": "B",
        "accent": "north",
        "meta": {"source": "fixture"},
    }


def test_item_list_uses_opaque_cursor_and_filters(client: TestClient, db: Session) -> None:
    version, items = _sample_rows(db)
    headers = {"Authorization": f"Bearer {_token(client)}"}
    first = client.get(f"/api/v1/dataset-versions/{version.id}/items?limit=2", headers=headers)

    assert first.status_code == 200
    payload = first.json()
    assert [item["id"] for item in payload["items"]] == [item.id for item in items[:2]]
    assert payload["has_more"] is True
    assert payload["next_cursor"]
    second = client.get(
        f"/api/v1/dataset-versions/{version.id}/items?limit=2&cursor={payload['next_cursor']}",
        headers=headers,
    )
    assert [item["id"] for item in second.json()["items"]] == [items[2].id]
    assert second.json()["has_more"] is False

    filtered = client.get(
        f"/api/v1/dataset-versions/{version.id}/items?split=test&bucket=gender:F&q=你好",
        headers=headers,
    )
    assert [item["id"] for item in filtered.json()["items"]] == [items[0].id]


def test_item_list_rejects_invalid_cursor_and_unknown_version(
    client: TestClient, db: Session
) -> None:
    version, _items = _sample_rows(db)
    headers = {"Authorization": f"Bearer {_token(client)}"}
    invalid = client.get(
        f"/api/v1/dataset-versions/{version.id}/items?cursor=invalid", headers=headers
    )
    assert invalid.status_code == 400, invalid.text
    missing = client.get(f"/api/v1/dataset-versions/{uuid.uuid4()}/items", headers=headers)
    assert missing.status_code == 404


def test_ten_thousand_item_first_page_under_300ms(client: TestClient, db: Session) -> None:
    """M1 §9.3：一万条版本首屏游标查询耗时低于 300ms。"""
    suffix = uuid.uuid4().hex[:10]
    dataset = Dataset(name=f"列表性能-{suffix}", source_type="aishell1")
    db.add(dataset)
    db.flush()
    version = DatasetVersion(dataset_id=dataset.id, version="v1", status="ready")
    db.add(version)
    db.flush()
    subset = DatasetSubset(dataset_version_id=version.id, name="all", is_primary=True)
    asset = AudioAsset(sha256=uuid.uuid4().hex.ljust(64, "0"), uri="/missing/perf.wav")
    db.add_all([subset, asset])
    db.flush()
    item_rows = [
        {
            "dataset_version_id": version.id,
            "subset_id": subset.id,
            "item_key": f"perf-{index:05d}",
            "content_uid": uuid.uuid4().hex[:24],
            "audio_asset_id": asset.id,
            "split": "test",
            "duration_ms": 1000,
            "lang": "zh",
            "speakers": [],
            "buckets": {},
            "ref_sha": uuid.uuid4().hex[:16],
        }
        for index in range(10_000)
    ]
    inserted = db.execute(insert(Item).returning(Item.id, Item.item_key), item_rows).all()
    db.execute(
        insert(RefText),
        [
            {
                "item_id": item_id,
                "kind": "verbatim",
                "lang": "zh",
                "text": f"样本{item_key}",
                "sha256": uuid.uuid4().hex[:16],
            }
            for item_id, item_key in inserted
        ],
    )
    db.commit()

    headers = {"Authorization": f"Bearer {_token(client)}"}
    started = time.perf_counter()
    response = client.get(f"/api/v1/dataset-versions/{version.id}/items?limit=50", headers=headers)
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert response.status_code == 200
    assert len(response.json()["items"]) == 50
    assert response.json()["has_more"] is True
    assert elapsed_ms < 300, f"一万条样本首屏耗时 {elapsed_ms:.1f}ms，超过 300ms"
