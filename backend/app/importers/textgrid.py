"""Praat TextGrid 解析（AliMeeting 用）。

只支持 IntervalTier；AliMeeting 的 TextGrid 是 ooTextFile 的固定缩进格式，
不需要通用解析器。
"""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Interval:
    """一个时间区间及其文本。"""

    start_s: float
    end_s: float
    text: str


@dataclass
class Tier:
    """一个说话人/声道对应的层。"""

    name: str
    intervals: list[Interval] = field(default_factory=list)


def parse_textgrid(path: Path) -> list[Tier]:
    """解析 IntervalTier 列表；跳过空文本区间由调用方决定。"""
    tiers: list[Tier] = []
    current_name: str | None = None
    current_intervals: list[Interval] = []
    pending: dict[str, float | str] = {}
    in_interval = False

    def flush_interval() -> None:
        nonlocal in_interval
        in_interval = False
        if pending.get("start") is None or pending.get("end") is None:
            return
        current_intervals.append(
            Interval(
                start_s=float(pending["start"]),
                end_s=float(pending["end"]),
                text=str(pending.get("text") or ""),
            )
        )
        pending.clear()

    def flush_tier() -> None:
        nonlocal current_name, current_intervals
        flush_interval()
        if current_name is not None and current_intervals:
            tiers.append(Tier(name=current_name, intervals=current_intervals))
        current_name = None
        current_intervals = []

    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line.startswith("item ["):
            flush_tier()
        elif line.startswith("name = "):
            current_name = _unquote(line.split("=", 1)[1].strip())
        elif line.startswith("intervals ["):
            flush_interval()
            in_interval = True
        elif in_interval and line.startswith("xmin = "):
            pending["start"] = float(line.split("=", 1)[1])
        elif in_interval and line.startswith("xmax = "):
            pending["end"] = float(line.split("=", 1)[1])
        elif in_interval and line.startswith("text = "):
            pending["text"] = _unquote(line.split("=", 1)[1].strip())

    flush_tier()
    return tiers


def _unquote(value: str) -> str:
    """去掉 TextGrid 的双引号，并还原转义的双引号。"""
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1].replace('""', '"')
    return value
