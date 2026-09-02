"""
raspi.pkg — FC abstraction for guided mission.
Pemanggil di luar cukup: from raspi.pkg import get_vehicle, Target
vehicle = get_vehicle()
await vehicle.connect()
result = await vehicle.send_guided(lat, lon, alt)
pos = vehicle.get_position()
"""
from raspi.pkg.config import Config, Target, get_config
from raspi.pkg.vehicle import Vehicle
from raspi.pkg.state import MAVState
from raspi.pkg.utils import horizontal_distance_m

_vehicle_singleton: Vehicle | None = None

def get_vehicle(config: Config | None = None) -> Vehicle:
    global _vehicle_singleton
    if _vehicle_singleton is None:
        _vehicle_singleton = Vehicle(config or get_config())
    return _vehicle_singleton

def reset_vehicle():
    global _vehicle_singleton
    if _vehicle_singleton is not None:
        try:
            _vehicle_singleton.close()
        except:
            pass
    _vehicle_singleton = None

__all__ = ["Config", "Target", "Vehicle", "MAVState", "horizontal_distance_m", "get_vehicle", "reset_vehicle", "get_config"]
