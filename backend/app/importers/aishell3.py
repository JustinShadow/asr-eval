"""AISHELL-3 导入器。

content.txt 的第二列是"汉字 拼音"交替序列，取偶数下标拼回文本；
spk-info.txt 提供年龄组、性别、口音。
"""

from collections.abc import Iterator
from pathlib import Path

from app.importers.base import RawItem, SpeakerInfo, read_text


class Aishell3Importer:
    """train/test 两个 split。"""

    name = "aishell3"
    version = "3"

    def detect(self, root: Path) -> bool:
        return (root / "train" / "content.txt").is_file() or (
            root / "test" / "content.txt"
        ).is_file()

    def scan(self, root: Path, limit: int) -> Iterator[RawItem]:
        speakers = _load_speaker_meta(root / "spk-info.txt")
        produced = 0
        for split in ("train", "test"):
            content = root / split / "content.txt"
            if not content.is_file():
                continue
            for line in read_text(content).splitlines():
                if produced >= limit:
                    return
                line = line.strip()
                if not line or "\t" not in line:
                    continue
                filename, transcript = line.split("\t", 1)
                tokens = transcript.split()
                # 交替序列：汉字在偶数位、拼音在奇数位
                text = "".join(tokens[::2])
                if not text:
                    continue
                speaker = filename.split("_")[0].split(".")[0][:7]
                audio = root / split / "wav" / speaker / filename
                if not audio.is_file():
                    continue
                meta: dict[str, str] = speakers.get(speaker, {})
                gender = _gender_code(meta.get("gender"))
                speaker_meta: SpeakerInfo = {
                    "gender": gender,
                    "age": meta.get("age_group"),
                    "accent": meta.get("accent"),
                }
                produced += 1
                yield RawItem(
                    item_key=f"{split}-{filename}",
                    audio=audio,
                    ref_text=text,
                    split=split,
                    speaker=speaker,
                    duration_ms=0,
                    sample_rate=0,
                    channels=0,
                    audio_profile="mono_mix",
                    speakers={speaker: speaker_meta},
                    buckets={
                        "language": "zh",
                        "gender": gender,
                        "age_group": meta.get("age_group"),
                        "accent": meta.get("accent"),
                    },
                )


def _load_speaker_meta(path: Path) -> dict[str, dict[str, str]]:
    """spk-info.txt: speaker \\t age_group \\t gender \\t accent。"""
    result: dict[str, dict[str, str]] = {}
    if not path.is_file():
        return result
    for line in read_text(path).splitlines():
        parts = line.strip().split("\t")
        if len(parts) >= 4 and not line.startswith("#"):
            result[parts[0]] = {
                "age_group": parts[1],
                "gender": _gender_code(parts[2]) or "unknown",
                "accent": parts[3],
            }
    return result


def _gender_code(value: str | None) -> str | None:
    """将语料性别写为 M/F 代码，与 M1 §7.2 枚举一致。"""
    if value is None:
        return None
    normalized = value.strip().casefold()
    if normalized in {"m", "male", "男"}:
        return "M"
    if normalized in {"f", "female", "女"}:
        return "F"
    return "unknown"
