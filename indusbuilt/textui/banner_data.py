"""
ASCII banner data for the welcome screen wordmark "INDUSBUILT".

The wordmark is a 5-row, 5-col-per-letter block font.  Each visual row
of the wordmark is two terminal rows tall (every cell is either a full
block or a blank), so the banner is 10 terminal lines tall and 59
terminal columns wide.  Every line is padded to the same width so the
right edge stays vertical at any terminal width.
"""
from __future__ import annotations

# Block-font glyphs (5 rows x 5 cols, '#' = filled, ' ' = empty).
GLYPHS: dict[str, list[str]] = {
    "I": [
        "#####",
        "  #  ",
        "  #  ",
        "  #  ",
        "#####",
    ],
    "N": [
        "#   #",
        "##  #",
        "# # #",
        "#  ##",
        "#   #",
    ],
    "D": [
        "#### ",
        "#   #",
        "#   #",
        "#   #",
        "#### ",
    ],
    "U": [
        "#   #",
        "#   #",
        "#   #",
        "#   #",
        " ### ",
    ],
    "S": [
        " ####",
        "#    ",
        " ### ",
        "    #",
        "#### ",
    ],
    "B": [
        "#### ",
        "#   #",
        "#### ",
        "#   #",
        "#### ",
    ],
    "L": [
        "#    ",
        "#    ",
        "#    ",
        "#    ",
        "#####",
    ],
    "T": [
        "#####",
        "  #  ",
        "  #  ",
        "  #  ",
        "  #  ",
    ],
    " ": [
        "  ",
        "  ",
        "  ",
        "  ",
        "  ",
    ],
}


def _render(text: str) -> list[str]:
    glyphs = [GLYPHS[c] for c in text]
    out: list[str] = []
    for r in range(5):
        top_parts: list[str] = []
        bot_parts: list[str] = []
        for i, g in enumerate(glyphs):
            for ch in g[r]:
                if ch == "#":
                    top_parts.append("\u2588")
                    bot_parts.append("\u2588")
                else:
                    top_parts.append(" ")
                    bot_parts.append(" ")
            if i != len(glyphs) - 1:
                top_parts.append(" ")
                bot_parts.append(" ")
        out.append("".join(top_parts))
        out.append("".join(bot_parts))
    return out


BANNER_LINES_RAW: list[str] = _render("INDUSBUILT")
BANNER_LINES: list[str] = [ln.rstrip() for ln in BANNER_LINES_RAW]
BANNER_WIDTH: int = max(len(ln) for ln in BANNER_LINES)
BANNER_LINES = [ln.ljust(BANNER_WIDTH) for ln in BANNER_LINES]
