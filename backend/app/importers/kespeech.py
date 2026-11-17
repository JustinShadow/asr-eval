"""KeSpeech 导入器。

目录约定（每个方言一个目录）：
    kespeech/Beijing/{speaker}_{hash}.wav
    kespeech/Beijing/{speaker}_{hash}.txt

文本是单行纯文本；方言来自目录名。
"""

from collections.abc import Iterator
from pathlib import Path

from app.importers.base import RawItem, read_text


class KespeechImporter:
    """按方言目录扫描 wav/txt 对。"""

    name = "kespeech"
    version = "1"

    def detect(self, root: Path) -> bool:
        """root 本身或它的直接子目录里存在 wav/txt 对。"""
        if _has_pairs(root):
            return True
        return any(child.is_dir() and _has_pairs(child) for child in root.iterdir())

    def scan(self, root: Path, limit: int) -> Iterator[RawItem]:
        """按方言名、文件名排序，最多 limit 条。"""
        roots = (
            [root]
            if _has_pairs(root)
            else [child for child in sorted(root.iterdir()) if child.is_dir() and _has_pairs(child)]
        )
        produced = 0
        for dialect_dir in roots:
            dialect = dialect_dir.name
            for txt in sorted(dialect_dir.glob("*.txt")):
                if produced >= limit:
                    return
                audio = txt.with_suffix(".wav")
                if not audio.is_file():
                    continue
                text = read_text(txt).strip()
                if not text:
                    continue
                produced += 1
                yield RawItem(
                    item_key=txt.stem,
                    audio=audio,
                    ref_text=text,
                    split="all",
                    speaker=txt.stem.split("_")[0],
                    duration_ms=0,
                    sample_rate=0,
                    channels=0,
                    buckets={"dialect": dialect, "language": "zh"},
                )


def _has_pairs(path: Path) -> bool:
    """轻量判断：目录下同时有 .wav 和 .txt 即可。"""
    if not path.is_dir():
        return False
    has_wav = any(path.glob("*.wav"))
    has_txt = any(path.glob("*.txt"))
    return has_wav and has_txt
