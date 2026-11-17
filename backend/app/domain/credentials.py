"""设备凭据保存与读取；密文使用 Q7 本机 Fernet 密钥。"""

import json
import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.credentials import CredentialKeyError, decrypt_credential, encrypt_credential
from app.domain.models import Device, DeviceCredential

CredentialKind = Literal["ssh_password", "ssh_private_key", "cloud_api_key"]


class CredentialUnavailable(RuntimeError):
    """设备凭据引用失效或密文不可解。"""


@dataclass(frozen=True)
class DeviceCredentialValue:
    """解密后仅供连接调用使用的设备凭据。"""

    kind: CredentialKind
    secret: str
    passphrase: str | None = None


def credential_for_device(db: Session, device: Device) -> DeviceCredentialValue | None:
    """按设备引用解密凭据；API 序列化层不得返回该对象。"""
    if not device.secret_ref:
        return None
    row = db.scalar(
        select(DeviceCredential).where(
            DeviceCredential.id == device.secret_ref,
            DeviceCredential.device_id == device.id,
        )
    )
    if row is None:
        raise CredentialUnavailable("凭据不可用，请重新保存")
    try:
        payload = json.loads(decrypt_credential(row.ciphertext))
        kind = payload["kind"]
        secret = payload["secret"]
        passphrase = payload.get("passphrase")
        if kind not in {"ssh_password", "ssh_private_key", "cloud_api_key"}:
            raise ValueError("unknown credential kind")
        if kind != row.kind:
            raise ValueError("credential kind does not match metadata")
        if not isinstance(secret, str) or (
            passphrase is not None and not isinstance(passphrase, str)
        ):
            raise ValueError("invalid credential payload")
        return DeviceCredentialValue(kind, secret, passphrase)
    except (CredentialKeyError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CredentialUnavailable("凭据不可用，请重新保存") from exc


def save_device_credential(
    db: Session,
    device: Device,
    *,
    kind: CredentialKind,
    secret: str,
    passphrase: str | None = None,
) -> None:
    """加密并保存 SSH 密码、私钥或云 API Key。"""
    payload = json.dumps(
        {"kind": kind, "secret": secret, "passphrase": passphrase},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    ciphertext = encrypt_credential(payload)
    row = db.scalar(select(DeviceCredential).where(DeviceCredential.device_id == device.id))
    if row is not None and row.device_id != device.id:
        raise CredentialUnavailable("凭据引用无效，请重新保存")
    if row is None:
        row = DeviceCredential(
            id=str(uuid.uuid4()), device_id=device.id, kind=kind, ciphertext=ciphertext
        )
        db.add(row)
        device.secret_ref = row.id
    else:
        row.kind = kind
        row.ciphertext = ciphertext
    device.secret_ref = row.id
    device.version += 1


def remove_device_credential(db: Session, device: Device) -> None:
    """删除设备凭据，并让旧 ETag 失效。"""
    row = db.scalar(select(DeviceCredential).where(DeviceCredential.device_id == device.id))
    if row is not None:
        db.delete(row)
    device.secret_ref = None
    device.version += 1


def credential_kind_for_device(db: Session, device: Device) -> str | None:
    """只读取凭据类型元数据，不解密或返回敏感值。"""
    if not device.secret_ref:
        return None
    return db.scalar(select(DeviceCredential.kind).where(DeviceCredential.device_id == device.id))
