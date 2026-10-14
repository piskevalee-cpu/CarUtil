"""Minimal .sii/.sui reader and writer.

SII files are a simple nested-block language:

    SiiNunit
    {
    unit_name : unit_id
    {
        key: value
        key[]: value
    }
    }

We deliberately do NOT fully parse this. The converter needs to read a unit's
top-level keys and emit modified copies while preserving every line it does not
touch, including comments and @include directives. A line-oriented approach
preserves formatting faithfully; a full parser would lose comments.
"""

from __future__ import annotations

import re
import struct

# key: value  |  key[]: value  |  key[N]: value
KEY_RE = re.compile(r"^\s*([A-Za-z_][\w.]*)\s*(\[[^\]]*\])?\s*:\s*(.*?)\s*$")
UNIT_RE = re.compile(r"^\s*([A-Za-z_][\w.]*)\s*:\s*([A-Za-z_.][\w.]*)\s*\{?\s*$")
# "@include \"x.sui\"" and "@name \"\"" style preprocessor lines
INCLUDE_RE = re.compile(r'^\s*@include\s+"([^"]+)"\s*$')
ANY_DIRECTIVE_RE = re.compile(r"^\s*@[A-Za-z_]")


# A `key:` or `key[]:` token, used to find where the next pair starts on a line
# that may have collapsed several of them together.
_KEY_TOKEN_RE = re.compile(r"[A-Za-z_][\w.]*(\s*\[[^\]]*\])?\s*:")


def _key_token_offsets(line: str) -> list[int]:
    """Offsets of every key token in `line`, skipping quoted text and comments.

    Scanned by hand rather than by regex because a value may legitimately
    contain a colon (`info[]: "V: 8"`) and a trailing comment may contain
    anything at all; only a token outside quotes and before the comment is a
    real key.
    """
    offsets: list[int] = []
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if ch == '"':
            i += 1
            while i < n:
                if line[i] == "\\":
                    i += 2
                    continue
                if line[i] == '"':
                    i += 1
                    break
                i += 1
            continue
        if (ch == "/" and line[i:i + 2] == "//") or ch == "#":
            break                       # a comment; nothing after it is a key
        if (ch.isalpha() or ch == "_") and (i == 0 or line[i - 1].isspace()):
            m = _KEY_TOKEN_RE.match(line, i)
            if m:
                offsets.append(i)
                i = m.end()
                continue
        i += 1
    return offsets


def expand_multi_key_lines(lines: list[str]) -> list[str]:
    """Put every `key: value` pair on a line of its own.

    Some mods collapse several keys onto one line:

        residual_travel[]: 0.08   nominal_voltage: 12   tank_size: 1200

    Every helper in this module is line-oriented, so a collapsed line reads as
    ONE key whose value happens to contain the other keys. set_key then
    replaces the whole line with `residual_travel[]: 0.093` and silently takes
    `nominal_voltage` and `tank_size` with it -- which is how a converted car
    ended up with a 0-gallon tank and no battery, reported in game.

    Normalising before editing fixes the entire class at once: get_key,
    get_all_indexed, count_indexed and set_key are all already correct once
    each pair owns a line. A line carrying a single pair, or one this scanner
    cannot parse confidently, is returned untouched -- so a def we never edit
    keeps the mod's own bytes.
    """
    out: list[str] = []
    for line in lines:
        offsets = _key_token_offsets(line)
        indent = line[: len(line) - len(line.lstrip())]
        # Only split when the first token IS the start of the line. Anything
        # else (an @include, a stray brace, text we did not understand) is
        # left exactly as the author wrote it.
        if len(offsets) < 2 or offsets[0] != len(indent):
            out.append(line)
            continue
        for n, start in enumerate(offsets):
            end = offsets[n + 1] if n + 1 < len(offsets) else len(line)
            piece = line[start:end].rstrip()
            if piece:
                out.append((indent or "\t") + piece)
    return out


def edit_lines(text: str) -> list[str]:
    """Split a definition's text into editable lines, one key pair per line.

    The single entry point every editing function should use, so a def that
    arrives with collapsed keys cannot reach set_key/set_indexed unexpanded.
    """
    return expand_multi_key_lines(text.splitlines())


