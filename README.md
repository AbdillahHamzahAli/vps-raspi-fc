# vps-raspi-fc — WebRTC 2-Arah VPS ↔ Raspi

**Raspi -> VPS**: Video USB cam (VP8/H264) via WebRTC MediaTrack, latency target <200ms  
**VPS -> Raspi**: Hasil YOLO11s via WebRTC DataChannel, Raspi `print()` (contoh trigger script)

Jaringan modem/CGNAT → wajib TURN (`coturn` di VPS).

## Arsitektur

```
[Raspi - modem] --VideoTrack--> [VPS - YOLO11s] --DataChannel JSON--> [Raspi print]
                <-DataChannel--                --signaling WS:8000--
TURN: coturn di VPS :3478 + STUN
```

## Quick Start

### 1. Install deps

```bash
uv sync
cp .env.example .env   # edit VPS_IP, TURN cred
```

### 2. VPS — Signaling + TURN

```bash
# di VPS
sudo bash scripts/setup_coturn.sh
# buka firewall
sudo ufw allow 8000/tcp && sudo ufw allow 3478/tcp && sudo ufw allow 3478/udp && sudo ufw allow 49160:49200/udp

# jalankan signaling
uv run uvicorn vps.signaling_server:app --host 0.0.0.0 --port 8000
# test: curl http://VPS_IP:8000/  -> {"status":"ok", "peers":[]}
```

### 3. VPS — Receiver + YOLO11s

```bash
# di VPS (terminal 2)
# .env: SIGNALING_URL=ws://localhost:8000/ws  jika 1 mesin, atau ws://VPS_IP:8000/ws
# YOLO11s akan auto-download yolo11s.pt di first run
uv run python -m vps.main_vps
# log: [vps] waiting for offer...
# saat Raspi connect: DETECTED [{"cls":"person","conf":0.91,...}] -> kirim DataChannel
```

### 4. Raspi — USB Cam + Trigger Print

```bash
# di Raspi
# .env: SIGNALING_URL=ws://VPS_IP:8000/ws, CAM_DEVICE=0
uv run python -m raspi.main_raspi
# log: [raspi] offer created, DataChannel OPEN
# saat VPS deteksi: 
# ============================================================
# [TRIGGER 2026-08-29 12:00:00] Dari VPS:
# {
#   "type": "detection",
#   "objects": [{"cls": "person", "conf": 0.92, "xyxy": [10,20,200,400]}]
# }
# ============================================================
```

## Konfigurasi Penting (.env)

| Key | Default | Ket |
|---|---|---|
| `SIGNALING_URL` | `ws://localhost:8000/ws` | WS signaling di VPS |
| `TURN_URL` | `turn:VPS_IP:3478` | Wajib untuk modem |
| `CAM_DEVICE` | `0` | `/dev/video0` atau `0` |
| `VIDEO_WIDTH/HEIGHT/FPS` | `640x480@20` | Turunkan jika latency >200ms |
| `YOLO_MODEL` | `yolo11s.pt` | Auto download |
| `YOLO_CONF` | `0.5` | Threshold |
| `YOLO_CLASSES` | `` | Kosong semua, `0` = person only |
| `DETECTION_THROTTLE_MS` | `200` | Max 5 msg/detik |

## Test Tanpa Hardware

- **Tanpa USB cam**: `raspi/capture.py` otomatis kirim black frame dengan timestamp (tetap bisa test signaling & DataChannel).
- **Tanpa YOLO**: Jika `ultralytics` belum install, detector jadi dummy (tidak deteksi) tapi DataChannel tetap jalan. Test trigger manual: di VPS `data_channel.send('{"type":"trigger","msg":"hello"}')`.

## Browser Viewer (Stream Detection)

VPS otomatis push frame terannotasi YOLO11s ke signaling server, browser bisa lihat tanpa WebRTC:

- **Viewer**: `http://VPS_IP:8000/viewer` — MJPEG + canvas overlay box hijau
- **Raw MJPEG**: `http://VPS_IP:8000/stream.mjpg` — bisa di `<img>` atau VLC
- **API state**: `http://VPS_IP:8000/api/state` — JSON detections terakhir
- **WS live**: `ws://VPS_IP:8000/ws/viewer` — broadcast detections real-time

Cara:
```bash
# 1. signaling sudah jalan :8000
# 2. VPS jalan (akan push ke /api/frame tiap ada deteksi)
uv run python -m vps.main_vps
# 3. buka di laptop/HP:
open http://VPS_IP:8000/viewer
open http://localhost:8000/viewer   # untuk test lokal 1 laptop
```

Di viewer kamu lihat: stream 640x480 + box `person 88%` + JSON list. Raspi tetap dapat trigger via DataChannel (print) — viewer hanya display.

Test lokal sudah terbukti: VPS push `POST /api/frame` 50+ kali, `/api/state` has_frame=true, `/stream.mjpg` ngalir.

## Troubleshooting

- **Peers tidak connect / ICE failed**: Cek `TURN_URL` & `coturn` status `systemctl status coturn`. Test `turnutils_uclient -u raspi -w pass -p 3478 VPS_IP`. Pastikan `SIGNALING_URL` pakai `VPS_IP` bukan `localhost` dari sisi Raspi.
- **Latency >200ms**: Turunkan `VIDEO_WIDTH=640 HEIGHT=480 FPS=15 BITRATE=600`, set `YOLO_CLASSES=0` (person only), `DETECTION_EVERY_N_FRAMES=4`.
- **Raspi CPU 100%**: Pakai `VIDEO_FPS=15`, atau ganti ke GStreamer `v4l2h264enc` (TODO).
- **Signaling 404**: Pastikan server jalan `uvicorn ... --port 8000` dan `SIGNALING_URL` akhiri `/ws`.
- **Viewer hitam / no-signal**: Tunggu 2-3 detik setelah VPS+raspi connect. Cek `curl http://VPS_IP:8000/api/state` has_frame harus true. Cek log VPS `[viewer] push` tidak error.
- **Port 8000 bentrok**: Signaling + viewer jadi 1 port (8000). Jangan jalankan viewer terpisah di 8001.

```
common/config.py        ICE + env + viewer push URL
common/signaling.py     WS client helper
vps/signaling_server.py FastAPI WS relay + viewer (/viewer, /stream.mjpg, /ws/viewer, POST /api/frame)
static/viewer.html      Browser viewer MJPEG + canvas overlay + WS
vps/detector.py         YOLO11s wrapper
vps/main_vps.py         WebRTC receiver + detector + DataChannel sender + push viewer (aiohttp)
raspi/capture.py        UsbCameraTrack (cv2)
raspi/handler.py        print payload VPS (contoh trigger)
raspi/main_raspi.py     WebRTC sender + DataChannel receiver
scripts/setup_coturn.sh install TURN di VPS
scripts/test_local.sh   Test 1 laptop (signaling + vps + raspi)
```

## Next: Ganti print jadi aksi nyata

Edit `raspi/handler.py:handle_vps_message`:
```python
if data["type"]=="detection":
    for o in data["objects"]:
        if o["cls"]=="person" and o["conf"]>0.8:
            subprocess.run(["bash", "./scripts/buka_buzzer.sh"])
```
Whitelist script di `ALLOWED_SCRIPTS`.
