from __future__ import annotations
import asyncio
import logging
import math
import time
from typing import Optional, Dict

logger = logging.getLogger(__name__)

try:
    from pymavlink import mavutil
    HAS_PYMAVLINK = True
except ImportError:
    HAS_PYMAVLINK = False
    mavutil = None  # type: ignore

from raspi.pkg.config import Config, Target, get_config
from raspi.pkg.state import MAVState
from raspi.pkg.utils import horizontal_distance_m

class Vehicle:
    """
    FC abstraction mirip pkg.vehicle.QuadPlane di guided-dropping-mission,
    tapi disesuaikan untuk raspipkg:
    - connect() dengan udp: -> udpin: fallback (Mission Planner)
    - mock mode jika heartbeat timeout
    - state cache via MAVState
    - guided via mission_item_int_send (reliable untuk ArduPilot GUIDED)
    API luar cukup: await vehicle.connect(); await vehicle.send_guided(lat,lon,alt)
    """
    def __init__(self, config: Config | None = None) -> None:
        self.cfg = config or get_config()
        self.master: Optional[object] = None  # mavutil.mavfile
        self.state: Optional[MAVState] = None
        self._mock = False
        self._mock_pos_t0 = time.time()

    # ---------- internal helpers ----------
    def _candidates(self) -> list[str]:
        cs = self.cfg.connection_string.strip() or "udp:127.0.0.1:14550"
        cands = [cs]
        if cs.startswith("udp:") and "udpin:" not in cs:
            cands.append(cs.replace("udp:", "udpin:", 1))
        return cands

    def _ensure_master(self) -> bool:
        return self.master is not None and not self._mock

    # ---------- public API ----------
    async def connect(self, timeout: float = 10) -> bool:
        """Async connect. Returns True if real, False if mock."""
        if not HAS_PYMAVLINK:
            logger.warning("[pkg] pymavlink not installed, mock mode")
            self._mock = True
            return False
        for cand in self._candidates():
            try:
                logger.info(f"[pkg] connecting {cand} baud={self.cfg.baud}")
                # mavutil.mavlink_connection handles baud for serial; udp/tcp ignore baud
                if cand.startswith("serial://"):
                    master = mavutil.mavlink_connection(cand)
                elif cand.startswith(("/", "COM")):
                    master = mavutil.mavlink_connection(cand, baud=self.cfg.baud)
                else:
                    master = mavutil.mavlink_connection(cand, baud=self.cfg.baud)
                await asyncio.to_thread(master.wait_heartbeat, timeout)
                self.master = master
                logger.info(f"[pkg] heartbeat sys {master.target_system} comp {master.target_component} via {cand}")
                logger.info(f"[pkg] modes: {list(master.mode_mapping().keys())}")
                self.state = MAVState(master)
                self.state.start()
                # request streams 5Hz
                self._request_interval(mavutil.mavlink.MAVLINK_MSG_ID_GLOBAL_POSITION_INT, 5)
                self._request_interval(mavutil.mavlink.MAVLINK_MSG_ID_EXTENDED_SYS_STATE, 5)
                await asyncio.sleep(0.5)
                self._mock = False
                return True
            except Exception as e:
                logger.warning(f"[pkg] connect {cand} failed: {e}")
                continue
        logger.warning("[pkg] all candidates failed, entering mock mode")
        self._mock = True
        self.master = None
        self.state = None
        return False

    def connect_sync(self, timeout: float = 10) -> bool:
        return asyncio.run(self.connect(timeout))

    def _request_interval(self, msg_id: int, hz: int) -> None:
        if not self._ensure_master() or self.state is None:
            return
        try:
            self.master.mav.command_long_send(
                self.master.target_system, self.master.target_component,
                mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL, 0,
                msg_id, int(1e6 / hz), 0, 0, 0, 0, 0,
            )
            ack = self.state.wait("COMMAND_ACK", timeout_s=3, max_age=3)
            logger.debug(f"[pkg] stream {msg_id} @{hz}Hz -> {ack}")
        except Exception as e:
            logger.debug(f"[pkg] request interval failed {e}")

    # ---------- mode / arm helpers (port dari QuadPlane) ----------
    def ensure_q_guided_mode(self) -> bool:
        if self._mock:
            logger.info(f"[pkg mock] Q_GUIDED_MODE={self.cfg.q_guided_mode}")
            return True
        if not self._ensure_master() or self.state is None:
            return False
        try:
            logger.info(f"[pkg] set Q_GUIDED_MODE={self.cfg.q_guided_mode}")
            self.master.mav.param_set_send(
                self.master.target_system, self.master.target_component,
                b"Q_GUIDED_MODE", self.cfg.q_guided_mode, mavutil.mavlink.MAV_PARAM_TYPE_INT32,
            )
            msg = self.state.wait("PARAM_VALUE", timeout_s=3, max_age=3)
            val = msg.param_value if msg else "no ack"
            logger.info(f"[pkg] Q_GUIDED_MODE -> {val}")
            return True
        except Exception as e:
            logger.warning(f"[pkg] Q_GUIDED_MODE failed {e}")
            return False

    def set_mode(self, mode_name: str, timeout_s: float = 10) -> bool:
        if self._mock:
            logger.info(f"[pkg mock] set_mode {mode_name}")
            return True
        if not self._ensure_master() or self.state is None:
            return False
        if mode_name == "QGUIDED":
            mode_name = "GUIDED"
        try:
            mode_id = self.master.mode_mapping()[mode_name]
        except KeyError:
            logger.warning(f"[pkg] mode {mode_name} not in mapping {list(self.master.mode_mapping().keys())}")
            return False
        self.master.mav.set_mode_send(
            self.master.target_system, mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, mode_id,
        )
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            hb = self.state.wait("HEARTBEAT", timeout_s=2, max_age=2)
            if hb and mavutil.mode_string_v10(hb) == mode_name:
                logger.info(f"[pkg] mode confirmed: {mode_name}")
                return True
            time.sleep(0.2)
        logger.warning(f"[pkg] mode {mode_name} not confirmed")
        return False

    def is_armed(self) -> bool:
        if self._mock:
            return True
        if self.state is None:
            return False
        hb = self.state.get("HEARTBEAT", max_age=2)
        return bool(hb and (hb.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED))

    def get_position(self) -> Optional[Dict]:
        if self._mock:
            t = time.time()
            # slow drift for testing
            return {
                "lat": -7.2778252 + 0.00001 * (t - self._mock_pos_t0) * 0.1,
                "lon": 112.7976662 + 0.00001 * (t - self._mock_pos_t0) * 0.1,
                "alt": 15.0,
                "rel_alt": 15.0,
                "speed": 0.2,
                "ts": t,
                "mock": True,
                "source": "mock",
            }
        if self.state is None:
            return None
        msg = self.state.get("GLOBAL_POSITION_INT", max_age=1.0)
        if msg is None:
            return None
        return {
            "lat": msg.lat / 1e7,
            "lon": msg.lon / 1e7,
            "alt": msg.relative_alt / 1000.0,
            "rel_alt": msg.relative_alt / 1000.0,
            "amsl": msg.alt / 1000.0,
            "speed": math.hypot(msg.vx / 100.0, msg.vy / 100.0),
            "vx": msg.vx / 100.0,
            "vy": msg.vy / 100.0,
            "hdop": None,
            "ts": time.time(),
            "source": "GLOBAL_POSITION_INT",
        }

    def ensure_armed_and_airborne(self) -> None:
        if self._mock:
            return
        if not self.is_armed():
            self.arm()
        self.takeoff_if_needed(self.cfg.target.alt_m)

    def arm(self) -> None:
        if self._mock or not self._ensure_master() or self.state is None:
            return
        logger.info("[pkg] arming")
        self.master.mav.command_long_send(
            self.master.target_system, self.master.target_component,
            mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 0, 1, 0, 0, 0, 0, 0, 0,
        )
        ack = self.state.wait("COMMAND_ACK", timeout_s=3, max_age=3)
        logger.info(f"[pkg] arm ack {ack}")
        try:
            self.master.motors_armed_wait()
            logger.info("[pkg] ARMED")
        except Exception:
            pass

    def takeoff_if_needed(self, alt_m: float) -> None:
        if self._mock:
            return
        pos = self.get_position()
        alt_now = pos["alt"] if pos else 0
        if alt_now >= 5:
            logger.info(f"[pkg] alt {alt_now:.1f}m airborne - skip takeoff")
            return
        if not self._ensure_master() or self.state is None:
            return
        logger.info(f"[pkg] takeoff {alt_m}m (now {alt_now:.1f}m)")
        self.master.mav.command_long_send(
            self.master.target_system, self.master.target_component,
            mavutil.mavlink.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, 0, 0, 0, 0, alt_m,
        )
        ack = self.state.wait("COMMAND_ACK", timeout_s=3, max_age=3)
        logger.info(f"[pkg] takeoff ack {ack}")
        time.sleep(8)

    # ---------- GUIDED core (dipanggil luar) ----------
    def goto(self, target: Target) -> bool:
        """Sync goto via mission_item_int_send. Caller pakai asyncio.to_thread jika dari async."""
        if self._mock:
            logger.info(f"[pkg mock] goto {target.lat},{target.lon} alt {target.alt_m}")
            time.sleep(0.2)
            return True
        if not self._ensure_master() or self.state is None:
            logger.warning("[pkg] goto failed: not connected")
            return False
        self.master.mav.mission_item_int_send(
            self.master.target_system, self.master.target_component,
            0, mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
            mavutil.mavlink.MAV_CMD_NAV_WAYPOINT, 2, 0, 0, 0, 0, 0,
            int(target.lat * 1e7), int(target.lon * 1e7), target.alt_m,
        )
        ack = self.state.wait("MISSION_ACK", timeout_s=3, max_age=3)
        logger.info(f"[pkg] GOTO {target.lat},{target.lon} alt {target.alt_m}m -> MISSION_ACK {ack}")
        if ack is None or getattr(ack, "result", 1) != 0:
            logger.warning(f"[pkg] goto rejected result={getattr(ack, 'result', 'none')}")
            return False
        return True

    def wait_arrival(self, target: Target, timeout_s: float | None = None) -> bool:
        if self._mock:
            time.sleep(0.5)
            return True
        timeout_s = timeout_s or self.cfg.guided_timeout_s
        logger.info(f"[pkg] wait arrival dist<{self.cfg.arrival_radius_m}m speed<{self.cfg.arrival_speed_mps}m/s timeout {timeout_s}s")
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            pos = self.get_position()
            if pos is None:
                time.sleep(0.2)
                continue
            dist = horizontal_distance_m(pos["lat"], pos["lon"], target.lat, target.lon)
            logger.info(f"[pkg] dist={dist:.1f}m speed={pos['speed']:.2f}m/s alt={pos['alt']:.1f}m")
            if dist <= self.cfg.arrival_radius_m and pos["speed"] <= self.cfg.arrival_speed_mps:
                logger.info("[pkg] *** ARRIVED ***")
                return True
            time.sleep(0.5)
        logger.warning("[pkg] arrival timeout")
        return False

    def wait_vtol_hover(self, timeout_s: float = 30) -> bool:
        if self._mock:
            return True
        if self.state is None:
            return False
        logger.info("[pkg] wait VTOL MC hover")
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            msg = self.state.get("EXTENDED_SYS_STATE", max_age=2)
            if msg and msg.vtol_state == mavutil.mavlink.MAV_VTOL_STATE_MC:
                logger.info("[pkg] VTOL MC confirmed")
                return True
            time.sleep(0.2)
        logger.warning("[pkg] VTOL MC timeout")
        return False

    # ---------- high-level async API untuk pemanggil luar ----------
    async def send_guided(self, lat: float, lon: float, alt: float | None = None) -> Dict:
        """
        API utama luar: cukup panggil await vehicle.send_guided(lat,lon,alt)
        Langkah: set_mode GUIDED -> goto -> return dict {ok, ...}
        alt: relatif AGL, default dari config.guided_alt_default
        """
        if alt is None:
            alt = self.cfg.guided_alt_default
        try:
            lat = float(lat); lon = float(lon); alt = float(alt)
        except:
            return {"ok": False, "error": "lat/lon/alt must be numbers"}
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return {"ok": False, "error": "lat/lon out of range"}
        target = Target(lat=lat, lon=lon, alt_m=alt)

        # mock fast path
        if self._mock:
            # ensure mock still logs
            await asyncio.sleep(0.1)
            logger.info(f"[pkg mock] send_guided {lat},{lon} alt {alt}")
            return {"ok": True, "mock": True, "target": {"lat": lat, "lon": lon, "alt_m": alt}}

        # ensure connected
        if not self._ensure_master():
            return {"ok": False, "error": "FC not connected", "mock": self._mock}

        # blocking ops via thread
        def _blocking():
            # set mode GUIDED (with Q_GUIDED_MODE if needed)
            # for QuadPlane we set Q_GUIDED_MODE=1, for generic copter this is harmless but we skip if param fails
            try:
                self.ensure_q_guided_mode()
            except:
                pass
            ok_mode = self.set_mode("GUIDED")
            if not ok_mode:
                return {"ok": False, "error": "failed to set GUIDED mode"}
            ok_goto = self.goto(target)
            if not ok_goto:
                return {"ok": False, "error": "goto rejected"}
            return {"ok": True, "target": {"lat": lat, "lon": lon, "alt_m": alt}, "mode": "GUIDED"}

        result = await asyncio.to_thread(_blocking)
        return result

    def is_mock(self) -> bool:
        return self._mock

    def is_connected(self) -> bool:
        return self._ensure_master() or self._mock

    def close(self):
        if self.state:
            try:
                self.state.stop()
            except:
                pass
            self.state = None
        # master close not needed for pymavlink
