"""Terminal presentation: colour, ASCII banner, and arrow-key menus.

Kept separate from the conversion logic and entirely optional. Every helper
degrades to plain ASCII when stdout is not a terminal, when NO_COLOR is set,
or when TERM is "dumb" -- piping the output to a file stays clean.
"""

from __future__ import annotations

import os
import sys

_RESET = "\033[0m"
_STYLES = {
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
}


def colour_enabled(stream=None) -> bool:
    """True when it is safe to emit ANSI escapes to `stream`."""
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if not hasattr(stream, "isatty") or not stream.isatty():
        return False
    return os.environ.get("TERM", "") not in ("dumb", "")


def paint(text: str, *styles: str) -> str:
    """Wrap text in ANSI styles, or return it unchanged when colour is off."""
    if not styles or not colour_enabled():
        return text
    prefix = "".join(_STYLES[s] for s in styles if s in _STYLES)
    return f"{prefix}{text}{_RESET}" if prefix else text


def rule(char: str = "-", width: int = 62) -> str:
    return char * width


BANNER = r"""
  _____           _    _ _   _ _
 / ____|         | |  | | | (_) |
| |     __ _ _ __| |  | | |_ _| |
| |    / _` | '__| |  | | __| | |
| |___| (_| | |  | |__| | |_| | |
 \_____\__,_|_|   \____/ \__|_|_|

        CarUtil -- ATS Road Trip car converter
"""


def banner() -> str:
    """The startup banner, dimmed so it never shouts over the real output."""
    return paint(BANNER.strip("\n"), "cyan") + "\n"


def heading(text: str) -> None:
    print()
    print(paint(f"  {text}", "bold"))


def step(index: int, total: int, text: str) -> None:
    """A numbered step in the guided walkthrough."""
    print(paint(f"  [{index}/{total}]", "magenta", "bold") + f" {text}")


def ok(text: str) -> None:
    print("  " + paint("+", "green", "bold") + " " + text)


def bullet(text: str) -> None:
    print("  " + paint("*", "cyan") + " " + text)


def warn(text: str) -> None:
    print("  " + paint("!", "yellow", "bold") + " " + paint(text, "yellow"))


def fail(text: str) -> None:
    print("  " + paint("x", "red", "bold") + " " + paint(text, "red"))


def path_line(label: str, value: str) -> None:
    print(f"  {paint(label + ':', 'bold')} {value}")


# --------------------------------------------------------------------- menus

_ARROW_UP = "\x1b[A"
_ARROW_DOWN = "\x1b[B"


def arrow_menu(options: list[tuple[str, str]], default: int = 0) -> int:
    """Pick one option with the arrow keys; falls back to typing a number.

    `options` is a list of (label, hint). Returns the chosen index. Requires a
    terminal that can read raw keys; anything else falls back to the numbered
    prompt so scripted and piped runs still work.
    """
    if not (sys.stdin.isatty() and _raw_keys_available()):
        return _numbered_menu(options, default)

    cursor = default
    redraw = 0
    while True:
        if redraw:
            sys.stdout.write(f"\033[{len(options)}A")
        # Rebuilt every frame: the marker follows the cursor.
        for i, (label, hint) in enumerate(options):
            sys.stdout.write("\033[2K" + _menu_label(label, hint, i == cursor) + "\n")
        redraw = len(options)
        key = _read_key()
        if key in ("\r", "\n"):
            sys.stdout.write("\n")
            return cursor
        if key == _ARROW_UP:
            cursor = (cursor - 1) % len(options)
        elif key == _ARROW_DOWN:
            cursor = (cursor + 1) % len(options)
        elif key in ("q", "\x03", "\x04"):
            raise SystemExit(1)
        elif key.isdigit():
            n = int(key)
            if 1 <= n <= len(options):
                sys.stdout.write("\n")
                return n - 1


def _menu_label(label: str, hint: str, selected: bool) -> str:
    marker = paint("  > ", "cyan", "bold") if selected else "    "
    style = "bold" if selected else "dim"
    return marker + paint(label, style) + _hint(hint)


def _hint(hint: str) -> str:
    return "  " + paint(hint, "dim") if hint else ""


def _numbered_menu(options: list[tuple[str, str]], default: int) -> int:
    print()
    for i, (label, hint) in enumerate(options, 1):
        print(f"    {i}. {label}" + (f"  {paint(hint, 'dim')}" if hint else ""))
    print()
    while True:
        try:
            raw = input(f"  Choice [{default + 1}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            raise SystemExit(1)
        if not raw:
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1


def _raw_keys_available() -> bool:
    """True if we can read single keypresses without Enter.

    termios/tty are POSIX-only. The imports must sit inside the try: on
    Windows they raise ModuleNotFoundError, and an unguarded import here
    takes down the whole guided walkthrough one prompt after the user
    pastes their mod path -- before the numbered-menu fallback can engage.
    """
    try:
        import termios
        import tty

        termios.tcgetattr(sys.stdin.fileno())
        return True
    except Exception:
        return False


def _read_key() -> str:
    """Read one keypress, collapsing an ANSI arrow sequence into a single str."""
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":                      # escape sequence
            nxt = sys.stdin.read(2)
            return {"[A": _ARROW_UP, "[B": _ARROW_DOWN}.get(nxt, "\x1b")
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)