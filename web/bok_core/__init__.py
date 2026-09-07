"""Bok local-first memory core."""

from .config import BokConfig
from .service import BokService
from .version import VERSION

__all__ = ["BokConfig", "BokService"]
__version__ = VERSION
