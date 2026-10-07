# -*- coding: utf-8 -*-
"""SenseVoiceSmall 本地语音识别服务：OpenAI 兼容接口，供 AIRI 接入。

与 whisper_server.py 完全同构（同端口 8931、同端点），用于 A/B 对比：
  POST /v1/audio/transcriptions   识别语音（multipart 上传音频），返回 {"text": "..."}
  WS   /ws                        流式识别（PCM16 二进制帧；文本帧 {"type":"end"} 结束）
  GET  /v1/models                 模型列表
  GET  /health                    健康检查

切换方式：停掉 whisper_server.py，启动本文件（或反之），AIRI 配置不用动。
SenseVoiceSmall 为非自回归模型，CPU 上比 whisper-turbo 快数倍，中文专优。
"""
import asyncio
import json
import os
import re
import time

import numpy as np
import uvicorn
from fastapi import FastAPI, File, Form, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

MODEL_PATH = r"F:\digital-human\models\models\iic--SenseVoiceSmall\snapshots\master"
DEVICE = "cpu"
PORT = 8931
SR = 16000            # AIRI 上传的直播音频为 16kHz 单声道 PCM16

# 流式识别参数：一句话结束（尾部静音超过该值）就立即转写并推送给前端
MIN_TRAIL_SILENCE = int(1.0 * SR)   # 句尾至少 1s 静音才切（逗号停顿通常 <0.5s，避免切断句子）
MIN_DRAIN_BYTES = SR * 2              # 每攒够 1s 音频做一次 VAD 断句检查
MAX_BUFFER_S = 30                     # 保险丝：单句超过 30s 强制切一次

app = FastAPI(title="SenseVoiceSmall Local ASR")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

sv_model = None
model_lock = None
vad_lock = None


def _now():
    return time.strftime('%H:%M:%S')


@app.on_event("startup")
def startup():
    global sv_model, model_lock, vad_lock
    import threading
    from funasr import AutoModel
    t0 = time.time()
    print(f"[{_now()}] 正在加载 SenseVoiceSmall ({DEVICE})...", flush=True)
    sv_model = AutoModel(
        model=MODEL_PATH,
        device=DEVICE,
        disable_update=True,
    )
    # SenseVoice 单线程已很快（非自回归），并发 2 路防突发排队即可
    model_lock = threading.BoundedSemaphore(2)
    vad_lock = threading.Lock()
    _speaker_init()
    print(f"[{_now()}] 模型就绪，耗时 {time.time()-t0:.0f}s", flush=True)


# SenseVoice 输出带元信息标签：😊<|zh|><|HAPPY|><|Speech|>内容<|/Speech|><|woitn|>
_META_TAG = re.compile(r"<\|[^|]*\|>")
# 句首情感 emoji（SenseVoice 的情绪标记，不是正文）
_LEADING_EMOJI = re.compile(r"^[\U0001F300-\U0001FAFF☀-➿\U0001F1E6-\U0001F1FF]+")


def _normalize(text: str) -> str:
    t = _META_TAG.sub("", text)
    t = _LEADING_EMOJI.sub("", t).strip()
    return t


# ---------- 回声过滤：不把数字人自己说的话当用户输入 ----------
# 与 whisper_server 相同的策略：向 TTS 服务查询最近合成过的文本，相似即丢弃。
import difflib
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
        if t in s or s in t:
            return True
        if difflib.SequenceMatcher(None, t, s).ratio() >= 0.55:
            return True
    return False


def _filter_echo(text: str) -> str:
    t = text.strip()
    if t and _is_echo(t):
        print(f"[{_now()}] 回声过滤：「{t[:25]}」与近期合成文本重复，丢弃", flush=True)
        return ""
    return t


# ---------- 声纹过滤：把数字人自己的嗓音从识别输入中剔除 ----------
# 与文本回声过滤互补：声纹层在 VAD 分段级别剔除"声音是丽丽"的语音段
# （不管说的是什么），文本层再兜底内容重复的段。任何异常一律放行（fail-open）。
_SPEAKER_MODEL = r"F:\digital-human\models\speaker_id\3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"
_SPEAKER_ENROLL_WAVS = [
    r"F:\digital-human\voices\default.wav",                # 克隆提示音
    r"F:\digital-human\models\speaker_id\enroll_tts.wav",  # 实际合成输出
]
_SPEAKER_SIM_THRESHOLD = float(os.environ.get("SPEAKER_SIM_THRESHOLD", "0.45"))
_SPEAKER_MIN_SPAN_S = 0.6   # 短于该时长的 VAD 段特征不够，不做声纹判断直接放行

