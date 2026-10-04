# -*- coding: utf-8 -*-
"""Faster-Whisper 本地语音识别服务：OpenAI 兼容接口，供 AIRI 接入。

端点：
  POST /v1/audio/transcriptions   识别语音（multipart 上传音频），返回 {"text": "..."}
  WS   /ws                        流式识别（PCM16 二进制帧；文本帧 {"type":"end"} 结束）
  GET  /v1/models                 模型列表
  GET  /health                    健康检查

启动：python whisper_server.py     （首次运行自动下载模型，small 约 460MB）
"""
import asyncio
import json
import time

import numpy as np
import uvicorn
from fastapi import FastAPI, File, Form, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

MODEL_SIZE = "small"  # 中文识别质量明显好于 base/tiny；可改 "base"(145MB) 提速
# 快速回退：GPU 显存不够或识别异常时，改回下面两行 CPU 配置即可
#   DEVICE = "cpu"
#   COMPUTE_TYPE = "int8"
DEVICE = "cuda"          # RTX 3050 上 small 约 5~10 倍提速
COMPUTE_TYPE = "int8_float16"  # 显存占用最小的 GPU 模式；仍有压力可改 "int8"
PORT = 8931
SR = 16000            # AIRI 上传的直播音频为 16kHz 单声道 PCM16

# 流式识别参数：一句话结束（尾部静音超过该值）就立即转写并推送给前端
MIN_TRAIL_SILENCE = int(1.0 * SR)   # 句尾至少 1s 静音才切（逗号停顿通常 <0.5s，避免切断句子）
MIN_DRAIN_BYTES = SR * 2              # 每攒够 1s 音频做一次 VAD 断句检查
MAX_BUFFER_S = 30                     # 保险丝：单句超过 30s 强制切一次

app = FastAPI(title="Faster-Whisper Local ASR")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

whisper_model = None
model_lock = None
vad_lock = None


def _now():
    return time.strftime('%H:%M:%S')


@app.on_event("startup")
def startup():
    global whisper_model, model_lock, vad_lock
    import threading
    if DEVICE == "cuda":
        # ctranslate2 loads cublas64_12.dll via a search that only honors the process
        # PATH (add_dll_directory is ignored), so prepend the pip CUDA libs before
        # faster_whisper (and thus ctranslate2) gets imported.
        import os
        import faster_whisper  # only to locate site-packages; CUDA libs still load lazily
        _site = os.path.dirname(os.path.dirname(faster_whisper.__file__))
        for _lib in ("cublas", "cudnn"):
            _p = os.path.join(_site, "nvidia", _lib, "bin")
            if os.path.isdir(_p):
                os.environ["PATH"] = _p + os.pathsep + os.environ.get("PATH", "")
    from faster_whisper import WhisperModel
    t0 = time.time()
    print(f"[{_now()}] 正在加载 faster-whisper ({MODEL_SIZE}, {DEVICE})...", flush=True)
    whisper_model = WhisperModel(
        MODEL_SIZE,
        device=DEVICE,
        compute_type=COMPUTE_TYPE,
        cpu_threads=4,        # 单次识别用 4 线程
    )
    # CTranslate2 模型线程安全：用信号量允许多路并发识别（最多 2 路，共 8 线程），
    # 突发排队时吞吐翻倍；同时必须给 VoxCPM 合成（GPU 的数据供给吃 CPU）留够核心，
    # 20 核机器上 8 线程识别 + 合成 + 浏览器刚好平衡，再多就互相拖死。
    model_lock = threading.BoundedSemaphore(2)
    vad_lock = threading.Lock()
    print(f"[{_now()}] 模型就绪，耗时 {time.time()-t0:.0f}s", flush=True)


# Whisper 幻听时会把提示词原样输出，这里把这些回声直接过滤掉
_HALLUCINATION_FRAGMENTS = ("请使用简体中文字", "以下是普通话的句子", "以下是普通话的句子，请使用简体中文字。")


def _filter_hallucination(text: str) -> str:
    t = text.strip()
    if not t:
        return ""
    for frag in _HALLUCINATION_FRAGMENTS:
        if t == frag or t == frag + "。":
            return ""
    return t


# ---------- 回声过滤：不把数字人自己说的话当用户输入 ----------
# 数字人喇叭播出的声音会被麦克风收回并识别出来（声学回声）。
# 向 TTS 服务查询最近合成过的文本，相似即判定为回声丢弃。
import difflib
import re
import urllib.request

_TTS_RECENT_URL = "http://localhost:8930/v1/recent_speeches?window=45"
_ECHO_MIN_LEN = 4


def _norm_text(s: str) -> str:
    return re.sub(r"[\s，。！？、,.!?~～…\"'“”‘’：:；;（）()《》\-—]+", "", s).lower()


