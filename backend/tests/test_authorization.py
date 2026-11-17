"""grant 权限并集、数据集范围与过期行为。"""

from datetime import UTC, datetime, timedelta

from app.domain.authorization import claim_allows_permission


def test_scoped_dataset_grant_only_matches_its_resource_path() -> None:
    claims = {
        "role_perms": ["dataset:read"],
        "grants": [
            {
                "permission": "dataset:write",
                "scope": {"dataset_id": "dataset-a"},
                "expires_at": None,
            }
        ],
    }

    assert claim_allows_permission(claims, "dataset:write", path_params={"dataset_id": "dataset-a"})
    assert not claim_allows_permission(
        claims, "dataset:write", path_params={"dataset_id": "dataset-b"}
    )
    assert not claim_allows_permission(claims, "dataset:write", path_params={})


def test_scoped_grant_cannot_be_selected_by_query_parameter() -> None:
    claims = {
        "role_perms": ["dataset:read"],
        "grants": [
            {
                "permission": "dataset:write",
                "scope": {"dataset_id": "dataset-a"},
                "expires_at": None,
            }
        ],
    }

    assert not claim_allows_permission(claims, "dataset:write", path_params={"dataset_id": None})


def test_expired_grant_is_rejected_even_if_access_token_is_still_valid() -> None:
    claims = {
        "role_perms": ["dataset:read"],
        "grants": [
            {
                "permission": "dataset:write",
                "scope": {},
                "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            }
        ],
    }

    assert not claim_allows_permission(claims, "dataset:write", path_params={})


def test_role_permission_does_not_depend_on_grants() -> None:
    claims = {"role_perms": ["dataset:write"], "grants": []}

    assert claim_allows_permission(claims, "dataset:write", path_params={})