speaker_extractor = None
speaker_enrolled = None     # 归一化的登记嵌入（各登记音频嵌入的均值）


def _speaker_embed(extractor, samples_f32: np.ndarray):
    stream = extractor.create_stream()
    stream.accept_waveform(SR, samples_f32)
    stream.input_finished()
    if not extractor.is_ready(stream):
        return None
    emb = np.asarray(extractor.compute(stream), dtype=np.float32)
    n = float(np.linalg.norm(emb))
    return emb / n if n > 1e-9 else None


def _speaker_init():
    global speaker_extractor, speaker_enrolled
    try:
        import sherpa_onnx
        import soundfile as sf
        extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=_SPEAKER_MODEL, num_threads=2))
        embs = []
        for path in _SPEAKER_ENROLL_WAVS:
            samples, sr = sf.read(path, dtype="float32")
            if samples.ndim > 1:
                samples = samples.mean(axis=1)
            if sr != SR:
                import librosa
                samples = librosa.resample(samples, orig_sr=sr, target_sr=SR)
            emb = _speaker_embed(extractor, np.asarray(samples, dtype=np.float32))
            if emb is not None:
                embs.append(emb)
        if not embs:
            raise RuntimeError("no enrollment embeddings")
        mean = np.mean(embs, axis=0)
        speaker_enrolled = mean / (np.linalg.norm(mean) + 1e-9)
        speaker_extractor = extractor
        print(f"[{_now()}] 声纹过滤器就绪：登记 {len(embs)} 条音色，阈值 {_SPEAKER_SIM_THRESHOLD}",
              flush=True)
    except Exception as e:
        speaker_extractor = None
        print(f"[{_now()}] 声纹过滤器初始化失败（退回纯文本过滤）："
              f"{type(e).__name__} {e}", flush=True)


def _filter_speaker_spans(audio_f32: np.ndarray, spans) -> np.ndarray:
    """剔除与登记音色（数字人）匹配的 VAD 语音段，返回剩余音频。"""
    if speaker_extractor is None or speaker_enrolled is None or not spans:
        return audio_f32
    try:
        kept = []
        dropped = 0
        for sp in spans:
            seg = audio_f32[sp["start"]:sp["end"]]
            if (sp["end"] - sp["start"]) < _SPEAKER_MIN_SPAN_S * SR:
                kept.append(seg)
                continue
            emb = _speaker_embed(speaker_extractor, seg)
            if emb is None:
                kept.append(seg)
                continue
            sim = float(np.dot(emb, speaker_enrolled))
            if sim >= _SPEAKER_SIM_THRESHOLD:
                dropped += 1
                print(f"[{_now()}] 声纹过滤：丢弃 {len(seg)/SR:.1f}s 语音段"
                      f"（相似度 {sim:.2f}）", flush=True)
                continue
            kept.append(seg)
        if dropped == 0:
            return audio_f32
        if not kept:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(kept)
    except Exception as e:
        print(f"[{_now()}] 声纹过滤异常（放行）：{type(e).__name__} {e}", flush=True)
        return audio_f32


def _transcribe_np(audio_f32: np.ndarray) -> str:
    """阻塞式转写（在线程池里调用）。SenseVoice 自带抗噪，无需 VAD 过滤。"""
    with model_lock:
        res = sv_model.generate(
            input=audio_f32,
            cache={},
            language="zh",
            use_itn=True,
            batch_size=1,
        )
    return _normalize(res[0]["text"] if res else "")


async def _transcribe(audio_f32: np.ndarray) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _transcribe_np(audio_f32))


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
    return {"status": "ok", "model_loaded": sv_model is not None}


@app.get("/v1/models")
def list_models():
    return {"object": "list", "data": [
        {"id": "sensevoice-small", "object": "model", "created": 0, "owned_by": "local"},
    ]}


