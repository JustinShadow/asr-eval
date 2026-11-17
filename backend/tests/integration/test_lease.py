"""设备租约的并发语义。"""

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import ApiError
from app.domain.models import (
    Base,
    Dataset,
    DatasetVersion,
    Device,
    Run,
    RunTarget,
)
from app.domain.runs import _acquire_lease, _release_lease, ensure_benchmark, ensure_default_profile

TEST_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://asr:asr@127.0.0.1:5432/asr_eval_test",
)

pytestmark = pytest.mark.integration


@pytest.fixture()
def db() -> Session:
    engine = create_engine(TEST_URL, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_lease_blocks_second_holder_and_expires(db: Session) -> None:
    suffix = uuid.uuid4().hex[:8]
    dataset = Dataset(name=f"lease-ds-{suffix}", source_type="aishell1")
    db.add(dataset)
    db.flush()
    version = DatasetVersion(dataset_id=dataset.id, version="v1", status="ready")
    db.add(version)
    db.flush()
    profile = ensure_default_profile(db)
    benchmark = ensure_benchmark(db, version, profile)

    device = Device(
        name=f"lease-dev-{suffix}",
        platform="205",
        kind="etsstt",
        host="127.0.0.1",
        port=9,
    )
    db.add(device)
    db.flush()

    run = Run(benchmark_id=benchmark.id, name=f"lease-run-{suffix}")
    db.add(run)
    db.flush()
    first = RunTarget(run_id=run.id, device_id=device.id)
    second = RunTarget(run_id=run.id, device_id=device.id, subset="second")
    db.add_all([first, second])
    db.commit()

    _acquire_lease(db, device.id, first.id, "holder-1")
    with pytest.raises(ApiError) as exc:
        _acquire_lease(db, device.id, second.id, "holder-2")
    assert exc.value.code == "E_DEVICE_BUSY"

    # 模拟租约过期后，第二个持有者可以抢占
    db.execute(text("update device_lease set expires_at = now() - interval '1 second'"))
    db.commit()
    _acquire_lease(db, device.id, second.id, "holder-2")

    _release_lease(db, device.id, "holder-2")
    # 测试库由多个用例共享，只检查本用例设备上的租约。
    remaining = db.execute(
        text("select count(*) from device_lease where device_id = :device_id"),
        {"device_id": device.id},
    ).scalar()
    assert remaining == 0
