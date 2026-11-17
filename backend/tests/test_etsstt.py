"""设备转写协议、声纹提取与说话人标签对齐测试。"""

import asyncio
import json
import subprocess
import wave
from pathlib import Path
from typing import Any

import asyncssh
import httpx
import pytest

from app.adapters import etsstt
from app.adapters.etsstt import (
    _apply_sentence_speakers,
    _coerce_global_labels,
    _collect_sentences,
    _model_quant,
    _model_soc,
    _parse_get_version,
    _time_ms,
)


def test_global_speaker_labels_skip_final_sentinel() -> None:
    rows = [
        {"speaker": "spk", "start_ms": 0, "end_ms": 900, "text": "甲"},
        {"speaker": "spk", "start_ms": 1000, "end_ms": 2000, "text": "乙"},
    ]
    assert _apply_sentence_speakers(rows, [0, 1, 99]) is True
    assert [row["speaker"] for row in rows] == ["0", "1"]


def test_global_speaker_labels_are_not_guessed_on_length_mismatch() -> None:
    rows = [{"speaker": "spk", "start_ms": 0, "end_ms": 900, "text": "甲"}]
    assert _apply_sentence_speakers(rows, [0, 1, 2, 99]) is False
    assert rows[0]["speaker"] == "spk"


def test_sentence_timestamps_accept_integer_milliseconds_and_float_seconds() -> None:
    """适配 205/502 的整数毫秒及秒制浮点报文，避免时间放大 1,000 倍。"""
    assert _time_ms({"start": 130}, ("start",)) == 130
    assert _time_ms({"end": 0.77}, ("end",)) == 770
    assert _time_ms({"start_ms": 130}, ("start_ms",)) == 130
    assert _time_ms({"start": True}, ("start",)) is None


def test_parse_rknn_get_version_identity_fields() -> None:
    """RKNN 型号键可解析量化与 SoC，未返回的字段仍保持未知。"""
    parsed = _parse_get_version('{"model":"model-asr-b-w8a8_rk3588","model_version":"1.0.3"}')

    assert _model_quant(parsed["model_key"]) == "w8a8"
    assert _model_soc(parsed["model_key"]) == "rk3588"
    assert parsed["model_version"] == "1.0.3"


def test_parse_get_version_reads_205_component_model_version() -> None:
    """205 的组件版本键 model-etsstt_onnx 可作为模型版本。"""
    parsed = _parse_get_version(
        '{"bigversion":"3.2.671","model-etsstt_onnx":"1.0.2","etsstt":"master"}'
    )

    assert parsed["bigversion"] == "3.2.671"
    assert parsed["model_version"] == "1.0.2"


