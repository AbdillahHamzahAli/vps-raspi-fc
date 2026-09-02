import asyncio
import json
import logging
import os
import time

import cv2
import numpy as np
from av import VideoFrame
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer, VideoStreamTrack
from aiortc.sdp import candidate_from_sdp

from common.config import (
    get_ice_servers, get_signaling_url, get_signaling_http_base,
    DETECTION_EVERY_N_FRAMES, DETECTION_THROTTLE_MS,
    BROWSER_VIEWER_ENABLED, VIEWER_PUSH_URL, VIEWER_FPS, VIEWER_QUALITY,
    VIDEO_WIDTH, VIDEO_HEIGHT,
)
from common.signaling import SignalingClient
from vps.detector import get_detector

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vps")

PEER_ID = os.getenv("VPS_PEER_ID", "vps")
RASPI_ID = os.getenv("RASPI_PEER_ID", "raspi")

latest_view_bgr: np.ndarray | None = None
viewer_interval_ms = int(1000 / max(1, VIEWER_FPS))

class RelayTrack(VideoStreamTrack):
    kind = "video"

    async def recv(self):
        pts, time_base = await self.next_timestamp()
        bgr = latest_view_bgr
        if bgr is None:
            bgr = np.zeros((VIDEO_HEIGHT, VIDEO_WIDTH, 3), dtype=np.uint8)
        vf = VideoFrame.from_ndarray(bgr, format="bgr24")
        vf.pts = pts
        vf.time_base = time_base
        return vf

def ice_config():
    servers = []
    for s in get_ice_servers():
        servers.append(RTCIceServer(urls=s["urls"], username=s.get("username"), credential=s.get("credential")))
    return RTCConfiguration(iceServers=servers)

