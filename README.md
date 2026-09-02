# vps-raspi-fc — Drone 2-Arah VPS ↔ Raspi ↔ FC (ArduPilot)

**Raspi (drone) → VPS**: Video USB cam (VP8/H264) + koordinat FC **sinkron ≤100ms** via WebRTC VideoTrack + DataChannel `telemetry` (fallback WS)  
**VPS → Raspi**: Hasil YOLO11s via DataChannel `detection` + perintah **GUIDED** ke koordinat via `raspi/pkg` (pymavlink)  
**VPS storage**: Frame + `.json` (koordinat) dengan nama sama `YYYYMMDD_HHMMSS_ms_frameId_cls_conf`

Jaringan modem/CGNAT → wajib TURN (`coturn` di VPS). FC konek Raspi via USB/UART (config `.env` `FC_CONNECTION_STRING`).

## Arsitektur

```
[Drone]
  USB cam ──┐
            ├─ [Raspi] ─ UsbCameraTrack (frame_id++) ─┐
  FC (USB) ─┘   └─ raspi/pkg.Vehicle (pymavlink 10Hz) ─┤─ DataChannel telemetry {frame_id, ts, lat,lon,alt} (100ms)
                └─ DC commands RX {type:guided} ───────┘  DataChannel commands {detection, guided_ack}
                         ↕ WebRTC (TURN :3478) + WS signaling :8000 (VPS)

[VPS]
  main_vps: consume VideoTrack + telemetry buffer → YOLO → if dets → storage.py save jpg+json (pakai koordinat terdekat)
  signaling_server: FastAPI WS relay + REST API + static /detections
```

* Raspi `raspi/pkg` (mirip `guided-dropping-mission`): `Vehicle` + `MAVState` + `utils.horizontal_distance_m` — luar cukup `await vehicle.send_guided(lat,lon,alt)` dan `vehicle.get_position()`
* Sinkron: Raspi kirim `telemetry` tiap `TELEMETRY_HZ=10` dengan `frame_id`; VPS buffer `OrderedDict` 200 entri, cari `frame_id` exact lalu `latest` dalam `TELEMETRY_STALE_MS=100`

## Quick Start

### 1. Install deps (terpisah Raspi vs VPS)

```bash
cp .env.example .env   # edit VPS_IP, TURN cred, FC_CONNECTION_STRING, API_KEY

# Di VPS (butuh YOLO + FastAPI, tanpa pymavlink):
uv sync --extra vps
# Di Raspi (butuh pymavlink + cam, tanpa YOLO/FastAPI):
uv sync --extra raspi
# Dev / test 1 laptop (semua):
uv sync --extra all
# Minimal (common saja):
uv sync
```

### 2. VPS — Signaling + TURN

```bash
# di VPS
sudo bash scripts/setup_coturn.sh
sudo ufw allow 8000/tcp && sudo ufw allow 3478/tcp && sudo ufw allow 3478/udp && sudo ufw allow 49160:49200/udp

uv run uvicorn vps.signaling_server:app --host 0.0.0.0 --port 8000
# test: curl http://VPS_IP:8000/  -> {"status":"ok", "peers":[], "detections":0}
```

### 3. VPS — Receiver + YOLO11s

```bash
# di VPS (terminal 2)
uv run python -m vps.main_vps
# log: [vps] waiting for offers...
# saat Raspi connect: DETECTED [...] + [storage] saved 20260902_..._person_0.92.jpg
```

### 4. Raspi — USB Cam + FC (pilih salah satu FC)

```bash
# Real drone: FC via USB
FC_CONNECTION_STRING=/dev/ttyACM0 FC_BAUD=57600 uv run python -m raspi.main_raspi

# SITL Mission Planner (Windows/WSL): MP sudah connect udp:127.0.0.1:14550
FC_CONNECTION_STRING=udp:127.0.0.1:14550 uv run python -m raspi.main_raspi
# jika MP bind port -> pakai udpin:
FC_CONNECTION_STRING=udpin:127.0.0.1:14550 uv run python -m raspi.main_raspi
# log: [pkg] heartbeat sys 1 comp 1 via udp..., [raspi] DataChannel OPEN, telemetry
```

Jika deteksi: Raspi print `[TRIGGER]` + VPS simpan `data/detections/`.

### 5. Test Mission Planner (tanpa download SITL)

```bash
bash scripts/test_missionplanner.sh
# cek FC 5s, lalu instruksi 3 terminal di atas
```

## Konfigurasi Penting (.env)

