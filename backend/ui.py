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

# The interface is one palette. Every colour below is one of its six, so nothing
# on screen is a colour the rest of the program does not also use -- which is the
# only reason a six-colour scheme reads as one program rather than six.
#
# Two are a surface pair: the banner panel is painted Slate and its art is Ivory,
# which is the whole point of carrying a near-black and a near-white. The rest are
# chrome: Dusty Blue carries every structural mark, Clay every warning, Sage every
# success, Cloud everything secondary.
#
# Slate and Ivory are deliberately NOT given escape sequences in `_STYLES`. The
# plain-ANSI fallback has no background to paint a panel on, and `paint()` reaches
# `_STYLES` for foregrounds only, so a name there means "a colour we can write on
# the terminal's own background".
_ACCENT = "#6a9bcc"      # Claude Dusty Blue: steps, rules, menu cursor, bullets
_WARN = "#d97757"        # Claude Clay: every warning
_OK = "#788c5d"          # Claude Sage Green: success
_MUTED = "#b0aea5"       # Claude Cloud: hints, instructions, secondary text
_SURFACE = "#141413"     # Claude Slate: the banner panel's background
_TEXT = "#faf9f5"        # Claude Ivory: the banner art on that panel

# Truecolour for all four, because the terminal's own "yellow" and "green" are
# different colours to these and the point of naming them is that they are not.
_STYLES = {
    "bold": "\033[1m",
    "dim": "\033[2m",
    "accent": "\033[38;2;106;155;204m",
    "warn": "\033[38;2;217;119;87m",
    "ok": "\033[38;2;120;140;93m",
    "fail": "\033[38;2;250;249;245m",
}

# The names above, as Rich spells them. Anything absent is a colour Rich and the
# fallback agree on already, so it can be passed straight through.
#
# A failure is the surface pair reversed -- Ivory on Slate -- because the palette
# has no red and an unstyled marker is the one thing here that would be read as a
# bug. It is also distinct from a warning without borrowing a second warning
# colour, and it is legible on either background, which a mid-tone is not.
#
# The names are what the thing IS, never its colour: this palette has no yellow
# and no green, so a key called either would describe a colour the interface does
# not use and mislead the next person to grep for it.
_RICH = {
    "accent": _ACCENT,
    "warn": _WARN,
    "ok": _OK,
    "fail": f"bold {_TEXT} on {_SURFACE}",
}

# Questionary's own `DEFAULT_STYLE` has no `highlighted` rule, but
# prompt_toolkit's built-in style defines `selected` as `reverse` -- and the two
# are merged attribute-wise rather than replaced, so a `selected` rule that only
# sets a colour keeps the reverse video. `noreverse` is what cancels it.
_QUESTIONARY_STYLE = [
    ("qmark", f"fg:{_ACCENT} bold"),
    ("question", "bold"),
    ("answer", f"fg:{_ACCENT} bold"),
    ("pointer", f"fg:{_ACCENT} bold"),
    ("highlighted", f"fg:{_ACCENT} bold"),
    ("selected", f"fg:{_ACCENT} bold noreverse"),
    ("instruction", f"fg:{_MUTED}"),
    ("text", ""),
    ("separator", f"fg:{_MUTED}"),
    ("disabled", f"fg:{_MUTED} italic"),
]


def colour_enabled(stream=None) -> bool:
    """True when it is safe to emit ANSI escapes to `stream`."""
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if not hasattr(stream, "isatty") or not stream.isatty():
        return False
    # `dumb` is a Unix convention and Rich refuses it, so this must too. An
    # absent TERM is not a refusal: Windows never sets one, so demanding it
    # switched colour off for every console user on the platform that ships the
    # launcher, and left the two halves of the interface disagreeing -- Rich,
    # which asks for `dumb`/`unknown` only, would have coloured a terminal this
    # function called plain.
    return os.environ.get("TERM", "") not in ("dumb", "unknown")


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
        # Pinned, but only when colour is on. Rich otherwise sniffs the host
        # and on Windows resolves to its 4-bit `windows` system even for a
        # terminal that handles 24-bit, which collapses the palette onto 16
        # colours -- slate and ivory arrive as black and bright white, and the
        # downgrade is invisible because a name still "paints". Left unset when
        # colour is off, because naming a system makes Rich treat the stream as
        # colour-capable: a piped run then keeps attributes it should drop, and
        # output that must be plain grows a stray `ESC[1m`.
        color_system="truecolor" if enabled else None,
        # Rich's Windows path otherwise routes output through the legacy
        # console renderer, which approximates every colour to 16 again.
        legacy_windows=False,
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
    return paint(BANNER.strip("\n"), "accent") + "\n"


