import asyncio
import base64
import json
import logging
import time
from pathlib import Path
from typing import Dict, Set, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from common.config import get_ice_servers

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("signaling")

app = FastAPI(title="VPS-Raspi Signaling + Viewer")
peers: Dict[str, WebSocket] = {}

latest_jpeg: Optional[bytes] = None
latest_detections: list = []
latest_ts: float = 0
latest_width: int = 640
latest_height: int = 480
latest_frame_id: int = 0
viewer_clients: Set[WebSocket] = set()
_frame_lock = asyncio.Lock()
_new_frame_event = asyncio.Event()
_frame_version = 0

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
try:
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
except:
    pass

@app.get("/")
async def health():
    return {"status": "ok", "peers": list(peers.keys()), "viewer": "/viewer", "stream": "/stream.mjpg"}

@app.get("/peers")
async def list_peers():
    return {"peers": list(peers.keys())}

@app.get("/api/ice")
async def api_ice():
    return {"iceServers": get_ice_servers()}

@app.get("/viewer", response_class=HTMLResponse)
async def viewer():
    p = STATIC_DIR / "viewer.html"
    try:
        return HTMLResponse(p.read_text())
    except:
        return HTMLResponse("viewer.html not found", status_code=404)

@app.get("/api/state")
async def api_state():
    return {"ts": latest_ts, "frame_id": latest_frame_id, "width": latest_width, "height": latest_height, "objects": latest_detections, "has_frame": latest_jpeg is not None, "peers": list(peers.keys())}

@app.post("/api/frame")
async def post_frame(req: Request):
    global latest_jpeg, latest_detections, latest_ts, latest_width, latest_height, latest_frame_id, _frame_version
    try:
        data = await req.json()
    except:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    b64 = data.get("jpeg_b64") or data.get("jpeg") or ""
    if not b64:
        return JSONResponse({"error": "jpeg_b64 required"}, status_code=400)
    try:
        jpeg = base64.b64decode(b64)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    async with _frame_lock:
        latest_jpeg = jpeg
        latest_detections = data.get("detections") or data.get("objects") or []
        latest_ts = data.get("ts") or time.time()
        latest_width = int(data.get("width") or 640)
        latest_height = int(data.get("height") or 480)
        latest_frame_id = int(data.get("frame_id") or 0)
        _frame_version += 1
    _new_frame_event.set()
    payload = {"type": "detection", "ts": latest_ts, "frame_id": latest_frame_id, "width": latest_width, "height": latest_height, "objects": latest_detections}
    for ws in list(viewer_clients):
        try:
            await ws.send_text(json.dumps(payload))
        except:
            viewer_clients.discard(ws)
    return {"ok": True, "frame_id": latest_frame_id, "viewers": len(viewer_clients)}

@app.get("/stream.mjpg")
async def mjpeg_stream():
    boundary = "frame"
    async def gen():
        for _ in range(100):
            if latest_jpeg is not None:
                break
            await asyncio.sleep(0.1)
        last = -1
        while True:
            try:
                await asyncio.wait_for(_new_frame_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            async with _frame_lock:
                if _frame_version == last:
                    _new_frame_event.clear()
                    continue
                jpeg = latest_jpeg
                last = _frame_version
                _new_frame_event.clear()
            if jpeg is None:
                continue
            yield f"--{boundary}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpeg)}\r\nCache-Control: no-cache\r\n\r\n".encode() + jpeg + b"\r\n"
    headers = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}
    return StreamingResponse(gen(), media_type=f"multipart/x-mixed-replace; boundary={boundary}", headers=headers)

@app.websocket("/ws/viewer")
async def ws_viewer(ws: WebSocket):
    await ws.accept()
    viewer_clients.add(ws)
    try:
        await ws.send_text(json.dumps({"type": "state", "ts": latest_ts, "frame_id": latest_frame_id, "width": latest_width, "height": latest_height, "objects": latest_detections, "peers": list(peers.keys())}))
        await ws.send_text(json.dumps({"type": "peers", "peers": list(peers.keys())}))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        viewer_clients.discard(ws)

@app.websocket("/ws/{peer_id}")
async def ws_endpoint(ws: WebSocket, peer_id: str):
    await ws.accept()
    peers[peer_id] = ws
    await broadcast({"type": "peer-joined", "peer_id": peer_id}, exclude=peer_id)
    await notify_viewers_peers()
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except:
                await ws.send_text(json.dumps({"type": "error", "msg": "invalid json"}))
                continue
            msg["from"] = peer_id
            target = msg.get("target")
            if target:
                dest = peers.get(target)
                if dest:
                    await dest.send_text(json.dumps(msg))
                else:
                    await ws.send_text(json.dumps({"type": "error", "msg": f"target {target} not connected"}))
            else:
                await broadcast(msg, exclude=peer_id)
    except WebSocketDisconnect:
        pass
    finally:
        peers.pop(peer_id, None)
        await broadcast({"type": "peer-left", "peer_id": peer_id})
        await notify_viewers_peers()

async def broadcast(msg: dict, exclude: str | None = None):
    raw = json.dumps(msg)
    for pid, sock in list(peers.items()):
        if pid == exclude:
            continue
        try:
            await sock.send_text(raw)
        except:
            pass

async def notify_viewers_peers():
    payload = json.dumps({"type": "peers", "peers": list(peers.keys())})
    for ws in list(viewer_clients):
        try:
            await ws.send_text(payload)
        except:
            viewer_clients.discard(ws)

def main():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

if __name__ == "__main__":
    main()
