import asyncio
import base64
import json
import logging
import time
from pathlib import Path
from typing import Dict, Set, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from common.config import get_ice_servers, API_KEY, DETECTION_SAVE_DIR, GUIDED_ALT_DEFAULT, VIDEO_SAVE_DIR, VIDEO_MAX_S

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("signaling")

app = FastAPI(title="VPS-Raspi Signaling + Viewer")
# allow_credentials harus False bila allow_origins=["*"] — jika True browser menolak preflight X-API-Key
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)
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

# SSE live detection per-hari
sse_clients: Set[asyncio.Queue] = set()
sse_clients_lock = asyncio.Lock()

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
try:
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
except:
    pass

# Detection storage mount
DETECTION_DIR = Path(DETECTION_SAVE_DIR).resolve()
try:
    DETECTION_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/detections", StaticFiles(directory=str(DETECTION_DIR)), name="detections")
except Exception as e:
    logger.warning(f"[api] mount detections failed {e}")

# Video storage mount (mkv)
VIDEO_DIR = Path(VIDEO_SAVE_DIR).resolve()
try:
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/videos", StaticFiles(directory=str(VIDEO_DIR)), name="videos")
except Exception as e:
    logger.warning(f"[api] mount videos failed {e}")


def require_api_key(x_api_key: str | None = Header(None), authorization: str | None = Header(None)):
    # support X-API-Key or Authorization: Bearer <key>
    key = x_api_key
    if not key and authorization:
        if authorization.lower().startswith("bearer "):
            key = authorization[7:].strip()
        else:
            key = authorization.strip()
    if key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid api key, set header X-API-Key")
    return key

@app.get("/")
async def health():
    try:
        from vps.storage import count_detections
        det_count = count_detections()
    except:
        det_count = -1
    try:
        from vps.video_recorder import get_state as video_state, count_videos
        v_state = video_state()
        v_count = count_videos()
    except:
        v_state = {"recording": False}
        v_count = -1
    return {"status": "ok", "peers": list(peers.keys()), "viewer": "/viewer", "stream": "/stream.mjpg", "detections": det_count, "videos": v_count, "video_state": v_state, "raspi_connected": "raspi" in peers}

@app.get("/peers")
async def list_peers():
    return {"peers": list(peers.keys())}


# ---- SSE live detection per-hari (id only, live only) ----
@app.get("/api/detections/stream")
async def sse_detections_stream(request: Request, token: str | None = None):
    # EventSource tidak bisa header X-API-Key, pakai ?token=secret
    if token != API_KEY:
        # also allow header bearer for curl testing
        hdr = request.headers.get("x-api-key") or request.headers.get("authorization", "")
        if hdr.lower().startswith("bearer "):
            hdr = hdr[7:].strip()
        if hdr != API_KEY:
            raise HTTPException(status_code=401, detail="invalid token, use ?token=API_KEY")
    # per-hari paling baru: today Asia/Jakarta
    import datetime
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    queue: asyncio.Queue = asyncio.Queue()

    async with sse_clients_lock:
        sse_clients.add(queue)

    async def gen():
        try:
            # live only: tidak replay history, hanya push baru
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15)
                    # filter per-hari terbaru (live only)
                    # item is {"id":...,"date":YYYY-MM-DD} dari internal
                    if item.get("date") and item["date"] != today:
                        continue
                    # jika item tanpa date (legacy), tetap kirim
                    payload = json.dumps({"id": item.get("id")})
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    # heartbeat agar proxy tidak close (comment line)
                    yield ": ping\n\n"
        finally:
            async with sse_clients_lock:
                sse_clients.discard(queue)

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}
    return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)


