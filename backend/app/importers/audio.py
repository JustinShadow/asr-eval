"""音频指纹：一次读取同时算 sha256、时长、采样率、声道。

WSL 的 /mnt/f 上 wave.open 单文件约 15~20ms，逐条打开是导入瓶颈；
这里只打开一次、顺序读，再由调用方用线程池并行。
"""

import hashlib
import struct
import wave
from dataclasses import dataclass
from pathlib import Path


@dataclass
class AudioFingerprint:
    """一条音频的指纹与格式信息。"""

    sha256: str
    bytes: int
    duration_ms: int
    sample_rate: int
    channels: int
    codec: str = "pcm_s16le"


@dataclass
class AudioMetadata:
    """预览所需的轻量音频属性；不包含内容哈希。"""

    bytes: int
    duration_ms: int
    sample_rate: int
    channels: int


def fingerprint_wav(path: Path, chunk_size: int = 4 * 1024 * 1024) -> AudioFingerprint:
    """读一遍文件：sha256 + WAV 头解析；损坏文件不抛异常。"""
    digest = hashlib.sha256()
    head = b""
    total = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            if len(head) < 65536:
                head += chunk[: 65536 - len(head)]
            digest.update(chunk)
            total += len(chunk)

    duration_ms, rate, channels = _parse_wav_header(head, total)
    return AudioFingerprint(
        sha256=digest.hexdigest(),
        bytes=total,
        duration_ms=duration_ms,
        sample_rate=rate,
        channels=channels,
    )


def read_wav_metadata(path: Path) -> AudioMetadata:
    """只读 WAV 容器头，供 M1 §5.2 预览使用，不扫描大型音频数据块。"""
    size = path.stat().st_size
    try:
        with wave.open(str(path), "rb") as audio:
            sample_rate = audio.getframerate()
            duration_ms = int(audio.getnframes() * 1000 / sample_rate) if sample_rate > 0 else 0
            return AudioMetadata(
                bytes=size,
                duration_ms=duration_ms,
                sample_rate=sample_rate,
                channels=audio.getnchannels(),
            )
    except (EOFError, OSError, wave.Error):
        return AudioMetadata(bytes=size, duration_ms=0, sample_rate=0, channels=0)


def _parse_wav_header(data: bytes, file_size: int) -> tuple[int, int, int]:
    """从 RIFF 头解析 (duration_ms, sample_rate, channels)。"""
    if len(data) < 44 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        return 0, 0, 0
    offset = 12
    rate = 0
    channels = 0
    byte_rate = 0
    data_size = 0
    while offset + 8 <= len(data):
        chunk_id = data[offset : offset + 4]
        chunk_size = struct.unpack("<I", data[offset + 4 : offset + 8])[0]
        body = offset + 8
        if chunk_id == b"fmt " and body + 16 <= len(data):
            _fmt, channels, rate, byte_rate = struct.unpack("<HHII", data[body : body + 12])
        elif chunk_id == b"data":
            data_size = chunk_size
            if data_size in (0, 0xFFFFFFFF):
                data_size = max(0, file_size - body)
            break
        offset = body + chunk_size + (chunk_size & 1)
    if byte_rate <= 0 or data_size <= 0:
        return 0, rate, channels
    return int(data_size / byte_rate * 1000), rate, channels