def _is_echo(text: str) -> bool:
    t = _norm_text(text)
    if len(t) < _ECHO_MIN_LEN:
        return False
    try:
        with urllib.request.urlopen(_TTS_RECENT_URL, timeout=1.5) as r:
            speeches = json.loads(r.read().decode("utf-8")).get("texts", [])
    except Exception:
        return False  # TTS 服务不可达时不过滤，宁可漏过也不误杀
    for sp in speeches:
        s = _norm_text(sp)
        if len(s) < _ECHO_MIN_LEN:
            continue
        # 互相包含（回声常只录到半句）
        if t in s or s in t:
            return True
        # 模糊相似（房间声学导致个别字识别错）
        if difflib.SequenceMatcher(None, t, s).ratio() >= 0.55:
            return True
    return False


def _filter_echo(text: str) -> str:
    t = text.strip()
    if t and _is_echo(t):
        print(f"[{_now()}] 回声过滤：「{t[:25]}」与近期合成文本重复，丢弃", flush=True)
        return ""
    return t


def _transcribe_np(audio_f32: np.ndarray, vad_filter: bool = True) -> str:
    """阻塞式转写（在线程池里调用）。vad_filter 抑制静音幻听。"""
    with model_lock:
        segments, _ = whisper_model.transcribe(
            audio_f32, beam_size=1, language="zh",  # beam=1 贪婪解码，CPU 上快 3~5 倍
            initial_prompt="以下是普通话的句子，请使用简体中文字。",
            vad_filter=vad_filter,
            condition_on_previous_text=False,
        )
        return _filter_hallucination("".join(seg.text for seg in segments))


async def _transcribe(audio_f32: np.ndarray, vad_filter: bool = True) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _transcribe_np(audio_f32, vad_filter=vad_filter))


async def _vad_spans(audio_f32: np.ndarray):
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    loop = asyncio.get_running_loop()

    def _run():
        with vad_lock:
            return get_speech_timestamps(
                audio_f32,
                VadOptions(
                    threshold=0.5,
                    min_speech_duration_ms=250,
                    min_silence_duration_ms=500,
                    speech_pad_ms=200,
                ),
                sampling_rate=SR,
            )

    return await loop.run_in_executor(None, _run)


def _pcm16_to_f32(buf: bytearray) -> np.ndarray:
    n_even = len(buf) - (len(buf) % 2)
    return np.frombuffer(bytes(buf[:n_even]), dtype=np.int16).astype(np.float32) / 32768.0


@app.get("/health")
def health():
    return {"status": "ok", "model_loaded": whisper_model is not None}


@app.get("/v1/models")
def list_models():
    return {"object": "list", "data": [
        {"id": "whisper-local", "object": "model", "created": 0, "owned_by": "local"},
    ]}


@app.post("/v1/audio/transcriptions")
async def transcribe(file: UploadFile = File(...), model: str = Form("whisper-local")):
    if whisper_model is None:
        return JSONResponse({"error": "model not loaded"}, status_code=503)

    audio_bytes = await file.read()
    if not audio_bytes:
        return JSONResponse({"error": "empty audio"}, status_code=400)

    import numpy as np
    import soundfile as sf
    import io
    try:
        audio, sr = sf.read(io.BytesIO(audio_bytes), dtype="float32")
    except Exception:
        # 非 wav 格式（如 webm/m4a）用 av 解码
        import av
        buf = io.BytesIO(audio_bytes)
        with av.open(buf) as container:
            frames = [f.to_ndarray().reshape(-1).astype("float32") / 32768.0
                      for f in container.decode(audio=0)]
        audio = np.concatenate(frames)
        sr = 16000

    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != 16000:
        import librosa  # noqa  一般不会走到这里，AIRI 上传的是 16k wav
        audio = librosa.resample(audio, orig_sr=sr, target_sr=16000)

    # 静音闸门：没有足够时长的有效语音就直接返回空，
    # 避免 Whisper 在纯噪音/超短音频上幻听出提示词文本（如"请使用简体中文字。"）
    if len(audio) < SR * 0.8:
        print(f"[{_now()}] 音频过短（{len(audio)/SR:.1f}s），跳过识别", flush=True)
        return {"text": ""}
    spans = await _vad_spans(audio)
    speech_s = sum((sp["end"] - sp["start"]) for sp in spans) / SR
    if speech_s < 0.6:
        print(f"[{_now()}] 有效语音不足（{speech_s:.1f}s），返回空文本", flush=True)
        return {"text": ""}

    t0 = time.time()
    # 放到线程池执行，避免 whisper 阻塞事件循环（事件循环一卡，
    # 流式识别的 SSE 推送和音频上传读取全部停摆）
    text = await _transcribe(audio, vad_filter=True)
    text = _filter_echo(text)  # 回声过滤：数字人自己说的话不送给大模型
    dur = time.time() - t0
    print(f"[{_now()}] 识别 {len(audio)/16000:.1f}s 音频，"
          f"耗时 {dur:.1f}s → {text[:30]}", flush=True)
    return {"text": text}


