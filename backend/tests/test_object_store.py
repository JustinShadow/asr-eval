"""验证 M9 本地对象存储的路径、原子读写与短期签名。"""

from urllib.parse import parse_qs, urlparse

import pytest

from app.core.object_store import LocalObjectStore


def test_local_object_store_roundtrip_and_signed_url(tmp_path) -> None:
    """对象可读写，签名只对原 key 与有效期有效。"""
    store = LocalObjectStore(tmp_path / "objects", "test-signing-key")
    key = "speakers/123/enrollments/audio.wav"
    store.put(key, b"wav-bytes", "audio/wav")

    assert store.exists(key)
    with store.open(key) as stream:
        assert stream.read() == b"wav-bytes"

    url = store.signed_url(key, ttl=60)
    query = parse_qs(urlparse(url).query)
    expires = int(query["exp"][0])
    assert store.verify_signature(key, expires, query["sig"][0])
    assert not store.verify_signature(f"{key}.other", expires, query["sig"][0])

    store.delete(key)
    assert not store.exists(key)


@pytest.mark.parametrize("key", ["../secret", "/etc/passwd", "", "a/../../secret"])
def test_local_object_store_rejects_unsafe_keys(tmp_path, key: str) -> None:
    """对象 key 不得访问对象根目录以外的路径。"""
    store = LocalObjectStore(tmp_path / "objects", "test-signing-key")
    with pytest.raises(ValueError):
        store.put(key, b"blocked", "application/octet-stream")
