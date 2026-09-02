from dataclasses import dataclass, field
import os

@dataclass(frozen=True)
class Target:
    lat: float = -7.2778252
    lon: float = 112.7976662
    alt_m: float = 20.0

@dataclass(frozen=True)
class Config:
    connection_string: str = field(default_factory=lambda: os.getenv("FC_CONNECTION_STRING", "udp:127.0.0.1:14550"))
    baud: int = field(default_factory=lambda: int(os.getenv("FC_BAUD", "57600")))
    target: Target = field(default_factory=Target)
    arrival_radius_m: float = field(default_factory=lambda: float(os.getenv("FC_ARRIVAL_RADIUS_M", "5.0")))
    arrival_speed_mps: float = field(default_factory=lambda: float(os.getenv("FC_ARRIVAL_SPEED_MPS", "0.5")))
    guided_timeout_s: float = field(default_factory=lambda: float(os.getenv("FC_GUIDED_TIMEOUT_S", "90")))
    q_guided_mode: int = field(default_factory=lambda: int(os.getenv("FC_Q_GUIDED_MODE", "1")))
    telemetry_hz: int = field(default_factory=lambda: int(os.getenv("TELEMETRY_HZ", "10")))
    guided_alt_default: float = field(default_factory=lambda: float(os.getenv("GUIDED_ALT_DEFAULT", "20")))

# singleton default
_DEFAULT_CONFIG: Config | None = None

def get_config() -> Config:
    global _DEFAULT_CONFIG
    if _DEFAULT_CONFIG is None:
        _DEFAULT_CONFIG = Config()
    return _DEFAULT_CONFIG

# for compat with common.config
def get_fc_config() -> Config:
    return get_config()
