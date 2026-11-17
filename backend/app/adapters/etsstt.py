"""etsstt / gateway 的协议探测实现。

包含 M2 的 probe_lite 与实时转写协议；探测音频由领域层按设计素材提供。
真实协议细节见 docs/m2-device-design.md 与 docs/asr-eval-verification.md。
"""

import asyncio
import json
import math
import re
import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import websockets


@dataclass
class ProbeOutcome:
    """一次 lite 探测的结果；失败时 error 非空。"""

    ok: bool
    model_key: str | None = None
    accel: dict[str, Any] = field(default_factory=dict)
    declared: dict[str, Any] = field(default_factory=dict)
    init_latency_ms: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class VoiceprintOutcome:
    """设备声纹提取结果；向量无效时不返回部分数据。"""

    ok: bool
    embedding: list[float] = field(default_factory=list)
    sample_duration_ms: int | None = None
    error: str | None = None


@dataclass
class IdentityOutcome:
    """一次不建立 ASR 会话的设备身份采集结果。"""

    ok: bool
    identity: dict[str, Any] = field(default_factory=dict)
    sn: str | None = None
    soc: str | None = None
    quant: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    version_collection_ok: bool = False
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


async def extract_voiceprint(
    host: str,
    port: int,
    model_key: str,
    remote_path: str,
    voiceprint_id: str,
    duration_ms: int,
    timeout_s: float = 15.0,
) -> VoiceprintOutcome:
    """从设备侧音频路径提取 192 维声纹（M2 §5.2.1）。"""
    body = {
        "model": model_key,
        "file_path": remote_path,
        "timestamp_ranges": [
            {"start": 0, "end": duration_ms, "id": voiceprint_id},
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            response = await client.post(f"http://{host}:{port}/v1/audio/voiceprints", json=body)
            response.raise_for_status()
        payload = response.json()
        rows = payload.get("voiceprints") if isinstance(payload, dict) else None
        match = next(
            (row for row in rows or [] if isinstance(row, dict) and row.get("id") == voiceprint_id),
            None,
        )
        if match is None:
            return VoiceprintOutcome(ok=False, error="设备未返回请求的声纹片段")
        embedding = match.get("embedding")
        if not isinstance(embedding, list) or len(embedding) != 192:
            return VoiceprintOutcome(ok=False, error="设备未返回 192 维声纹向量")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in embedding
        ):
            return VoiceprintOutcome(ok=False, error="设备返回的声纹向量含无效数值")
        sample_duration = match.get("sample_duration")
        if not isinstance(sample_duration, int) or sample_duration <= 0:
            sample_duration = duration_ms
        return VoiceprintOutcome(
            ok=True,
            embedding=[float(value) for value in embedding],
            sample_duration_ms=sample_duration,
        )
    except Exception as exc:
        return VoiceprintOutcome(ok=False, error=f"{type(exc).__name__}: {exc}")


async def probe_identity(
    host: str,
    port: int,
    *,
    kind: str,
    platform: str,
    ssh_user: str | None,
    ssh_port: int,
    ssh_password: str | None = None,
    ssh_private_key: str | None = None,
    ssh_passphrase: str | None = None,
    ssh_host: str | None = None,
    system_info_host: str | None = None,
    timeout_s: float = 5.0,
) -> IdentityOutcome:
    """采集部署身份；全程不创建 WebSocket 或转写会话（M2 §5.1.1）。"""
    if kind not in {"etsstt", "gateway"}:
        return IdentityOutcome(ok=False, error="云端设备暂不支持本地设备身份检查")

    models_result, info_result, get_version_result, etsllm_result = await asyncio.gather(
        _capture(_fetch_models(host, port, timeout_s)),
        _capture(_fetch_system_info(system_info_host or host, _system_info_port(kind), timeout_s)),
        _capture(
            asyncio.to_thread(
                _run_identity_command,
                ssh_host or host,
                kind,
                ssh_user,
                ssh_port,
                ("get_version",),
                ssh_password,
                ssh_private_key,
                ssh_passphrase,
            )
        ),
        _capture(
            asyncio.to_thread(
                _run_identity_command,
                ssh_host or host,
                kind,
                ssh_user,
                ssh_port,
                ("etsllm", "--version"),
                ssh_password,
                ssh_private_key,
                ssh_passphrase,
            )
        ),
    )
    models, models_error = models_result
    system_info, system_info_error = info_result
    get_version_output, get_version_error = get_version_result
    etsllm_output, etsllm_error = etsllm_result
    version_fields = _parse_get_version(get_version_output or "")

    # 205 等设备的 HTTP 模型目录可能未开放；版本命令仍能给出型号与大版本时，
    # 不应让整个身份采集失败（M2 §5.1.1 的降级要求）。
    version_fallback_available = bool(
        version_fields.get("model_key")
        or version_fields.get("bigversion")
        or version_fields.get("model_version")
    )
    if (models_error is not None or system_info_error is not None) and not (
        kind == "etsstt" and version_fallback_available
    ):
        failures = [
            message
            for message in (
                f"/v1/models 读取失败：{models_error}" if models_error else None,
                f"/v1/system/info 读取失败：{system_info_error}" if system_info_error else None,
            )
            if message
        ]
        return IdentityOutcome(
            ok=False,
            raw={
                "models_error": models_error,
                "system_info_error": system_info_error,
                "get_version_error": get_version_error,
                "etsllm_version_error": etsllm_error,
            },
            error="；".join(failures),
        )

    model: dict[str, Any] = {}
    if isinstance(models, list) and models:
        try:
            model = _pick_stt_model(models)
        except ValueError as exc:
            if not (kind == "etsstt" and version_fallback_available):
                return IdentityOutcome(
                    ok=False,
                    raw={"models": models, "system_info": system_info},
                    error=str(exc),
                )
    system_info = system_info if isinstance(system_info, dict) else {}
    system = system_info.get("system")
    system = system if isinstance(system, dict) else {}
    extra = model.get("extra_info")
    extra = extra if isinstance(extra, dict) else {}
    model_key = _first_text(
        model.get("invocation_name"),
        model.get("id"),
        version_fields.get("model_key"),
        "etsstt" if kind == "etsstt" else None,
    )
    if model_key is None:
        return IdentityOutcome(
            ok=False,
            raw={"models": models, "system_info": system_info},
            error="/v1/models 中的 STT 模型缺少 invocation_name 和 id",
        )
    model_version = _first_text(
        _find_value(extra, {"model_version", "modelversion"}),
        version_fields.get("model_version"),
    )
    if model_version is None and kind == "etsstt":
        # 205 的 get_version 用组件键暴露 ASR 模型版本，例如 model-etsstt_onnx。
        model_version = _first_text(
            version_fields.get("model-etsstt_onnx"),
            version_fields.get("Model_ASR_GEN_A"),
        )
    accel_value = extra.get("accel")
    accel = _normalize_accel(accel_value)
    version_accel = version_fields.get("accel")
    if isinstance(version_accel, dict):
        accel = {**version_accel, **accel}
    elif isinstance(version_accel, str) and version_accel.strip():
        accel.setdefault("runtime", version_accel.strip())

    resolved_platform = _resolved_platform(
        system.get("platform"), version_fields.get("platform"), platform
    )
    sn = _first_text(
        _find_value(system_info, {"sn", "serial", "serial_number", "serialnumber", "device_sn"})
    )
    model_type = _first_text(
        _find_value(
            system_info,
            {"model_type", "modeltype", "support_model_type", "supportmodeltype"},
        ),
        version_fields.get("soc"),
    )
    soc = model_type.lower() if model_type else _model_soc(model_key)
    quant = _first_text(version_fields.get("quant")) or _model_quant(model_key)
    bigversion = _first_text(system.get("version"), version_fields.get("bigversion"))
    etsllm_version = _version_from_output(etsllm_output or "")
    if etsllm_version is None:
        etsllm_version = _first_text(version_fields.get("etsllm_version"))

    warnings = []
    if models_error:
        warnings.append(f"/v1/models 读取失败：{models_error}")
    if system_info_error:
        warnings.append(f"/v1/system/info 读取失败：{system_info_error}")
    if get_version_error:
        warnings.append(f"get_version 采集失败：{get_version_error}")
    if etsllm_error:
        warnings.append(f"etsllm --version 采集失败：{etsllm_error}")
    if bigversion is None:
        warnings.append("未能解析设备系统版本")
    if etsllm_version is None and not etsllm_error:
        warnings.append("未能解析 etsllm 版本")
    identity = {
        "platform": resolved_platform,
        "kind": kind,
        "bigversion": bigversion,
        "etsllm_version": etsllm_version,
        "accel": accel,
        "model_key": model_key,
        "model_version": model_version,
    }
    return IdentityOutcome(
        ok=True,
        identity=identity,
        sn=sn,
        soc=soc,
        quant=quant,
        version_collection_ok=not warnings,
        warnings=warnings,
        raw={
            "models": models,
            "system_info": system_info,
            "get_version": _limit_output(get_version_output),
            "etsllm_version": _limit_output(etsllm_output),
            "get_version_error": get_version_error,
            "etsllm_version_error": etsllm_error,
        },
    )


async def _capture(awaitable: Any) -> tuple[Any, str | None]:
    """把单项采集异常转换为可记录的结果，不影响其他并行采集。"""
    try:
        return await awaitable, None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


async def _fetch_system_info(host: str, port: int, timeout_s: float) -> dict[str, Any]:
    """读取平台系统信息接口。"""
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        response = await client.get(f"http://{_http_host(host)}:{port}/v1/system/info")
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("/v1/system/info 返回值不是 JSON 对象")
    return payload


def _system_info_port(kind: str) -> int:
    """按设备类型返回设计文档约定的系统信息端口。"""
    return 30010 if kind == "gateway" else 8080


def _resolved_platform(system_value: Any, version_value: Any, configured: str) -> str:
    """只接受 M2 §2.1 定义的产品平台码，避免把固件版本当平台号。"""
    for value in (system_value, version_value, configured):
        candidate = _first_text(value)
        if candidate in {"205", "600", "502"}:
            return candidate
    return configured


def _http_host(host: str) -> str:
    """为 IPv6 地址补齐 URL 所需的方括号。"""
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def _run_identity_command(
    host: str,
    kind: str,
    ssh_user: str | None,
    ssh_port: int,
    command: tuple[str, ...],
    ssh_password: str | None = None,
    ssh_private_key: str | None = None,
    ssh_passphrase: str | None = None,
) -> str:
    """执行设计中列出的固定只读命令，不接受任意远端命令。"""
    if kind == "etsstt":
        if not ssh_user or not re.fullmatch(r"[A-Za-z0-9._-]+", ssh_user):
            raise ValueError("设备未配置有效的 SSH 用户")
        if not re.fullmatch(r"[A-Za-z0-9.:-]+", host):
            raise ValueError("设备 SSH 地址格式无效")
        host_part = f"[{host}]" if ":" in host and not host.startswith("[") else host
        # SSH exec 不加载设备登录 PATH；205 上的 get_version 与 etsllm 位于登录 PATH（M2 §5.1.1）。
        login_command = f"bash -lc {shlex.quote(shlex.join(command))}"
        if ssh_password is not None or ssh_private_key is not None:
            return asyncio.run(
                _run_authenticated_ssh(
                    host,
                    ssh_port,
                    ssh_user,
                    login_command,
                    ssh_password=ssh_password,
                    ssh_private_key=ssh_private_key,
                    ssh_passphrase=ssh_passphrase,
                    timeout_s=5,
                )
            )
        remote_command = [
            "ssh",
            "-p",
            str(ssh_port),
            "-oBatchMode=yes",
            "-oConnectTimeout=3",
            "-oServerAliveInterval=2",
            "-oServerAliveCountMax=1",
            f"{ssh_user}@{host_part}",
            login_command,
        ]
    elif kind == "gateway":
        if not ssh_user or not re.fullmatch(r"[A-Za-z0-9._-]+", ssh_user):
            raise ValueError("网关未配置有效的 SSH 用户")
        if not re.fullmatch(r"[A-Za-z0-9.:-]+", host):
            raise ValueError("网关 SSH 地址格式无效")
        container_command: tuple[str, ...]
        if command == ("get_version",):
            target = "deploy/mebox"
            container_command = command
        elif command == ("etsllm", "--version"):
            target = "deploy/etsllm"
            # 502 镜像没有把 etsllm 放入 PATH；固定使用网关容器的安装路径（M2 §5.1.1）。
            container_command = ("/opt/etsme/bin/etsllm", "--version")
        else:
            raise ValueError("不支持的网关身份采集命令")
        # P0 开发环境从网关 SSH 主机访问其 kubectl 上下文（M2 §5.1.1；实现指南 §4.6）。
        gateway_command = shlex.join(
            ("sudo", "-n", "kubectl", "exec", "-n", "ns-meagt", target, "--", *container_command)
        )
        if ssh_password is not None or ssh_private_key is not None:
            return asyncio.run(
                _run_authenticated_ssh(
                    host,
                    ssh_port,
                    ssh_user,
                    gateway_command,
                    ssh_password=ssh_password,
                    ssh_private_key=ssh_private_key,
                    ssh_passphrase=ssh_passphrase,
                    timeout_s=5,
                )
            )
        host_part = f"[{host}]" if ":" in host and not host.startswith("[") else host
        remote_command = [
            "ssh",
            "-p",
            str(ssh_port),
            "-oBatchMode=yes",
            "-oConnectTimeout=3",
            "-oServerAliveInterval=2",
            "-oServerAliveCountMax=1",
            f"{ssh_user}@{host_part}",
            gateway_command,
        ]
    else:
        raise ValueError("不支持的设备类型")

    result = subprocess.run(  # noqa: S603 - 命令由固定字段组成且不经过 shell。
        remote_command,
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result.stdout.strip()


async def _run_authenticated_ssh(
    host: str,
    port: int,
    username: str,
    command: str,
    *,
    ssh_password: str | None,
    ssh_private_key: str | None,
    ssh_passphrase: str | None,
    timeout_s: float,
) -> str:
    """用 AsyncSSH 传递解密凭据，不写临时密钥文件或命令行参数。"""
    import asyncssh

    connect_options: dict[str, Any] = {
        "port": port,
        "username": username,
        "connect_timeout": timeout_s,
    }
    try:
        if ssh_private_key is not None:
            connect_options["client_keys"] = [
                asyncssh.import_private_key(ssh_private_key, passphrase=ssh_passphrase)
            ]
            connect_options["password_auth"] = False
        elif ssh_password is not None:
            connect_options["password"] = ssh_password
            connect_options["client_keys"] = []
        async with asyncssh.connect(host, **connect_options) as connection:
            result = await connection.run(command, check=True, timeout=timeout_s)
            return str(result.stdout).strip()
    except Exception as exc:
        # AsyncSSH 异常原文不进入 ProbeRun、Job 或日志，避免意外携带连接参数。
        raise RuntimeError(f"SSH {type(exc).__name__}") from None


def _parse_get_version(output: str) -> dict[str, Any]:
    """解析 get_version 的 JSON 或键值文本，只识别明确字段。"""
    try:
        payload = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        payload = {}
        for line in output.splitlines():
            match = re.match(r"\s*([A-Za-z][A-Za-z0-9_-]*)\s*[:=]\s*(.*?)\s*$", line)
            if match:
                payload[match.group(1)] = match.group(2)
    if not isinstance(payload, dict):
        return {}
    return {
        "platform": _find_value(payload, {"platform"}),
        "bigversion": _find_value(payload, {"bigversion", "big_version", "system_version"}),
        "etsllm_version": _find_value(payload, {"etsllm_version", "etsllmversion"}),
        "model_key": _first_text(
            _find_value(payload, {"model_key", "modelkey", "model_name", "modelname", "model"})
        ),
        "model_version": _find_value(
            payload,
            {
                "model_version",
                "modelversion",
                "model-etsstt_onnx",
                "Model_ASR_GEN_A",
            },
        ),
        "accel": _find_value(payload, {"accel", "accelerator"}),
        "quant": _find_value(payload, {"quant", "quantization"}),
        "soc": _find_value(payload, {"soc", "model_type", "support_model_type"}),
    }


def _find_value(value: Any, keys: set[str]) -> Any:
    """在嵌套字典中按大小写和分隔符无关的键名查找首个非空值。"""
    if isinstance(value, dict):
        normalized_keys = {re.sub(r"[^a-z0-9]", "", key.lower()) for key in keys}
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in normalized_keys and item is not None and item != "":
                return item
        for item in value.values():
            found = _find_value(item, keys)
            if found is not None and found != "":
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_value(item, keys)
            if found is not None and found != "":
                return found
    return None


def _first_text(*values: Any) -> str | None:
    """返回首个非空字符串值。"""
    for value in values:
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            text = str(value).strip()
            if text:
                return text
    return None


def _normalize_accel(value: Any) -> dict[str, Any]:
    """把模型接口的加速器字段归一化成部署快照对象。"""
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    if isinstance(value, str) and value.strip():
        return {"type": value.strip()}
    return {}


def _model_quant(model_key: str | None) -> str | None:
    """从型号键中识别明示的量化规格。"""
    if model_key is None:
        return None
    match = re.search(r"(?:^|[-_])(w\d+a\d+|fp16|bf16)(?:$|[-_])", model_key, re.IGNORECASE)
    return match.group(1).lower() if match else None


def _model_soc(model_key: str | None) -> str | None:
    """从型号键中识别明示的 SoC 型号。"""
    if model_key is None:
        return None
    match = re.search(r"(?:^|[-_])(rk\d+[a-z0-9]*)(?:$|[-_])", model_key, re.IGNORECASE)
    return match.group(1).lower() if match else None


def _version_from_output(output: str) -> str | None:
    """从 etsllm --version 输出中取版本号。"""
    match = re.search(r"\bv?\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.-]+)?\b", output)
    return match.group(0).lstrip("v") if match else None


def _server_error_message(payload: dict[str, Any]) -> str:
    """从设备错误报文提取有限字段，避免并发失败只显示 None。"""
    details: list[str] = []
    for key in ("code", "error_code", "message", "error", "detail"):
        value = payload.get(key)
        if value is None:
            continue
        rendered = (
            json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            if isinstance(value, (dict, list))
            else str(value)
        )
        details.append(f"{key}={rendered[:160]}")
    if not details:
        details.append(f"type={payload.get('type', 'error')}")
    return f"init error: {', '.join(details)}"[:240]


def _limit_output(output: str | None, max_chars: int = 16_384) -> str | None:
    """限制命令原始输出大小，避免探测日志膨胀。"""
    if output is None:
        return None
    return output[:max_chars]


async def probe_lite(
    host: str,
    port: int,
    timeout_s: float = 10.0,
    *,
    fallback_model: str | None = None,
) -> ProbeOutcome:
    """探测设备是否可初始化，并取回声明的能力。

    先查 /v1/models 拿到 STT 模型与加速后端；再建 WS 发 init。
    205 可能未开放模型目录，此时可用设备类型约定的 fallback_model 直接 init。
    任何一步失败都返回 ok=False，不抛异常给 API 层。
    """
    started = time.perf_counter()
    try:
        models: list[dict[str, Any]] = []
        stt: dict[str, Any] = {}
        accel: dict[str, Any] = {}
        try:
            models = await _fetch_models(host, port, timeout_s)
            stt = _pick_stt_model(models)
            model_key = str(stt["id"])
        except Exception:
            if fallback_model is None:
                raise
            model_key = fallback_model
        extra = stt.get("extra_info") or {}
        accel_raw = extra.get("accel") if isinstance(extra, dict) else None
        accel = {"type": str(accel_raw)} if accel_raw else {}

        init = {
            "type": "init",
            "model": model_key,
            "sample_rate": 16000,
            "disable_speaker_diarization": True,
            "delay_speaker_diarization": False,
            "itn": True,
            "disable_streaming_partial": False,
        }
        url = f"ws://{host}:{port}/v1/audio/transcription/realtime"
        async with websockets.connect(
            url, ping_interval=None, max_size=None, open_timeout=timeout_s
        ) as ws:
            await ws.send(json.dumps(init, ensure_ascii=False))
            raw_message = await asyncio.wait_for(ws.recv(), timeout=timeout_s)

        message = json.loads(raw_message)
        if message.get("type") == "error":
            return ProbeOutcome(
                ok=False,
                model_key=model_key,
                accel=accel,
                raw={"models": models, "init": message},
                error=_server_error_message(message),
            )

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return ProbeOutcome(
            ok=True,
            model_key=model_key,
            accel=accel,
            declared=dict(message.get("capabilities") or {}),
            init_latency_ms=elapsed_ms,
            raw={"models": models, "init": message},
        )
    except Exception as exc:  # 网络/协议错误都归为探测失败
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return ProbeOutcome(
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            init_latency_ms=elapsed_ms,
        )


async def _fetch_models(host: str, port: int, timeout_s: float) -> list[dict[str, Any]]:
    """读取 /v1/models；只取 data 数组。"""
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.get(f"http://{_http_host(host)}:{port}/v1/models")
        resp.raise_for_status()
        payload = resp.json()
    data = payload.get("data")
    if not isinstance(data, list):
        raise ValueError("/v1/models 返回缺少 data 数组")
    return [item for item in data if isinstance(item, dict)]


def _pick_stt_model(models: list[dict[str, Any]]) -> dict[str, Any]:
    """选择 model_type=stt 的模型；找不到就报错，避免拿 LLM 去 init。"""
    for item in models:
        extra = item.get("extra_info") or {}
        if extra.get("model_type") == "stt":
            return item
    raise ValueError("/v1/models 中没有 model_type=stt 的模型")


@dataclass
class TranscribeOutcome:
    """一次转写的结果；text 已从 segment 聚合，避开 final.text 的坑。"""

    ok: bool
    text: str = ""
    segment_count: int = 0
    usage: dict[str, Any] = field(default_factory=dict)
    wall_ms: int = 0
    error: str | None = None
    # 识别分段（含时间/说话人时才有）；DER/SA-DER/命名/泳道的数据源
    segments: list[dict[str, Any]] = field(default_factory=list)
    # 设备声纹命中与簇级置信度；命名/EER 的数据源
    speaker_match: dict[str, Any] = field(default_factory=dict)
    # 真机诊断需要保留协议形状；调用方须在输出前清理声纹向量等敏感字段。
    message_counts: dict[str, int] = field(default_factory=dict)
    final_text: str = ""
    global_spk_list: list[Any] = field(default_factory=list)
    # 流式性能测量；设备未发 partial 时 ttft_ms 保持为空，不用 wall 冒充。
    ttft_ms: int | None = None
    finalize_ms: int | None = None
    # full probe 仅保留能力布尔值；性能时延不作为设备能力快照。
    observed: dict[str, bool] = field(default_factory=dict)


def _time_ms(sentence: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    """从设备句子取时间；*_ms 与整数视为毫秒，裸浮点数按秒换算。

    为什么：2026-11-07 的 205 / 502 真机报文都以整数毫秒返回裸 start/end；
    小数秒格式仍由既有接口样例覆盖。
    """
    for key in keys:
        value = sentence.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            if key.endswith("_ms") or isinstance(value, int):
                return round(value)
            return round(float(value) * 1000)
    return None


def _collect_sentences(
    message: dict[str, Any],
    rows: dict[tuple[int, int, str], dict[str, Any]],
) -> None:
    """收集全部句子（含零时长标点句），用于与 global_spk_list 逐句对齐。"""
    for sentence in message.get("sentences") or []:
        if not isinstance(sentence, dict):
            continue
        text = sentence.get("text") or ""
        if not text:
            continue
        start = _time_ms(sentence, ("start_ms", "begin_ms", "start", "begin"))
        end = _time_ms(sentence, ("end_ms", "end"))
        if start is None or end is None:
            continue
        key = (start, end, str(text))
        if key not in rows:
            rows[key] = {
                # 过程中的 spk 会动态变化，不能作为最终说话人；
                # 统一等 final 的 global_spk_list 对齐。
                "speaker": "spk",
                "start_ms": start,
                "end_ms": end,
                "text": str(text),
            }


def _label_count(labels: list[Any], row_count: int) -> int:
    """global_spk_list 可能多一个句末哨兵；返回能对应到句子的标签数。"""
    return len(labels) - 1 if len(labels) == row_count + 1 else len(labels)


def _apply_sentence_speakers(rows: list[dict[str, Any]], labels: list[Any]) -> bool:
    """把 final 的 global_spk_list 对齐到全部句子；长度不符时不猜测。"""
    if not rows or not labels:
        return False
    if _label_count(labels, len(rows)) != len(rows):
        return False
    applied = False
    for row, label in zip(rows, labels, strict=False):
        label_text = str(label).strip()
        if label_text in {"", "-1", "None", "spk"}:
            row["speaker"] = "spk"
            continue
        # final 列表是唯一可信来源，覆盖过程中可能变化的 spk。
        row["speaker"] = label_text
        applied = True
    return applied


def _coerce_global_labels(raw: Any) -> list[Any] | None:
    """兼容设备把 global_spk_list 序列化成字符串的固件。"""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        text = re.sub(r"\s*\(len=\d+\)\s*$", "", raw.strip())
        if "..." in text:
            return None
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, list) else None
    return None


async def transcribe_file(
    host: str,
    port: int,
    model_key: str,
    audio_path: str,
    timeout_s: float = 120.0,
    itn: bool = False,
    diarization: bool = False,
    voiceprints: list[dict[str, Any]] | None = None,
    speaker_match_threshold: float = 0.6,
    max_num_spks: int = 6,
    abort_event: asyncio.Event | None = None,
    mode: str = "burst",
    chunk_ms: int = 1000,
    input_rate: float = 1.0,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> TranscribeOutcome:
    """按 burst 或实时流模式转写一个 16k/16bit/mono 的 wav。

    - burst 每 1 秒发一个 chunk；stream 按配置分片并按输入速率节流（M4 §5.6）
    - 文本从 transcript.segment 聚合：205 的 final.text 恒为 "。"
    - 会话必须优雅关闭；强杀会让引擎卡死（M2）
    """
    import wave
    from pathlib import Path

    started = time.perf_counter()
    try:
        with wave.open(str(Path(audio_path)), "rb") as handle:
            if handle.getframerate() != 16000 or handle.getnchannels() != 1:
                raise ValueError(
                    f"要求 16kHz 单声道，实际 {handle.getframerate()}Hz/{handle.getnchannels()}ch"
                )
            frames = handle.readframes(handle.getnframes())

        if mode not in {"burst", "stream"}:
            raise ValueError("mode 只能是 burst 或 stream")
        if chunk_ms <= 0 or input_rate <= 0:
            raise ValueError("chunk_ms 与 input_rate 必须为正数")

        init = {
            "type": "init",
            "model": model_key,
            "sample_rate": 16000,
            "disable_speaker_diarization": not diarization,
            "delay_speaker_diarization": False,
            "itn": itn,
            "disable_streaming_partial": mode != "stream",
        }
        if diarization:
            init["max_num_spks"] = max_num_spks
        if voiceprints:
            init.update(
                {
                    "speaker_diarization_mode": "matching",
                    "max_num_spks": max_num_spks,
                    "voiceprints": voiceprints,
                    "speaker_match_threshold": speaker_match_threshold,
                }
            )
        url = f"ws://{host}:{port}/v1/audio/transcription/realtime"
        sentences: list[str] = []
        sentence_rows: dict[tuple[int, int, str], dict[str, Any]] = {}
        usage: dict[str, Any] = {}
        speaker_match: dict[str, Any] = {}
        global_speakers: list[Any] = []
        saw_global_speakers = False
        saw_partial = False
        saw_usage = False
        saw_sentence_speaker = False
        saw_punctuation = False
        final_text = ""
        audio_started_at: float | None = None
        audio_ended_at: float | None = None
        ttft_ms: int | None = None
        finalize_ms: int | None = None
        engine_error: str | None = None
        message_counts: dict[str, int] = {}

        def report_progress(kind: str, processed_ms: int, **extra: Any) -> None:
            """把设备侧已处理到的音频位置回调给 runner；失败不影响转写。"""
            if on_progress is None:
                return
            try:
                on_progress(
                    {
                        "kind": kind,
                        "processed_ms": max(0, int(processed_ms)),
                        **extra,
                    }
                )
            except Exception:
                return

        def consume_message(raw: str | bytes) -> str:
            """聚合设备消息，并按首个非空 partial 与 final 记录流式时间。"""
            nonlocal usage, speaker_match, global_speakers, final_text
            nonlocal saw_global_speakers, saw_partial, saw_usage
            nonlocal saw_sentence_speaker, saw_punctuation
            nonlocal audio_started_at, audio_ended_at, ttft_ms, finalize_ms, engine_error
            message = json.loads(raw)
            kind = message.get("type")
            if isinstance(kind, str):
                message_counts[kind] = message_counts.get(kind, 0) + 1
            else:
                message_counts["<unknown>"] = message_counts.get("<unknown>", 0) + 1
            if kind == "transcript.partial":
                saw_partial = True
                partial_text = message.get("text") or "".join(
                    str(sentence.get("text") or "")
                    for sentence in message.get("sentences") or []
                    if isinstance(sentence, dict)
                )
                saw_punctuation = saw_punctuation or _has_punctuation(partial_text)
                if mode == "stream" and partial_text and ttft_ms is None and audio_started_at:
                    ttft_ms = int((time.perf_counter() - audio_started_at) * 1000)
                # 进度只看 segment：partial 只用于 TTFT/标点能力，
                # 不能代表“已完成转写”，否则会在 final 前提前到 100%。
                return "partial"
            if kind == "transcript.segment":
                _collect_sentences(message, sentence_rows)
                for sentence in message.get("sentences") or []:
                    if not isinstance(sentence, dict):
                        continue
                    text = sentence.get("text") or ""
                    if text:
                        sentences.append(text)
                        saw_punctuation = saw_punctuation or _has_punctuation(str(text))
                    saw_sentence_speaker = saw_sentence_speaker or (
                        sentence.get("spk") is not None or sentence.get("speaker") is not None
                    )
                segment_end = max(
                    (
                        int(_time_ms(sentence, ("end_ms", "end")) or 0)
                        for sentence in message.get("sentences") or []
                        if isinstance(sentence, dict)
                    ),
                    default=0,
                )
                report_progress("segment", segment_end, segments=len(sentence_rows))
                return "segment"
            if kind == "transcript.final":
                usage = dict(message.get("usage") or {})
                saw_usage = isinstance(message.get("usage"), dict)
                raw_matches = message.get("spk_voiceprints")
                if isinstance(raw_matches, dict):
                    speaker_match = raw_matches
                raw_speakers = _coerce_global_labels(message.get("global_spk_list"))
                if raw_speakers is not None:
                    global_speakers = raw_speakers
                    saw_global_speakers = True
                _collect_sentences(message, sentence_rows)
                final_rows: dict[tuple[int, int, str], dict[str, Any]] = {}
                _collect_sentences({"sentences": message.get("sentences")}, final_rows)
                for sentence in message.get("sentences") or []:
                    if not isinstance(sentence, dict):
                        continue
                    text = sentence.get("text") or ""
                    if text:
                        sentences.append(text)
                        saw_punctuation = saw_punctuation or _has_punctuation(str(text))
                    saw_sentence_speaker = saw_sentence_speaker or (
                        sentence.get("spk") is not None or sentence.get("speaker") is not None
                    )
                if (
                    final_rows
                    and raw_speakers is not None
                    and _label_count(raw_speakers, len(final_rows)) == len(final_rows)
                ):
                    # final 重发完整句子时以其为准：过程中累积的句子可能带旧时间戳，
                    # 与新标签错位；这里整体替换而不是合并。
                    sentence_rows.clear()
                    sentence_rows.update(final_rows)
                    sentences.clear()
                    sentences.extend(row["text"] for row in final_rows.values())
                final_text = message.get("text") or ""
                if mode == "stream" and audio_ended_at is not None:
                    finalize_ms = max(0, int((time.perf_counter() - audio_ended_at) * 1000))
                report_progress(
                    "final",
                    int(usage.get("audio_duration_ms") or 0),
                    segments=len(sentence_rows),
                )
                return "final"
            if kind == "error":
                engine_error = f"engine error: {message.get('message')}"
                return "error"
            return "other"

        async def receive_streaming_messages(ws: Any) -> str:
            """发送音频期间持续读取 partial，避免把 TTFT 误记为积压后的读数。"""
            while True:
                raw = await _recv_with_abort(ws, timeout_s=timeout_s, abort_event=abort_event)
                if raw is None:
                    return "aborted"
                state = consume_message(raw)
                if state in {"final", "error"}:
                    return state

        async with websockets.connect(
            url, ping_interval=None, max_size=None, open_timeout=10
        ) as ws:
            await ws.send(json.dumps(init, ensure_ascii=False))
            init_message = await _recv_with_abort(ws, timeout_s=10, abort_event=abort_event)
            if init_message is None:
                await _close_for_abort(ws)
                return TranscribeOutcome(ok=False, error="aborted_condition_changed")
            init_resp = json.loads(init_message)
            init_type = init_resp.get("type")
            if isinstance(init_type, str):
                message_counts[init_type] = message_counts.get(init_type, 0) + 1
            else:
                message_counts["<unknown>"] = message_counts.get("<unknown>", 0) + 1
            if init_resp.get("type") == "error":
                return TranscribeOutcome(ok=False, error=_server_error_message(init_resp))

            if mode == "burst":
                chunk_bytes = 16000 * 2  # 每个 burst chunk 为 1 秒音频。
                for offset in range(0, len(frames), chunk_bytes):
                    if abort_event is not None and abort_event.is_set():
                        await _close_for_abort(ws)
                        return TranscribeOutcome(ok=False, error="aborted_condition_changed")
                    await ws.send(frames[offset : offset + chunk_bytes])
                    await asyncio.sleep(0.01)
                audio_ended_at = time.perf_counter()
                await ws.send(json.dumps({"type": "audio.end"}))
                while True:
                    raw = await _recv_with_abort(ws, timeout_s=timeout_s, abort_event=abort_event)
                    if raw is None:
                        await _close_for_abort(ws)
                        return TranscribeOutcome(ok=False, error="aborted_condition_changed")
                    state = consume_message(raw)
                    if state == "error":
                        await _close_for_abort(ws)
                        return TranscribeOutcome(ok=False, error=engine_error)
                    if state == "final":
                        break
            else:
                chunk_bytes = max(2, int(32000 * chunk_ms / 1000))
                chunk_bytes -= chunk_bytes % 2
                audio_started_at = time.perf_counter()
                receiver = asyncio.create_task(receive_streaming_messages(ws))
                for offset in range(0, len(frames), chunk_bytes):
                    if abort_event is not None and abort_event.is_set():
                        await asyncio.gather(receiver, return_exceptions=True)
                        await _close_for_abort(ws)
                        return TranscribeOutcome(ok=False, error="aborted_condition_changed")
                    chunk = frames[offset : offset + chunk_bytes]
                    await ws.send(chunk)
                    await asyncio.sleep((len(chunk) / 32000) / input_rate)
                    if receiver.done():
                        break
                if not receiver.done():
                    audio_ended_at = time.perf_counter()
                    await ws.send(json.dumps({"type": "audio.end"}))
                response_state = await receiver
                if response_state == "aborted":
                    await _close_for_abort(ws)
                    return TranscribeOutcome(ok=False, error="aborted_condition_changed")
                if response_state == "error":
                    await _close_for_abort(ws)
                    return TranscribeOutcome(ok=False, error=engine_error)

        # final 的 global_spk_list 是逐句标签；先对齐全部句子（含标点句），
        # 再从句子重建带时间的分段，避免零时长句造成长度错位。
        ordered_sentences = sorted(
            sentence_rows.values(), key=lambda row: (row["start_ms"], row["end_ms"])
        )
        # 文本从去重后的句子重建：部分固件会在 final 重复全部 segment，
        # 直接拼接消息里的文本会把同一句算两次，CER 随之虚高。
        text = "".join(row["text"] for row in ordered_sentences).strip()
        if not text:
            # 句子没有时间戳时无法去重，退回消息顺序拼接。
            text = "".join(sentences).strip()
        applied = _apply_sentence_speakers(ordered_sentences, global_speakers)
        if not applied:
            # final 标签缺失或与句子数不符：过程中变化过的 spk 不可信，
            # 统一留占位符，让 DER 走 na(no_diarization_output) 而不是编造映射。
            for row in ordered_sentences:
                row["speaker"] = "spk"
        segments = [
            {
                "speaker": row["speaker"],
                "start_ms": row["start_ms"],
                "end_ms": row["end_ms"],
                "text": row["text"],
            }
            for row in ordered_sentences
            if row["end_ms"] > row["start_ms"] and row["text"]
        ]
        if not text and final_text and final_text.strip() not in {"", "。"}:
            text = final_text.strip()
        wall_ms = int((time.perf_counter() - started) * 1000)
        if not text:
            return TranscribeOutcome(
                ok=False,
                wall_ms=wall_ms,
                error="empty transcript",
                ttft_ms=ttft_ms,
                finalize_ms=finalize_ms,
            )
        return TranscribeOutcome(
            ok=True,
            text=text,
            segment_count=len(sentences),
            usage=usage,
            wall_ms=wall_ms,
            segments=segments,
            speaker_match=speaker_match,
            message_counts=message_counts,
            final_text=final_text,
            global_spk_list=global_speakers,
            ttft_ms=ttft_ms,
            finalize_ms=finalize_ms,
            observed={
                "partials": saw_partial,
                "segments": bool(segments),
                "speakers_in_sentence": saw_sentence_speaker,
                "global_spk_list": saw_global_speakers,
                "punctuation_output": saw_punctuation,
                "usage_field": saw_usage,
                "finalizes_in_stream": mode == "stream" and finalize_ms is not None,
            },
        )
    except Exception as exc:
        wall_ms = int((time.perf_counter() - started) * 1000)
        return TranscribeOutcome(
            ok=False,
            wall_ms=wall_ms,
            error=f"{type(exc).__name__}: {exc}",
        )


_PUNCTUATION_RE = re.compile(r"[,，。.!?！？;；:：、]")


def _has_punctuation(text: str) -> bool:
    """只检查 sentences/partial 的内容，避免 205 恒定的 final.text 造成假阳性。"""
    return _PUNCTUATION_RE.search(text) is not None


async def _recv_with_abort(
    websocket: Any,
    *,
    timeout_s: float,
    abort_event: asyncio.Event | None,
) -> str | bytes | None:
    """并行等待设备消息和身份变化信号；不以强制断连打断设备。"""
    if abort_event is None:
        return await asyncio.wait_for(websocket.recv(), timeout=timeout_s)

    receive_task = asyncio.create_task(websocket.recv())
    abort_task = asyncio.create_task(abort_event.wait())
    done, _pending = await asyncio.wait(
        {receive_task, abort_task},
        timeout=timeout_s,
        return_when=asyncio.FIRST_COMPLETED,
    )
    if receive_task in done:
        abort_task.cancel()
        await asyncio.gather(abort_task, return_exceptions=True)
        message = receive_task.result()
        if not isinstance(message, (str, bytes)):
            raise TypeError("WebSocket recv 返回了非文本或二进制帧")
        return message

    receive_task.cancel()
    await asyncio.gather(receive_task, return_exceptions=True)
    if abort_task in done and abort_event.is_set():
        return None
    abort_task.cancel()
    await asyncio.gather(abort_task, return_exceptions=True)
    raise TimeoutError


async def _close_for_abort(websocket: Any) -> None:
    """身份变化时发送协议 close，并最多等待 5 秒让设备完成收尾。"""
    try:
        await websocket.send(json.dumps({"type": "close"}))
        await asyncio.wait_for(websocket.recv(), timeout=5)
    except Exception:
        return
