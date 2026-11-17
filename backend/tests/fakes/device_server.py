"""回放最小转写协议，供不访问真机的 E2E 主路径使用。"""

import asyncio
import json
import os
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

app = FastAPI()
TRANSCRIPT = os.environ.get("ASR_E2E_TRANSCRIPT", "2024年这是一条合成回归样本")
_FINAL_REQUESTS = 0


@app.get("/v1/models")
def models() -> dict[str, list[dict[str, object]]]:
    """返回与 E2E 部署快照一致的语音识别模型。"""
    return {
        "data": [
            {
                "id": "e2e-stt",
                "extra_info": {"model_type": "stt", "accel": "fake"},
            }
        ]
    }


@app.websocket("/v1/audio/transcription/realtime")
async def transcribe(websocket: WebSocket) -> None:
    """接收 E2E WAV，并按设备协议返回对应参考文本。"""
    global _FINAL_REQUESTS

    await websocket.accept()
    try:
        init = json.loads(await websocket.receive_text())
        if init.get("type") != "init":
            await websocket.send_json({"type": "error", "message": "缺少 init"})
            return
        await websocket.send_json({"type": "ready", "capabilities": {}})

        while True:
            message = await websocket.receive()
            # 探测只读取 init 后会关闭连接；返回 disconnect 后应退出，不能再次 receive。
            if message.get("type") == "websocket.disconnect":
                return
            if message.get("bytes") is not None:
                continue
            payload = message.get("text")
            if payload is None:
                continue
            command = json.loads(payload)
            if command.get("type") == "audio.end":
                _FINAL_REQUESTS += 1
                if os.environ.get("ASR_E2E_HOLD_FIRST_FINAL") == "1" and _FINAL_REQUESTS == 1:
                    marker = os.environ.get("ASR_E2E_FIRST_FINAL_MARKER")
                    if marker:
                        await asyncio.to_thread(Path(marker).touch)
                    # 进程恢复 E2E 在此等待首次 app 崩溃带来的 WebSocket disconnect。
                    continue
                # 模拟 205 的 final.text 恒为句号；识别文本从 sentences 聚合（AGENTS.md 领域约束）。
                await websocket.send_json(
                    {
                        "type": "transcript.final",
                        "text": "。",
                        "sentences": [{"text": TRANSCRIPT, "start_ms": 0, "end_ms": 1000}],
                        # 模拟协议坑：句子没有 spk；末尾标签是句末哨兵（M2 §2.3）。
                        "global_spk_list": [0, 99],
                        "usage": {"duration_ms": 1000},
                    }
                )
                return
            if command.get("type") == "close":
                await websocket.send_json({"type": "closed"})
                return
    except WebSocketDisconnect:
        return
