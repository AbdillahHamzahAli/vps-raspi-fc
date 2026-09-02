import os
from dotenv import load_dotenv

load_dotenv()

def get_ice_servers() -> list:
    servers = [{"urls": os.getenv("STUN_URL", "stun:stun.l.google.com:19302")}]
    turn_url = os.getenv("TURN_URL", "")
    turn_user = os.getenv("TURN_USERNAME", "")
    turn_cred = os.getenv("TURN_CREDENTIAL", "")
    if turn_url:
        entry = {"urls": turn_url}
        if turn_user:
            entry["username"] = turn_user
        if turn_cred:
            entry["credential"] = turn_cred
        servers.append(entry)
    else:
        vps_host = os.getenv("VPS_HOST", "")
        if vps_host and turn_user and turn_cred:
            servers.append({"urls": f"turn:{vps_host}:3478", "username": turn_user, "credential": turn_cred})
    return servers

def get_signaling_url(peer_id: str) -> str:
    base = os.getenv("SIGNALING_URL", "ws://localhost:8000/ws").rstrip("/")
    return f"{base}/{peer_id}" if base.endswith("/ws") else f"{base}/ws/{peer_id}"

def get_signaling_http_base() -> str:
    ws = os.getenv("SIGNALING_URL", "ws://localhost:8000/ws")
    http = ws.replace("ws://", "http://").replace("wss://", "https://")
    return http.split("/ws")[0].rstrip("/")

VIDEO_WIDTH = int(os.getenv("VIDEO_WIDTH", "640"))
VIDEO_HEIGHT = int(os.getenv("VIDEO_HEIGHT", "480"))
VIDEO_FPS = int(os.getenv("VIDEO_FPS", "20"))
VIDEO_BITRATE_KBPS = int(os.getenv("VIDEO_BITRATE_KBPS", "800"))

YOLO_MODEL = os.getenv("YOLO_MODEL", "yolo11s.pt")
YOLO_CONF = float(os.getenv("YOLO_CONF", "0.5"))
YOLO_CLASSES = os.getenv("YOLO_CLASSES", "")
DETECTION_THROTTLE_MS = int(os.getenv("DETECTION_THROTTLE_MS", "200"))
DETECTION_EVERY_N_FRAMES = int(os.getenv("DETECTION_EVERY_N_FRAMES", "3"))

VIEWER_PUSH_URL = os.getenv("VIEWER_PUSH_URL", "")
BROWSER_VIEWER_ENABLED = os.getenv("BROWSER_VIEWER_ENABLED", "1") == "1"
VIEWER_FPS = int(os.getenv("VIEWER_FPS", "12"))
VIEWER_QUALITY = int(os.getenv("VIEWER_QUALITY", "75"))