def test_probe_identity_collects_versions_models_and_system_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """身份探测只读取 HTTP 与固定版本命令，并按部署字段生成结果。"""
    requests: list[str] = []
    transport = httpx.MockTransport(lambda request: _identity_http_response(request, requests))
    original_client = httpx.AsyncClient

    def client_factory(*, timeout: float) -> httpx.AsyncClient:
        return original_client(transport=transport, timeout=timeout)

    commands: list[tuple[str, ...]] = []

    def run_command(
        _host: str,
        _kind: str,
        _ssh_user: str | None,
        _ssh_port: int,
        command: tuple[str, ...],
        *_auth_args: str | None,
    ) -> str:
        commands.append(command)
        if command == ("get_version",):
            return '{"model_version": null, "platform": ""}'
        return "etsllm version 1.4.49"

    monkeypatch.setattr(etsstt.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(etsstt, "_run_identity_command", run_command)
    result = asyncio.run(
        etsstt.probe_identity(
            "127.0.0.1",
            8701,
            kind="etsstt",
            platform="205",
            ssh_user="tester",
            ssh_port=2222,
        )
    )

    assert result.ok
    assert result.identity == {
        "platform": "502",
        "kind": "etsstt",
        "bigversion": "3.2.412",
        "etsllm_version": "1.4.49",
        "accel": {"type": "cann"},
        "model_key": "qwen3-asr-1.7b",
        "model_version": None,
    }
    assert result.sn == "SN-502-001"
    assert result.soc == "gjwg-310p-24g-arm64"
    assert result.version_collection_ok
    assert set(requests) == {
        "http://127.0.0.1:8701/v1/models",
        "http://127.0.0.1:8080/v1/system/info",
    }
    assert set(commands) == {("get_version",), ("etsllm", "--version")}


def test_probe_lite_uses_fallback_model_when_205_models_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """205 未开放 /v1/models 时，lite 用设备类型约定的模型键直接 init。"""

    async def fail_models(_host: str, _port: int, _timeout: float) -> list[dict[str, Any]]:
        raise RuntimeError("503")

    class FakeWebSocket:
        async def __aenter__(self) -> "FakeWebSocket":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def send(self, _payload: str) -> None:
            return None

        async def recv(self) -> str:
            return json.dumps({"type": "ready", "capabilities": {"punctuation": True}})

    monkeypatch.setattr(etsstt, "_fetch_models", fail_models)
    monkeypatch.setattr(etsstt.websockets, "connect", lambda *_args, **_kwargs: FakeWebSocket())
    result = asyncio.run(etsstt.probe_lite("127.0.0.1", 8701, fallback_model="etsstt"))

    assert result.ok
    assert result.model_key == "etsstt"
    assert result.declared == {"punctuation": True}


def test_probe_identity_uses_device_host_for_system_info_when_asr_is_tunneled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ASR 走本地隧道时，/v1/system/info 仍访问设备真实地址而非 127.0.0.1。"""
    requests: list[str] = []
    transport = httpx.MockTransport(lambda request: _identity_http_response(request, requests))
    original_client = httpx.AsyncClient

    def client_factory(*, timeout: float) -> httpx.AsyncClient:
        return original_client(transport=transport, timeout=timeout)

    def run_command(
        _host: str,
        _kind: str,
        _ssh_user: str | None,
        _ssh_port: int,
        command: tuple[str, ...],
        *_auth_args: str | None,
    ) -> str:
        if command == ("get_version",):
            return '{"platform":"205","bigversion":"3.2.671"}'
        return "etsllm version 1.4.52"

    monkeypatch.setattr(etsstt.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(etsstt, "_run_identity_command", run_command)
    result = asyncio.run(
        etsstt.probe_identity(
            "127.0.0.1",
            45261,
            kind="etsstt",
            platform="205",
            ssh_user="etsme",
            ssh_port=22,
            ssh_host="192.0.2.10",
            system_info_host="192.0.2.10",
        )
    )

    assert result.ok
    assert "http://127.0.0.1:45261/v1/models" in requests
    assert "http://192.0.2.10:8080/v1/system/info" in requests
    assert all("127.0.0.1:8080" not in url for url in requests)


def test_probe_identity_degrades_when_205_models_endpoint_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """205 未开放 /v1/models 时，仍用 get_version 的型号与大版本完成身份采集。"""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(503, json={"error": "not available"})
        return httpx.Response(
            200,
            json={
                "system": {"sn": "SN-205-001", "platform": "205", "version": "3.2.500"},
                "spec": {"features": {"ai": {"support-model-type": "rk3588"}}},
            },
        )

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handle)

    def client_factory(*, timeout: float) -> httpx.AsyncClient:
        return original_client(transport=transport, timeout=timeout)

    def run_command(
        _host: str,
        _kind: str,
        _ssh_user: str | None,
        _ssh_port: int,
        command: tuple[str, ...],
        *_auth_args: str | None,
    ) -> str:
        if command == ("get_version",):
            return (
                '{"platform":"205","model_key":"model-asr-b-w8a8_rk3588",'
                '"model_version":"1.4.49","bigversion":"3.2.500"}'
            )
        return "etsllm version 1.4.49"

    monkeypatch.setattr(etsstt.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(etsstt, "_run_identity_command", run_command)
    result = asyncio.run(
        etsstt.probe_identity(
            "127.0.0.1",
            8701,
            kind="etsstt",
            platform="205",
            ssh_user="etsme",
            ssh_port=22,
        )
    )

    assert result.ok
    assert result.identity["platform"] == "205"
    assert result.identity["model_key"] == "model-asr-b-w8a8_rk3588"
    assert result.identity["model_version"] == "1.4.49"
    assert result.identity["bigversion"] == "3.2.500"
    assert any("/v1/models 读取失败" in warning for warning in result.warnings)
    assert not result.version_collection_ok


def test_probe_identity_keeps_http_identity_when_version_collection_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """版本命令失败时仍返回 HTTP 身份，并将设备标记为需关注。"""
    transport = httpx.MockTransport(lambda request: _identity_http_response(request, []))
    original_client = httpx.AsyncClient

    def client_factory(*, timeout: float) -> httpx.AsyncClient:
        return original_client(transport=transport, timeout=timeout)

    def run_command(*_args: object) -> str:
        raise subprocess.CalledProcessError(1, "ssh")

    monkeypatch.setattr(etsstt.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(etsstt, "_run_identity_command", run_command)
    result = asyncio.run(
        etsstt.probe_identity(
            "127.0.0.1",
            8701,
            kind="etsstt",
            platform="205",
            ssh_user="tester",
            ssh_port=22,
        )
    )

    assert result.ok
    assert not result.version_collection_ok
    assert len(result.warnings) == 2
    assert result.identity["platform"] == "502"
    assert result.identity["model_version"] is None


def test_probe_identity_ignores_build_version_in_platform_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """system.platform 偶尔是固件版本时，保留台账里的 205/600/502 产品码。"""
    original_client = httpx.AsyncClient

    def handle(request: httpx.Request) -> httpx.Response:
        response = _identity_http_response(request, [])
        payload = response.json()
        if request.url.path == "/v1/system/info":
            payload["system"]["platform"] = "1.0.98"
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handle)

    def client_factory(*, timeout: float) -> httpx.AsyncClient:
        return original_client(transport=transport, timeout=timeout)

    def run_command(*_args: object) -> str:
        return "etsllm version 1.4.49"

    monkeypatch.setattr(etsstt.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(etsstt, "_run_identity_command", run_command)
    result = asyncio.run(
        etsstt.probe_identity(
            "127.0.0.1",
            8701,
            kind="etsstt",
            platform="205",
            ssh_user="tester",
            ssh_port=22,
        )
    )

    assert result.ok
    assert result.identity["platform"] == "205"


def test_saved_ssh_password_is_sent_through_asyncssh_without_command_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSH 密码仅作为 AsyncSSH 参数传入，不进入 OpenSSH 命令行。"""
    connections: list[tuple[str, dict[str, object]]] = []

    class FakeConnection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def run(self, command: str, **kwargs: object):
            assert command == "bash -lc get_version"
            assert kwargs["check"] is True
            assert kwargs["timeout"] == 5
            return type("Result", (), {"stdout": "3.2.412\n"})()

    def connect(host: str, **options: object) -> FakeConnection:
        connections.append((host, options))
        return FakeConnection()

    monkeypatch.setattr(asyncssh, "connect", connect)
    password = "password-only-in-memory"
    output = etsstt._run_identity_command(
        "192.0.2.10",
        "etsstt",
        "tester",
        22,
        ("get_version",),
        ssh_password=password,
    )

    assert output == "3.2.412"
    assert connections[0][0] == "192.0.2.10"
    assert connections[0][1]["password"] == password
    assert connections[0][1]["client_keys"] == []


def test_invalid_saved_ssh_key_error_does_not_echo_key_material() -> None:
    """SSH 私钥解析失败时错误摘要不包含提交的私钥内容。"""
    key_material = "not-a-valid-private-key-material"

    with pytest.raises(RuntimeError) as captured:
        etsstt._run_identity_command(
            "192.0.2.10",
            "etsstt",
            "tester",
            22,
            ("get_version",),
            ssh_private_key=key_material,
        )

    assert key_material not in str(captured.value)


def test_gateway_identity_commands_use_documented_deployments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """网关经 SSH 调用宿主 kubectl，并使用文档规定的命名空间和二进制路径。"""
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "1.4.49\n", "")

    monkeypatch.setattr(etsstt.subprocess, "run", run)

    assert (
        etsstt._run_identity_command("192.0.2.1", "gateway", "tester", 22, ("get_version",))
        == "1.4.49"
    )
    etsstt._run_identity_command("192.0.2.1", "gateway", "tester", 22, ("etsllm", "--version"))

    assert commands == [
        [
            "ssh",
            "-p",
            "22",
            "-oBatchMode=yes",
            "-oConnectTimeout=3",
            "-oServerAliveInterval=2",
            "-oServerAliveCountMax=1",
            "tester@192.0.2.1",
            "sudo -n kubectl exec -n ns-meagt deploy/mebox -- get_version",
        ],
        [
            "ssh",
            "-p",
            "22",
            "-oBatchMode=yes",
            "-oConnectTimeout=3",
            "-oServerAliveInterval=2",
            "-oServerAliveCountMax=1",
            "tester@192.0.2.1",
            "sudo -n kubectl exec -n ns-meagt deploy/etsllm -- /opt/etsme/bin/etsllm --version",
        ],
    ]


