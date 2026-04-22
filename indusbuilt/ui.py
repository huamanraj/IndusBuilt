"""
Terminal UI helpers for IndusBuilt.
"""
import json
import os
import shutil
import sys
import threading
import time
from itertools import cycle
from pathlib import Path
from typing import Any, Dict, List

if os.name == "nt":
    import msvcrt
else:
    import termios
    import tty

# ANSI colors
YOU_COLOR = "\u001b[94m"
ASSISTANT_COLOR = "\u001b[93m"
TOOL_COLOR = "\u001b[96m"
ERROR_COLOR = "\u001b[91m"
SUCCESS_COLOR = "\u001b[92m"
MUTED_COLOR = "\u001b[90m"
TITLE_COLOR = "\u001b[95m"
RESET_COLOR = "\u001b[0m"

BANNER_FULL = [
    "     ____          __           ____        _ ____       ________    ____",
    "    /  _/___  ____/ /_  _______/ __ )__  __(_) / /_     / ____/ /   /  _/",
    "    / // __ \\/ __  / / / / ___/ __  / / / / / / __/    / /   / /    / /  ",
    "  _/ // / / / /_/ / /_/ (__  ) /_/ / /_/ / / / /_     / /___/ /____/ /   ",
    " /___/_/ /_/\\__,_/\\__,_/____/_____/\\__,_/_/_/\\__/     \\____/_____/___/   ",
]

BANNER_COMPACT = [
    "IndusBuilt CLI",
    "The fastest coding agent",
]


def _terminal_width() -> int:
    return shutil.get_terminal_size((100, 20)).columns


def print_startup_banner(sandbox_root: Path, model: str) -> None:
    width = _terminal_width()
    full_width = max(len(line) for line in BANNER_FULL)
    print()

    if width >= full_width:
        for line in BANNER_FULL:
            print(f"{TITLE_COLOR}{line}{RESET_COLOR}")
    else:
        for line in BANNER_COMPACT:
            print(f"{TITLE_COLOR}{line}{RESET_COLOR}")

    print(f"{SUCCESS_COLOR}  The fastest coding agent{RESET_COLOR}")
    print(f"{TOOL_COLOR}  Sandbox: {sandbox_root}{RESET_COLOR}")
    print()


def print_runtime_meta(provider: str, model: str) -> None:
    print(f"{TOOL_COLOR}  Provider: {provider}{RESET_COLOR}")
    print(f"{TOOL_COLOR}  Model:    {model}{RESET_COLOR}")
    print(f"{MUTED_COLOR}  Type '/' for settings and commands.{RESET_COLOR}")
    print(f"{MUTED_COLOR}  Type 'exit' or Ctrl+C to quit.{RESET_COLOR}\n")


def print_slash_help() -> None:
    print(f"{TITLE_COLOR}Slash Commands{RESET_COLOR}")
    print(f"{MUTED_COLOR}  /         Open command palette{RESET_COLOR}")
    print(f"{MUTED_COLOR}  /key      Set API key (provider-wise){RESET_COLOR}")
    print(f"{MUTED_COLOR}  /model    Choose model for provider{RESET_COLOR}")
    print(f"{MUTED_COLOR}  /provider Switch active provider{RESET_COLOR}")
    print(f"{MUTED_COLOR}  /show     Show current provider + model{RESET_COLOR}")
    print(f"{MUTED_COLOR}  /help     Show this help{RESET_COLOR}")
    print(f"{MUTED_COLOR}  /exit     Exit agent{RESET_COLOR}\n")


def print_success(message: str) -> None:
    print(f"{SUCCESS_COLOR}{message}{RESET_COLOR}")


def print_error(message: str) -> None:
    print(f"{ERROR_COLOR}{message}{RESET_COLOR}")


class Spinner:
    """Simple terminal spinner for short loading states."""

    def __init__(self, label: str):
        self.label = label
        self._frames = cycle(["-", "\\", "|", "/"])
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._cleared = False

    def _run(self) -> None:
        while not self._stop_event.is_set():
            frame = next(self._frames)
            sys.stdout.write(f"\r{MUTED_COLOR}{frame} {self.label}...{RESET_COLOR}")
            sys.stdout.flush()
            time.sleep(0.1)

    def stop(self) -> None:
        if self._stop_event.is_set():
            return
        self._stop_event.set()
        self._thread.join(timeout=0.3)
        if not self._cleared:
            clear_width = max(60, len(self.label) + 8)
            sys.stdout.write("\r" + (" " * clear_width) + "\r")
            sys.stdout.flush()
            self._cleared = True

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()


TOOL_STATES = {
    "read_file": "reading",
    "list_files": "scanning",
    "edit_file": "creating",
}


def print_tool_call(tool_name: str, args: Dict[str, Any], result: Dict[str, Any]) -> None:
    state = TOOL_STATES.get(tool_name, "working")
    args_text = json.dumps(args, ensure_ascii=False)
    status = result.get("action") or ("error" if "error" in result else "ok")

    print(f"{TOOL_COLOR}[tool] {tool_name}  state={state}{RESET_COLOR}")
    print(f"{MUTED_COLOR}       args: {args_text}{RESET_COLOR}")
    print(f"{MUTED_COLOR}       result: {status}{RESET_COLOR}")


def print_assistant_prefix() -> None:
    print(f"{ASSISTANT_COLOR}IndusBuilt > {RESET_COLOR}", end="")


def print_user_prompt() -> str:
    return f"{YOU_COLOR}You >{RESET_COLOR} "


def _clear_screen() -> None:
    sys.stdout.write("\x1b[2J\x1b[H")
    sys.stdout.flush()


def _read_key() -> str:
    if os.name == "nt":
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            ch2 = msvcrt.getwch()
            if ch2 == "H":
                return "UP"
            if ch2 == "P":
                return "DOWN"
            return "OTHER"
        if ch == "\r":
            return "ENTER"
        if ch == "\x1b":
            return "ESC"
        return "OTHER"

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            seq = sys.stdin.read(2)
            if seq == "[A":
                return "UP"
            if seq == "[B":
                return "DOWN"
            return "ESC"
        if ch in ("\n", "\r"):
            return "ENTER"
        return "OTHER"
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def choose_from_list(title: str, options: List[str], hint: str = "Use ↑/↓ and Enter") -> int:
    """Arrow-key selector with simple fallback for very limited terminals."""
    if not options:
        raise ValueError("options must not be empty")

    # Fallback for non-tty input (e.g. piped execution)
    if not sys.stdin.isatty():
        print(title)
        for i, option in enumerate(options, start=1):
            print(f"  {i}. {option}")
        raw = input("Select number: ").strip()
        try:
            idx = int(raw) - 1
        except ValueError:
            idx = 0
        return max(0, min(idx, len(options) - 1))

    selected = 0
    while True:
        _clear_screen()
        print(f"{TITLE_COLOR}{title}{RESET_COLOR}")
        print(f"{MUTED_COLOR}{hint}{RESET_COLOR}\n")
        for i, option in enumerate(options):
            if i == selected:
                print(f"{SUCCESS_COLOR}> {option}{RESET_COLOR}")
            else:
                print(f"  {option}")

        key = _read_key()
        if key == "UP":
            selected = (selected - 1) % len(options)
        elif key == "DOWN":
            selected = (selected + 1) % len(options)
        elif key == "ENTER":
            _clear_screen()
            return selected
        elif key == "ESC":
            _clear_screen()
            return selected
