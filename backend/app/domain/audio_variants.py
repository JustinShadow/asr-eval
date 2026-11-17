"""按 M1 音频 profile 懒生成并缓存 16k 单声道音频。"""

import os
import shutil
import subprocess
import uuid
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ApiError
from app.domain.models import AudioAsset, AudioVariant
from app.importers.audio import fingerprint_wav

_PROFILE_ALIASES = {
    "16k_mono": "16k_mono",
    "mono_mix": "16k_mono",
    "16k_mono_ch0": "16k_mono_ch0",
    "mono_ch0": "16k_mono_ch0",
}
AUDIO_CONVERSION_TIMEOUT_S = 300  # 长音频在本地 NFS 上留足转换时间，M1 §5.4。


def resolve_audio_path(db: Session, asset: AudioAsset, profile: str) -> Path:
    """返回满足 subset profile 的音频路径；转换结果以内容 sha256 缓存。"""
    if profile == "as_is":
        if asset.sample_rate not in {None, 0, 16000} or asset.channels not in {None, 0, 1}:
            raise ApiError(
                "E_AUDIO_PROFILE",
                "as_is 音频必须为 16kHz 单声道",
                status_code=422,
                detail={"sample_rate": asset.sample_rate, "channels": asset.channels},
            )
        return Path(asset.uri)
    canonical_profile = _PROFILE_ALIASES.get(profile)
    if canonical_profile is None:
        raise ApiError("E_AUDIO_PROFILE", "不支持的音频 profile", status_code=422)

    variant = db.get(AudioVariant, (asset.id, canonical_profile))
    if variant is not None and Path(variant.uri).is_file():
        return Path(variant.uri)

    source = Path(asset.uri)
    if not source.is_file():
        raise ApiError("E_AUDIO_MISSING", "原始音频文件不存在", status_code=404)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise ApiError("E_FFMPEG_MISSING", "音频转换需要 ffmpeg", status_code=503)

    target_dir = get_settings().data_dir / "audio_variants" / str(asset.id)
    target_dir.mkdir(parents=True, exist_ok=True)
    temporary_path = target_dir / f".{uuid.uuid4().hex}.wav"
    command = [
        ffmpeg,
        "-y",
        "-nostdin",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
    ]
    if canonical_profile == "16k_mono_ch0":
        command.extend(["-af", "pan=mono|c0=c0"])
    command.extend(["-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(temporary_path)])
    try:
        subprocess.run(  # noqa: S603 - 参数列表不经过 shell，音频路径作为单个参数传入
            command,
            check=True,
            capture_output=True,
            timeout=AUDIO_CONVERSION_TIMEOUT_S,
        )
        fingerprint = fingerprint_wav(temporary_path)
        if (
            fingerprint.duration_ms <= 0
            or fingerprint.sample_rate != 16000
            or fingerprint.channels != 1
        ):
            raise ApiError(
                "E_AUDIO_PROFILE", "转换结果不是有效的 16kHz 单声道 WAV", status_code=422
            )
        output_path = target_dir / f"{fingerprint.sha256}.wav"
        os.replace(temporary_path, output_path)
        statement = pg_insert(AudioVariant).values(
            asset_id=asset.id,
            profile=canonical_profile,
            sha256=fingerprint.sha256,
            uri=str(output_path),
            bytes=fingerprint.bytes,
        )
        statement = statement.on_conflict_do_update(
            index_elements=["asset_id", "profile"],
            set_={
                "sha256": statement.excluded.sha256,
                "uri": statement.excluded.uri,
                "bytes": statement.excluded.bytes,
            },
        )
        db.execute(statement)
        db.flush()
        return output_path
    except subprocess.TimeoutExpired as exc:
        raise ApiError("E_AUDIO_CONVERT_TIMEOUT", "音频转换超时", status_code=422) from exc
    except subprocess.CalledProcessError as exc:
        raise ApiError(
            "E_AUDIO_CONVERT_FAILED", "音频转换失败，请检查音频文件", status_code=422
        ) from exc
    finally:
        temporary_path.unlink(missing_ok=True)