def show_banner() -> None:
    console = _console()
    if console is not None and sys.stdout.isatty():
        # Slate behind Ivory, which is the only reason the palette carries a
        # near-black and a near-white: the panel is a surface, not just a border.
        # `style` paints the whole Panel, so the padding and border are inside it.
        console.print(
            _Panel(
                _Text(BANNER.strip("\n"), style=_TEXT),
                # A Text, not title_style= : Panel has no such keyword, and
                # `title` accepting a Text is the part that is actually in the
                # signature. show_banner only runs on a TTY, so nothing in the
                # suite calls it and a wrong keyword here goes unseen until a
                # real terminal does -- see test_ui.
                title=_Text("CarUtil", style=_MUTED),
                title_align="left",
                border_style=_ACCENT,
                style=f"on {_SURFACE}",
                padding=(1, 4),
                expand=False,
            )
        )
    else:
        print(banner(), end="")


def heading(text: str) -> None:
    """The question a prompt is asking, drawn the same way for every prompt.

    A menu has always introduced itself with this: a rule with the question on
    it, then the choices, then the answer. The typed prompts borrow the shape so
    the walkthrough reads as one program rather than two.
    """
    console = _console()
    if console is not None and sys.stdout.isatty():
        console.print()
        console.print(
            _Rule(_Text(text, style=f"bold {_ACCENT}"), align="left",
                  style=_ACCENT)
        )
    else:
        print()
        print(paint(f"  {text}", "bold"))


def hint(text: str) -> None:
    """One dim line of guidance under a heading.

    The range and the unit live here rather than in the field's label: a label
    like `Weight in kg:` puts words between the question and the answer, and a
    pre-filled default in brackets invites the answer the prompt is trying to
    avoid. A hint states the sensible answer without being one.
    """
    console = _console()
    if console is not None and sys.stdout.isatty():
        console.print(_Text("  " + text, style=f"dim {_MUTED}"))
    else:
        print("  " + paint(text, "dim"))


def step(index: int, total: int, text: str) -> None:
    """A numbered step in the guided walkthrough."""
    console = _console()
    if console is not None:
        console.print(
            _Text.assemble(
                (f"  [{index}/{total}]", f"bold {_ACCENT}"),
                (f" {text}", "bold"),
            )
        )
    else:
        print(paint(f"  [{index}/{total}]", "accent", "bold") + f" {text}")


def ok(text: str) -> None:
    _status("+", text, "ok")


def bullet(text: str) -> None:
    _status("*", text, "accent")


def warn(text: str) -> None:
    _status("!", text, "warn")


def fail(text: str) -> None:
    _status("x", text, "fail")


def _status(marker: str, text: str, color: str) -> None:
    """A status line: two spaces, a coloured marker, then the message.

    `color` is a logical name, resolved per surface -- `_RICH` for Rich, `_STYLES`
    for the plain-ANSI fallback. A name in one and not the other is how a marker
    quietly loses its colour, which is why both tables are checked in test_ui
    rather than trusted.
    """
    console = _console()
    if console is not None:
        console.print(
            _Text.assemble(("  ", ""),
                           (marker, _RICH.get(color, f"bold {color}")),
                           (" ", ""), (text, ""))
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
        _questionary.Style(_QUESTIONARY_STYLE)
        if colour_enabled()
        else _questionary.Style([])
    )
    question = _questionary.select(
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
    )
    _only_the_cursor_is_lit(question)
    answer = question.ask()
    if answer is None:
        raise SystemExit(1)
    return answer


def _only_the_cursor_is_lit(question) -> None:
    """Stop Questionary lighting the starting row *as well as* the cursor row.

    `select(default=...)` records the default in the control's
    `selected_options`, and a row in that list is drawn with `class:selected`
    for as long as the menu is open -- moving the pointer away does not clear
    it. So the default row and the row under the cursor are both drawn lit, in
    two different styles. The starting row is really `pointed_at`, a separate
    field the pointer moves, so emptying the list moves nothing and leaves
    `class:highlighted` as the only lit style.

    The control has to be found by walking the layout, because Questionary
    builds it inside `select()` and hands back only a `Question`. Nothing here
    is load-bearing: if a future Questionary stops exposing it, the menu still
    runs and `selected` is styled to match `highlighted` so the worst case is
    two rows lit in the same colour.
    """
    layout = getattr(getattr(question, "application", None), "layout", None)
    for container in getattr(layout, "walk", list)():
        control = getattr(container, "content", None)
        if isinstance(getattr(control, "selected_options", None), list):
            control.selected_options = []


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