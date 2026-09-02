import json
import subprocess
from datetime import datetime

ALLOWED_SCRIPTS = {
    "print": None,
    "demo": "./scripts/demo.sh",
}

def handle_vps_message(raw: str | bytes):
    try:
        if isinstance(raw, bytes):
            raw = raw.decode()
        data = json.loads(raw)
    except Exception as e:
        print(f"[TRIGGER] raw non-json: {raw!r} {e}")
        return

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*60}\n[TRIGGER {ts}] Dari VPS:\n{json.dumps(data, indent=2, ensure_ascii=False)}\n{'='*60}\n")

    action = data.get("action")
    script = data.get("script")
    if action == "run" and script:
        if script in ALLOWED_SCRIPTS:
            target = ALLOWED_SCRIPTS[script]
            if target is None:
                print("[handler] print only")
            else:
                print(f"[handler] executing {target}")
                try:
                    r = subprocess.run(["bash", target], capture_output=True, text=True, timeout=10)
                    print(f"[handler] stdout: {r.stdout}\n[handler] stderr: {r.stderr}")
                except Exception as e:
                    print(f"[handler] exec error {e}")
        else:
            print(f"[handler] not allowed {script}, allowed: {list(ALLOWED_SCRIPTS)}")
