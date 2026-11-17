"""本地假设备的协议回放契约测试。"""

from fakes.device_server import app
from fastapi.testclient import TestClient


def test_fake_device_reproduces_dot_final_and_missing_sentence_speaker() -> None:
    """假设备回放句号 final、无句级 spk 和带末尾哨兵的全局标签。"""
    with (
        TestClient(app) as client,
        client.websocket_connect("/v1/audio/transcription/realtime") as websocket,
    ):
        websocket.send_json({"type": "init", "model": "e2e-stt"})
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_bytes(b"\x00\x00")
        websocket.send_json({"type": "audio.end"})

        final = websocket.receive_json()

    sentences = final["sentences"]
    speakers = final["global_spk_list"]
    assert final["text"] == "。"
    assert all("spk" not in sentence for sentence in sentences)
    assert len(speakers) == len(sentences) + 1