@app.post("/api/internal/new_detection")
async def internal_new_detection(request: Request):
    # allow 127.0.0.1 tanpa token, atau qualquer host dengan ?token=API_KEY (untuk testing)
    client_host = request.client.host if request.client else ""
    token_q = request.query_params.get("token") or request.headers.get("x-api-key") or ""
    # if token matches, allow dari mana saja
    allow_via_token = token_q == API_KEY
    if client_host not in ("127.0.0.1", "::1", "testclient") and not allow_via_token:
        raise HTTPException(status_code=403, detail="internal only 127.0.0.1 (or use ?token=API_KEY)")
    try:
        data = await request.json()
    except:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    # also allow token in body
    if not allow_via_token and data.get("token") == API_KEY:
        allow_via_token = True
        if client_host not in ("127.0.0.1", "::1", "testclient"):
            logger.warning(f"[sse] internal called from {client_host} with body token, allowed for test")
    det_id = data.get("id")
    if not det_id:
        return JSONResponse({"ok": False, "error": "id required"}, status_code=400)
    # enrich date per-hari
    date = data.get("date")
    if not date:
        # try load from storage to get date
        try:
            from vps.storage import get_detection
            item = get_detection(det_id)
            if item:
                date = item.get("date") or time.strftime("%Y-%m-%d", time.localtime(item.get("ts", time.time())))
        except:
            date = time.strftime("%Y-%m-%d")
    item = {"id": det_id, "date": date}
    # broadcast ke semua SSE client (per-hari filter di gen())
    async with sse_clients_lock:
        for q in list(sse_clients):
            try:
                q.put_nowait(item)
            except:
                pass
    return {"ok": True, "broadcast": len(sse_clients), "item": item}

@app.get("/api/ice")
async def api_ice():
    return {"iceServers": get_ice_servers()}


# ---- Detection + Guided API ----
@app.get("/api/detections/days")
async def api_list_detection_days(x_api_key: str | None = Header(None), authorization: str | None = Header(None)):
    require_api_key(x_api_key, authorization)
    try:
        from vps.storage import list_days, migrate_flat_to_days
        # lazy migrate flat lama
        migrate_flat_to_days()
        days = list_days()
        return {"ok": True, "days": days, "total_days": len(days)}
    except Exception as e:
        logger.exception(f"[api] list days failed {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/detections")
async def api_list_detections(limit: int = 50, offset: int = 0, date: str | None = None, x_api_key: str | None = Header(None), authorization: str | None = Header(None)):
    require_api_key(x_api_key, authorization)
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    try:
        from vps.storage import list_detections, count_detections, migrate_flat_to_days
        migrate_flat_to_days()
        items = list_detections(limit=limit, offset=offset, date=date)
        total = count_detections(date=date)
        return {"ok": True, "total": total, "limit": limit, "offset": offset, "date": date, "items": items}
    except ValueError as ve:
        return JSONResponse({"ok": False, "error": str(ve)}, status_code=400)
    except Exception as e:
        logger.exception(f"[api] list detections failed {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/detections/{det_id}")
async def api_get_detection(det_id: str, x_api_key: str | None = Header(None), authorization: str | None = Header(None)):
    require_api_key(x_api_key, authorization)
    try:
        from vps.storage import get_detection
        data = get_detection(det_id)
        if data is None:
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        return {"ok": True, "item": data}
    except Exception as e:
        logger.exception(f"[api] get detection failed {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/guided")
