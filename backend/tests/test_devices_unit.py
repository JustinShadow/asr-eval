"""设备指纹与模型选择的单元测试。"""

from types import SimpleNamespace

import pytest

from app.adapters.etsstt import _pick_stt_model
from app.domain import devices as devices_domain
from app.domain.devices import compute_uid, resolve_probe_mode


def _identity(model: str = "etsstt") -> dict[str, object]:
    return {
        "platform": "205",
        "kind": "etsstt",
        "bigversion": None,
        "etsllm_version": None,
        "accel": {"type": "rknn"},
        "model_key": model,
        "model_version": None,
    }


def test_uid_is_stable_and_ignores_host() -> None:
    assert compute_uid(_identity()) == compute_uid(_identity())
    assert len(compute_uid(_identity())) == 16


def test_uid_changes_with_model() -> None:
    assert compute_uid(_identity("etsstt")) != compute_uid(_identity("qwen3-asr-1.7b"))


def test_auto_probe_mode_uses_full_when_idle_and_identity_when_leased(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一键检测空闲时跑 full，Run 租约有效时自动降为 identity。"""
    device = SimpleNamespace(kind="etsstt", id="device-1")
    monkeypatch.setattr(devices_domain, "_lease_active", lambda *_args: False)
    assert resolve_probe_mode(None, device, "auto") == "full"

    monkeypatch.setattr(devices_domain, "_lease_active", lambda *_args: True)
    assert resolve_probe_mode(None, device, "auto") == "identity"
    assert resolve_probe_mode(None, SimpleNamespace(kind="cloud", id="cloud-1"), "auto") == "full"


def test_pick_stt_model() -> None:
    models = [
        {"id": "llm", "extra_info": {"model_type": "llm"}},
        {"id": "etsstt", "extra_info": {"model_type": "stt", "accel": "rknn"}},
    ]
    assert _pick_stt_model(models)["id"] == "etsstt"