def to_float(value: str) -> float:
    """A SII numeric value as a float, hex-float form included.

    SII allows a value written `&459380cd`: a big-endian IEEE-754 single in
    eight hex digits, which is how the game itself writes some scalars. Plain
    float() raises ValueError on those, and every caller here wraps float() in
    `except ValueError: return False` -- so a hex-float kerb_weight read as
    "not truck-scale" and a 9.4 t truck's chassis was preserved as if it were a
    car's. A value that cannot be read at all still raises, because pretending
    it is a number is how a misread becomes a written-out definition.
    """
    v = (value or "").strip()
    if v.startswith("&") and len(v) == 9:
        return struct.unpack(">f", bytes.fromhex(v[1:]))[0]
    return float(v)


def strip_quotes(value: str) -> str:
    """Return the unquoted payload of a SII scalar."""
    v = value.strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        return v[1:-1]
    return v


def get_key(lines: list[str], key: str, *, indexed: str | None = None) -> str | None:
    """Return the last value for `key`, or for `key[indexed]`.

    SII files may repeat a key; the last occurrence wins, matching how SCS's
    own loader behaves for scalars. `indexed` may be given bare ("0") or
    already bracketed ("[0]"); the bare empty form is written "[]".
    """
    want = None
    if indexed is not None:
        want = indexed if indexed.startswith("[") else f"[{indexed}]"
    found = None
    for line in lines:
        m = KEY_RE.match(line)
        if not m:
            continue
        if m.group(1) != key:
            continue
        idx = m.group(2)
        if indexed is not None:
            if idx != want:
                continue
        elif idx is not None:
            continue
        found = strip_quotes(m.group(3))
    return found


def get_all_indexed(lines: list[str], key: str) -> list[str]:
    """Return every value for `key[...]`, in file order."""
    out = []
    for line in lines:
        m = KEY_RE.match(line)
        if m and m.group(1) == key and m.group(2) is not None:
            out.append(strip_quotes(m.group(3)))
    return out


def set_key(lines: list[str], key: str, value: str,
            *, indexed: str | None = None) -> list[str]:
    """Replace the final `key:` line, or append one if absent.

    Comments and ordering of untouched lines are preserved.

    `indexed` supplies the bracket separately (`"[]"` or `"[0]"`). A caller
    cannot just write `set_key(lines, "fallback[]", ...)` to target a repeated
    key: KEY_RE's name group is `[A-Za-z_][\\w.]*`, which excludes `[` and `]`,
    so that call matched nothing and silently appended on every invocation --
    turning the mod's own fallback[] list into a second, conflicting one.
    """
    out = list(lines)
    want = None
    if indexed is not None:
        want = indexed if indexed.startswith("[") else f"[{indexed}]"
    last = None
    for i, line in enumerate(out):
        m = KEY_RE.match(line)
        if not m or m.group(1) != key:
            continue
        if want is None:
            if m.group(2) is not None:
                continue
        elif m.group(2) != want:
            continue
        last = i
    new_line = f"\t{key}{want or ''}: {value}"
    if last is None:
        # insert just inside the unit block
        out.insert(unit_body_end(out), new_line)
    else:
        out[last] = new_line
    return out


def append_key(lines: list[str], key: str, value: str,
               *, indexed: str | None = None) -> list[str]:
    """Add a `key: value` line, always adding.

    set_key replaces the LAST matching line, which is what a scalar wants and
    the opposite of what a repeated key needs: writing five fallback[] entries
    in a loop through set_key leaves one, because each call rewrites the entry
    the previous call made. Anything whose value is genuinely a list needs this.
    """
    out = list(lines)
    want = "" if indexed is None else (
        indexed if indexed.startswith("[") else f"[{indexed}]")
    out.insert(unit_body_end(out), f"\t{key}{want}: {value}")
    return out


