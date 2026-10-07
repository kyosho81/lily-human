# -*- coding: utf-8 -*-
"""VoxCPM 本地 TTS 服务：OpenAI 兼容接口，供 AIRI 接入。

端点：
  POST /v1/audio/speech   合成语音（body: {model, input, voice}），返回 WAV
  GET  /v1/models         模型列表（AIRI 用它筛选 tts 模型）
  GET  /health            健康检查

voice 参数：默认 "default" 用模型自带音色；
若 voices/ 目录下有 {voice}.wav（+ 可选 {voice}.txt 参考文本），则用该音频做音色克隆。

启动：python voxcpm_server.py        （模型加载约 20~160 秒，首次编译最慢）
"""
import argparse
import io
import os
import threading
import time

# torchinductor/triton 默认把编译缓存放在系统 Temp 下，会被存储感知等
# 清理任务在中途删掉，导致 InductorError: FileNotFoundError（tmp 目录消失）。
# 固定到非 Temp 目录，避免再次复发。
_CACHE_DIR = r"F:\digital-human\cache\torchinductor"
os.makedirs(_CACHE_DIR, exist_ok=True)
os.environ.setdefault("TORCHINDUCTOR_CACHE_DIR", _CACHE_DIR)
os.environ.setdefault("TRITON_CACHE_DIR", os.path.join(_CACHE_DIR, "triton"))

import numpy as np
import soundfile as sf
import torch
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

# Windows 上 triton-windows 与 torch 2.9 静态启动器有兼容问题，关闭它
torch._inductor.config.use_static_cuda_launcher = False

# torchcodec 0.17 是给 torch 2.14 编译的，与当前 torch 2.9 ABI 不兼容，
# 会导致 torchaudio.load 加载参考音频时报错；用 soundfile 顶替
import torchaudio


def _safe_load(path, *args, **kwargs):
    import soundfile as sf
    data, sr = sf.read(str(path), dtype="float32")
    if data.ndim == 1:
        data = data[None, :]  # (1, N)
    else:
        data = data.T
    return torch.from_numpy(np.ascontiguousarray(data)), sr


torchaudio.load = _safe_load

MODEL_PATH = "F:/digital-human/models/voxcpm-0.5b"
VOICES_DIR = "F:/digital-human/voices"
PORT = 8930

# --fp16 开关：True 时模型以 torch.float16 加载（省显存/提速），默认 False 保持原有行为
USE_FP16 = False

app = FastAPI(title="VoxCPM Local TTS")
# AIRI 在浏览器里跨端口调用本服务，放开 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
model = None
model_lock = threading.Lock()  # 4GB 显存放不下并发，串行合成

# 最近合成过的文本（时间戳, 文本），供语音识别端做回声过滤：
# 数字人喇叭播出的声音被麦克风收回、识别出来，不能再当用户输入送给大模型
recent_speeches = []
recent_lock = threading.Lock()
RECENT_MAX = 50


class SpeechRequest(BaseModel):
    model: str = "voxcpm-tts-0.5b"
    input: str
    voice: str = "default"
    response_format: str = "wav"


def load_model():
    global model
    from voxcpm import VoxCPM
    t0 = time.time()
    print(f"[{time.strftime('%H:%M:%S')}] 正在加载 VoxCPM（CUDA{'，FP16' if USE_FP16 else ''}）...", flush=True)
    kwargs = {}
    if USE_FP16:
        kwargs["dtype"] = torch.float16
    model = VoxCPM(
        voxcpm_model_path=MODEL_PATH,
        enable_denoiser=False,
        optimize=True,
        device="cuda",
        **kwargs,
    )
    print(f"[{time.strftime('%H:%M:%S')}] 模型就绪，耗时 {time.time()-t0:.0f}s", flush=True)


def wav_bytes(audio: np.ndarray) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, audio, 16000, format="WAV", subtype="PCM_16")
    return buf.getvalue()


@app.on_event("startup")
def startup():
    load_model()


@app.get("/health")
def health():
    return {"status": "ok", "model_loaded": model is not None}


@app.get("/v1/models")
def list_models():
    return {"object": "list", "data": [
        {"id": "voxcpm-tts-0.5b", "object": "model", "created": 0, "owned_by": "local"},
    ]}


@app.post("/v1/audio/speech")
def speech(req: SpeechRequest, request: Request):
    if model is None:
        return Response(content='{"error":"model not loaded"}', status_code=503,
                        media_type="application/json")

    text = req.input.strip()
    if not text:
        return Response(content='{"error":"empty input"}', status_code=400,
                        media_type="application/json")

    print(f"[{time.strftime('%H:%M:%S')}.{(time.time()*1000)%1000:03.0f}] 合成请求到达：{text[:25]}",
          flush=True)

    # 音色：voices/{voice}.wav 存在则做克隆
    import os
    prompt_wav = None
    prompt_text = None
    voice_wav = os.path.join(VOICES_DIR, f"{req.voice}.wav")
    voice_txt = os.path.join(VOICES_DIR, f"{req.voice}.txt")
    if os.path.exists(voice_wav):
        prompt_wav = voice_wav
        if os.path.exists(voice_txt):
            prompt_text = open(voice_txt, encoding="utf-8").read().strip()

    t_arr = time.time()
    # 4GB 显存只够串行合成；高峰期语音洪峰会把等待队列堆到几分钟，
    # 排到时句子早已过期（用户新的语音输入会打断播放），
    # 与其 stale 播放不如快速拒绝，让客户端丢句保最新。
    if not model_lock.acquire(timeout=10):
        return Response(content='{"error":"server busy"}', status_code=503,
                        media_type="application/json")
    try:
        t0 = time.time()
        audio = model.generate(
            text,
            prompt_wav_path=prompt_wav,
            prompt_text=prompt_text,
            inference_timesteps=6,  # 默认10步；6步平衡速度与听感（此前调为4步提速，现改回）
        )
    finally:
        model_lock.release()
    dur = time.time() - t0
    with recent_lock:
        recent_speeches.append((time.time(), text))
        if len(recent_speeches) > RECENT_MAX:
            del recent_speeches[:len(recent_speeches) - RECENT_MAX]
    print(f"[{time.strftime('%H:%M:%S')}] 合成 {len(audio)/16000:.1f}s 音频，"
          f"排队 {t0 - t_arr:.1f}s 耗时 {dur:.1f}s（{req.input[:20]}...）", flush=True)
    return Response(content=wav_bytes(np.asarray(audio)), media_type="audio/wav")


@app.get("/v1/recent_speeches")
def get_recent_speeches(window: float = 45.0):
    """返回 window 秒内合成过的文本列表，供 ASR 端过滤回声。"""
    now = time.time()
    with recent_lock:
        return {"texts": [t for ts, t in recent_speeches if now - ts < window]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VoxCPM 本地 TTS 服务")
    parser.add_argument("--fp16", action="store_true", default=False,
                        help="以 torch.float16 加载模型（省显存/提速），默认关闭")
    args = parser.parse_args()
    USE_FP16 = args.fp16
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
