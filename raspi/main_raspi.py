import asyncio
import json
import logging
import os
import time

from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer
from aiortc.sdp import candidate_from_sdp

from common.config import get_ice_servers, get_signaling_url, TELEMETRY_HZ, GUIDED_ALT_DEFAULT
from common.signaling import SignalingClient
from raspi.capture import UsbCameraTrack
from raspi.handler import handle_vps_message
# pkg: luar hanya panggil ini
from raspi.pkg import get_vehicle

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("raspi")

PEER_ID = os.getenv("RASPI_PEER_ID", "raspi")
TARGET_ID = os.getenv("VPS_PEER_ID", "vps")
CAM_DEVICE = os.getenv("CAM_DEVICE", "0")
try:
    CAM_DEVICE = int(CAM_DEVICE)
except:
    pass

def ice_config():
    return RTCConfiguration(iceServers=[RTCIceServer(urls=s["urls"], username=s.get("username"), credential=s.get("credential")) for s in get_ice_servers()])

async def handle_guided_via_pkg(vehicle, data: dict, reply_ch=None, sig=None):
    """Execute guided via raspi.pkg — luar hanya panggil vehicle.send_guided()."""
    lat = data.get("lat")
    lon = data.get("lon")
    alt = data.get("alt", GUIDED_ALT_DEFAULT)
    if lat is None or lon is None:
        logger.warning(f"[raspi] guided missing lat/lon {data}")
        resp = {"type": "guided_ack", "ok": False, "error": "lat/lon required"}
        if reply_ch and getattr(reply_ch, "readyState", "") == "open":
            try: reply_ch.send(json.dumps(resp))
            except: pass
        if sig:
            try: await sig.send({"type": "guided_ack", "ok": False, "error": "lat/lon required", "target": TARGET_ID})
            except: pass
        return
    try:
        alt_f = float(alt) if alt is not None else GUIDED_ALT_DEFAULT
    except:
        alt_f = GUIDED_ALT_DEFAULT
    logger.info(f"[raspi] guided via pkg lat={lat} lon={lon} alt={alt_f}")
    # pkg API: await vehicle.send_guided(lat, lon, alt)
    result = await vehicle.send_guided(float(lat), float(lon), alt_f)
    ack = {"type": "guided_ack", "ok": result.get("ok", False), "result": result, "lat": lat, "lon": lon, "alt": alt_f}
    if reply_ch and getattr(reply_ch, "readyState", "") == "open":
        try: reply_ch.send(json.dumps(ack))
        except: pass
    if sig:
        try: await sig.send({**ack, "target": TARGET_ID})
        except: pass
    logger.info(f"[raspi] guided ack {ack}")

