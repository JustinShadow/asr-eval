"""认证基础设施的单元测试。"""

from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    verify_password,
)
from app.domain.permissions import permissions_for


def test_password_roundtrip() -> None:
    digest = hash_password("correct-horse-battery")
    assert digest != "correct-horse-battery"
    assert verify_password("correct-horse-battery", digest)
    assert not verify_password("wrong", digest)


def test_refresh_token_is_opaque_and_hashed() -> None:
    raw = new_refresh_token()
    assert len(raw) >= 40
    digest = hash_refresh_token(raw)
    assert len(digest) == 64
    assert raw not in digest


def test_access_token_roundtrip() -> None:
    token, expires_in = create_access_token(
        username="zhangsan",
        role="engineer",
        permissions=permissions_for("engineer"),
        session_id="11111111-1111-1111-1111-111111111111",
    )
    claims = decode_access_token(token)
    assert expires_in == 1800
    assert claims["sub"] == "zhangsan"
    assert claims["role"] == "engineer"
    assert "run:create" in claims["perms"]


def test_role_permissions() -> None:
    assert "device:ops" in permissions_for("admin")
    assert "run:create" in permissions_for("tester")
    assert "dataset:write" not in permissions_for("tester")
    assert "run:create" not in permissions_for("viewer")
    # 未知角色按只读处理
    assert permissions_for("unknown") == permissions_for("viewer")