def test_etsstt_identity_commands_use_login_shell_for_device_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSH exec 不含设备登录 PATH 时，版本命令仍能按 M2 身份采集执行。"""
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "version\n", "")

    monkeypatch.setattr(etsstt.subprocess, "run", run)

    assert (
        etsstt._run_identity_command("192.0.2.1", "etsstt", "etsme", 22, ("get_version",))
        == "version"
    )
    assert (
        etsstt._run_identity_command("192.0.2.1", "etsstt", "etsme", 22, ("etsllm", "--version"))
        == "version"
    )

    assert commands == [
        [
            "ssh",
            "-p",
            "22",
            "-oBatchMode=yes",
            "-oConnectTimeout=3",
            "-oServerAliveInterval=2",
            "-oServerAliveCountMax=1",
            "etsme@192.0.2.1",
            "bash -lc get_version",
        ],
        [
            "ssh",
            "-p",
            "22",
            "-oBatchMode=yes",
            "-oConnectTimeout=3",
            "-oServerAliveInterval=2",
            "-oServerAliveCountMax=1",
            "etsme@192.0.2.1",
            "bash -lc 'etsllm --version'",
        ],
    ]


def _identity_http_response(request: httpx.Request, requests: list[str]) -> httpx.Response:
    """按身份探测约定返回两类只读端点的固定样例。"""
    requests.append(str(request.url))
    if request.url.path == "/v1/models":
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "qwen3-asr-1.7b",
                        "invocation_name": "qwen3-asr-1.7b",
                        "extra_info": {"model_type": "stt", "accel": "cann"},
                    }
                ]
            },
        )
    return httpx.Response(
        200,
        json={
            "system": {"sn": "SN-502-001", "platform": "502", "version": "3.2.412"},
            "spec": {"features": {"ai": {"support-model-type": "gjwg-310p-24g-arm64"}}},
        },
    )


def test_extract_voiceprint_uses_device_path_and_validates_embedding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """声纹接口按文档发送设备路径和时间范围，并校验 192 维返回值。"""
    seen: dict[str, Any] = {}
    embedding = [0.125] * 192

    def handle(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "voiceprints": [{"id": "probe_A", "embedding": embedding, "sample_duration": 5800}]
            },
        )

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handle)

    def client_factory(*, timeout: float) -> httpx.AsyncClient:
        return original_client(transport=transport, timeout=timeout)

    monkeypatch.setattr(etsstt.httpx, "AsyncClient", client_factory)
    result = asyncio.run(
        etsstt.extract_voiceprint(
            "127.0.0.1", 8701, "qwen3-asr-1.7b", "/data/voiceprint/a.wav", "probe_A", 6000
        )
    )

    assert result.ok
    assert result.embedding == embedding
    assert result.sample_duration_ms == 5800
    assert seen["url"] == "http://127.0.0.1:8701/v1/audio/voiceprints"
    assert seen["body"] == {
        "model": "qwen3-asr-1.7b",
        "file_path": "/data/voiceprint/a.wav",
        "timestamp_ranges": [{"start": 0, "end": 6000, "id": "probe_A"}],
    }


def test_transcribe_file_sends_matching_voiceprints(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """声纹组通过 init.voiceprints 传入并启用 matching 模式。"""
    audio_path = tmp_path / "sample.wav"
    with wave.open(str(audio_path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 16000)

    class FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str | bytes] = []
            self.responses = iter(
                [
                    json.dumps({"type": "init"}),
                    json.dumps(
                        {
                            "type": "transcript.segment",
                            "sentences": [{"text": "你好。", "start": 0.1, "end": 0.9, "spk": "0"}],
                        }
                    ),
                    json.dumps(
                        {
                            "type": "transcript.final",
                            "sentences": [],
                            "spk_voiceprints": {
                                "0": {"match_info": {"matched_id": "A", "confidence": 0.91}}
                            },
                        }
                    ),
                ]
            )

        async def __aenter__(self) -> "FakeWebSocket":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def send(self, payload: str | bytes) -> None:
            self.sent.append(payload)

        async def recv(self) -> str:
            return next(self.responses)

    socket = FakeWebSocket()
    monkeypatch.setattr(etsstt.websockets, "connect", lambda *_args, **_kwargs: socket)
    voiceprints = [{"id": "A", "voiceprints": [{"embedding": [0.25] * 192, "weight": 5800}]}]
    result = asyncio.run(
        etsstt.transcribe_file(
            "127.0.0.1",
            8701,
            "qwen3-asr-1.7b",
            str(audio_path),
            diarization=True,
            voiceprints=voiceprints,
            speaker_match_threshold=0.6,
            max_num_spks=2,
        )
    )

    init = json.loads(next(payload for payload in socket.sent if isinstance(payload, str)))
    assert result.ok
    assert result.speaker_match["0"]["match_info"]["matched_id"] == "A"
    assert init["disable_speaker_diarization"] is False
    assert init["speaker_diarization_mode"] == "matching"
    assert init["speaker_match_threshold"] == 0.6
    assert init["max_num_spks"] == 2
    assert init["voiceprints"] == voiceprints


def test_server_error_message_preserves_safe_error_fields() -> None:
    """设备 init 错误优先保留 code/message，缺字段时也不给出 None。"""
    assert (
        etsstt._server_error_message({"type": "error", "code": 503, "message": "capacity reached"})
        == "init error: code=503, message=capacity reached"
    )
    assert etsstt._server_error_message({"type": "error"}) == "init error: type=error"


def test_transcribe_file_sends_protocol_close_when_identity_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """身份变化中断在途转写时发送 close 帧并等待设备收尾。"""
    audio_path = tmp_path / "condition-change.wav"
    with wave.open(str(audio_path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 16000)

    class FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str | bytes] = []
            self.audio_ended = asyncio.Event()
            self.recv_count = 0

        async def __aenter__(self) -> "FakeWebSocket":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def send(self, payload: str | bytes) -> None:
            self.sent.append(payload)
            if isinstance(payload, str) and json.loads(payload).get("type") == "audio.end":
                self.audio_ended.set()

        async def recv(self) -> str:
            self.recv_count += 1
            if self.recv_count == 1:
                return json.dumps({"type": "init"})
            if any(
                isinstance(payload, str) and json.loads(payload).get("type") == "close"
                for payload in self.sent
            ):
                return json.dumps({"type": "closed"})
            await asyncio.Event().wait()
            raise AssertionError("身份中止后不应继续等待转写结果")

    socket = FakeWebSocket()
    monkeypatch.setattr(etsstt.websockets, "connect", lambda *_args, **_kwargs: socket)
    abort_event = asyncio.Event()

    async def run_transcription() -> etsstt.TranscribeOutcome:
        task = asyncio.create_task(
            etsstt.transcribe_file(
                "127.0.0.1",
                8701,
                "test-model",
                str(audio_path),
                abort_event=abort_event,
            )
        )
        await socket.audio_ended.wait()
        abort_event.set()
        return await task

    result = asyncio.run(run_transcription())

    assert not result.ok
    assert result.error == "aborted_condition_changed"
    assert json.dumps({"type": "close"}) in socket.sent


def test_streaming_transcription_measures_partial_and_finalize_timing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """实时模式按 chunk/rate 发音频，并分别记录首个 partial 与收尾时延。"""
    audio_path = tmp_path / "stream.wav"
    with wave.open(str(audio_path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 1920)  # 120ms

    class FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str | bytes] = []
            self.messages: asyncio.Queue[str] = asyncio.Queue()
            self.messages.put_nowait(json.dumps({"type": "init"}))

        async def __aenter__(self) -> "FakeWebSocket":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def send(self, payload: str | bytes) -> None:
            self.sent.append(payload)
            if (
                isinstance(payload, bytes)
                and len([item for item in self.sent if isinstance(item, bytes)]) == 1
            ):
                await asyncio.sleep(0.005)
                await self.messages.put(json.dumps({"type": "transcript.partial", "text": "你"}))
            if isinstance(payload, str) and json.loads(payload).get("type") == "audio.end":
                await self.messages.put(
                    json.dumps(
                        {
                            "type": "transcript.final",
                            "sentences": [{"text": "你好。"}],
                            "usage": {"inference_ms": 20},
                        }
                    )
                )

        async def recv(self) -> str:
            return await self.messages.get()

    socket = FakeWebSocket()
    monkeypatch.setattr(etsstt.websockets, "connect", lambda *_args, **_kwargs: socket)
    result = asyncio.run(
        etsstt.transcribe_file(
            "127.0.0.1",
            8701,
            "test-model",
            str(audio_path),
            mode="stream",
            chunk_ms=30,
            input_rate=1.0,
        )
    )

    init = json.loads(next(payload for payload in socket.sent if isinstance(payload, str)))
    audio_chunks = [payload for payload in socket.sent if isinstance(payload, bytes)]
    assert result.ok
    assert result.text == "你好。"
    assert result.ttft_ms is not None and result.ttft_ms >= 0
    assert result.finalize_ms is not None and result.finalize_ms >= 0
    assert len(audio_chunks) == 4
    assert [len(chunk) for chunk in audio_chunks] == [960, 960, 960, 960]
    assert init["disable_streaming_partial"] is False


def test_streaming_transcription_records_full_probe_capabilities(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """full 探测只从实际 transcript 消息观察能力，不把 final.text 句号当标点。"""
    audio_path = tmp_path / "capabilities.wav"
    with wave.open(str(audio_path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 1920)

    class FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str | bytes] = []
            self.messages: asyncio.Queue[str] = asyncio.Queue()
            self.messages.put_nowait(json.dumps({"type": "init"}))

        async def __aenter__(self) -> "FakeWebSocket":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def send(self, payload: str | bytes) -> None:
            self.sent.append(payload)
            if (
                isinstance(payload, bytes)
                and len([item for item in self.sent if isinstance(item, bytes)]) == 1
            ):
                await self.messages.put(json.dumps({"type": "transcript.partial", "text": "你好"}))
            elif (
                isinstance(payload, bytes)
                and len([item for item in self.sent if isinstance(item, bytes)]) == 2
            ):
                await self.messages.put(
                    json.dumps(
                        {
                            "type": "transcript.segment",
                            "sentences": [{"text": "你好", "start": 0.0, "end": 0.1, "spk": "0"}],
                        }
                    )
                )
            elif isinstance(payload, str) and json.loads(payload).get("type") == "audio.end":
                await self.messages.put(
                    json.dumps(
                        {
                            "type": "transcript.final",
                            "text": "。",
                            "sentences": [],
                            "global_spk_list": [0, 99],
                            "usage": {"inference_ms": 12},
                        }
                    )
                )

        async def recv(self) -> str:
            return await self.messages.get()

    socket = FakeWebSocket()
    monkeypatch.setattr(etsstt.websockets, "connect", lambda *_args, **_kwargs: socket)
    result = asyncio.run(
        etsstt.transcribe_file(
            "127.0.0.1",
            8701,
            "test-model",
            str(audio_path),
            mode="stream",
            chunk_ms=30,
            input_rate=1.0,
            diarization=True,
            max_num_spks=2,
        )
    )

    init = json.loads(next(payload for payload in socket.sent if isinstance(payload, str)))
    assert result.ok
    assert result.observed == {
        "partials": True,
        "segments": True,
        "speakers_in_sentence": True,
        "global_spk_list": True,
        "punctuation_output": False,
        "usage_field": True,
        "finalizes_in_stream": True,
    }
    assert result.message_counts == {
        "init": 1,
        "transcript.partial": 1,
        "transcript.segment": 1,
        "transcript.final": 1,
    }
    assert result.final_text == "。"
    assert result.global_spk_list == [0, 99]
    assert init["max_num_spks"] == 2
    assert etsstt._has_punctuation("包含逗号，")


def test_final_repeated_sentences_do_not_duplicate_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """final 重复 segment 句子时只保留一份文本，避免 CER 被重复文本抬高。"""
    audio_path = tmp_path / "repeat.wav"
    with wave.open(str(audio_path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 16000)

    sentence = {"text": "你好。", "start": 0.1, "end": 0.9}

    class FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str | bytes] = []
            self.responses = iter(
                [
                    json.dumps({"type": "init"}),
                    json.dumps({"type": "transcript.segment", "sentences": [sentence]}),
                    json.dumps(
                        {
                            "type": "transcript.final",
                            "sentences": [sentence],
                            "global_spk_list": [0, 99],
                        }
                    ),
                ]
            )

        async def __aenter__(self) -> "FakeWebSocket":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def send(self, payload: str | bytes) -> None:
            self.sent.append(payload)

        async def recv(self) -> str:
            return next(self.responses)

    monkeypatch.setattr(etsstt.websockets, "connect", lambda *_args, **_kwargs: FakeWebSocket())
    result = asyncio.run(
        etsstt.transcribe_file(
            "127.0.0.1",
            8701,
            "qwen3-asr-1.7b",
            str(audio_path),
            diarization=True,
            max_num_spks=2,
        )
    )

    assert result.ok
    assert result.text == "你好。"
    assert len(result.segments) == 1
    assert result.segments[0]["speaker"] == "0"


def test_final_sentences_win_when_timestamps_are_refined(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """final 重发并微调时间戳时整体替换，避免同一句被算两次或标签错位。"""
    audio_path = tmp_path / "refined.wav"
    with wave.open(str(audio_path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 16000)

    class FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str | bytes] = []
            self.responses = iter(
                [
                    json.dumps({"type": "init"}),
                    json.dumps(
                        {
                            "type": "transcript.segment",
                            "sentences": [{"text": "你好。", "start": 0.1, "end": 0.9}],
                        }
                    ),
                    json.dumps(
                        {
                            "type": "transcript.final",
                            "sentences": [{"text": "你好。", "start": 0.2, "end": 1.0}],
                            "global_spk_list": [2, 99],
                        }
                    ),
                ]
            )

        async def __aenter__(self) -> "FakeWebSocket":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def send(self, payload: str | bytes) -> None:
            self.sent.append(payload)

        async def recv(self) -> str:
            return next(self.responses)

    monkeypatch.setattr(etsstt.websockets, "connect", lambda *_args, **_kwargs: FakeWebSocket())
    result = asyncio.run(
        etsstt.transcribe_file(
            "127.0.0.1",
            8701,
            "qwen3-asr-1.7b",
            str(audio_path),
            diarization=True,
            max_num_spks=3,
        )
    )

    assert result.ok
    assert result.text == "你好。"
    assert result.segments == [{"speaker": "2", "start_ms": 200, "end_ms": 1000, "text": "你好。"}]


def test_sentence_speakers_align_with_zero_duration_sentences() -> None:
    """逐句标签要包含零时长标点句，否则长会议会整体错位。"""
    rows = [
        {"speaker": "spk", "start_ms": 0, "end_ms": 1000, "text": "甲"},
        {"speaker": "spk", "start_ms": 1000, "end_ms": 1000, "text": "。"},
        {"speaker": "spk", "start_ms": 1000, "end_ms": 2000, "text": "乙"},
    ]
    assert _apply_sentence_speakers(rows, [0, 0, 1, 99]) is True
    assert [row["speaker"] for row in rows] == ["0", "0", "1"]


def test_final_zero_duration_sentence_consumes_last_label() -> None:
    """205 的 final 句号句落在列表末尾；此时 79 个标签对应 79 个句子。"""
    rows: dict[tuple[int, int, str], dict[str, Any]] = {}
    _collect_sentences(
        {
            "sentences": [
                {"text": "甲", "start": 0, "end": 900},
                {"text": "乙", "start": 900, "end": 1800},
            ]
        },
        rows,
    )
    _collect_sentences({"sentences": [{"text": "。", "start": 1800, "end": 1800}]}, rows)
    ordered = sorted(rows.values(), key=lambda row: (row["start_ms"], row["end_ms"]))
    assert _apply_sentence_speakers(ordered, [0, 1, 2]) is True
    assert [row["speaker"] for row in ordered] == ["0", "1", "2"]


def test_sentence_speaker_keeps_zero_id() -> None:
    """说话人 0 是合法编号，不能被 or 当成缺失。"""
    rows = [{"speaker": "spk", "start_ms": 0, "end_ms": 900, "text": "甲"}]
    assert _apply_sentence_speakers(rows, [0]) is True
    assert rows[0]["speaker"] == "0"


def test_intermediate_sentence_spk_is_ignored() -> None:
    """过程中的 spk 会实时变化，收集句子时不能当作最终说话人。"""
    rows: dict[tuple[int, int, str], dict[str, Any]] = {}
    _collect_sentences({"sentences": [{"text": "甲", "start": 0, "end": 900, "spk": "1"}]}, rows)
    collected = list(rows.values())
    assert collected[0]["speaker"] == "spk"


def test_final_global_speakers_override_intermediate_spk() -> None:
    """确认 final 是唯一可信来源：与过程中 spk 冲突时按 final 覆盖。"""
    rows: dict[tuple[int, int, str], dict[str, Any]] = {}
    _collect_sentences(
        {
            "sentences": [
                {"text": "甲", "start": 0, "end": 900, "spk": "0"},
                {"text": "乙", "start": 900, "end": 2000, "spk": "0"},
            ]
        },
        rows,
    )
    collected = list(rows.values())
    assert _apply_sentence_speakers(collected, [1, 2, 99]) is True
    assert [row["speaker"] for row in collected] == ["1", "2"]


def test_global_speaker_labels_accept_serialized_list() -> None:
    """部分固件把 global_spk_list 序列化为字符串；可解析时仍应使用。"""
    assert _coerce_global_labels("[0, 1, 2]") == [0, 1, 2]
    assert _coerce_global_labels("[0, 1, 2] (len=3)") == [0, 1, 2]
    assert _coerce_global_labels("[0, 1, ...] (len=1200)") is None