def set_tag(lines: list[str], tag: str | list[str]) -> list[str]:
    """Ensure the `tags[]` list contains exactly the given tag(s).

    The vehicle type tag is what registers the vehicle with Road Trip's
    vehicle-typing system. `tag` may be a single string or a list; a list
    emits one `tags[]` line per entry, in order.

    Road Trip matches `car_job vehicle_types[]` against this list, so a vehicle
    carrying several tags can satisfy more than one job category.
    """
    tags = [tag] if isinstance(tag, str) else list(tag)
    kept = [
        line
        for line in lines
        if not (KEY_RE.match(line) and KEY_RE.match(line).group(1) == "tags")
    ]
    # The reference emits tags[] after the fallback[] block, not after info[].
    anchor = None
    for i, line in enumerate(kept):
        m = KEY_RE.match(line)
        if m and m.group(1) == "fallback":
            anchor = i + 1
    if anchor is None:
        for i, line in enumerate(kept):
            m = KEY_RE.match(line)
            if m and m.group(1) == "info":
                anchor = i + 1
    tag_lines = [f'\ttags[]: "{t}"' for t in tags]
    if anchor is None:
        at = unit_body_end(kept)
        for offset, tl in enumerate(tag_lines):
            kept.insert(at + offset, tl)
    else:
        for offset, tl in enumerate(tag_lines):
            kept.insert(anchor + offset, tl)
    return kept


def remove_keys(lines: list[str], keys: set[str]) -> list[str]:
    """Drop every top-level occurrence of the given keys."""
    out = []
    for line in lines:
        m = KEY_RE.match(line)
        if m and m.group(1) in keys:
            continue
        out.append(line)
    return out


def unit_body_end(lines: list[str]) -> int:
    """Index of the '}' that closes the unit block.

    A SII file is nested:

        SiiNunit      depth 0
        {             depth 1
        unit : id     <- the unit header
        {             depth 2
            <body>
        }             depth 1  <- this is what we want
        }

    Counted purely by depth. Matching the unit header with a regex is not
    safe: keys like `variant: chs` inside the body look exactly like a header
    line, and starting there skips the real closing brace. Getting this wrong
    appends new keys OUTSIDE the unit, where the loader ignores them or the
    file fails to parse ("Expecting end of file").
    """
    depth = 0
    seen_open = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.endswith("{"):
            depth += 1
            seen_open = True
        elif stripped == "}":
            if not seen_open:
                continue
            depth -= 1
            if depth == 1:      # unit block just closed
                return i
    return len(lines)


def _matching_close(lines: list[str], start: int) -> int:
    """Index of the '}' closing the block opened at/after `start`."""
    depth = 0
    for i in range(start, len(lines)):
        stripped = lines[i].strip()
        if stripped.endswith("{") and not stripped.startswith("//"):
            depth += 1
        elif stripped == "}":
            depth -= 1
            if depth == 0:
                return i
    return len(lines)


def render_unit(unit_name: str, unit_id: str, body: list[str]) -> str:
    """Render a single-unit SII file from a list of body lines."""
    out = ["SiiNunit", "{", f"{unit_name} : {unit_id}", "{"]
    out.extend("\t" + line if line.strip() else line for line in body)
    out.append("}")
    out.append("}")
    return "\n".join(out) + "\n"

def set_indexed(lines: list[str], key: str, values: list[str]) -> list[str]:
    """Rewrite `key[0..n-1]` with exactly `values`.

    The count MUST match the vehicle's axle count. In SCS's SII dialect
    `key[N]: v` means "element N equals v" -- there is no array-size line.
    Writing one element too many invents a phantom axle (a third axle weighing
    2 kg is the classic symptom), and feeding a number into a bool or name
    field can crash the game.
    """
    out = list(lines)
    # Both `key[]` and `key[N]` are element declarations; position them by order.
    idx_re = re.compile(r"^(\s*)" + re.escape(key) + r"\[(\d*)\](\s*:\s*)(.*?)(\s*(?:#.*)?)$")
    spots: list[int] = []
    for i, line in enumerate(out):
        if idx_re.match(line):
            spots.append(i)

    # Drop surplus elements (this is the phantom-axle bug's other half).
    for i in reversed(spots[len(values):]):
        del out[i]
    spots = spots[: len(values)]

    for n, val in enumerate(values):
        new_line = f"\t{key}[{n}]: {val}"
        if n < len(spots):
            out[spots[n]] = new_line
        else:
            out.insert(unit_body_end(out), new_line)
    return out


def count_indexed(lines: list[str], key: str) -> int:
    """How many elements `key` currently has."""
    pat = re.compile(r"^\s*" + re.escape(key) + r"\[\d*\]")
    return sum(1 for line in lines if pat.match(line))
