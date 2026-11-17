"""CER 计算。

口径见 docs/m5-scoring-design.md §5.1/§5.2：
中文按字、去标点与空格，micro 聚合用 Σ(S+D+I)/Σlen(ref)。
`<sc>` 分隔符默认剥离（只用于 AliMeeting FIFO 排序，不计入编辑距离），
由口径参数 `strip_sc` 控制是否计入（`count` 模式复现官方口径）。
"""

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from importlib import import_module
from typing import Any

from rapidfuzz.distance import Levenshtein

_PUNCT_RE = re.compile(r"[，。！？、；：,.!?;:\"'“”‘’（）()《》〈〉…—\-·]")
_SPACE_RE = re.compile(r"\s+")
# AliMeeting FIFO 参考里的会话分隔符；strip 策略下在归一化前去掉
_SC_RE = re.compile(r"</?sc>", re.IGNORECASE)


@lru_cache(maxsize=1)
def _simplified_converter() -> Any:
    """复用繁转简转换器，避免逐条评分重复初始化词典。"""
    return import_module("opencc").OpenCC("t2s")


def normalize_script(text: str, *, traditional: str = "keep") -> str:
    """按 profile 的 text.traditional 设置转换繁简。"""
    normalized = unicodedata.normalize("NFKC", text)
    if traditional == "keep":
        return normalized
    if traditional == "to_simplified":
        return str(_simplified_converter().convert(normalized))
    raise ValueError(f"unsupported traditional normalization: {traditional}")


@dataclass(frozen=True)
class CerResult:
    """单条 CER 结果：错误类型分解见 M5 §2.1（s=替换 d=删除 i=插入）。"""

    value: float
    s: int
    d: int
    i: int
    n: int

    @property
    def edits(self) -> int:
        """总编辑数 Σ(S+D+I)。"""
        return self.s + self.d + self.i


def normalize_for_cer(
    text: str,
    *,
    strip_sc: bool = True,
    strip_punct: bool = True,
    strip_space: bool = True,
    lowercase: bool = True,
    drop_tags: tuple[str, ...] = (),
    drop_fillers: tuple[str, ...] = (),
    traditional: str = "keep",
) -> str:
    """按口径配置清理文本；默认行为兼容旧版 CER。"""
    text = normalize_script(text, traditional=traditional)
    if strip_sc:
        text = _SC_RE.sub("", text)
    for tag in drop_tags:
        text = text.replace(tag, "")
    for filler in drop_fillers:
        text = text.replace(filler, "")
    if strip_punct:
        text = _PUNCT_RE.sub("", text)
    if strip_space:
        text = _SPACE_RE.sub("", text)
    return text.lower() if lowercase else text


def cer(
    ref: str,
    hyp: str,
    *,
    strip_sc: bool = True,
    strip_punct: bool = True,
    strip_space: bool = True,
    lowercase: bool = True,
    drop_tags: tuple[str, ...] = (),
    drop_fillers: tuple[str, ...] = (),
    traditional: str = "keep",
) -> CerResult:
    """计算单条 CER。

    参考为空时抛 ValueError；调用方应记为 na(empty_reference)。
    """
    ref_norm = normalize_for_cer(
        ref,
        strip_sc=strip_sc,
        strip_punct=strip_punct,
        strip_space=strip_space,
        lowercase=lowercase,
        drop_tags=drop_tags,
        drop_fillers=drop_fillers,
        traditional=traditional,
    )
    hyp_norm = normalize_for_cer(
        hyp,
        strip_sc=strip_sc,
        strip_punct=strip_punct,
        strip_space=strip_space,
        lowercase=lowercase,
        drop_tags=drop_tags,
        drop_fillers=drop_fillers,
        traditional=traditional,
    )
    if not ref_norm:
        raise ValueError("empty reference")
    # editops 的 tag 与 M5 §2.1 的 S/D/I 一一对应：replace→s、delete→d、insert→i
    s = d = i = 0
    for op in Levenshtein.editops(ref_norm, hyp_norm):
        if op.tag == "replace":
            s += 1
        elif op.tag == "delete":
            d += 1
        else:
            i += 1
    n = len(ref_norm)
    return CerResult(value=(s + d + i) / n, s=s, d=d, i=i, n=n)
