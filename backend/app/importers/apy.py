"""APY（DataTang LHD）导入器，按 paired subset 导入 phone 与 mic。

目录：
    APY.../data/category/{n}/{session}/{domain}_phone.wav
    .../{domain}_phone.metadata
    .../{domain}_phone.txt

phone 为 16k 单声道；mic 为 44.1k 双声道，使用 mono_mix profile 懒生成 16k 单声道。
"""

from collections.abc import Iterator
from pathlib import Path

from app.importers.base import RawItem, Segment, SpeakerInfo, read_text


class ApyImporter:
    """扫描 session×domain 下可用的 phone/mic 条件并写入同一个 pair_key。"""

    name = "apy"
    version = "3"

    def detect(self, root: Path) -> bool:
        if any(root.glob("**/*_phone.wav")) or any(root.glob("**/*_mic.wav")):
            return True
        # 部分 APY/DataTang 包用 LHD 通用文件名（如 2_154_001.wav），
        # 采集条件由 metadata 的 MIT 字段给出，而不是 *_phone/*_mic 后缀。
        return any(
            meta.with_suffix(".wav").is_file() and meta.with_suffix(".txt").is_file()
            for meta in root.glob("**/*.metadata")
        )

    def scan(self, root: Path, limit: int) -> Iterator[RawItem]:
        produced = 0
        groups: dict[tuple[Path, str], dict[str, Path]] = {}
        for audio in sorted(root.glob("**/*.wav")):
            device = next(
                (name for name in ("phone", "mic") if audio.stem.endswith(f"_{name}")), None
            )
            if device is None:
                continue
            domain = audio.stem.removesuffix(f"_{device}")
            groups.setdefault((audio.parent, domain), {})[device] = audio

        # LHD 通用文件名：用同名 metadata 的 MIT 判断 phone/mic。
        for meta_path in sorted(root.glob("**/*.metadata")):
            audio = meta_path.with_suffix(".wav")
            label = meta_path.with_suffix(".txt")
            if not audio.is_file() or not label.is_file():
                continue
            if any(audio.stem.endswith(f"_{name}") for name in ("phone", "mic")):
                continue
            meta = _parse_metadata(meta_path)
            device = _device_from_metadata(meta)
            if device is None:
                continue
            groups.setdefault((audio.parent, audio.stem), {})[device] = audio

        for (_session_dir, domain), devices in sorted(
            groups.items(), key=lambda entry: (str(entry[0][0]), entry[0][1])
        ):
            for subset in ("phone", "mic"):
                if produced >= limit:
                    return
                subset_audio = devices.get(subset)
                if subset_audio is None:
                    continue
                label = subset_audio.with_suffix(".txt")
                meta_path = subset_audio.with_suffix(".metadata")
                if not label.is_file() or not meta_path.is_file():
                    continue
                text, speakers, segments = _parse_label(label)
                if not text:
                    continue
                meta = _parse_metadata(meta_path)
                speaker_meta = _speaker_metadata(meta)
                for speaker_id in speakers:
                    speaker_meta.setdefault(speaker_id, {"gender": "unknown"})
                language, dialect = infer_dialect(subset_audio)
                if language is None:
                    language = _language(meta.get("ACT", ""))
                    dialect = _DIALECT_LABELS.get(language)
                session_id = meta.get("SES", subset_audio.parent.name)
                pair_key = f"{session_id}/{domain}"
                buckets: dict[str, object] = {
                    "domain": domain,
                    "language": language,
                    "accent": meta.get("ACC"),
                    "gender": next(
                        (
                            speaker_meta[speaker_id].get("gender")
                            for speaker_id in speakers
                            if speaker_id in speaker_meta
                        ),
                        "unknown",
                    ),
                }
                if dialect:
                    buckets["dialect"] = dialect
                produced += 1
                yield RawItem(
                    item_key=f"{pair_key}/{subset}",
                    audio=subset_audio,
                    ref_text=text,
                    split="all",
                    speaker=speakers[0] if speakers else "unknown",
                    duration_ms=0,
                    sample_rate=0,
                    channels=0,
                    lang=language,
                    subset=subset,
                    audio_profile="as_is" if subset == "phone" else "mono_mix",
                    segments=segments,
                    speakers=speaker_meta,
                    buckets=buckets,
                    # 同一 session/domain 在两个 subset 中共享 pair_key，M1 §5.6。
                    pair_key=pair_key,
                )


