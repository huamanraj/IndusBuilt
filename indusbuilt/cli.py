"""
IndusBuilt CLI entry point.

Usage:
    indusbuilt                       Launch the TUI in the current directory
    indusbuilt --version             Print version and exit
"""
from __future__ import annotations

import argparse
from pathlib import Path

from . import __version__
from .settings import load_settings
from .textui import run


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="indusbuilt",
        description="IndusBuilt – AI coding agent sandboxed to your project directory.",
    )
    parser.add_argument(
        "--version", "-v",
        action="version",
        version=f"IndusBuilt {__version__}",
    )
    args = parser.parse_args()

    sandbox_root = Path.cwd().resolve()
    settings = load_settings()
    run(sandbox_root=sandbox_root, settings=settings)


if __name__ == "__main__":
    main()