async def run():
    signaling = SignalingClient(get_signaling_url(PEER_ID))
    await signaling.connect()

    viewer_http = VIEWER_PUSH_URL or get_signaling_http_base() + "/api/frame"
    viewer_session = None
    if BROWSER_VIEWER_ENABLED:
        import aiohttp
        viewer_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2))
        logger.info(f"[vps] viewer push {viewer_http} fps={VIEWER_FPS}")

    async def push_viewer(jpeg: bytes, dets: list, fid: int, w: int, h: int):
        if not BROWSER_VIEWER_ENABLED or viewer_session is None:
            return
        try:
            import base64
            payload = {"jpeg_b64": base64.b64encode(jpeg).decode(), "detections": dets, "ts": time.time(), "frame_id": fid, "width": w, "height": h}
            async with viewer_session.post(viewer_http, json=payload) as r:
                if r.status != 200:
                    logger.warning(f"[viewer] push {r.status} {await r.text()}")
        except Exception as e:
            logger.debug(f"[viewer] push error {e}")

    raspi_pc = RTCPeerConnection(configuration=ice_config())
    viewer_pcs: dict[str, RTCPeerConnection] = {}
    detector = get_detector()
    data_channel = None

    @raspi_pc.on("datachannel")
    def on_dc(ch):
        nonlocal data_channel
        data_channel = ch
        @ch.on("open")
        def _o(): logger.info("[vps] DataChannel OPEN")
        @ch.on("message")
        def _m(m): logger.info(f"[vps] dc msg {m}")
        @ch.on("close")
        def _c(): logger.info("[vps] DataChannel closed")

    frame_count = 0
    last_det_ms = 0
    last_log_ms = 0
    last_view_ms = 0
    cached_dets: list = []

    @raspi_pc.on("track")
    def on_track(track):
        nonlocal frame_count, last_det_ms, last_log_ms, last_view_ms, cached_dets
        if track.kind != "video":
            return
        async def consume():
            nonlocal frame_count, last_det_ms, last_log_ms, last_view_ms, cached_dets
            global latest_view_bgr
            while True:
                try:
                    frame: VideoFrame = await track.recv()
                except:
                    break
                frame_count += 1
                now = int(time.time() * 1000)
                if now - last_log_ms > 2000:
                    logger.info(f"[vps] frames {frame_count}")
                    last_log_ms = now
                try:
                    bgr = frame.to_ndarray(format="bgr24")
                except:
                    continue

                if BROWSER_VIEWER_ENABLED and now - last_view_ms >= viewer_interval_ms:
                    last_view_ms = now
                    try:
                        ann = bgr.copy()
                        for d in cached_dets:
                            x1, y1, x2, y2 = d["xyxy"]
                            cv2.rectangle(ann, (x1, y1), (x2, y2), (0, 255, 0), 2)
                            cv2.putText(ann, f"{d['cls']} {d['conf']:.2f}", (x1, max(0, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
                        latest_view_bgr = ann
                        ok, buf = cv2.imencode(".jpg", ann, [int(cv2.IMWRITE_JPEG_QUALITY), VIEWER_QUALITY])
                        if ok:
                            asyncio.create_task(push_viewer(buf.tobytes(), cached_dets, frame_count, bgr.shape[1], bgr.shape[0]))
                    except:
                        latest_view_bgr = bgr
                        pass
                else:
                    if latest_view_bgr is None:
                        latest_view_bgr = bgr

                if frame_count % DETECTION_EVERY_N_FRAMES != 0:
                    continue
                if now - last_det_ms < DETECTION_THROTTLE_MS:
                    continue
                try:
                    dets = await asyncio.to_thread(detector.infer, bgr)
                except:
                    dets = []
                cached_dets = dets
                if dets:
                    last_det_ms = now
                    logger.info(f"[vps] DETECTED {dets}")
                    if data_channel and data_channel.readyState == "open":
                        try:
                            data_channel.send(json.dumps({"type": "detection", "ts": time.time(), "frame_id": frame_count, "objects": dets, "msg": f"Detected {len(dets)}: " + ", ".join(f"{d['cls']} {d['conf']:.2f}" for d in dets)}))
                        except:
                            pass
        asyncio.create_task(consume())

    async def handle_viewer_offer(from_peer: str, sdp: str, sdp_type: str):
        pc = RTCPeerConnection(configuration=ice_config())
        viewer_pcs[from_peer] = pc
        pc.addTrack(RelayTrack())
        @pc.on("connectionstatechange")
        async def _cs():
            if pc.connectionState in ("failed", "closed"):
                try:
                    await pc.close()
                except:
                    pass
                viewer_pcs.pop(from_peer, None)
        await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=sdp_type))
        ans = await pc.createAnswer()
        await pc.setLocalDescription(ans)
        await signaling.send({"type": "answer", "sdp": pc.localDescription.sdp, "sdpType": pc.localDescription.type, "target": from_peer})
        logger.info(f"[vps] viewer {from_peer} answer sent")

    logger.info("[vps] waiting for offers")
    while True:
        msg = await signaling.recv()
        mtype = msg.get("type")
        frm = msg.get("from")
        if mtype == "offer":
            sdp = msg.get("sdp")
            st = msg.get("sdpType", "offer")
            if not sdp:
                continue
            if frm == RASPI_ID:
                await raspi_pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=st))
                ans = await raspi_pc.createAnswer()
                await raspi_pc.setLocalDescription(ans)
                await signaling.send({"type": "answer", "sdp": raspi_pc.localDescription.sdp, "sdpType": raspi_pc.localDescription.type, "target": frm})
                logger.info("[vps] raspi answer sent")
            else:
                await handle_viewer_offer(frm, sdp, st)
        elif mtype == "candidate":
            cand = msg.get("candidate")
            if cand and frm == RASPI_ID:
                try:
                    c = candidate_from_sdp(cand)
                    c.sdpMid = msg.get("sdpMid")
                    c.sdpMLineIndex = msg.get("sdpMLineIndex", 0)
                    await raspi_pc.addIceCandidate(c)
                except:
                    pass
            elif cand:
                pc = viewer_pcs.get(frm)
                if pc:
                    try:
                        c = candidate_from_sdp(cand)
                        c.sdpMid = msg.get("sdpMid")
                        c.sdpMLineIndex = msg.get("sdpMLineIndex", 0)
                        await pc.addIceCandidate(c)
                    except:
                        pass
        elif mtype in ("peer-left",):
            pid = msg.get("peer_id")
            pc = viewer_pcs.pop(pid, None)
            if pc:
                try:
                    await pc.close()
                except:
                    pass
        elif mtype == "closed":
            break

    if viewer_session:
        await viewer_session.close()
    await raspi_pc.close()
    for pc in viewer_pcs.values():
        try:
            await pc.close()
        except:
            pass
    await signaling.close()

async def main_async():
    while True:
        try:
            await run()
        except Exception as e:
            logger.exception(f"[vps] error {e}")
        await asyncio.sleep(3)

def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
