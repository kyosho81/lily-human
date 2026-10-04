# -*- coding: utf-8 -*-
"""模拟 AIRI：通过 WebSocket 分块流式上传 PCM16，打印收到的识别事件。"""
import asyncio
import json
import sys
import time

import soundfile as sf
import websockets

wav_path = sys.argv[1] if len(sys.argv) > 1 else r"F:\digital-human\voices\default.wav"
chunk_s = 0.3

audio, sr = sf.read(wav_path, dtype="int16")
if audio.ndim > 1:
    audio = audio.mean(axis=1)
assert sr == 16000
pcm = audio.astype("<i2").tobytes()
chunk_bytes = int(chunk_s * 16000) * 2

async def main():
    t0 = time.time()
    async with websockets.connect("ws://127.0.0.1:8931/ws") as ws:
        async def send():
            for i in range(0, len(pcm), chunk_bytes):
                await ws.send(pcm[i:i + chunk_bytes])
                await asyncio.sleep(chunk_s)
            await ws.send(json.dumps({"type": "end"}))

        sender = asyncio.create_task(send())
        try:
            async for msg in ws:
                ev = json.loads(msg)
                print(f"[t={time.time()-t0:5.1f}s] {ev}")
        finally:
            sender.cancel()
    print(f"总耗时 {time.time()-t0:.1f}s（音频长 {len(pcm)/2/16000:.1f}s）")

asyncio.run(main())