| Key | Default | Ket |
|---|---|---|
| `SIGNALING_URL` | `ws://localhost:8000/ws` | WS signaling di VPS |
| `TURN_URL` | `turn:VPS_IP:3478` | Wajib modem |
| `CAM_DEVICE` | `0` | `/dev/video0` atau `0` |
| `VIDEO_WIDTH/HEIGHT/FPS` | `640x480@20` | Turunkan jika latency >200ms |
| `YOLO_MODEL` | `yolo11s.pt` | Auto download |
| `YOLO_CONF` | `0.5` | Threshold |
| `YOLO_CLASSES` | `` | `0`=person only |
| `DETECTION_THROTTLE_MS` | `200` | Max 5 msg/detik |
| `FC_CONNECTION_STRING` | `/dev/ttyACM0` | SITL: `udp:127.0.0.1:14550` |
| `FC_BAUD` | `57600` | Baud serial |
| `TELEMETRY_HZ` | `10` | Hz kirim koordinat |
| `TELEMETRY_STALE_MS` | `100` | Budget sinkron |
| `DETECTION_SAVE_DIR` | `data/detections` | Storage VPS |
| `DETECTION_SAVE_THROTTLE_MS` | `1000` | Anti spam save |
| `GUIDED_ALT_DEFAULT` | `20` | Alt relatif AGL (m) |
| `API_KEY` | `secret` | **Ganti!** untuk produksi |
| `FC_Q_GUIDED_MODE` | `1` | QuadPlane Q_GUIDED |
| `FC_ARRIVAL_RADIUS_M` | `5.0` | Radius arrival |
| `FC_ARRIVAL_SPEED_MPS` | `0.5` | Speed arrival |

## REST API (VPS)

Semua butuh header `X-API-Key: <API_KEY>` atau `Authorization: Bearer <API_KEY>`.

### List deteksi
```bash
curl -H "X-API-Key: secret" "http://VPS_IP:8000/api/detections?limit=50&offset=0" | jq
# {"ok":true,"total":12,"items":[{"id":"20260902_143001_123_person_0.92","ts":...,"coords":{"lat":-7.28,"lon":112.79,"alt":15,"rel_alt":15},"detections":[...],"image":"/detections/20260902_...jpg"}]}
```

### Detail + koordinat
```bash
curl -H "X-API-Key: secret" http://VPS_IP:8000/api/detections/20260902_143001_123_person_0.92 | jq
curl -H "X-API-Key: secret" http://VPS_IP:8000/detections/20260902_143001_123_person_0.92.jpg --output frame.jpg
curl -H "X-API-Key: secret" http://VPS_IP:8000/detections/20260902_143001_123_person_0.92.json | jq
```

### Kirim GUIDED ke koordinat (alt relatif)
```bash
curl -X POST -H "X-API-Key: secret" -H "Content-Type: application/json" \
  -d '{"lat":-7.28,"lon":112.79,"alt":10}' http://VPS_IP:8000/api/guided | jq
# {"ok":true,"via":"ws","sent":{"type":"guided","lat":-7.28,...}}
# via: ws (2 proses) atau datachannel (embedded). Butuh raspi connect else 503
# 401 tanpa key, 400 lat out of range
```
Raspi `raspi/pkg.Vehicle` akan `set_mode GUIDED` → `mission_item_int_send` → `MISSION_ACK`. Cek MP HUD mode GUIDED.

### Health
```bash
curl http://VPS_IP:8000/ | jq
# {"status":"ok","peers":["raspi","vps"],"detections":12,"raspi_connected":true}
```

## Storage

`data/detections/` di VPS (gitignore). Tiap deteksi:
```
20260902_143001_123_person_0.92.jpg  # frame annotasi
20260902_143001_123_person_0.92.json # {"id","ts","frame_id","coords":{"lat","lon","alt","rel_alt","ts","speed","mock"},"detections":[...],"coords_stale_ms":12}
```

## Browser Viewer (Stream Detection)

VPS push frame annotasi ke signaling:

- **Viewer**: `http://VPS_IP:8000/viewer` — MJPEG + canvas overlay
- **Raw MJPEG**: `http://VPS_IP:8000/stream.mjpg`
- **API state**: `http://VPS_IP:8000/api/state`
- **WS live**: `ws://VPS_IP:8000/ws/viewer`

```bash
uv run python -m vps.main_vps
open http://VPS_IP:8000/viewer
```

## Raspi pkg — Guided Abstraction

Luar hanya panggil `raspi/pkg` (port dari `guided-dropping-mission`):

