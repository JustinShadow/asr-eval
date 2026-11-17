"""ITN 准确率（M5 §5.4）。

评分口径：
- 参考侧必须有 ITN 金标（RefText kind=itn）；无金标 → na(no_itn_reference)；
- 按实体类型抽取（数字/日期/时间/金额/百分比/单位）后逐类型比对；
- 归一化使用 WeTextProcessing runtime 的中文/英文 ITN Normalizer；
- 评分前统一做 NFKC 与去空白，不将 ITN 格式差异计入实体错误。

为什么"实体级"而不是整句 CER：ITN 错的是数字格式（2014年 vs 二零一四年），
整句 CER 会把格式差异当错字，无法区分"识别错"和"格式没归一"。
"""

import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from importlib import import_module
from typing import Protocol, TypedDict, cast

from app.scoring.cer import normalize_script

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("date", re.compile(r"(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*日?")),
    ("time", re.compile(r"(\d{1,2})\s*[:：点时]\s*(\d{1,2})(?:\s*分)?")),
    ("percent", re.compile(r"(\d+(?:\.\d+)?)\s*[%％]")),
    (
        "money",
        re.compile(
            r"(?:(?P<currency>[¥￥$])\s*(?P<prefix_value>\d+(?:\.\d+)?)\s*"
            r"(?P<scale>万|亿)?|(?P<plain_value>\d+(?:\.\d+)?)\s*"
            r"(?P<plain_currency>美元|元|块))"
        ),
    ),
    (
        "unit",
        re.compile(
            r"(\d+(?:\.\d+)?)\s*(平方公里|平方千米|立方米|千克|公斤|毫克|毫米|厘米|公里|千米|毫升|升|克|米|km|cm|mm|kg|mg|ml|g|l)",
            re.IGNORECASE,
        ),
    ),
    ("number", re.compile(r"\d+(?:\.\d+)?")),
]


class _TextNormalizer(Protocol):
    """WeTextProcessing Normalizer 的最小调用协议。"""

    def normalize(self, text: str) -> str: ...


@dataclass
class ItnResult:
    """单条样本的 ITN 实体比对。"""

    total: int = 0
    correct: int = 0
    by_type: dict[str, tuple[int, int]] | None = None


class ItnTypeSummary(TypedDict):
    """单类实体的准确率统计。"""

    correct: int
    total: int
    accuracy: float


class ItnSummary(TypedDict):
    """ITN 实体级 micro 汇总。"""

    value: float | None
    correct: int
    total: int
    n: int
    by_type: dict[str, ItnTypeSummary]


@lru_cache(maxsize=8)
def _normalizer(language: str) -> _TextNormalizer:
    """按语言复用 WeTextProcessing ITN 归一器，避免每个样本重复建图。"""
    module = import_module("wetext")
    factory = cast(Callable[..., _TextNormalizer], module.Normalizer)
    return factory(lang=language, operator="itn", full_to_half=True)


def _normalizer_language(language: str) -> str:
    """把数据集语言/方言代码映射到 WeTextProcessing 支持的识别语言。"""
    normalized = language.lower().replace("_", "-")
    if normalized == "zh" or normalized.startswith("zh-"):
        return "zh"
    if normalized == "en" or normalized.startswith("en-"):
        return "en"
    return "auto"


def normalize_text(
    text: str,
    *,
    language: str = "zh",
    drop_tags: tuple[str, ...] = (),
    drop_fillers: tuple[str, ...] = (),
    traditional: str = "keep",
) -> str:
    """用 WeTextProcessing ITN 归一中英文数字实体并去除比较空白。"""
    text = normalize_script(text, traditional=traditional)
    for token in (*drop_tags, *drop_fillers):
        text = text.replace(token, "")
    wetext_language = _normalizer_language(language)
    normalized = _normalizer(wetext_language).normalize(unicodedata.normalize("NFKC", text))
    return re.sub(r"\s+", "", normalized)


