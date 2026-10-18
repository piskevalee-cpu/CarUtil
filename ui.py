"""Terminal presentation: Rich output, Questionary menus, and plain fallbacks.

The UI packages are optional. Colors respect NO_COLOR and FORCE_COLOR, piped
output stays unstyled, and menus use numbered choices without a usable TTY.
"""

from __future__ import annotations

import os
import sys

try:
    import questionary as _questionary
except ImportError:
    _questionary = None

try:
    from rich.console import Console as _Console
    from rich.panel import Panel as _Panel
    from rich.rule import Rule as _Rule
    from rich.text import Text as _Text
except ImportError:
    _Console = None
    _Panel = None
    _Rule = None
    _Text = None

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


def enhanced_tui_available() -> bool:
    return _questionary is not None and _Console is not None


def missing_tui_packages() -> list[str]:
    missing = []
    if _questionary is None:
        missing.append("questionary")
    if _Console is None:
        missing.append("rich")
    return missing


def setup_hint() -> None:
    if enhanced_tui_available():
        return
    executable = "py" if os.name == "nt" else "python3"
    print()
    print("  The enhanced menus are not installed yet.")
    print("  To set them up, run this command from the CarUtil folder:")
    print(f"    {executable} -m pip install -r requirements.txt")
    print("  CarUtil will continue with basic numbered menus for now.")
    print()


def _console(stream=None):
    if _Console is None:
        return None
    stream = stream or sys.stdout
    enabled = colour_enabled(stream)
    return _Console(
        file=stream,
        force_terminal=enabled,
        no_color=not enabled,
        highlight=False,
    )


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


def show_banner() -> None:
    console = _console()
    if console is not None and sys.stdout.isatty():
        console.print(
            _Panel(
                BANNER.strip("\n"),
                title="CarUtil",
                border_style="cyan",
                padding=(0, 2),
            )
        )
    else:
        print(banner(), end="")


def heading(text: str) -> None:
    console = _console()
    if console is not None and sys.stdout.isatty():
        console.print(_Rule(text, style="cyan"))
    else:
        print()
        print(paint(f"  {text}", "bold"))


def step(index: int, total: int, text: str) -> None:
    """A numbered step in the guided walkthrough."""
    console = _console()
    if console is not None:
        console.print(
            _Text.assemble(
                (f"  [{index}/{total}]", "bold cyan"),
                (f" {text}", "bold"),
            )
        )
    else:
        print(paint(f"  [{index}/{total}]", "magenta", "bold") + f" {text}")


def ok(text: str) -> None:
    _status("+", text, "green")


def bullet(text: str) -> None:
    _status("*", text, "cyan")


def warn(text: str) -> None:
    _status("!", text, "yellow")


def fail(text: str) -> None:
    _status("x", text, "red")


def _status(marker: str, text: str, color: str) -> None:
    console = _console()
    if console is not None:
        console.print(
            _Text.assemble(("  ", ""), (marker, f"bold {color}"), (" ", ""),
                           (text, ""))
        )
    else:
        print("  " + paint(marker, color, "bold") + " " + text)


def path_line(label: str, value: str) -> None:
    console = _console()
    if console is not None:
        console.print(
            _Text.assemble(("  " + label + ":", "bold"), (" " + value, ""))
        )
    else:
        print(f"  {paint(label + ':', 'bold')} {value}")


# --------------------------------------------------------------------- menus

def arrow_menu(options: list[tuple[str, str]], default: int = 0) -> int:
    """Show an arrow-key menu, with a numbered fallback for plain terminals."""
    if (
        _questionary is None
        or not sys.stdin.isatty()
        or not sys.stdout.isatty()
        or os.environ.get("TERM") == "dumb"
    ):
        return _numbered_menu(options, default)

    choices = [
        _questionary.Choice(title=label, value=i, description=hint or None)
        for i, (label, hint) in enumerate(options)
    ]
    color_style = (
        _questionary.Style([
            ("qmark", "fg:#00afff bold"),
            ("question", "bold"),
            ("answer", "fg:#00afff bold"),
            ("pointer", "fg:#00afff bold"),
            ("highlighted", "fg:#00afff bold"),
            ("selected", "fg:#00afff"),
            ("instruction", "fg:#888888"),
            ("text", ""),
            ("disabled", "fg:#888888 italic"),
            ("description", "fg:#888888"),
        ])
        if colour_enabled()
        else _questionary.Style([])
    )
    answer = _questionary.select(
        "",
        choices=choices,
        default=next((choice for choice in choices if choice.value == default),
                     None),
        qmark="",
        pointer="›",
        style=color_style,
        use_arrow_keys=True,
        show_description=True,
        instruction="(Use ↑/↓ to move, Enter to select)",
    ).ask()
    if answer is None:
        raise SystemExit(1)
    return answer


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
        print(f"  -> choose a number from 1 to {len(options)}")