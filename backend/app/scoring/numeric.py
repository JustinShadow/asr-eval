"""无 ITN 金标时的数字相关 CER 近似信号（M5 §5.4）。"""

import re
from dataclasses import dataclass

from rapidfuzz.distance import Levenshtein

from app.scoring.cer import normalize_for_cer

_NUMBER_CHARS = "0-9〇零一二三四五六七八九十百千万亿两俩幺点负壹贰叁肆伍陆柒捌玖拾佰仟萬億"
_NUMBER_SPAN_RE = re.compile(rf"[{_NUMBER_CHARS}]+")


@dataclass(frozen=True)
class NumericCerResult:
    """数字字符子集上的 CER 对齐结果；分母是参考中的数字相关字符数。"""

    value: float | None
    s: int
    d: int
    i: int
    n: int

    @property
    def edits(self) -> int:
        """数字相关的替换、删除和插入总数。"""
        return self.s + self.d + self.i


def _positions(text: str) -> set[int]:
    """提取数字及常见中文数词位置，供 CER 编辑操作归因。"""
    return {
        position
        for match in _NUMBER_SPAN_RE.finditer(text)
        for position in range(match.start(), match.end())
    }


def numeric_reference_length(
    ref: str,
    *,
    strip_sc: bool = True,
    strip_punct: bool = True,
    strip_space: bool = True,
    lowercase: bool = True,
    drop_tags: tuple[str, ...] = (),
    drop_fillers: tuple[str, ...] = (),
    traditional: str = "keep",
) -> int:
    """按 CER 同一套文本清理规则统计参考中的数字相关字符。"""
    normalized = normalize_for_cer(
        ref,
        strip_sc=strip_sc,
        strip_punct=strip_punct,
        strip_space=strip_space,
        lowercase=lowercase,
        drop_tags=drop_tags,
        drop_fillers=drop_fillers,
        traditional=traditional,
    )
    return len(_positions(normalized))


def numeric_cer(
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
) -> NumericCerResult:
    """将标准 CER editops 限定到数字相关字符，形成可解释的近似读数。

    这不是 ITN 准确率：数词写法差异也会计入错误，正式 ITN 仍要求独立金标。
    """
    ref_normalized = normalize_for_cer(
        ref,
        strip_sc=strip_sc,
        strip_punct=strip_punct,
        strip_space=strip_space,
        lowercase=lowercase,
        drop_tags=drop_tags,
        drop_fillers=drop_fillers,
        traditional=traditional,
    )
    hyp_normalized = normalize_for_cer(
        hyp,
        strip_sc=strip_sc,
        strip_punct=strip_punct,
        strip_space=strip_space,
        lowercase=lowercase,
        drop_tags=drop_tags,
        drop_fillers=drop_fillers,
        traditional=traditional,
    )
    ref_positions = _positions(ref_normalized)
    n = len(ref_positions)
    if not n:
        return NumericCerResult(value=None, s=0, d=0, i=0, n=0)

    hyp_positions = _positions(hyp_normalized)
    substitutions = deletions = insertions = 0
    for operation in Levenshtein.editops(ref_normalized, hyp_normalized):
        if operation.tag == "replace":
            if operation.src_pos in ref_positions or operation.dest_pos in hyp_positions:
                substitutions += 1
        elif operation.tag == "delete":
            if operation.src_pos in ref_positions:
                deletions += 1
        elif operation.dest_pos in hyp_positions and (
            operation.src_pos in ref_positions or operation.src_pos - 1 in ref_positions
        ):
            # 插入没有参考字符位置，只有紧邻参考数字时才归入数字近似指标。
            insertions += 1

    edits = substitutions + deletions + insertions
    return NumericCerResult(
        value=edits / n,
        s=substitutions,
        d=deletions,
        i=insertions,
        n=n,
    )
