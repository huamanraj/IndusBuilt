"""
ASCII banner for the welcome screen. Designed to be readable on terminals
narrower than the full BANNER_FULL width; falls back to compact text.
"""
from __future__ import annotations

import shutil

BANNER_FULL = [
    "     ____          __           ____        _ ____       ________    ____",
    "    /  _/___  ____/ /_  _______/ __ )__  __(_) / /_     / ____/ /   /  _/",
    "    / // __ \\/ __  / / / / ___/ __  / / / / / / __/    / /   / /    / /  ",
    "  _/ // / / / /_/ / /_/ (__  ) /_/ / /_/ / / / /_     / /___/ /____/ /   ",
    " /___/_/ /_/\\__,_/\\__,_/____/_____/\\__,_/_/_/\\__/     \\____/_____/___/   ",
]


def render_banner() -> str:
    width = shutil.get_terminal_size((100, 20)).columns
    full_width = max(len(line) for line in BANNER_FULL)
    if width >= full_width:
        return "\n".join(BANNER_FULL)
    return "IndusBuilt CLI"
