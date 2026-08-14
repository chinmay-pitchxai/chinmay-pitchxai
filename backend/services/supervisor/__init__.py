"""Super Boss parent supervisor."""

from services.supervisor.panther_mode import (
    get_panther_status,
    is_panther_mode_active,
    panther_codeword,
    trigger_panther_mode,
)
from services.supervisor.super_boss import (
    get_super_boss_status,
    start_super_boss,
    stop_super_boss,
)

__all__ = [
    "get_super_boss_status",
    "start_super_boss",
    "stop_super_boss",
    "get_panther_status",
    "is_panther_mode_active",
    "panther_codeword",
    "trigger_panther_mode",
]
