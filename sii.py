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

# key: value  |  key[]: value  |  key[N]: value
KEY_RE = re.compile(r"^\s*([A-Za-z_][\w.]*)\s*(\[[^\]]*\])?\s*:\s*(.*?)\s*$")
UNIT_RE = re.compile(r"^\s*([A-Za-z_][\w.]*)\s*:\s*([A-Za-z_.][\w.]*)\s*\{?\s*$")
# "@include \"x.sui\"" and "@name \"\"" style preprocessor lines
INCLUDE_RE = re.compile(r'^\s*@include\s+"([^"]+)"\s*$')
ANY_DIRECTIVE_RE = re.compile(r"^\s*@[A-Za-z_]")


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


def set_key(lines: list[str], key: str, value: str) -> list[str]:
    """Replace the final `key:` line, or append one if absent.

    Comments and ordering of untouched lines are preserved.
    """
    out = list(lines)
    last = None
    for i, line in enumerate(out):
        m = KEY_RE.match(line)
        if m and m.group(1) == key and m.group(2) is None:
            last = i
    new_line = f"\t{key}: {value}"
    if last is None:
        # insert just inside the unit block
        out.insert(unit_body_end(out), new_line)
    else:
        out[last] = new_line
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
