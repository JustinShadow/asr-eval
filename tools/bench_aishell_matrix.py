#!/usr/bin/env python3
"""用 AISHELL-1 指定 split 对多台真机执行隔离的流式 CER 矩阵。"""

import argparse
import asyncio
import json
import math
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "backend"))

from bench_stream import _audio_duration_ms, _parse_device, _percentile  # noqa: E402

from app.adapters.etsstt import TranscribeOutcome, transcribe_file  # noqa: E402
from app.scoring.cer import cer  # noqa: E402


@dataclass(frozen=True)
class Target:
    """一台设备的显示名、WS 地址、模型键与发送模式。"""

    name: str
    host: str
    port: int
    model: str
    mode: str


@dataclass(frozen=True)
class Sample:
    """AISHELL-1 的只读音频路径与对应参考文本。"""

    item_key: str
    audio_path: Path
    reference: str


@dataclass(frozen=True)
class CaseResult:
    """单条识别的安全摘要，不保留音频或转写原文。"""

    item_key: str
    ok: bool
    wall_ms: int
    audio_duration_ms: int
    ttft_ms: int | None
    finalize_ms: int | None
    rtf_dev: float | None
    errors: int | None
    reference_chars: int | None
    cer: float | None
    error: str | None


def _parse_target(value: str) -> Target:
    """解析 `NAME=HOST:PORT,MODEL[,MODE]` 真机目标定义。"""
    name, separator, details = value.partition("=")
    fields = details.split(",")
    if not separator or not name.strip() or len(fields) not in {2, 3} or not fields[1].strip():
        raise argparse.ArgumentTypeError("--target 必须采用 NAME=HOST:PORT,MODEL[,MODE] 格式")
    endpoint, model = fields[0], fields[1].strip()
    mode = fields[2].strip() if len(fields) == 3 else "stream"
    if mode not in {"burst", "stream"}:
        raise argparse.ArgumentTypeError("--target 模式只能是 burst 或 stream")
    try:
        host, port = _parse_device(endpoint)
    except argparse.ArgumentTypeError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return Target(name.strip(), host, port, model, mode)


def _load_samples(dataset_root: Path, subset: str, limit: int) -> list[Sample]:
    """先索引 split 音频名，再按参考顺序取前 N 条，不复制源数据。"""
    transcript_path = dataset_root / "transcript" / "aishell_transcript_v0.8.txt"
    audio_root = dataset_root / "wav" / subset
    if not transcript_path.is_file():
        raise FileNotFoundError(f"AISHELL-1 参考文本不存在：{transcript_path}")
    if not audio_root.is_dir():
        raise FileNotFoundError(f"AISHELL-1 split 不存在：{audio_root}")
    if limit < 1:
        raise ValueError("样本数量必须大于 0")

    # F 盘经 WSL 9P 挂载；先顺序枚举目录可避免为训练集的每行参考触发一次远端 stat。
    audio_by_key = {audio_path.stem: audio_path for audio_path in audio_root.glob("S*/*.wav")}
    samples: list[Sample] = []
    with transcript_path.open(encoding="utf-8") as transcript_file:
        for line in transcript_file:
            fields = line.strip().split(maxsplit=1)
            if len(fields) != 2:
                continue
            item_key, reference = fields[0], fields[1].strip()
            audio_path = audio_by_key.get(item_key)
            if not reference or audio_path is None:
                continue
            samples.append(Sample(item_key, audio_path, reference))
            if len(samples) == limit:
                break
    if len(samples) != limit:
        raise ValueError(
            f"{subset} split 只找到 {len(samples)} 条带参考音频，少于要求的 {limit} 条"
        )
    return samples


def _device_rtf(outcome: TranscribeOutcome, audio_duration_ms: int) -> float | None:
    """按 M4/M5 口径计算设备 RTF，不读取 usage.rtf。"""
    inference_ms: Any = outcome.usage.get("inference_ms")
    if (
        isinstance(inference_ms, bool)
        or not isinstance(inference_ms, (int, float))
        or not math.isfinite(float(inference_ms))
        or inference_ms < 0
        or audio_duration_ms <= 0
    ):
        return None
    return float(inference_ms) / audio_duration_ms