def _parse_label(path: Path) -> tuple[str, list[str], list[Segment]]:
    """标签行：start\\tend\\tspk\\ttext。"""
    texts: list[str] = []
    speakers: list[str] = []
    segments: list[Segment] = []
    for line in read_text(path).splitlines():
        parts = line.strip().split("\t", 3)
        if len(parts) < 4:
            continue
        text = parts[3].strip()
        if not text:
            continue
        texts.append(text)
        if parts[2] and parts[2] not in speakers:
            speakers.append(parts[2])
        start_ms = round(float(parts[0]) * 1000)
        end_ms = max(round(float(parts[1]) * 1000), start_ms + 1)
        segments.append(
            Segment(
                speaker=parts[2] or "unknown",
                start_ms=start_ms,
                end_ms=end_ms,
                text=text,
            )
        )
    return "".join(texts), speakers, segments


def _parse_metadata(path: Path) -> dict[str, str]:
    """LHD 是 KEY\\tvalue 的平面键值。"""
    meta: dict[str, str] = {}
    for line in read_text(path).splitlines():
        if "\t" not in line:
            continue
        key, value = line.split("\t", 1)
        if key in {"SES", "ACT", "ACC", "SEX", "AGE", "SAM", "NCH", "SCD", "MIT"}:
            meta[key] = value.strip()
    return meta


def _device_from_metadata(meta: dict[str, str]) -> str | None:
    """从 LHD 的 MIT（录音设备）推断 phone/mic subset。

    APY/DataTang 的通用文件名包没有 `_phone`/`_mic` 后缀：
    android/iphone 属于手机录音（phone），microphone 属于外接麦（mic）；
    无法判断时按采样率/声道兜底，16k 单声道视为 phone，其余视为 mic。
    """
    mit = meta.get("MIT", "").strip().casefold()
    if any(token in mit for token in ("android", "iphone", "phone", "mobile")):
        return "phone"
    if any(token in mit for token in ("mic", "microphone", "external")):
        return "mic"
    sample_rate = meta.get("SAM", "").strip()
    channels = meta.get("NCH", "").strip()
    if sample_rate == "16000" and channels == "1":
        return "phone"
    return "mic" if sample_rate or channels else None


def _language(act: str) -> str:
    lowered = act.lower()
    if "cantonese" in lowered or "粤" in act:
        return "yue"
    if "sichuan" in lowered or "四川" in act or "chongqing" in lowered or "重庆" in act:
        return "sichuan"
    if (
        "minnan" in lowered
        or "hokkien" in lowered
        or "闽" in act
        or "漳州" in act
        or "泉州" in act
        or "厦门" in act
    ):
        return "minnan"
    return "zh"


_DIALECT_LABELS = {"yue": "粤语", "sichuan": "四川话", "minnan": "闽南语"}


def infer_dialect(path: Path) -> tuple[str | None, str | None]:
    """从 APY 包路径推断方言。

    APY 的 ACT 字段并不总是语言名：闽语包的 ACT 是“漳州/泉州”这类地点，
    单靠 ACT 会落回 zh。包目录名（如 `APY210930001-闽语`）是更可靠的信号。
    这里只匹配明确词，避免把路径里的 `min` 等片段误判成闽南语。
    """
    text = str(path)
    lowered = text.casefold()
    if "闽" in text or "minnan" in lowered or "hokkien" in lowered:
        return "minnan", _DIALECT_LABELS["minnan"]
    if "粤" in text or "cantonese" in lowered:
        return "yue", _DIALECT_LABELS["yue"]
    if "四川" in text or "sichuan" in lowered:
        return "sichuan", _DIALECT_LABELS["sichuan"]
    return None, None


def _speaker_metadata(metadata: dict[str, str]) -> dict[str, SpeakerInfo]:
    """按 SCD 顺序对齐 APY 的 SEX/AGE/ACC 列表（M1 §7）。"""
    speaker_ids = _split_values(metadata.get("SCD", ""), ",")
    genders = _split_values(metadata.get("SEX", ""), ",")
    ages = _split_values(metadata.get("AGE", ""), ";")
    accents = _split_values(metadata.get("ACC", ""), ";")
    result: dict[str, SpeakerInfo] = {}
    for index, speaker_id in enumerate(speaker_ids):
        gender_value = genders[index].rsplit("_", 1)[-1] if index < len(genders) else "unknown"
        result[speaker_id] = {
            "gender": _gender_code(gender_value),
            "age": ages[index] if index < len(ages) else None,
            "accent": accents[index] if index < len(accents) else None,
            "meta": {
                "device": metadata.get("MIT"),
                "sample_rate": metadata.get("SAM"),
                "channels": metadata.get("NCH"),
            },
        }
    return result


def _split_values(value: str, separator: str) -> list[str]:
    return [part.strip() for part in value.split(separator) if part.strip()]


def _gender_code(value: str) -> str:
    """将语料性别写为 M1 §7.2 的 code。"""
    normalized = value.casefold()
    if normalized in {"m", "male", "男"}:
        return "M"
    if normalized in {"f", "female", "女"}:
        return "F"
    return "unknown"
