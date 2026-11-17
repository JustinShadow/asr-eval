"""本地对象存储及切换后端所需的接口（M9 §5.2）。"""

import hashlib
import hmac
import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol
from urllib.parse import quote

from app.core.config import get_settings


class ObjectStore(Protocol):
    """业务代码使用的对象存储接口。"""

    def put(self, key: str, data: bytes, content_type: str) -> None: ...

    def open(self, key: str) -> BinaryIO: ...

    def delete(self, key: str) -> None: ...

    def exists(self, key: str) -> bool: ...

    def signed_url(self, key: str, ttl: int = 300) -> str: ...


class LocalObjectStore:
    """默认本地实现：路径限制在 data/objects，写入使用临时文件原子替换。"""

    def __init__(self, root: Path, signing_key: str) -> None:
        self.root = root.resolve()
        self.signing_key = signing_key.encode("utf-8")

    def put(self, key: str, data: bytes, content_type: str) -> None:
        """原子写入对象；content_type 留给其他存储后端记录元数据。"""
        del content_type
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".upload-", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, target)
        except Exception:
            Path(temporary_name).unlink(missing_ok=True)
            raise

    def open(self, key: str) -> BinaryIO:
        """打开对象只读句柄；调用方负责关闭。"""
        return self._path(key).open("rb")

    def delete(self, key: str) -> None:
        """删除对象；对象不存在时保持幂等。"""
        self._path(key).unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        """检查对象是否存在。"""
        return self._path(key).is_file()

    def signed_url(self, key: str, ttl: int = 300) -> str:
        """生成限时下载地址；文件路由负责验证 exp 与 sig。"""
        if ttl <= 0:
            raise ValueError("签名有效期必须大于 0")
        import time

        expires = int(time.time()) + ttl
        signature = self._signature(key, expires)
        return f"/api/v1/files/{quote(key, safe='/')}?exp={expires}&sig={signature}"

    def verify_signature(self, key: str, expires: int, signature: str) -> bool:
        """验证限时下载地址的签名与有效期。"""
        import time

        if expires < int(time.time()):
            return False
        return hmac.compare_digest(signature, self._signature(key, expires))

    def _path(self, key: str) -> Path:
        """拒绝绝对路径、空段、点段及越出对象根目录的符号链接。"""
        relative = PurePosixPath(key)
        if (
            not key
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ValueError("对象 key 必须是对象根目录内的相对路径")
        target = self.root.joinpath(*relative.parts).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError("对象 key 越出对象根目录")
        return target

    def _signature(self, key: str, expires: int) -> str:
        message = f"{key}\n{expires}".encode()
        return hmac.new(self.signing_key, message, hashlib.sha256).hexdigest()


def get_object_store() -> ObjectStore:
    """按应用配置创建本地对象存储。"""
    settings = get_settings()
    return LocalObjectStore(settings.data_dir / "objects", settings.jwt_key)