```python
from raspi.pkg import get_vehicle, Target

vehicle = get_vehicle()  # baca FC_CONNECTION_STRING dari .env
await vehicle.connect(timeout=10)  # fallback mock jika FC mati, udpin fallback jika MP bind
pos = vehicle.get_position()  # {"lat","lon","alt","rel_alt","speed","ts"}
result = await vehicle.send_guided(lat=-7.28, lon=112.79, alt=10)
# result {"ok":True, "target":{...}} atau {"ok":False,"error":...}

# opsional:
vehicle.ensure_q_guided_mode()
vehicle.set_mode("GUIDED")
vehicle.goto(Target(lat,lon,alt_m=10))
vehicle.wait_arrival(Target(...))
```

`raspi/fc_interface.py` sekarang shim deprecated — pakai `raspi.pkg`.

## Test Tanpa Hardware

- **Tanpa USB cam**: black frame + timestamp otomatis.
- **Tanpa FC**: mock GPS `lat -7.27, lon 112.79` + `mock:true` di json, `send_guided` mock `ok:true`.
- **Tanpa YOLO**: dummy detector, DataChannel tetap jalan.

## Troubleshooting

- **Peers tidak connect / ICE failed**: `systemctl status coturn`, `turnutils_uclient -u raspi -w pass -p 3478 VPS_IP`, pastikan `SIGNALING_URL` pakai `VPS_IP` bukan `localhost` dari Raspi.
- **Latency >200ms**: turunkan `VIDEO_WIDTH=640 HEIGHT=480 FPS=15 BITRATE=600`, `YOLO_CLASSES=0`, `DETECTION_EVERY_N_FRAMES=4`.
- **FC heartbeat timeout / mock**: cek `ls /dev/serial/by-id/*`, `FC_CONNECTION_STRING` benar, baud `57600`/`921600`, untuk SITL coba `udpin:127.0.0.1:14550` dan tambah UDP output di MP.
- **Guided `503 raspi not connected`**: `curl http://VPS_IP:8000/` cek `peers`, pastikan Raspi `DataChannel OPEN` atau WS `guided` fallback.
- **Guided `goto rejected`**: pastikan drone armed + mode GUIDED, cek `Q_GUIDED_MODE=1` untuk QuadPlane, cek `is_armed` di MP.
- **Telemetry stale >100ms**: naikkan `TELEMETRY_HZ=15` atau turunkan `VIDEO_FPS`.
- **Signaling 404**: `SIGNALING_URL` akhiri `/ws`.
- **Viewer hitam**: `curl http://VPS_IP:8000/api/state` `has_frame` harus true.

```
common/config.py           ICE + env + viewer + FC
common/signaling.py        WS client helper
vps/signaling_server.py    FastAPI WS relay + viewer + /api/detections + /api/guided
vps/storage.py             Save jpg+json sinkron koordinat
vps/detector.py            YOLO11s wrapper
vps/main_vps.py            WebRTC receiver + telemetry buffer 100ms + storage + push viewer
raspi/pkg/                 FC pkg (Vehicle + MAVState + utils) — luar cukup get_vehicle()
  - config.py              Config/Target dataclass
  - state.py               MAVState cache thread
  - vehicle.py             GUIDED via mission_item_int_send + MISSION_ACK
  - utils.py               horizontal_distance_m
raspi/capture.py           UsbCameraTrack (frame_id)
raspi/handler.py           print payload VPS
raspi/main_raspi.py        WebRTC sender + 2 DC (telemetry/commands) + handle_guided_via_pkg
raspi/fc_interface.py      shim deprecated → raspi.pkg
static/viewer.html         Browser viewer
scripts/setup_coturn.sh    Install TURN di VPS
scripts/test_local.sh      Test 1 laptop
scripts/test_missionplanner.sh  Test MP SITL udp:127.0.0.1:14550
data/detections/           Storage VPS (gitignore)
```

## Keamanan

- Ganti `API_KEY` dari `secret` default sebelum expose VPS ke public.
- `/api/*` butuh `X-API-Key`, tapi `GET /detections/*.jpg` masih via static mount — untuk produksi tambah auth middleware atau reverse proxy `nginx` + auth.
- `POST /api/guided` belum ada geofence/rate-limit — tambah validasi jarak dari home sebelum terbang jauh.
- Gunakan `wss://`/`https://` + cert di produksi, jangan `ws://`.

## Next: Ganti print jadi aksi nyata

Edit `raspi/handler.py:handle_vps_message`:
```python
if data["type"]=="detection":
    for o in data["objects"]:
        if o["cls"]=="person" and o["conf"]>0.8:
            subprocess.run(["bash", "./scripts/buka_buzzer.sh"])
```
Whitelist di `ALLOWED_SCRIPTS`.
