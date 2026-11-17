"""Importer 共用的数据结构。"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import NotRequired, Protocol, TypedDict


class Segment(TypedDict):
    """参考分段：时间区间 + 说话人（M1 §4）。"""

    speaker: str
    start_ms: int
    end_ms: int
    text: str
    skip: NotRequired[bool]
    overlap: NotRequired[bool]


class SpeakerInfo(TypedDict, total=False):
    """说话人可分桶元数据（M1 §3.1）。"""

    gender: str | None
    age: str | None
    accent: str | None
    meta: dict[str, object]


@dataclass(frozen=True)
class ImportIssue:
    """单条导入记录的可恢复问题；保留 code 与可定位的路径/原因。"""

    code: str
    detail: str


@dataclass
class RawItem:
    """导入器输出的单条样本；不依赖数据库。"""

    item_key: str
    audio: Path
    ref_text: str
    split: str
    speaker: str
    duration_ms: int
    sample_rate: int
    channels: int
    lang: str = "zh"
    subset: str = "all"
    audio_profile: str = "as_is"
    buckets: dict[str, object] = field(default_factory=dict)
    # 跨 subset 配对键（M1 §5.6 规则 4）：APY 的 {SES}/{domain} 在 phone/mic 一致
    pair_key: str | None = None
    # ITN 参考文本（§7 特殊标记展开后）；暂无数据集提供，先留契约位
    itn_text: str | None = None
    # 参考分段（M1 §4）：[{speaker,start_ms,end_ms,text}]，长音频/会议用
    segments: list[Segment] = field(default_factory=list)
    # 说话人元数据；键同时作为 item.speakers 的候选值
    speakers: dict[str, SpeakerInfo] = field(default_factory=dict)
    # 原始媒体与参考路径；mp4 转码后仍需保留来源，便于追查与路径校验。
    origin_audio: Path | None = None
    origin_ref: Path | None = None
    # 可恢复的逐条问题，通用导入器记录后跳过该条（M1 §8）。
    issues: list[ImportIssue] = field(default_factory=list)


class EncodingError(ValueError):
    """E_ENCODING：非 UTF-8 且无法识别（M1 §8）。"""


def read_text(path: Path) -> str:
    """读文本标注：先 UTF-8，失败回退 GB18030（中文语料常见）。

    两种都解不开 → E_ENCODING（该条失败，M1 §8）；
    为什么不用 errors="replace"：静默替换会把乱码当成参考文本入库。
    """
    data = path.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("gb18030")
    except UnicodeDecodeError as exc:
        raise EncodingError(f"{path}: 非 UTF-8/GB18030 编码") from exc


class Importer(Protocol):
    """导入器契约：先 detect，再按 limit 产出 RawItem。"""

    name: str

    def detect(self, root: Path) -> bool: ...

    def scan(self, root: Path, limit: int) -> Iterator[RawItem]: ...
