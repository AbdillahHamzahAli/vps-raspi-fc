import asyncio
import json
import logging
import websockets

logger = logging.getLogger(__name__)

class SignalingClient:
    def __init__(self, url: str):
        self.url = url
        self.ws = None
        self._queue: asyncio.Queue[dict] = asyncio.Queue()

    async def connect(self):
        logger.info(f"[signaling] connecting to {self.url}")
        self.ws = await websockets.connect(self.url, ping_interval=20, ping_timeout=10)
        logger.info("[signaling] connected")
        asyncio.create_task(self._recv_loop())

    async def _recv_loop(self):
        try:
            async for raw in self.ws:
                try:
                    await self._queue.put(json.loads(raw))
                except Exception as e:
                    logger.warning(f"[signaling] bad json {e}")
        except websockets.ConnectionClosed as e:
            logger.warning(f"[signaling] closed {e}")
            await self._queue.put({"type": "closed"})
        except Exception as e:
            logger.exception(f"[signaling] recv error {e}")
            await self._queue.put({"type": "closed"})

    async def send(self, msg: dict):
        if self.ws is None:
            raise RuntimeError("not connected")
        await self.ws.send(json.dumps(msg))

    async def recv(self) -> dict:
        return await self._queue.get()

    async def close(self):
        if self.ws:
            await self.ws.close()