@app.post("/v1/audio/transcriptions")
async def transcribe(file: UploadFile = File(...), model: str = Form("sensevoice-small")):
    if sv_model is None:
        return JSONResponse({"error": "model not loaded"}, status_code=503)

    audio_bytes = await file.read()
    if not audio_bytes:
        return JSONResponse({"error": "empty audio"}, status_code=400)

    import soundfile as sf
    import io
    try:
        audio, sr = sf.read(io.BytesIO(audio_bytes), dtype="float32")
    except Exception:
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
        import librosa
        audio = librosa.resample(audio, orig_sr=sr, target_sr=16000)

    # 静音闸门（与 whisper_server 相同）
    if len(audio) < SR * 0.8:
        print(f"[{_now()}] 音频过短（{len(audio)/SR:.1f}s），跳过识别", flush=True)
        return {"text": ""}
    spans = await _vad_spans(audio)
    speech_s = sum((sp["end"] - sp["start"]) for sp in spans) / SR
    if speech_s < 0.6:
        print(f"[{_now()}] 有效语音不足（{speech_s:.1f}s），返回空文本", flush=True)
        return {"text": ""}

    t0 = time.time()
    audio = _filter_speaker_spans(audio, spans)
    if len(audio) < SR * 0.3:
        print(f"[{_now()}] 声纹过滤后无剩余语音，返回空文本", flush=True)
        return {"text": ""}
    text = await _transcribe(audio)
    text = _filter_echo(text)
    dur = time.time() - t0
    print(f"[{_now()}] 识别 {len(audio)/16000:.1f}s 音频，"
          f"耗时 {dur:.1f}s → {text[:30]}", flush=True)
    return {"text": text}


async def _streaming_session(recv_chunk, send_event):
    """流式识别主逻辑（与 whisper_server 相同）。"""
    buf = bytearray()
    finalized = ""
    dump = None
    if os.environ.get("DUMP_WS_AUDIO") == "1":
        import soundfile as sf
        path = os.path.join(r"F:\digital-human\logs",
                            f"ws_dump_{time.strftime('%H%M%S')}.wav")
        dump = sf.SoundFile(path, "w", samplerate=SR, channels=1, subtype="PCM_16")
        print(f"[{_now()}] WS 音频转储 → {path}", flush=True)
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
            if dump:
                dump.write(np.frombuffer(bytes(chunk), dtype=np.int16))
            while len(buf) >= MIN_DRAIN_BYTES:
                audio = _pcm16_to_f32(buf)
                spans = await _vad_spans(audio)
                cut = 0
                skip_transcribe = False
                if not spans:
                    cut = len(audio)
                    skip_transcribe = True
                else:
                    completed = [sp for sp in spans
                                 if sp["end"] + MIN_TRAIL_SILENCE <= len(audio)]
                    if completed:
                        cut = int(completed[-1]["end"])
                    elif len(audio) >= MAX_BUFFER_S * SR:
                        cut = int(spans[-1]["end"])
                if cut <= 0:
                    break
                part = audio[:cut]
                text = ""
                if not skip_transcribe:
                    part_spans = [sp for sp in spans if sp["end"] <= cut]
                    part = _filter_speaker_spans(part, part_spans)
                    if len(part) >= SR * 0.3:
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

        print(f"[{_now()}] 音频上传完毕，缓冲 {len(buf)/2/SR:.1f}s，开始收尾", flush=True)
        n_even = len(buf) - (len(buf) % 2)
        tail = ""
        if n_even >= SR // 4:
            audio = _pcm16_to_f32(buf)
            spans = await _vad_spans(audio)
            if spans:
                audio = _filter_speaker_spans(audio, spans)
                if len(audio) >= SR * 0.3:
                    tail = await _transcribe(audio)
                    print(f"[{_now()}] 收尾转写完成 → {tail[:30]}", flush=True)
        total = (finalized + tail).strip()
        total = _filter_echo(total)
        if total:
            await send_event({
                "type": "transcript.text.snapshot",
                "text": total,
                "isFinal": True,
            })
    except Exception as e:
        print(f"[{_now()}] 流式识别出错: {type(e).__name__} {e}", flush=True)
    finally:
        if dump:
            dump.close()


@app.websocket("/ws")
async def ws_transcribe(ws: WebSocket):
    if sv_model is None:
        await ws.close(code=1013)
        return

    await ws.accept()
    print(f"[{_now()}] WS 连接建立", flush=True)

    async def recv_chunk():
        msg = await ws.receive()
        if msg["type"] == "websocket.disconnect":
            raise WebSocketDisconnect()
        if msg.get("text") is not None:
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