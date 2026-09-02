#!/usr/bin/env bash
# scripts/test_missionplanner.sh - Test dengan SITL dari Mission Planner (udp:127.0.0.1:14550)
# Mission Planner sudah jalan SITL, connect UDP 127.0.0.1:14550
# Jika port bentrok (MP sudah bind), pakai udpin:127.0.0.1:14550 dan tambah output di MP: Simulation -> UDP output 127.0.0.1:14551
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "=== Test Mission Planner SITL ==="
echo "Pastikan Mission Planner SITL sudah connect UDP 127.0.0.1:14550"
echo "FC_CONNECTION_STRING default: udp:127.0.0.1:14550"
echo ""

# cek deps
echo "[1] cek deps..."
uv run python -c "from pymavlink import mavutil; print('pymavlink ok', mavutil.mavlink.MAV_TYPE_GCS)"
uv run python -c "import cv2; print('opencv', cv2.__version__)"
echo ""

# cek FC connection (quick)
echo "[2] Test FC connection 5s (mock jika gagal)..."
FC_CONNECTION_STRING=${FC_CONNECTION_STRING:-udp:127.0.0.1:14550} FC_BAUD=${FC_BAUD:-57600} uv run python -c "
import asyncio
from raspi.fc_interface import FCInterface
import os
async def test():
    fc = FCInterface(os.getenv('FC_CONNECTION_STRING','udp:127.0.0.1:14550'), int(os.getenv('FC_BAUD','57600')))
    await fc.connect(timeout=5)
    pos = fc.get_position()
    print('FC connected:', not fc.is_mock(), 'mock:', fc.is_mock())
    print('pos:', pos)
    if not fc.is_mock():
        print('-> FC real, akan dapat GPS dari SITL')
    else:
        print('-> Mock mode, coba cek MP: pastikan SITL running dan UDP output aktif')
        print('   Jika MP bind udp:127.0.0.1:14550, coba: FC_CONNECTION_STRING=udpin:127.0.0.1:14550')
    fc.close()
asyncio.run(test())
"
echo ""

# start signaling + vps + raspi instructions
cat <<'EOF'
[3] Jalankan 3 terminal:

Terminal 1 - Signaling:
  uv run uvicorn vps.signaling_server:app --host 0.0.0.0 --port 8000
  cek: curl http://localhost:8000/ && curl -H "X-API-Key: secret" http://localhost:8000/api/detections

Terminal 2 - VPS (YOLO + storage):
  uv run python -m vps.main_vps
  log harus: [vps] waiting for offers, frames, DETECTED...

Terminal 3 - Raspi (FC + cam):
  FC_CONNECTION_STRING=udp:127.0.0.1:14550 uv run python -m raspi.main_raspi
  log harus: [fc] heartbeat, [raspi] DataChannel OPEN, telemetry

[4] Test guided (setelah raspi connect):
  curl -X POST -H "X-API-Key: secret" -H "Content-Type: application/json" \
    -d '{"lat":-7.28,"lon":112.79,"alt":10}' http://localhost:8000/api/guided
  cek di MP HUD: mode GUIDED, target berubah. Tanpa key -> 401.

[5] Test detection storage:
  Hadapkan orang ke kamera -> cek VPS log [storage] saved ...
  curl -H "X-API-Key: secret" http://localhost:8000/api/detections | jq
  curl -H "X-API-Key: secret" http://localhost:8000/api/detections/{id} | jq
  ls -lh data/detections/

[6] Jika port udp bentrok:
  FC_CONNECTION_STRING=udpin:127.0.0.1:14550 uv run python -m raspi.main_raspi
  dan di MP tambahkan UDP output ke 127.0.0.1:14551 lalu pakai udpin:127.0.0.1:14551
EOF
