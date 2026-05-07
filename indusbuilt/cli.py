"""
IndusBuilt CLI entry point.
Usage: indusbuilt
"""
import argparse
from pathlib import Path

from .agent import run_agent
from . import __version__
from .settings import load_settings


def main():
    parser = argparse.ArgumentParser(
        prog="indusbuilt",
        description="IndusBuilt – AI coding agent sandboxed to your current directory.",
    )
    parser.add_argument(
        "--version", "-v",
        action="version",
        version=f"IndusBuilt {__version__}",
    )
    parser.parse_args()

    # Sandbox = wherever the user launched indusbuilt from
    sandbox_root = Path.cwd().resolve()
    settings = load_settings()

    run_agent(sandbox_root=sandbox_root, settings=settings)


if __name__ == "__main__":
    main()