def _case_result(
    sample: Sample,
    outcome: TranscribeOutcome,
    *,
    wall_ms: int,
    audio_duration_ms: int,
) -> CaseResult:
    """以项目 CER 口径评分并剔除转写原文。"""
    if not outcome.ok:
        return CaseResult(
            sample.item_key,
            False,
            wall_ms,
            audio_duration_ms,
            outcome.ttft_ms,
            outcome.finalize_ms,
            None,
            None,
            None,
            None,
            (outcome.error or "设备转写失败")[:240],
        )
    score = cer(sample.reference, outcome.text)
    return CaseResult(
        sample.item_key,
        True,
        wall_ms,
        audio_duration_ms,
        outcome.ttft_ms,
        outcome.finalize_ms,
        _device_rtf(outcome, audio_duration_ms),
        score.edits,
        score.n,
        score.value,
        None,
    )


async def _run_case(
    target: Target,
    sample: Sample,
    *,
    timeout_s: float,
    chunk_ms: int,
) -> CaseResult:
    """按目标配置在单台设备上执行一条识别。"""
    audio_duration_ms = _audio_duration_ms(sample.audio_path)
    started = time.perf_counter()
    try:
        outcome = await transcribe_file(
            target.host,
            target.port,
            target.model,
            str(sample.audio_path),
            timeout_s=timeout_s,
            mode=target.mode,
            chunk_ms=chunk_ms,
            input_rate=1.0,
        )
    except Exception as exc:
        return CaseResult(
            sample.item_key,
            False,
            round((time.perf_counter() - started) * 1000),
            audio_duration_ms,
            None,
            None,
            None,
            None,
            None,
            None,
            f"{type(exc).__name__}: {exc}"[:240],
        )
    wall_ms = round((time.perf_counter() - started) * 1000)
    return _case_result(sample, outcome, wall_ms=wall_ms, audio_duration_ms=audio_duration_ms)


def _summarize_target(target: Target, cases: list[CaseResult]) -> dict[str, Any]:
    """汇总 micro CER、成功率和流式性能分位数。"""
    successful = [case for case in cases if case.ok]
    scored = [case for case in successful if case.errors is not None and case.reference_chars]
    errors = sum(case.errors or 0 for case in scored)
    reference_chars = sum(case.reference_chars or 0 for case in scored)
    ttft = [float(case.ttft_ms) for case in successful if case.ttft_ms is not None]
    finalize = [float(case.finalize_ms) for case in successful if case.finalize_ms is not None]
    rtf = [case.rtf_dev for case in successful if case.rtf_dev is not None]
    return {
        "target": asdict(target),
        "samples": len(cases),
        "successes": len(successful),
        "success_rate": len(successful) / len(cases) if cases else 0.0,
        "cer": errors / reference_chars if reference_chars else None,
        "errors": errors,
        "reference_chars": reference_chars,
        "ttft_ms_median": statistics.median(ttft) if ttft else None,
        "ttft_ms_p95": _percentile(ttft, 0.95),
        "finalize_ms_median": statistics.median(finalize) if finalize else None,
        "finalize_ms_p95": _percentile(finalize, 0.95),
        "rtf_dev_median": statistics.median(rtf) if rtf else None,
        "rtf_dev_p95": _percentile(rtf, 0.95),
        "wall_ms_median": statistics.median([case.wall_ms for case in cases]) if cases else None,
        "cases": [asdict(case) for case in cases],
    }


async def _run_target(
    target: Target,
    samples: list[Sample],
    *,
    timeout_s: float,
    chunk_ms: int,
    progress_every: int,
) -> dict[str, Any]:
    """单台设备最多保持一路会话，避免绕开整机独占约束。"""
    cases: list[CaseResult] = []
    for index, sample in enumerate(samples, start=1):
        cases.append(await _run_case(target, sample, timeout_s=timeout_s, chunk_ms=chunk_ms))
        if index % progress_every == 0 or index == len(samples):
            print(f"{target.name}: {index}/{len(samples)}", file=sys.stderr, flush=True)
    return _summarize_target(target, cases)


