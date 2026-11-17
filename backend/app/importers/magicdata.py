"""MagicData 方言导入器。

目录约定为 TXT/WAV 同名配对；分段、噪声标记与说话人属性按 M1 §7 解析。
"""

import re
from collections.abc import Iterator
from pathlib import Path

from app.importers.base import RawItem, Segment, SpeakerInfo, read_text

_LINE_RE = re.compile(r"^\[([\d.]+),([\d.]+)\]\t([^\t]+)\t([^\t]+)\t(.*)$")
_SKIP_MARKERS = (
    "[*]",
    "[ENS]",
    "[LAUGHTER]",
    "[SONANT]",
    "[MUSIC]",
    "[SYSTEM]",
    "[FILLER]",
    "[PII]",
)


class MagicDataImporter:
    """按 TXT/WAV 同名配对，整段 session 作为一个 item。"""

    name = "magicdata"
    version = "2"

    def detect(self, root: Path) -> bool:
        return (root / "TXT").is_dir() and (root / "WAV").is_dir()

    def scan(self, root: Path, limit: int) -> Iterator[RawItem]:
        produced = 0
        language, dialect = infer_dialect(root.name)
        speaker_meta = _load_speaker_meta(root / "SPKINFO.txt")
        for txt in sorted((root / "TXT").glob("*.txt")):
            if produced >= limit:
                return
            audio = root / "WAV" / f"{txt.stem}.wav"
            if not audio.is_file():
                continue
            speakers: list[str] = []
            texts: list[str] = []
            segments: list[Segment] = []
            for line in read_text(txt).splitlines():
                match = _LINE_RE.match(line.strip())
                if not match:
                    continue
                start, end, raw_speaker, gender, raw_text = match.groups()
                text = raw_text.strip()
                if not text:
                    continue
                speaker_id = "unknown" if raw_speaker in {"0", "G0000"} else raw_speaker
                overlap = "[+]" in text
                text = text.replace("[+]", "").strip()
                skip = text.startswith(_SKIP_MARKERS)
                start_ms = round(float(start) * 1000)
                end_ms = max(round(float(end) * 1000), start_ms + 1)
                if text:
                    segments.append(
                        Segment(
                            speaker=speaker_id,
                            start_ms=start_ms,
                            end_ms=end_ms,
                            text=text,
                            skip=skip,
                            overlap=overlap,
                        )
                    )
                if speaker_id != "unknown":
                    speakers.append(speaker_id)
                    speaker_meta.setdefault(
                        speaker_id,
                        {"gender": _gender_code(gender)},
                    )
                if not skip and text:
                    texts.append(text)
            if not texts:
                continue
            speaker_infos = {
                speaker_id: speaker_meta[speaker_id] for speaker_id in sorted(set(speakers))
            }
            produced += 1
            yield RawItem(
                item_key=txt.stem,
                audio=audio,
                ref_text="".join(texts),
                split="all",
                speaker=speakers[0] if speakers else "unknown",
                duration_ms=0,
                sample_rate=0,
                channels=0,
                lang=language,
                segments=segments,
                speakers=speaker_infos,
                buckets={
                    "dialect": dialect,
                    "language": language,
                    "gender": speaker_infos.get(speakers[0], {}).get("gender", "unknown")
                    if speakers
                    else "unknown",
                },
            )


def _load_speaker_meta(path: Path) -> dict[str, SpeakerInfo]:
    """读取 SPKINFO 表；原始地区和设备同时写入 JSONB。"""
    result: dict[str, SpeakerInfo] = {}
    if not path.is_file():
        return result
    for line in read_text(path).splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        _channel, speaker_id, gender, age, region, device = parts[:6]
        result[speaker_id] = {
            "gender": _gender_code(gender),
            "age": age,
            "accent": region,
            "meta": {"region": region, "device": device},
        }
    return result


def _gender_code(value: str) -> str:
    """将原始性别值转换为 M1 §7.2 的 code。"""
    return {"male": "M", "female": "F", "m": "M", "f": "F"}.get(value.casefold(), "unknown")


def infer_dialect(name: str) -> tuple[str, str]:
    """从目录名推断语言代码与统一方言标签。

    目录名是 MagicData 唯一的方言信号；返回值用于 item.buckets，
    未知方言保留目录名，避免丢失来源信息。
    """
    if "沪" in name or "吴" in name:
        return "wuu", "沪语"
    if "湘" in name:
        return "xiang", "湘语"
    if "赣" in name:
        return "gan", "赣语"
    return "zh", name
