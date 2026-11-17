"""客家话 SRT/TXT 导入器；MP4 音轨由数据集服务统一抽取。"""

import html
import re
from collections.abc import Iterator
from pathlib import Path

from app.importers.base import ImportIssue, RawItem, Segment, read_text

_TIMECODE = re.compile(
    r"^(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s+-->\s+"
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})(?:\s+.*)?$"
)
_TAG = re.compile(r"<[^>]*>")


class SrtImporter:
    """按相对路径配对 MP4 与同名 SRT/TXT，保留 SRT 时间戳。"""

    name = "srt"
    version = "1"

    def detect(self, root: Path) -> bool:
        """目录中存在媒体或字幕即可识别，以便把缺失配对写入错误报告。"""
        return any(
            path.suffix.casefold() in {".mp4", ".srt", ".txt"}
            for path in root.rglob("*")
            if path.is_file()
        )

    def scan(self, root: Path, limit: int) -> Iterator[RawItem]:
        """按相对文件名配对字幕与视频；错误记录仍作为 RawItem 交给通用报告。"""
        media = {
            path.relative_to(root).with_suffix("").as_posix(): path
            for path in root.rglob("*")
            if path.is_file() and path.suffix.casefold() == ".mp4"
        }
        subtitles: dict[str, Path] = {}
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.casefold() not in {".txt", ".srt"}:
                continue
            key = path.relative_to(root).with_suffix("").as_posix()
            # SRT 的时间轴优先于同名纯文本文件。
            if key not in subtitles or path.suffix.casefold() == ".srt":
                subtitles[key] = path

        for produced, item_key in enumerate(sorted(set(media) | set(subtitles))):
            if produced >= limit:
                return
            audio = media.get(item_key)
            subtitle = subtitles.get(item_key)
            fallback_audio = audio or root / f"{item_key}.mp4"
            issues: list[ImportIssue] = []
            if audio is None:
                issues.append(ImportIssue("E_AUDIO_MISSING", str(fallback_audio)))
            if subtitle is None:
                issues.append(ImportIssue("E_REF_MISSING", str(fallback_audio)))

            ref_text = ""
            segments: list[Segment] = []
            if subtitle is not None:
                try:
                    ref_text, segments = _parse_subtitle(read_text(subtitle), subtitle.suffix)
                except ValueError as exc:
                    issues.append(ImportIssue("E_ENCODING", f"{subtitle}: {exc}"))
                if not ref_text.strip() and not issues:
                    issues.append(ImportIssue("E_REF_EMPTY", str(subtitle)))

            yield RawItem(
                item_key=item_key,
                audio=fallback_audio,
                ref_text=ref_text,
                split="all",
                speaker="unknown",
                duration_ms=0,
                sample_rate=0,
                channels=0,
                lang="hak",
                buckets={"language": "hak"},
                segments=segments,
                origin_ref=subtitle,
                issues=issues,
            )


def _parse_subtitle(content: str, suffix: str) -> tuple[str, list[Segment]]:
    """解析 SRT cue；纯 TXT 保留全文，但不伪造时间戳。"""
    normalized = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    has_timecodes = any(_TIMECODE.match(line.strip()) for line in normalized.splitlines())
    if suffix.casefold() != ".srt" and not has_timecodes:
        return normalized, []

    segments: list[Segment] = []
    for block in re.split(r"\n\s*\n", normalized):
        lines = block.splitlines()
        time_index = next(
            (index for index, line in enumerate(lines) if _TIMECODE.match(line.strip())), None
        )
        if time_index is None:
            continue
        match = _TIMECODE.match(lines[time_index].strip())
        if match is None:
            continue
        values = [int(part) for part in match.groups()]
        start_ms = _milliseconds(*values[:4])
        end_ms = _milliseconds(*values[4:])
        text_lines = [
            html.unescape(_TAG.sub("", line)).strip()
            for line in lines[time_index + 1 :]
            if line.strip()
        ]
        text = "".join(text_lines)
        if text:
            segments.append(
                Segment(
                    speaker="unknown",
                    start_ms=start_ms,
                    end_ms=end_ms,
                    text=text,
                )
            )
    return "".join(segment["text"] for segment in segments), segments


def _milliseconds(hours: int, minutes: int, seconds: int, milliseconds: int) -> int:
    """把 SRT 时码转换为整数毫秒。"""
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + milliseconds
