# -*- coding: utf-8 -*-
"""VoxCPM-0.5B CPU 合成测试：加载模型，合成一句中文，存 wav 并计时。"""
import time

import soundfile as sf
from voxcpm import VoxCPM

MODEL_PATH = "F:/digital-human/models/voxcpm-0.5b"
OUT_PATH = "F:/digital-human/services/test_output.wav"
TEXT = "你好呀，我是你的数字人助手，很高兴见到你。"

t0 = time.time()
print("正在加载模型（CPU）...", flush=True)
model = VoxCPM(
    voxcpm_model_path=MODEL_PATH,
    enable_denoiser=False,  # 跳过 zipenhancer 下载
    optimize=True,
    device="cuda",
)
print(f"模型加载完成，耗时 {time.time() - t0:.1f}s", flush=True)

t0 = time.time()
print("开始合成...", flush=True)
audio = model.generate(TEXT)
dur = time.time() - t0

sf.write(OUT_PATH, audio, 16000)
audio_len = len(audio) / 16000
print(f"合成完成：{len(audio)} 采样点 ≈ {audio_len:.2f}s 音频", flush=True)
print(f"耗时 {dur:.1f}s，RTF = {dur / audio_len:.2f}", flush=True)
print(f"已保存到 {OUT_PATH}", flush=True)
