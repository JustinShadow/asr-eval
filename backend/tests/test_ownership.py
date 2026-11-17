"""M7 对象级授权单元测试。"""

import uuid

import pytest

from app.core.errors import ApiError
from app.domain.models import Report, Run
from app.domain.runs import ensure_run_owner
from app.reports.service import ensure_report_owner


def test_run_owner_can_cancel_and_resume() -> None:
    run = Run(benchmark_id=uuid.uuid4(), name="自建 Run", created_by="tester")

    ensure_run_owner(run, actor="tester", role="tester")


@pytest.mark.parametrize("role", ["engineer", "admin"])
def test_engineer_and_admin_can_operate_other_users_run(role: str) -> None:
    run = Run(benchmark_id=uuid.uuid4(), name="他人 Run", created_by="other")

    ensure_run_owner(run, actor="operator", role=role)


def test_tester_cannot_operate_other_users_run() -> None:
    run = Run(benchmark_id=uuid.uuid4(), name="他人 Run", created_by="other")

    with pytest.raises(ApiError) as error:
        ensure_run_owner(run, actor="tester", role="tester")
    assert error.value.code == "E_NOT_OWNER"


def test_report_owner_can_delete_own_snapshot() -> None:
    report = Report(
        kind="single",
        evaluation_ids=[],
        query={},
        input_hash="hash",
        created_by="tester",
    )

    ensure_report_owner(report, actor="tester", role="tester")


def test_admin_can_delete_other_users_snapshot() -> None:
    report = Report(
        kind="single",
        evaluation_ids=[],
        query={},
        input_hash="hash",
        created_by="other",
    )

    ensure_report_owner(report, actor="admin", role="admin")


def test_non_admin_cannot_delete_other_users_snapshot() -> None:
    report = Report(
        kind="single",
        evaluation_ids=[],
        query={},
        input_hash="hash",
        created_by="other",
    )

    with pytest.raises(ApiError) as error:
        ensure_report_owner(report, actor="tester", role="tester")
    assert error.value.code == "E_NOT_OWNER"
