# AIRI 通道服务桩（localhost:6121）
# 作用：AIRI 浏览器版会反复尝试连接 ws://localhost:6121/ws（桌面版外部程序通道），
# 连不上就刷错误并无限重试。本服务做最小握手应答，让连接保持、控制台干净。
# 协议：接收 superjson 或纯 JSON 文本帧；回复用纯 JSON（客户端兼容）。
import json
import time

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

app = FastAPI()


def decode(text):
    """兼容 superjson（{"json": ...} 包装）与纯 JSON。"""
    try:
        obj = json.loads(text)
    except Exception:
        return None
    if isinstance(obj, dict) and "json" in obj and isinstance(obj["json"], dict):
        return obj["json"]
    return obj if isinstance(obj, dict) else None


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            text = await websocket.receive_text()
            event = decode(text)
            if not event:
                continue
            etype = event.get("type")
            data = event.get("data") or {}
            if etype == "extension:module:announce":
                # 握手：回显 name/identity，客户端据此判定就绪
                await websocket.send_text(json.dumps({
                    "type": "extension:module:announced",
                    "data": {"name": data.get("name"), "identity": data.get("identity")},
                }, ensure_ascii=False))
            elif etype == "transport:connection:heartbeat" and data.get("kind") == "ping":
                await websocket.send_text(json.dumps({
                    "type": "transport:connection:heartbeat",
                    "data": {"kind": "pong", "message": "🩵", "at": int(time.time() * 1000)},
                }, ensure_ascii=False))
            # 其余事件（context:update 等）一律忽略
    except WebSocketDisconnect:
        pass
    except RuntimeError:
        pass


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=6121, log_level="warning")
