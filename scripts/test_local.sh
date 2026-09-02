#!/usr/bin/env bash
# scripts/test_local.sh - Test 2-arah di 1 laptop (tanpa Raspi/VPS fisik)
# Semua jalan di localhost: signaling + vps (detector) + raspi (kamera laptop/USB)
# Tanpa TURN, tanpa modem.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "=== Test Lokal 1 Laptop ==="
echo "SIGNALING_URL=ws://localhost:8000/ws (TURN kosong)"
echo ""

# 1. cek kamera laptop
echo "[1] Cek kamera..."
uv run python -c "
import cv2
cap = cv2.VideoCapture(0, cv2.CAP_V4L2)
if not cap.isOpened():
    cap = cv2.VideoCapture(0)
print('CAM 0 opened:', cap.isOpened())
if cap.isOpened():
    ret, f = cap.read()
    print('read ok:', ret, 'shape:', f.shape if ret else 'none')
    cap.release()
else:
    print('-> Tidak ada kamera, raspi akan kirim black frame (tetap bisa test DataChannel)')
"
echo ""

# 2. jalankan signaling di background
echo "[2] Start signaling http://localhost:8000 ..."
uv run uvicorn vps.signaling_server:app --host 127.0.0.1 --port 8000 > /tmp/signaling.log 2>&1 &
SIG_PID=$!
sleep 2
curl -s http://127.0.0.1:8000/ | cat
echo ""
echo "signaling PID $SIG_PID log: /tmp/signaling.log"
echo ""

# trap cleanup
trap "echo 'cleanup...'; kill $SIG_PID 2>/dev/null || true; kill \$VPS_PID 2>/dev/null || true; kill \$RASPI_PID 2>/dev/null || true; exit" INT TERM

# 3. jalankan VPS di background
echo "[3] Start VPS (YOLO11s) ..."
uv run python -m vps.main_vps > /tmp/vps.log 2>&1 &
VPS_PID=$!
sleep 2
echo "VPS PID $VPS_PID log: /tmp/vps.log"
tail -n 20 /tmp/vps.log || true
echo ""

# 4. jalankan Raspi di foreground (block sampai Ctrl+C)
echo "[4] Start Raspi (USB cam -> VPS) ..."
echo "   Jika ada orang di depan kamera laptop, Raspi akan print:"
echo "   [TRIGGER] {\"type\":\"detection\",\"objects\":[{\"cls\":\"person\",...}]}"
echo ""
echo "   Tekan Ctrl+C untuk stop semua"
echo ""
uv run python -m raspi.main_raspi

# jika raspi exit, kill lainnya
kill $VPS_PID 2>/dev/null || true
kill $SIG_PID 2>/dev/null || true