async def _streaming_session(recv_chunk, send_event):
    """流式识别主逻辑。

    recv_chunk: async 函数，返回 bytes；返回 None 表示音频流正常结束。
    send_event: async 函数，接收 dict 事件并推给客户端。
    AIRI 前端自带 VAD，一次会话对应一句话；服务端再用 silero VAD 在流内
    断句：句子一结束就立刻转写，以 snapshot 事件推送中间文字（边说边出、
    不提交）；音频流结束时转写剩余部分，发 isFinal=true 的 snapshot，
    AIRI 收到后会提交文本并自动发送。
    """
    buf = bytearray()
    finalized = ""
    try:
        while True:
            try:
                chunk = await recv_chunk()
            except WebSocketDisconnect:
                print(f"[{_now()}] 客户端中止，放弃收尾", flush=True)
                return
            if chunk is None:
                break
            if not chunk:
                continue
            buf.extend(chunk)
            # 攒够 1s 音频做一次断句检查
            while len(buf) >= MIN_DRAIN_BYTES:
                audio = _pcm16_to_f32(buf)
                spans = await _vad_spans(audio)
                cut = 0
                skip_transcribe = False
                if not spans:
                    # 整段静音，直接丢掉（前端 VAD 已保证整句有效）
                    cut = len(audio)
                    skip_transcribe = True
                else:
                    # 已完结的语音段 = 结束点之后还留有足够静音。
                    # 切到最后一个已完结段的末尾，正在说的句子留在缓冲里。
                    completed = [sp for sp in spans
                                 if sp["end"] + MIN_TRAIL_SILENCE <= len(audio)]
                    if completed:
                        cut = int(completed[-1]["end"])
                    elif len(audio) >= MAX_BUFFER_S * SR:
                        # 保险丝：超长单句强制切分
                        cut = int(spans[-1]["end"])
                if cut <= 0:
                    break
                part = audio[:cut]
                text = ""
                if not skip_transcribe:
                    t0 = time.time()
                    text = await _transcribe(part)
                    print(f"[{_now()}] 流式段识别 {len(part)/SR:.1f}s，"
                          f"耗时 {time.time()-t0:.1f}s → {text[:30]}", flush=True)
                if text:
                    finalized += text
                    await send_event({
                        "type": "transcript.text.snapshot",
                        "text": finalized,
                        "isFinal": False,
                    })
                del buf[:cut * 2]

        # 音频流结束：转写剩余音频，发最终 snapshot
        print(f"[{_now()}] 音频上传完毕，缓冲 {len(buf)/2/SR:.1f}s，开始收尾", flush=True)
        n_even = len(buf) - (len(buf) % 2)
        tail = ""
        if n_even >= SR // 4:
            audio = _pcm16_to_f32(buf)
            spans = await _vad_spans(audio)
            if spans:
                tail = await _transcribe(audio)
                print(f"[{_now()}] 收尾转写完成 → {tail[:30]}", flush=True)
        total = (finalized + tail).strip()
        total = _filter_echo(total)  # 回声过滤：数字人自己说的话不送给大模型
        if total:
            await send_event({
                "type": "transcript.text.snapshot",
                "text": total,
                "isFinal": True,
            })
    except Exception as e:
        print(f"[{_now()}] 流式识别出错: {type(e).__name__} {e}", flush=True)


@app.websocket("/ws")
async def ws_transcribe(ws: WebSocket):
    """流式识别 WebSocket：客户端持续发送 PCM16 二进制帧，
    文本帧 {"type":"end"} 表示一句话说完；服务端推送 JSON 识别事件。
    """
    if whisper_model is None:
        await ws.close(code=1013)
        return

    await ws.accept()
    print(f"[{_now()}] WS 连接建立", flush=True)

    async def recv_chunk():
        msg = await ws.receive()
        if msg["type"] == "websocket.disconnect":
            raise WebSocketDisconnect()
        if msg.get("text") is not None:
            # 控制消息（目前只有 {"type":"end"}）
            return None
        return msg.get("bytes") or b""

    async def send_event(ev):
        await ws.send_text(json.dumps(ev, ensure_ascii=False))

    await _streaming_session(recv_chunk, send_event)
    try:
        await ws.close()
    except Exception:
        pass


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
