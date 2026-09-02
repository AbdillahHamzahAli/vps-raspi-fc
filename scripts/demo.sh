#!/usr/bin/env bash
# scripts/demo.sh - contoh script yang di-trigger VPS via DataChannel
echo "[demo.sh] Dipanggil dari VPS trigger pada $(date)"
echo "[demo.sh] Arg: $@"
# contoh GPIO/buzzer bisa di sini: gpio write 0 1
