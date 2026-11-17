"""本机凭据密钥的权限、创建与加解密验证。"""

import stat
from pathlib import Path

import pytest

from app.core.credentials import (
    CredentialKeyError,
    credentials_fernet,
    decrypt_credential,
    encrypt_credential,
)


def test_credentials_key_is_created_with_restricted_permissions(tmp_path: Path) -> None:
    key_path = tmp_path / "secrets" / "credentials_key"

    fernet = credentials_fernet(key_path)
    ciphertext = fernet.encrypt(b"test-secret")

    assert fernet.decrypt(ciphertext) == b"test-secret"
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(key_path.parent.stat().st_mode) == 0o700


def test_credential_encryption_rejects_wrong_key(tmp_path: Path) -> None:
    original = tmp_path / "one" / "credentials_key"
    other = tmp_path / "two" / "credentials_key"
    ciphertext = encrypt_credential(b"private", original)

    with pytest.raises(CredentialKeyError, match="凭据不可用"):
        decrypt_credential(ciphertext, other)


def test_invalid_key_file_fails_closed(tmp_path: Path) -> None:
    key_path = tmp_path / "secrets" / "credentials_key"
    key_path.parent.mkdir(mode=0o700)
    key_path.write_text("not-a-fernet-key", encoding="ascii")

    with pytest.raises(CredentialKeyError, match="格式无效"):
        credentials_fernet(key_path)