async def api_post_guided(req: Request, x_api_key: str | None = Header(None), authorization: str | None = Header(None)):
    require_api_key(x_api_key, authorization)
    try:
        body = await req.json()
    except:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    lat = body.get("lat")
    lon = body.get("lon")
    alt = body.get("alt", GUIDED_ALT_DEFAULT)
    if lat is None or lon is None:
        return JSONResponse({"ok": False, "error": "lat and lon required"}, status_code=400)
    try:
        lat = float(lat); lon = float(lon); alt = float(alt) if alt is not None else GUIDED_ALT_DEFAULT
    except:
        return JSONResponse({"ok": False, "error": "lat/lon/alt must be numbers"}, status_code=400)
    if not (-90 <= lat <= 90):
        return JSONResponse({"ok": False, "error": "lat out of range -90..90"}, status_code=400)
    if not (-180 <= lon <= 180):
        return JSONResponse({"ok": False, "error": "lon out of range -180..180"}, status_code=400)
    if not (0 < alt < 10000):
        return JSONResponse({"ok": False, "error": "alt out of range"}, status_code=400)

    # 1) try DataChannel via vps.main_vps if embedded
    dc_result = None
    try:
        from vps.main_vps import send_guided_via_dc
        dc_result = await send_guided_via_dc(lat, lon, alt)
        # if DC fallback flag, still do WS as well
        if dc_result and dc_result.get("ok") and not dc_result.get("fallback"):
            return {"ok": True, "via": "datachannel", "sent": {"lat": lat, "lon": lon, "alt": alt}, "result": dc_result}
    except Exception as e:
        logger.debug(f"[api] DC guided not available {e}")
        dc_result = None

    # 2) WS fallback: relay via signaling peers
    target_ws = peers.get("raspi")
    if target_ws is None:
        return JSONResponse({"ok": False, "error": "raspi not connected", "via": "none", "dc_result": dc_result}, status_code=503)
    try:
        payload = {"type": "guided", "lat": lat, "lon": lon, "alt": alt}
        await target_ws.send_text(json.dumps({**payload, "from": "vps"}))
        # also broadcast to vps peer for main_vps to pick guided_ack
        logger.info(f"[api] guided via WS lat={lat} lon={lon} alt={alt}")
        return {"ok": True, "via": "ws", "sent": payload, "dc_result": dc_result}
    except Exception as e:
        logger.exception(f"[api] guided WS send failed {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ---- Video record API (mkv h264 raw 20fps max 1 jam, start/stop) ----
@app.get("/api/videos/state")
async def api_video_state(x_api_key: str | None = Header(None), authorization: str | None = Header(None)):
    require_api_key(x_api_key, authorization)
    try:
        from vps.video_recorder import get_state
        return {"ok": True, "state": get_state()}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/videos")
async def api_list_videos(limit: int = 50, offset: int = 0, x_api_key: str | None = Header(None), authorization: str | None = Header(None)):
    require_api_key(x_api_key, authorization)
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    try:
        from vps.video_recorder import list_videos, count_videos, total_size_mb
        items = list_videos(limit=limit, offset=offset)
        return {"ok": True, "total": count_videos(), "total_size_mb": total_size_mb(), "limit": limit, "offset": offset, "items": items}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/videos/start")
async def api_video_start(x_api_key: str | None = Header(None), authorization: str | None = Header(None)):
    require_api_key(x_api_key, authorization)
    try:
        from vps.video_recorder import is_recording, set_state, get_state
        if is_recording():
            return JSONResponse({"ok": False, "error": "already recording", "state": get_state()}, status_code=409)
        # create file path YYYYMMDD_HHMMSS.mkv
        ts = time.time()
        name = time.strftime("%Y%m%d_%H%M%S", time.localtime(ts))
        # avoid collision same second
        base = VIDEO_DIR / f"{name}.mkv"
        idx = 0
        path = base
        while path.exists():
            idx += 1
            path = VIDEO_DIR / f"{name}_{idx}.mkv"
        set_state(str(path), ts)
        logger.info(f"[video] start {path}")
        return {"ok": True, "id": path.stem, "path": f"/videos/{path.name}", "file": path.name, "start_ts": ts, "fps": 20, "codec": "h264", "container": "mkv", "max_s": VIDEO_MAX_S}
    except Exception as e:
        logger.exception(f"[video] start failed {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/videos/stop")
async def api_video_stop(x_api_key: str | None = Header(None), authorization: str | None = Header(None)):
    require_api_key(x_api_key, authorization)
    try:
        from vps.video_recorder import is_recording, clear_state, get_state
        state = get_state()
        if not state.get("recording"):
            return JSONResponse({"ok": False, "error": "not recording", "state": state}, status_code=400)
        # clear marker, main_vps will close within 0.5s
        old = clear_state()
        # wait up to 2s for file to be closed (poll)
        path = Path(old.get("path", "")) if old else None
        for _ in range(20):
            await asyncio.sleep(0.1)
            if path and path.exists():
                # file should be finalized; check if recording cleared
                if not is_recording():
                    break
        # gather info
        info = {"ok": True, "stopped": True, "previous": old}
        if path and path.exists():
            try:
                st = path.stat()
                info["file"] = path.name
                info["path"] = f"/videos/{path.name}"
                info["size_mb"] = round(st.st_size / 1024 / 1024, 2)
                info["duration_s"] = round(time.time() - float(old.get("start_ts", time.time())), 1) if old else None
            except:
                pass
        logger.info(f"[video] stop {path}")
        return info
    except Exception as e:
        logger.exception(f"[video] stop failed {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

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
