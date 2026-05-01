"""Platform adapters for different equipment monitoring systems."""

from .base_platform import BasePlatform
from .vrm_platform import VRMPlatform

__all__ = ["BasePlatform", "VRMPlatform"]
