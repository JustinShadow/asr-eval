#!/usr/bin/env python3
"""按并发档测量设备 1× 流式转写时延、RTF 与吞吐。"""

import argparse
import asyncio
import hashlib
import json
import math
import statistics
import sys
import time
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "backend"))

from app.adapters.etsstt import TranscribeOutcome, transcribe_file  # noqa: E402


@dataclass(frozen=True)
class AttemptResult:
    """一个并发连接的安全性能摘要。"""

    ok: bool
    wall_ms: int
    ttft_ms: int | None
    finalize_ms: int | None
    rtf_dev: float | None
    partial_count: int
    segment_count: int
    error: str | None


@dataclass(frozen=True)
class WaveResult:
    """一次并发档运行的汇总。"""

    wall_ms: int
    audio_s_per_s: float
    attempts: list[AttemptResult]


def _parse_device(value: str) -> tuple[str, int]:
    """解析 host:port 格式的 ASR 设备地址。"""
    host, separator, raw_port = value.rpartition(":")
    if not separator or not host or not raw_port.isdecimal():
        raise argparse.ArgumentTypeError("--device 必须采用 host:port 格式")
    port = int(raw_port)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("--device 端口必须在 1 到 65535 之间")
    if any(character.isspace() for character in host) or "/" in host:
        raise argparse.ArgumentTypeError("--device 主机地址格式无效")
    return host, port


def _concurrency_levels(minimum: int, maximum: int) -> list[int]:
    """验证并发档范围并返回要执行的并发级别。"""
    if minimum < 1 or maximum < minimum:
        raise argparse.ArgumentTypeError(
            "并发档必须满足 1 <= --min-concurrency <= --max-concurrency"
        )
    return list(range(minimum, maximum + 1))


def _audio_duration_ms(path: Path) -> int:
    """仅读 WAV 头并验证设备支持的 16kHz、16-bit、单声道输入。"""
    with wave.open(str(path), "rb") as audio:
        if audio.getframerate() != 16000 or audio.getnchannels() != 1:
            raise ValueError("基准音频必须为 16kHz、单声道")
        if audio.getsampwidth() != 2:
            raise ValueError("基准音频必须为 16-bit PCM")
        return round(audio.getnframes() * 1000 / audio.getframerate())