async def run():
    sig = SignalingClient(get_signaling_url(PEER_ID))
    await sig.connect()
    pc = RTCPeerConnection(configuration=ice_config())

    # FC via pkg — luar hanya panggil pkg
    vehicle = get_vehicle()
    asyncio.create_task(vehicle.connect(timeout=10))

    # Capture track with public frame_id
    camera_track = UsbCameraTrack(device=CAM_DEVICE)
    pc.addTransceiver(camera_track, direction="sendonly")

    # Two DataChannels: telemetry (unreliable) + commands (reliable)
    dc_commands = pc.createDataChannel("commands", ordered=True)
    dc_telemetry = pc.createDataChannel("telemetry", ordered=False, maxRetransmits=0)

    @dc_commands.on("open")
    def _o():
        logger.info("[raspi] DataChannel commands OPEN")
        try:
            dc_commands.send(json.dumps({"type": "hello", "from": "raspi", "msg": "raspi ready"}))
        except: pass
    @dc_commands.on("message")
    def _m(m):
        try:
            data = json.loads(m.decode() if isinstance(m, bytes) else m)
            if data.get("type") == "guided":
                asyncio.create_task(handle_guided_via_pkg(vehicle, data, reply_ch=dc_commands, sig=sig))
                return
        except: pass
        handle_vps_message(m)
    @dc_commands.on("close")
    def _c(): logger.info("[raspi] DataChannel commands closed")

    @dc_telemetry.on("open")
    def _to(): logger.info("[raspi] DataChannel telemetry OPEN")
    @dc_telemetry.on("close")
    def _tc(): logger.info("[raspi] DataChannel telemetry closed")

    @pc.on("datachannel")
    def _dc(c):
        label = getattr(c, "label", "")
        logger.info(f"[raspi] incoming DataChannel {label}")
        @c.on("message")
        def _mm(m):
            try:
                data = json.loads(m.decode() if isinstance(m, bytes) else m)
                if data.get("type") == "guided":
                    asyncio.create_task(handle_guided_via_pkg(vehicle, data, reply_ch=c, sig=sig))
                    return
            except: pass
            handle_vps_message(m)

    @pc.on("connectionstatechange")
    async def _cs(): logger.info(f"[raspi] {pc.connectionState}")
    @pc.on("iceconnectionstatechange")
    async def _is(): logger.info(f"[raspi] ice {pc.iceConnectionState}")

    # Telemetry loop: via pkg vehicle.get_position()
    async def telemetry_loop():
        interval = 1.0 / max(1, TELEMETRY_HZ)
        while True:
            await asyncio.sleep(interval)
            pos = vehicle.get_position()
            if pos is None:
                continue
            fid = getattr(camera_track, "frame_id", 0)
            payload = {
                "type": "telemetry",
                "frame_id": fid,
                "ts": pos.get("ts", time.time()),
                "lat": pos.get("lat"),
                "lon": pos.get("lon"),
                "alt": pos.get("alt"),
                "rel_alt": pos.get("rel_alt", pos.get("alt")),
                "speed": pos.get("speed"),
                "mock": pos.get("mock", False),
            }
            if getattr(dc_telemetry, "readyState", "") == "open":
                try:
                    dc_telemetry.send(json.dumps(payload))
                except Exception as e:
                    logger.debug(f"[raspi] telemetry dc send fail {e}")
            dc_open = getattr(dc_telemetry, "readyState", "") == "open"
            if not dc_open or (fid % 2 == 0):
                try:
                    await sig.send({**payload, "target": TARGET_ID})
                except:
                    pass

    tel_task = asyncio.create_task(telemetry_loop())

    answer_evt = asyncio.Event()

    async def send_offer():
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        await sig.send({"type": "offer", "sdp": pc.localDescription.sdp, "sdpType": pc.localDescription.type, "target": TARGET_ID})

    async def retry():
        while not answer_evt.is_set():
            await send_offer()
            try:
                await asyncio.wait_for(answer_evt.wait(), timeout=3)
            except asyncio.TimeoutError:
                logger.warning("[raspi] no answer, retry")

    task = asyncio.create_task(retry())

    while True:
        msg = await sig.recv()
        t = msg.get("type")
        if t == "answer":
            try:
                await pc.setRemoteDescription(RTCSessionDescription(sdp=msg["sdp"], type=msg.get("sdpType", "answer")))
                answer_evt.set()
            except Exception as e:
                logger.warning(f"[raspi] answer fail {e}")
        elif t == "candidate":
            try:
                c = candidate_from_sdp(msg["candidate"])
                c.sdpMid = msg.get("sdpMid")
                c.sdpMLineIndex = msg.get("sdpMLineIndex", 0)
                await pc.addIceCandidate(c)
            except:
                pass
        elif t == "offer":
            await pc.setRemoteDescription(RTCSessionDescription(sdp=msg["sdp"], type=msg.get("sdpType", "offer")))
            ans = await pc.createAnswer()
            await pc.setLocalDescription(ans)
            await sig.send({"type": "answer", "sdp": pc.localDescription.sdp, "sdpType": pc.localDescription.type, "target": msg.get("from", TARGET_ID)})
        elif t == "guided":
            logger.info(f"[raspi] guided via WS {msg}")
            await handle_guided_via_pkg(vehicle, msg, reply_ch=dc_commands, sig=sig)
        elif t == "closed":
            break
        elif t == "error":
            logger.warning(f"[raspi] {msg}")

    tel_task.cancel()
    try: await tel_task
    except asyncio.CancelledError: pass
    task.cancel()
    try: await task
    except asyncio.CancelledError: pass
    try: vehicle.close()
    except: pass
    await pc.close()
    await sig.close()

async def main_async():
    while True:
        try:
            await run()
        except Exception as e:
            logger.exception(f"[raspi] {e}")
        await asyncio.sleep(3)

def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
