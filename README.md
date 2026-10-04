# digital-human

本地运行的中文语音数字人：以 [AIRI](https://github.com/moeru-ai/airi) 为前端形象与对话框架，
后端由一组本地 Python 微服务提供语音合成（TTS）、语音识别（ASR）和 Claude Code 语音桥接。
全部服务只监听 `127.0.0.1`，无需联网即可对话（LLM 除外）。

## 架构

```
浏览器 AIRI 前端（Live2D 形象 + 语音对话）
   │ OpenAI 兼容 API / WebSocket
   ├─ voxcpm_server.py        :8930  VoxCPM TTS（支持 voices/*.wav 音色克隆）
   ├─ whisper_server.py       :8931  Faster-Whisper ASR（含 /ws 流式识别）
   ├─ channel_stub_server.py  :6121  AIRI 通道服务桩（最小握手，消除重试报错）
   └─ claude_bridge_server.py :8932  Claude Code 桥接（SSE 进度 + 语音问答确认）
          │ claude-agent-sdk
          └─ claude.exe（继承本机 Claude Code 登录态）
```

对 AIRI 本体的全部改动见 [airi-patches/](airi-patches/README.md)（增量补丁，不内嵌源码）。

## 目录结构

```
airi-patches/   AIRI 增量补丁（diff + 新增文件，基于 moeru-ai/airi@2f59a4c）
claude-bridge/  Claude Code 桥接服务（HTTP/SSE，详见 claude-bridge/README.md）
services/       TTS / ASR / 通道桩服务 + 启动脚本
voices/         TTS 音色克隆参考音频与参考文本（default）
```

## 依赖

| 依赖 | 说明 |
|---|---|
| [moeru-ai/airi](https://github.com/moeru-ai/airi) | 前端（MIT，按其官方流程 pnpm 安装，再打本项目补丁） |
| Python 3.12 + CUDA | 建议按 `wheels/` 里的 torch 2.9 cu126 安装（不入库，自行下载） |
| [VoxCPM-0.5B](https://huggingface.co/OpenBMB/VoxCPM) | TTS 模型权重（约 1.5GB，不入库，放 `models/voxcpm-0.5b/`） |
| Faster-Whisper | ASR 模型首次运行自动下载 |
| claude-agent-sdk | Claude 桥接依赖（安装需先 `pip install hatchling` 再 `--no-build-isolation`） |

> 启动脚本 `services/启动语音服务.bat` 内写的是本机绝对路径
> （`F:\digital-human\...`），换机器使用请自行替换。

## 快速开始

1. 按 [airi-patches/README.md](airi-patches/README.md) 克隆并打好 AIRI 补丁，`pnpm install && pnpm build`
2. 下载 VoxCPM 模型权重到 `models/voxcpm-0.5b/`
3. 运行 `services/启动语音服务.bat`（自动按命令行杀旧进程、防双开，拉起全部 4 个服务 + 前端）
4. 浏览器打开 `http://localhost:5173`，在设置里配置 LLM provider（如 Moonshot/Kimi）
   与本地 TTS/ASR（`http://localhost:8930/v1/`、`http://localhost:8931/v1/`）

## License

[MIT](LICENSE)（airi 本体为 [moeru-ai/airi](https://github.com/moeru-ai/airi)，MIT License，版权归属 Neko Ayaka）