def extract_entities(
    text: str,
    *,
    language: str = "zh",
    drop_tags: tuple[str, ...] = (),
    drop_fillers: tuple[str, ...] = (),
    traditional: str = "keep",
) -> list[tuple[str, str]]:
    """抽取 (类型, 归一值)；重叠匹配按最长优先消费。"""
    normalized = normalize_text(
        text,
        language=language,
        drop_tags=drop_tags,
        drop_fillers=drop_fillers,
        traditional=traditional,
    )
    entities: list[tuple[str, str]] = []
    consumed: list[tuple[int, int]] = []

    def overlaps(start: int, end: int) -> bool:
        return any(start < used_end and end > used_start for used_start, used_end in consumed)

    for kind, pattern in _PATTERNS:
        for match in pattern.finditer(normalized):
            start, end = match.span()
            if overlaps(start, end):
                continue
            if kind == "date":
                value = f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
            elif kind == "time":
                value = f"{int(match.group(1)):02d}:{int(match.group(2)):02d}"
            elif kind == "percent":
                value = f"{float(match.group(1)):.4f}%"
            elif kind == "money":
                amount = match.group("prefix_value") or match.group("plain_value")
                assert amount is not None
                scale = match.group("scale") or ""
                currency = match.group("currency")
                plain_currency = match.group("plain_currency")
                multiplier = {"万": 10000, "亿": 100000000}.get(scale, 1)
                currency_code = "USD" if currency == "$" or plain_currency == "美元" else "CNY"
                value = f"{currency_code}:{float(amount) * multiplier:.2f}"
            elif kind == "unit":
                value = f"{float(match.group(1)):.4f}{match.group(2).casefold()}"
            else:
                value = f"{float(match.group(0)):.4f}"
            consumed.append((start, end))
            entities.append((kind, value))
    return entities


def score_itn(
    ref_itn: str,
    hyp: str,
    *,
    language: str = "zh",
    drop_tags: tuple[str, ...] = (),
    drop_fillers: tuple[str, ...] = (),
    traditional: str = "keep",
) -> ItnResult:
    """比较 ref.itn_expected 与 hyp 的实体集合。"""
    ref_entities = extract_entities(
        ref_itn,
        language=language,
        drop_tags=drop_tags,
        drop_fillers=drop_fillers,
        traditional=traditional,
    )
    if not ref_entities:
        return ItnResult(total=0, correct=0, by_type={})
    hyp_entities = Counter(
        extract_entities(
            hyp,
            language=language,
            drop_tags=drop_tags,
            drop_fillers=drop_fillers,
            traditional=traditional,
        )
    )
    counts: dict[str, list[int]] = {}
    for entity in ref_entities:
        kind, _value = entity
        kind_counts = counts.setdefault(kind, [0, 0])
        kind_counts[0] += 1
        if hyp_entities.get(entity, 0) > 0:
            hyp_entities[entity] -= 1
            kind_counts[1] += 1
    return ItnResult(
        total=len(ref_entities),
        correct=sum(correct for _total, correct in counts.values()),
        by_type={kind: (total, correct) for kind, (total, correct) in counts.items()},
    )


def aggregate(rows: list[ItnResult]) -> ItnSummary:
    total = sum(row.total for row in rows)
    correct = sum(row.correct for row in rows)
    by_type: dict[str, list[int]] = {}
    for row in rows:
        for kind, (kind_total, kind_correct) in (row.by_type or {}).items():
            counts = by_type.setdefault(kind, [0, 0])
            counts[0] += kind_total
            counts[1] += kind_correct
    return {
        "value": (correct / total) if total else None,
        "correct": correct,
        "total": total,
        "n": total,
        "by_type": {
            kind: {
                "correct": kind_correct,
                "total": kind_total,
                "accuracy": kind_correct / kind_total,
            }
            for kind, (kind_total, kind_correct) in by_type.items()
        },
    }
