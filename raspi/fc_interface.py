"""
Shim for backward compatibility.
New code should use: from raspi.pkg import get_vehicle
"""
import logging
logger = logging.getLogger(__name__)

# Re-export Vehicle as FCInterface for old imports
from raspi.pkg.vehicle import Vehicle as FCInterface
from raspi.pkg.config import Config, Target, get_config

__all__ = ["FCInterface", "Config", "Target", "get_config"]

# Also expose legacy import path warning
logger.debug("[fc_interface] deprecated, use raspi.pkg instead")