def _sha256(path: Path) -> str:
    """流式计算输入音频指纹，不复制或修改源文件。"""
    digest = hashlib.sha256()
    with path.open("rb") as audio:
        for chunk in iter(lambda: audio.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _percentile(values: list[float], percentile: float) -> float | None:
    """用线性插值计算稳定的样本分位数。"""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


async def _attempt(
    host: str,
    port: int,
    model: str,
    audio_path: Path,
    *,
    timeout_s: float,
    chunk_ms: int,
    diarize: bool,
    input_rate: float,
) -> AttemptResult:
    """以指定回放速率发起一个独立 WS 会话；1× 为实时口径。"""
    started = time.perf_counter()
    try:
        outcome = await transcribe_file(
            host,
            port,
            model,
            str(audio_path),
            timeout_s=timeout_s,
            diarization=diarize,
            max_num_spks=2,
            mode="stream",
            chunk_ms=chunk_ms,
            input_rate=input_rate,
        )
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        return AttemptResult(
            ok=False,
            wall_ms=elapsed_ms,
            ttft_ms=None,
            finalize_ms=None,
            rtf_dev=None,
            partial_count=0,
            segment_count=0,
            error=type(exc).__name__,
        )
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    return _attempt_result(outcome, audio_path, wall_ms=elapsed_ms)


def _attempt_result(outcome: TranscribeOutcome, audio_path: Path, *, wall_ms: int) -> AttemptResult:
    """计算设备 RTF，并丢弃转写内容及声纹细节。"""
    duration_ms = _audio_duration_ms(audio_path)
    inference_ms: Any = outcome.usage.get("inference_ms")
    rtf_dev = (
        float(inference_ms) / duration_ms
        if isinstance(inference_ms, (int, float))
        and not isinstance(inference_ms, bool)
        and math.isfinite(float(inference_ms))
        and inference_ms >= 0
        and duration_ms > 0
        else None
    )
    return AttemptResult(
        ok=outcome.ok,
        wall_ms=wall_ms,
        ttft_ms=outcome.ttft_ms,
        finalize_ms=outcome.finalize_ms,
        rtf_dev=rtf_dev,
        partial_count=outcome.message_counts.get("transcript.partial", 0),
        segment_count=outcome.segment_count,
        error=outcome.error[:240] if outcome.error else None,
    )


async def _run_wave(
    host: str,
    port: int,
    model: str,
    audio_path: Path,
    duration_ms: int,
    concurrency: int,
    *,
    timeout_s: float,
    chunk_ms: int,
    diarize: bool,
    input_rate: float,
) -> WaveResult:
    """并发启动独立连接，并测量该档整体处理时间。"""
    started = time.perf_counter()
    attempts = await asyncio.gather(
        *(
            _attempt(
                host,
                port,
                model,
                audio_path,
                timeout_s=timeout_s,
                chunk_ms=chunk_ms,
                diarize=diarize,
                input_rate=input_rate,
            )
            for _index in range(concurrency)
        )
    )
    wall_ms = max(1, round((time.perf_counter() - started) * 1000))
    successful = sum(result.ok for result in attempts)
    audio_s_per_s = successful * duration_ms / 1000 / (wall_ms / 1000)
    return WaveResult(
        wall_ms=wall_ms,
        audio_s_per_s=audio_s_per_s,
        attempts=attempts,
    )


def _summarize_level(
    concurrency: int,
    warmup: WaveResult,
    repeats: list[WaveResult],
) -> dict[str, Any]:
    """汇总某一并发档的成功率、median / P95 与吞吐。"""
    attempts = [attempt for wave_result in repeats for attempt in wave_result.attempts]
    successful = [attempt for attempt in attempts if attempt.ok]
    ttft = [float(attempt.ttft_ms) for attempt in successful if attempt.ttft_ms is not None]
    finalize = [
        float(attempt.finalize_ms) for attempt in successful if attempt.finalize_ms is not None
    ]
    rtf = [float(attempt.rtf_dev) for attempt in successful if attempt.rtf_dev is not None]
    return {
        "concurrency": concurrency,
        "warmup_successes": sum(attempt.ok for attempt in warmup.attempts),
        "warmup_sessions": len(warmup.attempts),
        "warmup_success_rate": (
            sum(attempt.ok for attempt in warmup.attempts) / len(warmup.attempts)
            if warmup.attempts
            else 0.0
        ),
        "successes": len(successful),
        "sessions": len(attempts),
        "success_rate": len(successful) / len(attempts) if attempts else 0.0,
        "ttft_ms_median": statistics.median(ttft) if ttft else None,
        "ttft_ms_p95": _percentile(ttft, 0.95),
        "finalize_ms_median": statistics.median(finalize) if finalize else None,
        "finalize_ms_p95": _percentile(finalize, 0.95),
        "rtf_dev_median": statistics.median(rtf) if rtf else None,
        "rtf_dev_p95": _percentile(rtf, 0.95),
        "audio_s_per_s_median": statistics.median(
            [wave_result.audio_s_per_s for wave_result in repeats]
        ),
        "warmup_attempts": [asdict(attempt) for attempt in warmup.attempts],
        "waves": [
            {
                "wall_ms": wave_result.wall_ms,
                "audio_s_per_s": wave_result.audio_s_per_s,
                "attempts": [asdict(attempt) for attempt in wave_result.attempts],
            }
            for wave_result in repeats
        ],
    }


def _level_passed(level: dict[str, Any]) -> bool:
    """档位仅在预热和所有重复连接都成功时通过。"""
    return bool(
        level["warmup_successes"] == level["warmup_sessions"] and level["success_rate"] == 1.0
    )


def _parser() -> argparse.ArgumentParser:
    """创建 1× 流式并发基准参数。"""
    parser = argparse.ArgumentParser(
        description="对设备执行 warmup + 并发档扫描，输出 1× 流式性能曲线。"
    )
    parser.add_argument("--device", required=True, help="ASR 地址，host:port")
    parser.add_argument("--model", required=True, help="设备 /v1/models 返回的 model key")
    parser.add_argument("--audio", required=True, type=Path, help="16kHz/16-bit/mono WAV")
    parser.add_argument("--min-concurrency", type=int, default=1, help="扫描起始并发档（默认 1）")
    parser.add_argument("--max-concurrency", type=int, default=3)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--chunk-ms", type=int, default=30)
    parser.add_argument(
        "--input-rate",
        type=float,
        default=1.0,
        help="回放速率；1× 为实时口径，>1 用于长音频加速（结果需注明）",
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--diarize", action="store_true")
    parser.add_argument("--save", type=Path, help="可选 JSON 输出文件")
    return parser


async def _run(
    args: argparse.Namespace,
    host: str,
    port: int,
    duration_ms: int,
    concurrency_levels: list[int],
) -> dict[str, Any]:
    """按指定并发范围逐档预热，再重复测量。"""
    levels: list[dict[str, Any]] = []
    for concurrency in concurrency_levels:
        print(f"warmup concurrency={concurrency}", file=sys.stderr, flush=True)
        warmup = await _run_wave(
            host,
            port,
            args.model,
            args.audio,
            duration_ms,
            concurrency,
            timeout_s=args.timeout,
            chunk_ms=args.chunk_ms,
            diarize=args.diarize,
            input_rate=args.input_rate,
        )
        repeats: list[WaveResult] = []
        for repeat in range(1, args.repeat + 1):
            print(f"measure concurrency={concurrency} repeat={repeat}", file=sys.stderr, flush=True)
            repeats.append(
                await _run_wave(
                    host,
                    port,
                    args.model,
                    args.audio,
                    duration_ms,
                    concurrency,
                    timeout_s=args.timeout,
                    chunk_ms=args.chunk_ms,
                    diarize=args.diarize,
                    input_rate=args.input_rate,
                )
            )
        levels.append(_summarize_level(concurrency, warmup, repeats))
    return {
        "device": f"{host}:{port}",
        "model": args.model,
        "audio": str(args.audio.resolve()),
        "audio_sha256": _sha256(args.audio),
        "audio_duration_ms": duration_ms,
        "profile": {
            "mode": "stream",
            "input_rate": args.input_rate,
            "chunk_ms": args.chunk_ms,
            "repeat": args.repeat,
            "warmups_per_level": 1,
            "min_concurrency": args.min_concurrency,
            "max_concurrency": args.max_concurrency,
            "diarization": args.diarize,
        },
        "levels": levels,
    }


def _print_table(result: dict[str, Any]) -> None:
    """打印可直接比较的并发-时延-RTF-成功率表。"""
    print(
        "并发 | TTFT median/P95 ms | finalize median/P95 ms | RTF_dev median/P95 | "
        "预热成功率 | 成功率 | audio-s/s"
    )
    print("---: | ---: | ---: | ---: | ---: | ---: | ---:")
    for level in result["levels"]:
        ttft = (
            f"{level['ttft_ms_median']:.0f}/{level['ttft_ms_p95']:.0f}"
            if level["ttft_ms_median"] is not None and level["ttft_ms_p95"] is not None
            else "N/A"
        )
        finalize = (
            f"{level['finalize_ms_median']:.0f}/{level['finalize_ms_p95']:.0f}"
            if level["finalize_ms_median"] is not None and level["finalize_ms_p95"] is not None
            else "N/A"
        )
        rtf = (
            f"{level['rtf_dev_median']:.3f}/{level['rtf_dev_p95']:.3f}"
            if level["rtf_dev_median"] is not None and level["rtf_dev_p95"] is not None
            else "N/A"
        )
        print(
            f"{level['concurrency']} | {ttft} | {finalize} | {rtf} | "
            f"{level['warmup_success_rate']:.1%} ({level['warmup_successes']}/"
            f"{level['warmup_sessions']}) | "
            f"{level['success_rate']:.1%} ({level['successes']}/{level['sessions']}) | "
            f"{level['audio_s_per_s_median']:.2f}"
        )


def main() -> int:
    """校验音频和设备参数，执行并发基准并输出 JSON 与表格。"""
    parser = _parser()
    args = parser.parse_args()
    try:
        host, port = _parse_device(args.device)
        duration_ms = _audio_duration_ms(args.audio)
    except (argparse.ArgumentTypeError, OSError, ValueError) as exc:
        parser.error(str(exc))
    if duration_ms <= 0:
        parser.error("--audio 时长必须大于 0")
    try:
        concurrency_levels = _concurrency_levels(args.min_concurrency, args.max_concurrency)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    if args.repeat < 1 or args.chunk_ms < 1 or args.timeout <= 0:
        parser.error("并发档、重复次数、分片和超时必须大于 0")
    try:
        result = asyncio.run(_run(args, host, port, duration_ms, concurrency_levels))
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"基准运行失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    _print_table(result)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if args.save is not None:
        args.save.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return 0 if all(_level_passed(level) for level in result["levels"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
