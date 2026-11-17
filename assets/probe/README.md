# M2 设备探测素材

这组素材依据 `docs/m2-device-design.md` 附录 A 制作，用于设备能力探测和声纹链路自检。三段 WAV 均为中文神经语音合成结果，不包含真人录音、开源数据集切片、背景音乐或环境噪声；两位说话人分别使用不同的合成音色。

| 文件 | 内容 | 时长 |
|---|---|---:|
| `probe_spk_A.wav` | 合成说话人 A 的单人注册音频 | 6 秒 |
| `probe_spk_B.wav` | 合成说话人 B 的单人注册音频 | 6 秒 |
| `probe_two_spk_10s.wav` | A、B 交替对话，中段包含 800ms 重叠 | 10 秒 |

全部音频为 16kHz、16bit、单声道 PCM WAV。双人音频包含中文逗号、句号和问号；A、B 的有效说话时长均超过 3 秒。`probe_ref.json` 保存逐段文本、说话人和时间戳，`manifest.json` 保存声线、生成文本、响度及 SHA-256。

声线：A=`zh-CN-XiaoxiaoNeural`，B=`zh-CN-YunxiNeural`。重新生成需要联网使用 Edge TTS 和本机 `ffmpeg`：

```bash
~/.local/bin/uv run --with edge-tts python tools/generate_probe_assets.py
```

运行时只读取已提交的音频文件，不依赖 TTS 服务。
