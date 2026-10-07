# lily-human

本地运行的中文语音数字人：以 [AIRI](https://github.com/moeru-ai/airi) 为前端形象与对话框架，
后端由一组本地 Python 微服务提供语音合成（TTS）、语音识别（ASR，含声纹回声过滤）和
Claude Code 语音桥接。全部服务只监听 `127.0.0.1`，无需联网即可对话（LLM 除外）。

## 架构

```
浏览器 AIRI 前端（Live2D 形象 + 语音对话，localhost:5173）
   │ OpenAI 兼容 API / WebSocket
   ├─ voxcpm_server.py        :8930  VoxCPM TTS（支持 voices/*.wav 音色克隆）
   ├─ sensevoice_server.py    :8931  SenseVoiceSmall ASR（默认；含声纹回声过滤、/ws 流式识别）
   ├─ channel_stub_server.py  :6121  AIRI 通道服务桩（最小握手，消除重试报错）
   └─ claude_bridge_server.py :8932  Claude Code 桥接（可选，SSE 进度 + 语音问答确认）
          │ claude-agent-sdk
          └─ claude.exe（继承本机 Claude Code 登录态）
```

- ASR 回声过滤两道：**声纹层**（sherpa-onnx CAM++，VAD 分段级剔除数字人自己的嗓音，
  阈值 `SPEAKER_SIM_THRESHOLD`，默认 0.45）+ **文本层**（与近期 TTS 合成文本比对）。
- 对 AIRI 本体的全部改动见 [airi-patches/](airi-patches/README.md)（增量补丁，不内嵌源码）。
- `whisper_server.py` 是与 sensevoice 完全同构（同端口同端点）的 A/B 备选 ASR，
  想换只需停 sensevoice、起 whisper，前端配置不用动。

## 目录结构

```
airi-patches/   AIRI 增量补丁（diff + 新增文件，基于 moeru-ai/airi@2f59a4c）
claude-bridge/  Claude Code 桥接服务（HTTP/SSE，详见 claude-bridge/README.md）
services/       TTS / ASR / 通道桩服务 + 启动/注入脚本
voices/         TTS 音色克隆参考音频与参考文本（default）
models/         模型权重（不入库，见下方安装步骤 3）
envs/           Python venv（不入库，见安装步骤 2）
```

## 环境要求

| 项目 | 要求 |
|---|---|
| 操作系统 | Windows 10/11（服务脚本为 bat + PowerShell 5.1） |
| Python | 3.12（venv 安装，见步骤 2） |
| GPU | NVIDIA 4GB+ 显存（voxcpm 串行合成，4GB 为实测下限；CPU 模式未调优） |
| Node.js | 20+ 与 pnpm（AIRI 前端构建） |
| Claude Code | 可选，仅语音桥接功能需要（本机已登录即可） |

> **路径约定**：代码与脚本中写死了 `F:\digital-human` 绝对路径（清单见步骤 4）。
> 最简单的做法是把本仓库克隆到 `F:\digital-human`；换其他目录请按清单替换。

## 安装步骤

### 1. 克隆本仓库 + AIRI 前端

```bash
git clone https://github.com/kyosho81/lily-human.git F:\digital-human
cd F:\digital-human

# AIRI 本体（不入库，按其官方流程获取后打补丁）
git clone https://github.com/moeru-ai/airi.git
cd airi && git checkout 2f59a4c && cd ..
# 打补丁与新增文件，步骤见 airi-patches/README.md
```

打补丁后在 `airi/` 内构建前端：

```bash
cd airi/apps/stage-web
pnpm install
pnpm build          # 若 pnpm 触发自动安装后 postinstall 报错，见下方排障
```

### 2. 建 Python 环境

```bash
cd F:\digital-human
py -3.12 -m venv envs\voxcpm
envs\voxcpm\Scripts\python.exe -m pip install --upgrade pip

# CUDA 版 torch（voxcpm 必需；版本以 wheels/ 备注或官网为准）
envs\voxcpm\Scripts\python.exe -m pip install torch==2.9.1 torchaudio==2.9.1 --index-url https://download.pytorch.org/whl/cu126

# 服务端依赖
envs\voxcpm\Scripts\python.exe -m pip install voxcpm==2.0.3 funasr==1.4.16 faster-whisper==1.2.1 \
    sherpa-onnx==1.13.8 fastapi uvicorn soundfile librosa modelscope

# Claude 桥接依赖（构建隔离下会失败，必须先装 hatchling）
envs\voxcpm\Scripts\python.exe -m pip install hatchling
envs\voxcpm\Scripts\python.exe -m pip install --no-build-isolation claude-agent-sdk
```

### 3. 下载模型（不入库，共约 1.6GB）

**VoxCPM-0.5B TTS（约 1.5GB，国内推荐 ModelScope）**：

```bash
envs\voxcpm\Scripts\python.exe -m modelscope download --model OpenBMB/VoxCPM --local_dir models\voxcpm-0.5b
# 或 pip install huggingface_hub 后：hf download OpenBMB/VoxCPM --local-dir models\voxcpm-0.5b
```

**SenseVoiceSmall ASR（CPU，约 1GB）**：

```bash
envs\voxcpm\Scripts\python.exe -m modelscope download --model iic/SenseVoiceSmall --local_dir "models\models\iic--SenseVoiceSmall"
```

> 注意是 `models\models\...` 两级目录——`sensevoice_server.py` 的 `MODEL_PATH` 如此写死。

**声纹过滤模型 CAM++（28MB）**：

```bash
curl -L -o models\speaker_id\3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx ^
  https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx
```

**VAD 模型（silero-vad，约 2.2MB）**：放 `airi/apps/stage-web/public/models/silero-vad/`，
按 AIRI 正常流程下载或自行放置（见 airi-patches/README.md 注意一节）。

### 4. 路径适配（若未克隆到 F:\digital-human）

| 文件 | 写死内容 |
|---|---|
| `services/启动语音服务.bat` | `F:\digital-human` 全部路径（PY、cd、桥接脚本路径） |
| `services/start-web5173.bat` | airi 目录与日志路径 |
| `services/voxcpm_server.py` | `MODEL_PATH`（第 57 行） |
| `services/sensevoice_server.py` | `MODEL_PATH`、`_SPEAKER_MODEL`、`_SPEAKER_ENROLL_WAVS` |
| `claude-bridge/config.json` | 首次启动自动生成，按本机改 `claude_bin_dir` 与 `cwd_whitelist` |

### 5. 准备音色（声音克隆）

1. 录一段 3~5 秒清晰人声（16k 单声道 wav），存为 `voices/default.wav`，
   并把对应文本写进 `voices/default.txt`。没有这段音频时 voxcpm 退回底模音色。
2. 生成声纹登记音频（把 TTS 实际输出音色也登记进回声过滤器）——**先在步骤 6 启动服务，
   voxcpm 就绪后**执行：

```bash
# 请求体放文件是为了避开 Windows shell 的中文引号问题
echo {"input":"你好呀，我是你的数字人助手，很高兴见到你。","voice":"default"} > req.json
curl -s -X POST http://localhost:8930/v1/audio/speech -H "Content-Type: application/json" -d @req.json -o models\speaker_id\enroll_tts.wav
```

然后重启 sensevoice（右键托盘/任务管理器结束 sensevoice_server.py 相关 python 进程后重跑启动 bat）。
启动日志出现 `声纹过滤器就绪：登记 2 条音色` 即成功；失败会打印原因并退回纯文本过滤。

### 6. 启动与验证

```bash
python services\apply_web_patches.py     # 注入流水线面板等补丁；每次 vite build 后必跑
services\启动语音服务.bat                # 拉起 TTS/ASR/通道桩/桥接 + 前端 5173
```

> `services/web-patches/config-restore.js` 含本机 LLM API key，**不入库**。
> 缺失时 `apply_web_patches.py` 自动跳过，不影响其他补丁。

三个健康检查应全部 `{"status":"ok", ...}`（TTS 首次加载模型需 1-2 分钟）：

```bash
curl http://localhost:8930/health
curl http://localhost:8931/health
curl http://localhost:8932/health
```

浏览器打开 `http://localhost:5173`：

1. 设置里配置 LLM provider（如 Moonshot/Kimi 的 baseURL + API key）
2. TTS provider 指向 `http://localhost:8930/v1/`，模型 `voxcpm-tts-0.5b`
3. 语音识别指向 `http://localhost:8931/v1/`，模型 `sensevoice-small`
4. 直接语音对话测试；右下角"流水线"面板可实时看 识别/合成 各阶段耗时与丢段计数

## 运维与排障

| 现象 | 原因与处理 |
|---|---|
| 页面没有流水线面板/配置没恢复 | `vite build` 冲掉了注入，重跑 `apply_web_patches.py` 后 **Ctrl+F5** 强刷 |
| 改了前端代码不生效 | dist 是构建产物：改 `airi/` 源码 → `pnpm build` → `apply_web_patches.py` → Ctrl+F5 |
| TTS 红字 503 `server busy` | voxcpm 串行合成本质，补丁已把前端合成改为严格串行；仍出现说明前端是旧 bundle，Ctrl+F5 |
| "合成完成但没播放" | 看页面 `window.__TTS_AUDIT` 漏斗计数定位（req→resOk→pbStart→pbEnd 哪一环掉了），勿凭感觉改 |
| ASR 把数字人自己的话当用户输入 | 看 sensevoice 日志 `声纹过滤`/`回声过滤` 行；丽丽克隆源自某位真人时该真人也会被声纹层过滤（已知取舍） |
| 怀疑 ASR 句首丢字 | 设环境变量 `DUMP_WS_AUDIO=1` 启动 sensevoice，每会话原始音频落到 `logs\ws_dump_*.wav`，对比实际收音 |
| `pnpm build` 报缺 `unrun` 模块 | pnpm 自动全量 install 触发 postinstall 失败：`pnpm --config.verify-deps-before-run=false build` |
| airi 仓库提交时 pre-commit 挂 | 同上原因（pnpm verify-deps-before-run）；本仓库提交脚本见 git 历史做法 |
| ASR 设置页 confidence-threshold 不生效 | SenseVoice 服务端不支持 `verbose_json`，该设置优雅降级忽略，属预期 |
| 换 whisper ASR | 停 sensevoice 进程，直接跑 `services/whisper_server.py`（同端口同端点），前端无感；注意 whisper 版没有声纹过滤 |

## License

[MIT](LICENSE)（airi 本体为 [moeru-ai/airi](https://github.com/moeru-ai/airi)，MIT License，版权归属 Neko Ayaka）
