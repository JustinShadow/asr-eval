"""本机 Fernet 密钥管理（Q7、M9 §3.2）。"""

import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings


class CredentialKeyError(RuntimeError):
    """凭据密钥缺失、权限不安全或格式无效。"""


def credential_key_path() -> Path:
    """返回 M9 §3.2 约定的本机凭据密钥位置。"""
    settings = get_settings()
    return settings.credentials_key_file or settings.data_dir / "secrets" / "credentials_key"


def credentials_fernet(key_path: Path | None = None) -> Fernet:
    """读取或原子创建 Fernet 密钥，并确保目录 0700、文件 0600。"""
    path = key_path or credential_key_path()
    directory = path.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink():
        raise CredentialKeyError("凭据密钥目录不能是符号链接")
    if os.name == "posix":
        os.chmod(directory, 0o700)

    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        descriptor = -1
    if descriptor >= 0:
        try:
            with os.fdopen(descriptor, "wb") as key_file:
                key_file.write(Fernet.generate_key())
                key_file.flush()
                os.fsync(key_file.fileno())
        except Exception:
            path.unlink(missing_ok=True)
            raise

    if path.is_symlink():
        raise CredentialKeyError("凭据密钥文件不能是符号链接")
    try:
        key = path.read_bytes().strip()
    except OSError as exc:
        raise CredentialKeyError("无法读取凭据密钥文件") from exc
    if os.name == "posix":
        os.chmod(path, 0o600)
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise CredentialKeyError("凭据密钥文件权限必须为 0600")
    try:
        return Fernet(key)
    except (TypeError, ValueError) as exc:
        raise CredentialKeyError("凭据密钥格式无效") from exc


def encrypt_credential(plaintext: bytes, key_path: Path | None = None) -> bytes:
    """使用本机密钥加密凭据明文。"""
    return credentials_fernet(key_path).encrypt(plaintext)


def decrypt_credential(ciphertext: bytes, key_path: Path | None = None) -> bytes:
    """解密凭据；密钥错误不向上层暴露密文或密钥内容。"""
    try:
        return credentials_fernet(key_path).decrypt(ciphertext)
    except InvalidToken as exc:
        raise CredentialKeyError("凭据不可用，请重新保存") from exc
