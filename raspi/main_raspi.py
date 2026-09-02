import asyncio
import json
import logging
import os

from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer
from aiortc.sdp import candidate_from_sdp

from common.config import get_ice_servers, get_signaling_url
from common.signaling import SignalingClient
from raspi.capture import UsbCameraTrack
from raspi.handler import handle_vps_message

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

async def run():
    sig = SignalingClient(get_signaling_url(PEER_ID))
    await sig.connect()
    pc = RTCPeerConnection(configuration=ice_config())
    ch = pc.createDataChannel("commands", ordered=True)

    @ch.on("open")
    def _o():
        logger.info("[raspi] DataChannel OPEN")
        try:
            ch.send(json.dumps({"type": "hello", "from": "raspi", "msg": "raspi ready"}))
        except:
            pass
    @ch.on("message")
    def _m(m): handle_vps_message(m)
    @ch.on("close")
    def _c(): logger.info("[raspi] DataChannel closed")
    @pc.on("datachannel")
    def _dc(c):
        @c.on("message")
        def _mm(m): handle_vps_message(m)

    pc.addTransceiver(UsbCameraTrack(device=CAM_DEVICE), direction="sendonly")

    @pc.on("connectionstatechange")
    async def _cs(): logger.info(f"[raspi] {pc.connectionState}")
    @pc.on("iceconnectionstatechange")
    async def _is(): logger.info(f"[raspi] ice {pc.iceConnectionState}")

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
        elif t == "closed":
            break
        elif t == "error":
            logger.warning(f"[raspi] {msg}")

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
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
