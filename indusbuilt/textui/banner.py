"""
ASCII banner for the welcome screen.

Uses figlet "block" half-block characters (`▄ █ ▀ ▌ ▐`) so the wordmark reads
like a filled-pixel logo. The text width is fixed per tier and every line in
a tier is padded to the same length, so the right edge stays vertical at any
terminal width.

Several width tiers are provided so the banner still looks intentional on
narrower terminals (the block font is half-width friendly, so a 50-col
version still reads as the same word).
"""
from __future__ import annotations

import shutil

from .banner_data import BANNER_LINES as _BANNER_LARGE, BANNER_WIDTH as _LARGE_WIDTH


def _art_width(art: list[str]) -> int:
    return max(len(line) for line in art) if art else 0


LARGE_BANNER: list[str] = list(_BANNER_LARGE)
LARGE_WIDTH: int = _art_width(LARGE_BANNER)


_BANNERS: list[tuple[int, list[str]]] = [
    (LARGE_WIDTH, LARGE_BANNER),
]


def render_banner() -> str:
    """Return ASCII art sized for the current terminal width.

    Always returns ASCII art. If the terminal is narrower than the full
    banner, returns a compact "INDUSBUILT" plain-text wordmark so the welcome
    screen is never blank.
    """
    try:
        width = shutil.get_terminal_size((100, 20)).columns
    except Exception:
        width = 100

    for min_w, art in _BANNERS:
        if width >= min_w:
            return "\n".join(art)

    return "INDUSBUILT"


def banner_width(banner: str) -> int:
    """Return the visual width of a rendered banner (max line length)."""
    if not banner:
        return 0
    return max(len(line) for line in banner.splitlines())