def _parser() -> argparse.ArgumentParser:
    """创建 AISHELL-1 多设备矩阵参数。"""
    parser = argparse.ArgumentParser(
        description="使用只读 AISHELL-1 音频按设备顺序执行 1× 流式 CER 矩阵。"
    )
    parser.add_argument("--dataset-root", required=True, type=Path, help="AISHELL-1 data_aishell")
    parser.add_argument("--subset", choices=("train", "test"), default="test")
    parser.add_argument("--limit", type=int, default=100, help="每台设备样本数")
    parser.add_argument(
        "--target",
        action="append",
        type=_parse_target,
        required=True,
        help="重复指定 NAME=HOST:PORT,MODEL[,MODE]；默认 stream；每台设备严格单路",
    )
    parser.add_argument("--chunk-ms", type=int, default=30)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--progress-every", type=int, default=10)
    parser.add_argument("--save", type=Path, help="可选 JSON 汇总路径；不含原文和音频")
    return parser


async def _run(args: argparse.Namespace, samples: list[Sample]) -> dict[str, Any]:
    """跨设备并行运行矩阵，但每台设备内部按样本顺序单路执行。"""
    results = await asyncio.gather(
        *(
            _run_target(
                target,
                samples,
                timeout_s=args.timeout,
                chunk_ms=args.chunk_ms,
                progress_every=args.progress_every,
            )
            for target in args.target
        )
    )
    return {
        "dataset": "AISHELL-1",
        "subset": args.subset,
        "sample_count": len(samples),
        "profile": {
            "input_rate": 1.0,
            "chunk_ms": args.chunk_ms,
            "timeout_s": args.timeout,
            "per_device_concurrency": 1,
        },
        "targets": results,
    }


def _print_summary(result: dict[str, Any]) -> None:
    """打印跨设备 CER 与时延矩阵。"""
    print(
        "设备 | 模式 | 成功率 | micro CER | 字符数 | TTFT median/P95 ms | "
        "finalize median/P95 ms | RTF_dev median/P95"
    )
    print("--- | --- | ---: | ---: | ---: | ---: | ---: | ---:")
    for target in result["targets"]:
        cer_value = target["cer"]
        ttft = target["ttft_ms_median"]
        ttft_p95 = target["ttft_ms_p95"]
        finalize = target["finalize_ms_median"]
        finalize_p95 = target["finalize_ms_p95"]
        rtf = target["rtf_dev_median"]
        rtf_p95 = target["rtf_dev_p95"]
        cer_display = f"{cer_value:.2%}" if cer_value is not None else "N/A"
        row = (
            f"{target['target']['name']} | {target['target']['mode']} | "
            f"{target['successes']}/{target['samples']} | {cer_display} | "
            f"{target['reference_chars']}"
        )
        if all(
            value is not None for value in (ttft, ttft_p95, finalize, finalize_p95, rtf, rtf_p95)
        ):
            row += (
                f" | {ttft:.0f}/{ttft_p95:.0f} | {finalize:.0f}/{finalize_p95:.0f} | "
                f"{rtf:.3f}/{rtf_p95:.3f}"
            )
        else:
            row += " | N/A | N/A | N/A"
        print(row)


def main() -> int:
    """校验数据与目标，执行矩阵并输出不含音频和转写原文的 JSON。"""
    parser = _parser()
    args = parser.parse_args()
    if args.limit < 1 or args.chunk_ms < 1 or args.timeout <= 0 or args.progress_every < 1:
        parser.error("样本数、分片、超时和进度间隔必须大于 0")
    target_names = [target.name for target in args.target]
    if len(set(target_names)) != len(target_names):
        parser.error("--target 名称不能重复")
    try:
        samples = _load_samples(args.dataset_root, args.subset, args.limit)
        for sample in samples:
            _audio_duration_ms(sample.audio_path)
        result = asyncio.run(_run(args, samples))
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"AISHELL-1 矩阵运行失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    _print_summary(result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.save is not None:
        args.save.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return 0 if all(target["successes"] == target["samples"] for target in result["targets"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
