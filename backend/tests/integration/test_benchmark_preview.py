"""M3 Benchmark 预览、版本冻结和归档 API 验收。"""

import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient

from app.domain.models import (
    AudioAsset,
    Dataset,
    DatasetSubset,
    DatasetVersion,
    Item,
    RefText,
    Run,
)

pytestmark = pytest.mark.integration


def _admin_headers(client: TestClient) -> dict[str, str]:
    client.cookies.clear()
    login = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin12345"},
    )
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def test_preview_is_deterministic_and_archive_preserves_run_history(client: TestClient) -> None:
    headers = _admin_headers(client)
    factory = client.app.state.evaluation_session_factory
    suffix = uuid.uuid4().hex[:8]
    with factory() as db:
        dataset = Dataset(name=f"预览数据集-{suffix}", source_type="alimeeting")
        db.add(dataset)
        db.flush()
        version = DatasetVersion(
            dataset_id=dataset.id,
            version="v1",
            status="ready",
            capabilities={"text_ratio": 1.0},
        )
        db.add(version)
        db.flush()
        subsets = {
            name: DatasetSubset(
                dataset_version_id=version.id,
                name=name,
                is_primary=name == "near",
                capabilities={"text_ratio": 1.0},
            )
            for name in ("near", "far")
        }
        db.add_all(list(subsets.values()))
        db.flush()
        for subset_name, durations in {
            "near": [4_000, 6_000, 20_000],
            "far": [8_000, 70_000],
        }.items():
            for index, duration in enumerate(durations):
                asset = AudioAsset(
                    sha256=hashlib.sha256(f"{suffix}-{subset_name}-{index}".encode()).hexdigest(),
                    uri=f"/preview/{suffix}/{subset_name}/{index}.wav",
                )
                db.add(asset)
                db.flush()
                item = Item(
                    dataset_version_id=version.id,
                    subset_id=subsets[subset_name].id,
                    item_key=f"{subset_name}-{index}",
                    content_uid=hashlib.sha256(
                        f"uid-{suffix}-{subset_name}-{index}".encode()
                    ).hexdigest()[:24],
                    audio_asset_id=asset.id,
                    duration_ms=duration,
                    buckets={"dialect": "mandarin" if index % 2 == 0 else "cantonese"},
                )
                db.add(item)
                db.flush()
                db.add(
                    RefText(
                        item_id=item.id,
                        kind="verbatim",
                        text=f"参考文本{subset_name}{index}",
                        sha256=hashlib.sha256(
                            f"ref-{suffix}-{subset_name}-{index}".encode()
                        ).hexdigest()[:16],
                    )
                )
                subsets[subset_name].item_count += 1
                subsets[subset_name].total_duration_ms += duration
                version.item_count += 1
                version.total_duration_ms += duration
        db.commit()
        version_id = str(version.id)

    profile = client.post(
        "/api/v1/metric-profiles",
        headers=headers,
        json={"name": f"预览口径-{suffix}", "params": {}},
    )
    assert profile.status_code == 201
    profile_id = profile.json()["id"]
    request_body = {
        "dataset_version_id": version_id,
        "metric_profile_id": profile_id,
        "subsets": ["near", "far"],
        "sampling": {"mode": "random", "n": 2, "seed": 42},
        "bucket_rules": {"dims": ["duration_bin", "dialect"]},
        "params": {},
    }
    before_count = client.get("/api/v1/benchmarks", headers=headers).json()["items"]
    first = client.post("/api/v1/benchmarks/preview", headers=headers, json=request_body)
    second = client.post("/api/v1/benchmarks/preview", headers=headers, json=request_body)
    assert first.status_code == second.status_code == 200
    assert first.json()["subsets"] == second.json()["subsets"]
    assert first.json()["selected_count"] == 4
    selected_duration_ms = first.json()["selected_duration_ms"]
    assert [
        (item["name"], item["source_count"], item["selected_count"])
        for item in first.json()["subsets"]
    ] == [
        ("near", 3, 2),
        ("far", 2, 2),
    ]
    for subset in first.json()["subsets"]:
        assert sum(subset["buckets"]["duration_bin"].values()) == subset["selected_count"]
    after_preview = client.get("/api/v1/benchmarks", headers=headers).json()["items"]
    assert len(after_preview) == len(before_count)

    created = client.post(
        "/api/v1/benchmarks",
        headers=headers,
        json={"name": f"预览验收-{suffix}", **request_body},
    )
    assert created.status_code == 201
    benchmark_id = created.json()["id"]
    listed = client.get("/api/v1/benchmarks", headers=headers).json()["items"]
    listed_benchmark = next(item for item in listed if item["id"] == benchmark_id)
    assert listed_benchmark["selected_count"] == 4
    assert listed_benchmark["selected_duration_ms"] == selected_duration_ms
    assert listed_benchmark["metric_profile_version"] == 1
    with factory() as db:
        db.add(Run(benchmark_id=uuid.UUID(benchmark_id), name=f"历史Run-{suffix}"))
        db.commit()

    archived = client.post(f"/api/v1/benchmarks/{benchmark_id}/archive", headers=headers)
    assert archived.status_code == 200
    assert archived.json()["archived"] is True
    historical = client.get(f"/api/v1/benchmarks/{benchmark_id}", headers=headers)
    assert historical.status_code == 200
    assert historical.json()["id"] == benchmark_id
    frozen = client.patch(
        f"/api/v1/benchmarks/{benchmark_id}",
        headers={**headers, "If-Match": historical.headers["ETag"]},
        json={"note": "不应改写已归档历史"},
    )
    assert frozen.status_code == 409
    assert frozen.json()["code"] == "E_CONFLICT"
