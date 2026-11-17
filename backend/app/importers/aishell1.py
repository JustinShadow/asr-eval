"""AISHELL-1 导入器。

目录约定：
    data_aishell/transcript/aishell_transcript_v0.8.txt
    data_aishell/wav/{train,dev,test}/{speaker}/{utt}.wav

用户选择的根目录既可以是 `data_aishell` 本身，也可以是它的上一层
（例如开源数据集目录中的 `aishell1/`）；检测与扫描会自动下钻。
文本是空格分词，按字符拼接（"而 对 楼市" -> "而对楼市"）。
"""

import re
from collections.abc import Iterator
from pathlib import Path

from app.importers.base import RawItem, read_text

_UTT_RE = re.compile(r"^(BAC\d{3})(S\d{4})(W\d{4})$")


class Aishell1Importer:
    """AISHELL-1 的 detect + scan。"""

    name = "aishell1"
    version = "1"

    def detect(self, root: Path) -> bool:
        """只认标准目录结构；允许根目录是 data_aishell 的上一层。"""
        resolved = _resolve_root(root)
        return (resolved / "transcript" / "aishell_transcript_v0.8.txt").is_file() and (
            resolved / "wav"
        ).is_dir()

    def scan(self, root: Path, limit: int) -> Iterator[RawItem]:
        """按 transcript 顺序产出，最多 limit 条有音频的样本。

        先扫一遍 wav 目录建索引，避免对 14 万行 transcript 逐行 stat
        （WSL 的 /mnt/f 上 stat 很慢，会从秒级变成十分钟级）。
        """
        resolved = _resolve_root(root)
        index = _build_index(resolved)
        transcript = resolved / "transcript" / "aishell_transcript_v0.8.txt"
        produced = 0
        for line in read_text(transcript).splitlines():
            if produced >= limit:
                break
            parts = line.strip().split()
            if len(parts) < 2:
                continue
            utt_id = parts[0]
            ref_text = "".join(parts[1:])
            match = _UTT_RE.match(utt_id)
            if not match or not ref_text:
                continue
            speaker = match.group(2)

            hit = index.get(utt_id)
            if hit is None:
                continue
            audio, split = hit
            produced += 1
            yield RawItem(
                item_key=utt_id,
                audio=audio,
                ref_text=ref_text,
                split=split,
                speaker=speaker,
                duration_ms=0,
                sample_rate=0,
                channels=0,
                buckets={"language": "zh", "split": split},
            )


def _resolve_root(root: Path) -> Path:
    """兼容用户选择 `aishell1/` 而不是 `aishell1/data_aishell/`。"""
    nested = root / "data_aishell"
    if (nested / "transcript" / "aishell_transcript_v0.8.txt").is_file() and (
        nested / "wav"
    ).is_dir():
        return nested
    return root


def _build_index(root: Path) -> dict[str, tuple[Path, str]]:
    """一次遍历 wav/{split}/{speaker}/*.wav，建立 utt_id -> (路径, split)。"""
    index: dict[str, tuple[Path, str]] = {}
    wav_root = root / "wav"
    for split_dir in sorted(wav_root.iterdir()):
        if not split_dir.is_dir():
            continue
        for speaker_dir in split_dir.iterdir():
            if not speaker_dir.is_dir():
                continue
            for audio in speaker_dir.glob("*.wav"):
                index[audio.stem] = (audio, split_dir.name)
    return index
