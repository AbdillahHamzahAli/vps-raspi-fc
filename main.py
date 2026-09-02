def main():
    print("vps-raspi-fc - WebRTC 2-arah")
    print("VPS signaling: uv run uvicorn vps.signaling_server:app --host 0.0.0.0 --port 8000")
    print("VPS detector : uv run python -m vps.main_vps")
    print("Raspi cam    : uv run python -m raspi.main_raspi")
    print("Lihat README.md untuk .env & coturn setup")


if __name__ == "__main__":
    main()
