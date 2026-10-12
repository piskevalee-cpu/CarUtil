#!/usr/bin/env python3
"""Convert a truck-classified ATS mod into a Road Trip / Car Mode vehicle.

Takes a mod (unpacked folder or a plain-ZIP .scs) and emits a new mod whose
vehicle registers with Road Trip's Car Dealer: a `tags[]` vehicle type plus the
car_dealer / car_desktop / car_company binding files.

Reference implementation studied: Volkswagen Parati 1994 (works in Car Mode).
See FINDINGS.md for the full schema notes.

The converter synthesises bindings ONLY for parts that exist in the source mod.
It never invents .pmd model paths -- a binding pointing at a nonexistent model
renders as an invisible accessory, which is worse than an honest omission. Every
synthetic, reused and missing part is reported at the end.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import struct
import subprocess
import sys
import zlib
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from sii import (  # noqa: E402
    count_indexed,
    set_indexed,
    get_all_indexed,
    get_key,
    remove_keys,
    set_key,
    set_tag,
)
import brands as _brands
import logo as _logo
import ui as _ui

# The seven vehicle types, per the Truck Simulator Wiki's Road Trip page.
VEHICLE_TYPES = ["pickup", "suv", "sedan", "muscle", "classic", "sports", "offroad"]

# Extra spellings seen in the wild. These are NOT in VEHICLE_TYPES (which is
# the menu/preset list) but are accepted on the command line and normalised, so
# an unusual token can be tested without editing this file. Whether the base
# game's car_job vehicle_types[] accepts a given spelling can only be confirmed
# in-game, so these are offered rather than asserted to be correct.
TYPE_ALIASES = {
    "off-road": "offroad",
    "off_road": "offroad",
    "4x4": "offroad",
    "suvs": "suv",
    "cross-over": "suv",
    "crossover": "suv",
    "estate": "sedan",
    "wagon": "sedan",
    "saloon": "sedan",
    "hatchback": "sedan",
    "coupe": "sports",
    "roadster": "sports",
    "supercar": "sports",
    "sportscar": "sports",
}

# Truck-only chassis keys that have no meaning for a car.
TRUCK_ONLY_CHASSIS_KEYS = {
    "info",  # replaced with the vehicle-type display
    "icon",  # per-accessory icon may not exist
}


class Report:
    """Collects what the converter did, for the user-facing summary."""

    def __init__(self) -> None:
        self.actions: list[str] = []
        self.warnings: list[str] = []
        self.gaps: list[str] = []
        self.bound: list[str] = []

    def action(self, msg: str) -> None:
        self.actions.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def gap(self, msg: str) -> None:
        self.gaps.append(msg)


class ModSource:
    """Reads a mod from either a folder or a plain-ZIP .scs, uniformly.

    Some community mods ship password-protected ZIP entries (an anti-piracy
    measure). Those cannot be read without the password, so they are tracked
    and skipped rather than crashing the conversion partway through.
    """

    def __init__(self, path: Path, password: str | None = None,
                 plain_read: bool = True) -> None:
        self.path = path
        self.is_zip = path.is_file() and zipfile.is_zipfile(path)
        self.encrypted: set[str] = set()
        self.locked: set[str] = set()
        self.names: list[str] = []
        self.wrapper_note: str | None = None
        self.plain_read = plain_read
        self.local: dict = {}
        self.fake_encryption = False

        # Mods are often distributed as a .zip wrapping a single .scs. Unwrap
        # that transparently instead of reporting "no vehicle definition".
        if self.is_zip and not password:
            inner = self._unwrap_single_scs()
            if inner is not None:
                self.path = inner
                self.wrapper_note = (
                    f"{path.name} contained a mod archive; read "
                    f"{inner.name} inside it"
                )

        if self.is_zip:
            self.zf = zipfile.ZipFile(self.path)
            if password:
                self.zf.setpassword(password.encode("utf-8"))
            for info in self.zf.infolist():
                if not info.filename.endswith("/"):
                    self.names.append(info.filename)
                if info.flag_bits & 0x1:
                    self.encrypted.add(info.filename)
        elif self.path.is_dir():
            self.zf = None
            self.names = [
                str(p.relative_to(self.path)).replace("\\", "/")
                for p in self.path.rglob("*")
                if p.is_file()
            ]
        else:
            raise FileNotFoundError(f"no such mod: {self.path}")

        # Verify which entries are genuinely unreadable (flagged encrypted, or
        # still locked because no/wrong password was supplied).
        if self.zf is not None:
            for name in list(self.names):
                if name in self.encrypted:
                    try:
                        self.zf.read(name)
                    except RuntimeError:
                        self.locked.add(name)
            if self.locked and plain_read:
                self._try_local_headers()
        self.readable = [n for n in self.names if n not in self.locked]

    def _try_local_headers(self) -> None:
        """Recover entries whose central-directory encryption flag is a lie.

        Only used when zipfile refused to read an entry but the local header
        says it is not encrypted. A genuinely password-protected archive keeps
        every local header flagged too, so this never unlocks real encryption.
        """
        try:
            locals_ = scan_local_headers(self.path)
        except OSError:
            return

        recoverable = {}
        for name in list(self.locked):
            info = locals_.get(name)
            if not info:
                continue
            offset, csize, method, lflag = info
            # Only trust a local header that does NOT claim encryption.
            if lflag & 0x1:
                continue
            recoverable[name] = info

        if not recoverable:
            return

        for name in recoverable:
            self.locked.discard(name)
        self.local = recoverable
        self.fake_encryption = True

    def _read_local(self, name: str) -> bytes:
        """Read an entry using its LOCAL header, which is what actually matches
        the bytes on disk when the central directory disagrees."""
        offset, csize, method, _flag = self.local[name]
        with open(self.path, "rb") as f:
            f.seek(offset)
            data = f.read(csize) if csize else f.read()

        # The local header is authoritative, but these archives sometimes use
        # a junk method value (1) for data that is simply stored -- the sound
        # banks are raw RIFF/WAV. So: try to inflate, and if that fails treat
        # the bytes as stored.
        if method == 0:
            return data
        try:
            return zlib.decompress(data, -15)
        except zlib.error:
            try:
                d = zlib.decompressobj(-15)
                out = d.decompress(data, 1 << 30)
                if d.eof:
                    return out
            except zlib.error:
                pass
            return data

    def _unwrap_single_scs(self) -> Path | None:
        """If this ZIP holds exactly one .scs and no defs, unpack it and use that.

        Only done for a plain (non-encrypted) outer zip; a locked outer zip is
        left alone so the real error message is the encryption one.
        """
        try:
            with zipfile.ZipFile(self.path) as z:
                members = [i for i in z.infolist() if not i.filename.endswith("/")]
                if any(i.flag_bits & 0x1 for i in members):
                    return None
                names = [i.filename for i in members]
                if any(n.endswith((".sii", ".sui")) for n in names):
                    return None  # already a mod archive
                scs = [n for n in names if n.lower().endswith(".scs")]
                if len(scs) != 1:
                    return None
                import tempfile

                dest = Path(tempfile.mkdtemp(prefix="atsrt_"))
                with zipfile.ZipFile(self.path) as z, z.open(scs[0]) as src:
                    out = dest / Path(scs[0]).name
                    with open(out, "wb") as fh:
                        fh.write(src.read())
                return out
        except (zipfile.BadZipFile, OSError):
            return None

    def read(self, name: str) -> str:
        if name in getattr(self, "local", {}):
            return self._read_local(name).decode("utf-8", "replace")
        if self.zf is not None:
            return self.zf.read(name).decode("utf-8", "replace")
        return (self.path / name).read_text(encoding="utf-8", errors="replace")

    def read_bytes(self, name: str) -> bytes | None:
        """Raw bytes of any entry, or None if it cannot be read."""
        try:
            if name in getattr(self, "local", {}):
                return self._read_local(name)
            if self.zf is not None:
                return self.zf.read(name)
            p = self.path / name
            return p.read_bytes() if p.is_file() else None
        except (KeyError, RuntimeError, OSError):
            return None

    def read_opt(self, name: str) -> str | None:
        try:
            return self.read(name)
        except (KeyError, RuntimeError):
            return None

    def exists(self, name: str) -> bool:
        return name in self.names and name not in self.locked

    def readable_defs(self) -> list[str]:
        return [
            n for n in self.readable
            if n.endswith((".sii", ".sui"))
        ]

    def check_readable(self) -> None:
        """Raise a clear, actionable error if nothing can be read."""
        if self.zf is None:
            return
        if not self.locked:
            return
        total = len(self.names)
        readable = len(self.readable)
        if readable == 0:
            raise SystemExit(
                f"error: cannot read {self.path.name} "
                f"({total} of {total} entries flagged encrypted).\n"
                "\n"
                "  The converter needs the .sii definition files to work with.\n"
                "\n"
                "  Options:\n"
                "    - If you have the mod author's password:\n"
                "        --password 'thepassword'\n"
                "    - Re-run without --strict-encryption to allow reading\n"
                "      entries whose index claims encryption but whose data is\n"
                "      actually plain (some mods are packaged this way)\n"
                "    - Use a different copy of the mod\n"
            )
        print(
            f"warning: {len(self.locked)} of {total} entries are password-protected "
            f"and will be skipped.",
            file=sys.stderr,
        )



def scan_local_headers(path: Path) -> dict[str, tuple[int, int, int, int]]:
    """Walk a ZIP's local file headers, which are authoritative for the payload.

    Some mod archives carry an encryption flag in the CENTRAL DIRECTORY while
    their local headers describe plain stored/deflated data. Standard readers
    (7-Zip, Python's zipfile) consult the central directory first, see
    "encrypted", and refuse to open anything. The bytes on disk are not
    encrypted at all.

    Returns {filename: (data_offset, compressed_size, method, local_flag)}.
    """
    out: dict[str, tuple[int, int, int, int]] = {}
    with open(path, "rb") as f:
        while True:
            head = f.read(30)
            if len(head) < 30:
                break
            sig, ver, flag, method, t, d, crc, csize, usize, nlen, elen = struct.unpack(
                "<IHHHHHIIIHH", head
            )
            if sig != 0x04034B50:
                break
            name = f.read(nlen).decode("utf-8", "replace")
            f.read(elen)
            offset = f.tell()
            if csize:
                f.seek(csize, 1)
            elif usize == 0:
                pass  # directory or empty entry: nothing to skip
            else:
                # size unknown (streaming/data descriptor): skip by inflating
                _skip_stream(f)
            if not name.endswith("/"):
                out[name] = (offset, csize, method, flag)
    return out


def _skip_stream(f) -> None:
    """Advance past one raw-deflate stream whose length is not declared."""
    start = f.tell()
    d = zlib.decompressobj(-15)
    pos = start
    while True:
        chunk = f.read(8192)
        if not chunk:
            break
        try:
            d.decompress(chunk, 1 << 24)
        except zlib.error:
            break
        if d.eof:
            consumed = len(d.unused_data)
            f.seek(pos + len(chunk) - consumed)
            return
        pos += len(chunk)
    f.seek(start)


def slug_from(name: str) -> str:
    """A conservative filesystem/identifier slug from a folder or file name."""
    base = Path(name).name
    if base.lower().endswith(".scs"):
        base = base[:-4]
    base = re.sub(r"[^\w.]+", "_", base).strip("_")
    return base.lower() or "vehicle"


def find_vehicle_dir(src: ModSource) -> tuple[str | None, str | None]:
    """Return (def_dir_with_slash, vehicle_slug) for def/vehicle/truck/<slug>/."""
    for name in src.readable:
        m = re.match(r"^(def/vehicle/truck/)([^/]+)/", name)
        if m:
            return m.group(1), m.group(2)
    return None, None


def asset_roots(src: ModSource) -> list[str]:
    """Discover model asset roots referenced by the mod's def files.

    Only real asset roots count. Def paths (/vehicle/truck/...) and UI or
    material paths are not model roots and must be left alone -- rewriting
    /vehicle/truck would break every data_path in the mod.
    """
    roots: set[str] = set()
    for name in src.readable:
        if not name.endswith((".sii", ".sui")):
            continue
        text = src.read_opt(name) or ""
        for m in re.finditer(r'"(/[A-Za-z0-9_./-]+?\.(?:pmd|pmc|tobj|pma|ods))"', text):
            p = m.group(1)
            parts = p.split("/")
            if len(parts) < 3:
                continue
            # Take everything up to the filename's own stem: for
            # /porshe_cayenne/car.pmd that is /porshe_cayenne, not
            # /porshe_cayenne/car.pmd. Keep nested roots (a/b/car.pmd).
            root = "/" + "/".join(parts[1:-1])
            # Def, UI, material and sound roots are not asset roots.
            if root.split("/")[1] in {
                "vehicle", "ui", "material", "def", "sound", "unit", "automat",
            }:
                continue
            roots.add(root)
    return sorted(roots)


def rewrite_asset_paths(text: str, mapping: dict[str, str]) -> tuple[str, int]:
    """Rewrite model asset roots per `mapping`. Returns (text, n_replacements)."""
    count = 0
    for old, new in sorted(mapping.items(), key=lambda kv: -len(kv[0])):
        pattern = re.compile(re.escape(old) + r"(?=[\"'/])")
        text, n = pattern.subn(new, text)
        count += n
    return text, count


def build_data_sii(slug: str, brand: str, vtype: str, parts: dict,
                   tags: list[str] | None = None) -> tuple[str, Report]:
    """Build def/vehicle/truck/<slug>/data.sii.

    This is the root unit: it carries the vehicle type tag, camera bindings and
    the fallback/require lists. The Parati's version references camera units we
    may not be able to synthesise, so those are only emitted when present.
    """
    rep = Report()
    body: list[str] = []

    body.append(f'\tinfo[]: "{brand}"')
    body.append('\tinfo[]: "@@prt_name_cr@@"')
    body.append('\tinfo[]: "@@prt_description_cr@@"')
    body.append("")
    for t in (tags or [vtype]):
        body.append(f'\ttags[]: "{t}"')

    # cameras -- only if the source mod defines them
    cameras = parts.get("cameras", {})
    for key, unit in cameras.items():
        body.append(f"\t{key}: {unit}")

    body.append("")
    for sub in parts.get("fallbacks", []):
        body.append(f'\tfallback[]: "{sub}"')

    for req in parts.get("requires", []):
        body.append(f"\trequire[]: {req}")

    return _render("accessory_truck_data", f"vehicle.{slug}", body), rep


def build_binding(parts: dict, unit_id: str = ".car", header: str = "") -> str:
    """Emit a vehicle binding file.

    Format is taken verbatim from the reference converter's output: the unit
    header is `vehicle : .car` (space before the colon), every accessory block
    puts its brace on its own line, and accessory names are the short slot
    names (.transm, .headl, .ftire...) rather than the directory names.

    The three car_* files are byte-identical; truck_desktop and truck_company
    differ only in the unit id (.desktop.truck / .company.truck).
    """
    entries: list[tuple[str, str, str, int | None]] = []

    # Reference order: core parts, then all wheels, then paint, then addons.
    CORE = {"data", "chassis", "cabin", "interior", "engine",
            "transm", "headl", "transmission", "head_light"}
    ranked: list[tuple[int, str, str, str, int | None]] = []
    for name in parts.get("order", []):
        kind, path = parts["accessories"][name]
        if name in CORE:
            rank = 0
        elif kind == "paint":
            rank = 2
        else:
            rank = 3
        ranked.append((rank, name, kind, path, None))
    for name in parts.get("wheel_order", []):
        kind, path, offset = parts["wheels"][name]
        ranked.append((1, name, kind, path, offset))
    ranked.sort(key=lambda r: r[0])   # stable: preserves discovery order per rank

    accs: list[str] = []
    for rank, name, kind, path, offset in ranked:
        accs.append(f" .{name}")
        entries.append((name, kind, path, offset))

    head = ["SiiNunit", "{"]
    if header:
        for line in header.strip().splitlines():
            head.append("# " + line if line.strip() else "#")
    head.append(f"vehicle : {unit_id}")
    head.append("{")
    head.extend(f"\taccessories[]:{a}" for a in accs)
    head.append("}")
    head.append("")

    # _binding_section already ends with a newline, so joining with "\n"
    # yields exactly one blank line between blocks.
    sections = [
        _binding_section(kind, f".{n}", path, off)
        for n, kind, path, off in entries
    ]
    # head already closed the `vehicle` unit and each section closes itself;
    # one final brace closes SiiNunit. A second is a parse error.
    return "\n".join(head + sections + ["}"]) + "\n"


def _binding_section(kind: str, name: str, path: str, offset: int | None = None) -> str:
    """One accessory block, matching the reference output byte for byte."""
    if kind == "wheel":
        return (
            f"vehicle_wheel_accessory : {name}\n"
            "{\n"
            f"\toffset: {offset}\n"
            "\tpaint_color: (1.000000, 1.000000, 1.000000)\n"
            f'\tdata_path: "{path}"\n'
            "}\n"
        )
    if kind == "paint":
        return (
            f"vehicle_paint_job_accessory : {name}\n"
            "{\n"
            "\tmask_r_color: (1.0, 0.0, 0.0)\n"
            "\tmask_g_color: (0.0, 1.0, 0.0)\n"
            "\tmask_b_color: (0.0, 0.0, 1.0)\n"
            f'\tdata_path: "{path}"\n'
            "}\n"
        )
    prefix = "vehicle_accessory" if kind == "vehicle" else f"vehicle_{kind}_accessory"
    return (
        f"{prefix} : {name}\n"
        "{\n"
        f'\tdata_path: "{path}"\n'
        "}\n"
    )


def _render(unit_name: str, unit_id: str, body: list[str]) -> str:
    out = ["SiiNunit", "{", f"{unit_name} : {unit_id}", "{"]
    out.extend(body)
    out.extend(["}", "}"])
    return "\n".join(out) + "\n"



def discover_parts(src: ModSource, vdir: str, slug: str, asset_root: str) -> tuple[dict, Report]:
    """Walk the source mod and work out which parts exist and how to bind them.

    Binding order follows the reference mod: the root data unit, then the core
    parts, then wheels, then paint, then addons.
    """
    rep = Report()
    parts: dict = {
        "accessories": {},
        "order": [],
        "wheels": {},
        "wheel_order": [],
        "fallbacks": [],
        "requires": [],
        "cameras": {},
    }

    # Slot names are the short ones the reference converter emits
    # (.transm, .headl, .paint) rather than the raw directory names.
    SLOT = {
        "transmission": "transm", "head_light": "headl", "paint_job": "paint",
    }

    def add_accessory(name: str, kind: str, path: str, slot: str | None = None) -> None:
        key = slot or name
        parts["accessories"][key] = (kind, path)
        parts["order"].append(key)

    # 1. the root data unit is itself bound first, matching the reference
    if src.exists(f"{vdir}data.sii"):
        add_accessory("data", "vehicle", f"/{vdir}data.sii")

    # 2. core parts, in reference order
    core = ["chassis", "cabin", "interior", "engine", "transmission", "head_light"]
    for name in core:
        candidates = [
            n for n in src.readable
            if n.startswith(f"{vdir}{name}/") and n.endswith(".sii")
        ]
        if candidates:
            first = sorted(candidates)[0]
            add_accessory(name, "vehicle", f"/{first}", SLOT.get(name))
            parts["fallbacks"].append(f"{name}|{Path(first).name}")
        else:
            rep.gap(f"no {name} part found (cars normally need one)")

    # 3. Wheels, bound EXACTLY as the mod ships them and never renamed.
    #
    # An earlier version rewrote f_tire/f_disc into car p_tire/p_disc and
    # deleted the originals. The game answered with "Redundant p-rim accessory
    # removed!" and "Incorrect f-/r-wheel definition", then crashed. A mod
    # that already ships car wheels (p_tire/p_disc) keeps those; a truck mod's
    # wheels are bound as they are. Either way nothing is moved.
    wheel_re = re.compile(r"^def/vehicle/(?:([fr])_(tire|disc|hub|nuts)|(p)_(tire|disc))/(.+)$")
    found: dict[str, str] = {}
    for n in sorted(src.readable):
        m = wheel_re.match(n)
        if not m or not n.endswith(".sii"):
            continue
        if m.group(3):                      # p_tire / p_disc
            key = m.group(3) + m.group(4)
        else:                                # f_/r_ tire|disc|hub|nuts
            key = f"{m.group(1)}{m.group(2)}"
        found.setdefault(key, n)

    # Reference order: front pair, then rear pair.
    for key in ("ftire", "fdisc", "fhub", "fnuts",
                "rtire", "rdisc", "rhub", "rnuts",
                "ptire", "pdisc"):
        path = found.get(key)
        if not path:
            continue
        parts["wheels"][key] = ("wheel", f"/{path}", 0)
        parts["wheel_order"].append(key)
        cat = path.split("/")[2]
        parts["fallbacks"].append(f"{cat}|{Path(path).name}")
    if not found:
        rep.gap("no wheel defs found; the car will have no selectable rims")

    # 4. paint job
    pj = sorted(n for n in src.readable if "/paint_job/" in n and n.endswith(".sii"))
    if pj:
        # Prefer a stock colour. color0 is usually "Custom Color"
        # (stock: false), which is the wrong default for a delivered car.
        stock = [
            n for n in pj
            if re.search(r"\bstock:\s*true\b", src.read(n))
        ]
        add_accessory("paint_job", "paint", f"/{(stock or pj)[0]}", "paint")
    else:
        rep.gap("no paint_job part; colours will not be selectable")

    # 5. addons. Every accessory/<dir> the mod ships is bound under its own
    # name, which is what the reference converter does -- including steering_w,
    # which it binds as ".steering_w" rather than renaming to ".swheel".
    addon_root = f"{vdir}accessory/"
    for n in sorted(src.readable):
        m = re.match(re.escape(addon_root) + r"([^/]+)/shape(\d+)\.sii$", n)
        if m:
            key = m.group(1)
            if key not in parts["accessories"]:
                add_accessory(key, "addon", f"/{n}")

    steering = sorted(
        n for n in src.readable
        if n.startswith(f"{addon_root}steering_w/") and n.endswith(".sii")
    )
    if steering:
        add_accessory("steering_w", "addon", f"/{steering[0]}")
    return parts, rep


# ---------------------------------------------------------------------------
# Car physics
# ---------------------------------------------------------------------------
# Grounded in the Volkswagen Parati's chassis (FINDINGS.md §3), which is the
# only Road-Trip-compatible vehicle we can read. The Parati is a small FWD
# wagon, so its values are the "sedan" baseline; every other preset scales
# from it using real-world kerb masses and drag.
#
# Token-valued keys (axle_suspension_type, suspension_class) are NOT invented:
# every preset reuses the Parati's exact tokens, because a wrong token is a
# load-time error whereas a numeric value is only a tuning difference.

# Parati baseline, read from def/vehicle/truck/volkswagen.parati1994/chassis/4x2a.sii
_PARATI = {
    "kerb_weight": [620, 415],          # kg, front/rear
    "air_resistance": "0.1",
    "cog_height": "0.4",
    "sway_bar_stiffness": "0.38",
    "max_steering_angle": "28",
    "residual_travel": ["0.18", "0.16"],
    "suspension_damping_factor": ["0.9", "0.7"],
    # A car-only key: all 4 of SCS's Road Trip chassis declare it, none of the
    # 39 truck chassis do, so a truck leaves it at the global truck default and
    # brakes far harder than any SCS car. FINDINGS section 16 declined to write
    # one on the grounds that tyre diameter "comes from the .pmd model" -- but
    # the evidence says SCS states it explicitly for every car and omits it
    # from every truck, and inheriting the truck figure is what produces the
    # trap-pedal braking this is meant to fix. Values measured across the Road
    # Trip fleet span 0.279-0.356 m; this pair is the F-150's, mid-range.
    "brake_rotor_diameter": ["0.302", "0.285"],
    "disc_brake": ["true", "true"],
    "axle_suspension_type": ["wishbone", "wishbone"],
    "suspension_class": "standard",
    "nominal_voltage": "12.0",
    "air_tank_pressure": "0.0",
    "tank_size": "47",
}

# Real-world kerb masses, front/rear split, for scaling. Values are kg and come
# from published manufacturer figures for the class, not invented.
_MASS = {
    "sedan":    [700, 520],      # ~1.2t compact saloon
    "suv":      [1150, 1050],    # ~2.2t, Cayenne-class
    "muscle":   [900, 800],      # ~1.7t Mustang-class
    "sports":   [720, 560],      # ~1.3t, front-mid engined
    "pickup":   [1350, 1050],    # ~2.4t F-150-class
    "offroad":  [1200, 1000],    # ~2.2t Bronco-class
    "classic":  [850, 700],      # ~1.55t Crown Victoria-class
}

# Weight is entered as a TOTAL, then split across axles using each type's
# natural weight distribution (the front/rear ratio above). A sports car is
# rear-biased, a pickup front-biased; preserving the ratio matters far more
# than the absolute split.
_MIN_KG = 400
_MAX_KG = 6000


def normalise_type(raw: str) -> str:
    """Normalise a user-supplied vehicle-type token.

    Known spellings pass through unchanged. Unlisted but recognisable
    spellings are mapped onto one of VEHICLE_TYPES. Anything else is passed
    through lowercased and stripped, so an unrecognised token can still be
    written and tested in-game rather than being rejected outright.
    """
    t = (raw or "").strip().lower()
    if not t:
        raise SystemExit("error: --type must not be empty")
    if t in VEHICLE_TYPES:
        return t
    return TYPE_ALIASES.get(t, t)


def split_mass(total_kg: int, vtype: str, axles: int) -> list[int]:
    """Split a total kerb weight across `axles` using the type's natural ratio.

    The total is distributed exactly: no weight is created or lost when the
    vehicle has more than two axles.
    """
    ref = _MASS.get(vtype, _MASS["sedan"])
    ref_total = ref[0] + ref[1]
    front = round(total_kg * ref[0] / ref_total)
    rear = total_kg - front

    if axles <= 2:
        weights = [front, rear][:axles]
    else:
        # Extra axles share the rear group so the sum stays exact.
        extra = axles - 2
        rear_each = rear // (extra + 1)
        weights = [front] + [rear_each] * extra
        weights.append(rear - rear_each * extra)
    return weights


def default_total(vtype: str) -> int:
    ref = _MASS.get(vtype, _MASS["sedan"])
    return ref[0] + ref[1]


TYPE_HELP = {
    "sedan":   "saloon / hatchback",
    "suv":     "crossover / SUV",
    "muscle":  "muscle car",
    "classic": "classic car",
    "sports":  "sports car",
    "pickup":  "pickup truck",
    "offroad": "off-road / 4x4",
}

RT_PREFIX = "rt"

# Folder the converted car is filed under in def/vehicle/car_dealer/. The Car
# Dealer looks the brand logo up by this name, so the logo files and the
# registration path must agree on it.
DEALER_FOLDER = "mod"


def _tty() -> bool:
    return sys.stdin.isatty()


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(1)


def _confirm(prompt: str) -> bool:
    """Ask a yes/no question; only an explicit y/yes continues."""
    while True:
        raw = _ask(f"  {prompt} [y/N] ").lower()
        if raw in ("y", "yes"):
            return True
        if raw in ("", "n", "no"):
            return False
        print("  -> please answer y or n")


# The one external program the converter needs. Everything else is stdlib.
# ImageMagick turns a shipped brand logo into the 256x64 DDS the game reads;
# the conversion itself works without it and falls back to the mod's own
# texture or a flat placeholder.
IMAGEMAGICK_WHY = "convert the shipped brand logos into the .dds the game reads"

# Package managers in the order we prefer them. Each entry is
# (manager name, argv). `winget` comes first because on Windows `convert`
# already resolves to the filesystem tool in System32, and `magick` is the
# name ImageMagick 7 installs; see logo.imagemagick.
_INSTALLERS: list[tuple[str, list[str]]] = [
    ("winget", ["winget", "install", "--id", "ImageMagick.ImageMagick",
                "--silent", "--accept-package-agreements",
                "--accept-source-agreements"]),
    ("brew", ["brew", "install", "imagemagick"]),
    ("apt-get", ["sudo", "apt-get", "install", "-y", "imagemagick"]),
    ("dnf", ["sudo", "dnf", "install", "-y", "ImageMagick"]),
    ("yum", ["sudo", "yum", "install", "-y", "ImageMagick"]),
    ("pacman", ["sudo", "pacman", "-S", "--noconfirm", "imagemagick"]),
    ("apk", ["sudo", "apk", "add", "imagemagick"]),
]

# Where a Windows install lands when PATH has not been refreshed yet.
_WINDOWS_MAGICK_GLOBS = (
    r"C:\Program Files\ImageMagick*",
    r"C:\Program Files (x86)\ImageMagick*",
)


def missing_dependencies() -> list[str]:
    """Names of external programs the converter needs but cannot find."""
    missing = []
    if _logo.imagemagick() is None:
        missing.append("ImageMagick")
    return missing


def _suggest_install() -> tuple[str, list[str]] | None:
    """The first install command whose package manager exists on this box."""
    for manager, argv in _INSTALLERS:
        if shutil.which(manager):
            return manager, argv
    return None


def _refresh_magick_path() -> None:
    """Make a just-installed ImageMagick visible to shutil.which.

    Windows installers add ImageMagick to the machine PATH, but the running
    process keeps its old copy, so `magick` stays "not found" for the rest of
    this run. Prepending the install directory sidesteps needing a re-login.
    """
    if os.name != "nt" or _logo.imagemagick() is not None:
        return
    import glob
    for pattern in _WINDOWS_MAGICK_GLOBS:
        for folder in glob.glob(pattern):
            exe = os.path.join(folder, "magick.exe")
            if os.path.isfile(exe):
                os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")
                return


def check_dependencies() -> None:
    """Check for the external program before the walkthrough starts.

    Interactive runs get offered an install. Scripted runs and CI never
    block on the prompt -- they just print the command and carry on, since
    the conversion itself does not require ImageMagick.
    """
    missing = missing_dependencies()
    if not missing:
        return

    print()
    _ui.heading("Before we start")
    for name in missing:
        print(f"  {_ui.paint('!', 'yellow')} {name} is not installed. "
              f"It is needed to {IMAGEMAGICK_WHY}.")

    suggestion = _suggest_install()
    if suggestion is None:
        print()
        print("  " + _ui.paint("No supported package manager was found on this "
                              "machine, so there is nothing to offer.", "dim"))
        print("  " + _ui.paint("Continuing without it -- brand logos will fall "
                              "back to the mod's own or a flat placeholder.",
                              "dim"))
        print()
        return

    _manager, argv = suggestion
    command = " ".join(argv)
    print()
    print("  " + _ui.paint(command, "cyan"))

    if not _tty():
        # Piped or scripted: never block. The printed command is the answer.
        print()
        print("  " + _ui.paint("Not attached to a terminal, so not running it "
                              "for you.", "dim"))
        print()
        return

    print()
    if not _confirm("Install it now?"):
        print()
        print("  " + _ui.paint("Skipped. Continuing without it -- brand logos "
                              "will fall back to the mod's own or a flat "
                              "placeholder.", "dim"))
        print()
        return

    print()
    print("  " + _ui.paint("Running the installer...", "dim"))
    try:
        rc = subprocess.run(argv).returncode
    except OSError as exc:
        print("  " + _ui.paint(f"x could not run the installer: {exc}", "red"))
        return

    if rc != 0:
        print("  " + _ui.paint(f"x the installer exited with status {rc}",
                              "red"))
        print("  " + _ui.paint(f"You can run it yourself: {command}", "dim"))
        return

    _refresh_magick_path()
    still = missing_dependencies()
    if still:
        # Installed, but this shell's PATH does not see it yet.
        print("  " + _ui.paint("x installed, but not visible to this session.",
                              "red"))
        print("  " + _ui.paint("Open a new terminal and re-run, or put "
                              "ImageMagick on your PATH.", "dim"))
    else:
        print("  " + _ui.paint(f"+ {_logo.imagemagick()} is ready.", "green"))
    print()


def prompt_source() -> Path:
    """Ask where the mod is."""
    print()
    print("  Where is your mod?")
    print("    Give a .scs file or an unpacked mod folder.")
    print()
    while True:
        raw = _ask("  Path: ").strip().strip('"').strip("'").strip()
        if raw.lower() in ("q", "quit", "exit"):
            raise SystemExit("cancelled")
        if not raw:
            print("  -> a path is required")
            continue
        p = Path(raw).expanduser()
        if p.is_dir():
            return p
        if p.is_file():
            return p
        print(f"  -> no such file or folder: {p}")


def prompt_type() -> str:
    """Ask what kind of car it is."""
    print()
    _ui.heading("What kind of car is it?")
    print("  " + _ui.paint("arrow keys to move, Enter to choose", "dim"))
    print()
    options = [(t.title(), TYPE_HELP[t]) for t in VEHICLE_TYPES]
    idx = _ui.arrow_menu(options, default=0)
    return VEHICLE_TYPES[idx]


def prompt_type_numbered() -> str:
    """Numbered fallback kept for non-interactive use."""
    while True:
        raw = _ask("  Type [1]: ").strip().lower()
        if raw in ("q", "quit", "exit"):
            raise SystemExit("cancelled")
        if not raw:
            return VEHICLE_TYPES[0]
        if raw.isdigit() and 1 <= int(raw) <= len(VEHICLE_TYPES):
            return VEHICLE_TYPES[int(raw) - 1]
        if raw in VEHICLE_TYPES:
            return raw
        print("  -> pick a number 1-7, or type one of the names")


def prompt_brand(slug: str) -> str:
    """Ask which brand to file it under, from the shipped logo set."""
    # Skip leading year/version noise: "2021_porsche_cayenne" -> "porsche".
    guess = ""
    for tok in re.split(r"[_\s]+", slug or ""):
        if tok and not tok.isdigit() and not re.fullmatch(r"v?\d+[a-z]?", tok):
            guess = re.sub(r"[^a-z0-9]", "", tok.lower())
            break
    guess = _brands.normalise(guess) or "other"
    if _brands.logo_path(guess) is None:
        guess = "other"

    print()
    _ui.heading("Which brand is it? The Car Dealer shows this name and logo.")
    print("  " + _ui.paint("arrow keys to move, Enter to choose", "dim"))
    print()

    have = _brands.available()
    options = [
        (label, "" if name in have else "no logo")
        for name, label, _stem in _brands.BRANDS
    ]
    start = next((i for i, (n, _l, _s) in enumerate(_brands.BRANDS) if n == guess), 0)
    return _brands.BRANDS[_ui.arrow_menu(options, default=start)][0]


def prompt_weight(vtype: str, preset: str | None) -> list[int] | None:
    """Ask how much the car should weigh.

    Returns [front, rear] in kg, or None to accept the type default.
    Skipped entirely when stdin is not a terminal, so piped and scripted
    runs never block.
    """
    if not _tty():
        return None

    suggestion = default_total(vtype)
    print()
    print("  How much do you want this car to weigh?")
    print(f"    Enter the total in kg (a typical {vtype} is about {suggestion} kg),")
    print("    or 'f,r' to set front and rear axles separately,")
    print("    or press Enter to accept the default.")
    print()
    while True:
        try:
            raw = input(f"  Weight [{suggestion} kg]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  cancelled -- using the type default.")
            return None

        if not raw:
            return None
        raw = raw.lower().replace(" ", "").replace("_", "")

        if raw in ("q", "quit", "exit"):
            raise SystemExit("cancelled")

        # split form: front,rear
        if "," in raw:
            parts = raw.split(",")
            if len(parts) != 2:
                print("  -> give exactly two numbers, e.g. 1150,1050")
                continue
            try:
                front, rear = int(parts[0]), int(parts[1])
            except ValueError:
                print("  -> those must be whole numbers in kg")
                continue
            if front < 1 or rear < 1:
                print("  -> each axle needs a positive weight")
                continue
            return [front, rear]

        # total form
        try:
            total = int(float(raw.rstrip("kgt.")))
        except ValueError:
            print("  -> enter a number in kg, e.g. 1650")
            continue
        if not (_MIN_KG <= total <= _MAX_KG):
            print(f"  -> a car should be {_MIN_KG}-{_MAX_KG} kg, got {total}")
            continue
        return split_mass(total, vtype, 2)


# Measured calibration, not a guess. Two timing points on the W223 (9.67 s at
# 520 Nm / 1550 kg, 3.70 s at 1600 Nm / 1550 kg) plus a confirmation on the
# same car at 2069 kg give an exponent of 0.855 on a 1/mass x torque^0.855
# relationship. Close to the 1.0 of pure torque limiting, so torque is very
# nearly the whole lever.
#
# Anchored on 1550 kg at 520 Nm taking 9.67 s. Mass is accounted for
# explicitly, which is what lets a target time be honoured across weights.
_CAL_MASS_KG = 1550.0
_CAL_TORQUE_NM = 520.0
_CAL_TIME_S = 9.67
_CAL_EXPONENT = 0.855

# Outside this the model is extrapolation. The fitting points span 520-1600 Nm
# at 1550 kg; a 2000 kg SUV sits near the top of that.
_CAL_MIN_NM = 200.0
_CAL_MAX_NM = 3000.0


def torque_for_time(mass_kg: float, seconds: float) -> float:
    """Peak engine torque that yields `seconds` to 60 mph at `mass_kg`.

    Returns Nm, clamped to the range the calibration actually covers so a
    silly request cannot silently become a guess.
    """
    if mass_kg <= 0 or seconds <= 0:
        return _CAL_TORQUE_NM
    mass_factor = mass_kg / _CAL_MASS_KG
    seconds_at_reference = _CAL_TIME_S * mass_factor
    nm = _CAL_TORQUE_NM * (seconds_at_reference / seconds) ** (1 / _CAL_EXPONENT)
    return max(_CAL_MIN_NM, min(_CAL_MAX_NM, nm))


def prompt_acceleration(mass_kg: float) -> float | None:
    """Ask how quick the car should be, and return the target 0-60 in seconds.

    A weight alone cannot produce a believable drivetrain, because SCS's own
    cars take roughly 2.5x their real-world 0-60 regardless of what the engine
    is. Asking for the time the user actually wants turns the engine target
    from a single fitted constant into a function of the two numbers that
    determine it.
    """
    if not _tty():
        return None

    default = _CAL_TIME_S * (mass_kg / _CAL_MASS_KG)
    print()
    print("  How quick should this car be?")
    print("    Enter the 0-60 mph time in seconds. Most cars are 4-7 s;")
    print("    a truck-based conversion lands near 12 s otherwise.")
    print(f"    Press Enter for {default:.1f} s.")
    print()
    while True:
        try:
            raw = input(f"  0-60 seconds [{default:.1f}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  cancelled -- using the measured default.")
            return None
        if not raw:
            return None
        raw = raw.lower().rstrip("s ").replace(",", ".")
        try:
            value = float(raw)
        except ValueError:
            print("  -> enter a number of seconds, e.g. 5.0")
            continue
        if not (1.0 <= value <= 30.0):
            print("  -> between 1 and 30 seconds is a car, not a spaceship")
            continue
        return value

# Per-type overrides applied on top of the baseline. Only numeric tuning.
_TUNING = {
    "sedan":   {},
    "suv":     {"air_resistance": "0.16", "cog_height": "0.55", "sway_bar_stiffness": "0.50",
                "max_steering_angle": "28", "tank_size": "70",
                "residual_travel": ["0.22", "0.20"],
                "suspension_damping_factor": ["0.95", "0.80"],},
    "muscle":  {"air_resistance": "0.13", "cog_height": "0.42", "sway_bar_stiffness": "0.55",
                "max_steering_angle": "32", "tank_size": "60",
                "residual_travel": ["0.16", "0.15"],
                "suspension_damping_factor": ["1.00", "0.85"],},
    "sports":  {"air_resistance": "0.07", "cog_height": "0.32", "sway_bar_stiffness": "0.80",
                "max_steering_angle": "35", "tank_size": "55",
                "residual_travel": ["0.12", "0.12"],
                "suspension_damping_factor": ["1.20", "1.00"],},
    "pickup":  {"air_resistance": "0.22", "cog_height": "0.75", "sway_bar_stiffness": "0.45",
                "max_steering_angle": "26", "tank_size": "80",
                "residual_travel": ["0.20", "0.22"],
                "suspension_damping_factor": ["0.90", "0.80"],},
    "offroad": {"air_resistance": "0.25", "cog_height": "0.80", "sway_bar_stiffness": "0.40",
                "max_steering_angle": "32", "tank_size": "75",
                "residual_travel": ["0.28", "0.28"],
                "suspension_damping_factor": ["0.80", "0.75"],},
    "classic": {"air_resistance": "0.15", "cog_height": "0.45", "sway_bar_stiffness": "0.35",
                "max_steering_angle": "30", "tank_size": "60",
                "residual_travel": ["0.17", "0.15"],
                "suspension_damping_factor": ["0.85", "0.70"],},
}

# Scalar keys that must be REMOVED from a converted car. They are truck-only
# and describe a trailer the car has no way to attach.
TRUCK_ONLY_PHYSICS = [
    "fifth_wheel_pitch", "fifth_wheel_roll",
    "trailer_joint_pitch", "trailer_joint_roll",
    "spreader_joint_pitch", "spreader_joint_roll",
    "cabin_suspension_travel", "cabin_suspension_damping_factor",
    "cabin_sway_bar_factor", "cabin_cog_height_offset", "cabin_mass",
    "cabin_max_pitch_angle_front", "cabin_max_pitch_angle_rear", "cabin_max_roll_angle",
    "damping_scaling_trailer_factor",
]


def car_physics(vtype: str, axles: int, mass: list[int] | None = None) -> dict:
    """Build the car physics key set for a vehicle type, scaled to `axles`."""
    cfg = dict(_PARATI)
    cfg.update(_TUNING.get(vtype, {}))
    kg = mass or _MASS.get(vtype, _MASS["sedan"])
    cfg["kerb_weight"] = [str(v) for v in kg]

    # Extend or trim per-axle arrays to match the vehicle's real axle count.
    # Getting this wrong is the phantom-axle bug described in the HOTFIX notes.
    def fit(key: str, default: str) -> None:
        vals = cfg.get(key)
        if not isinstance(vals, list):
            return
        vals = [str(v) for v in vals]
        while len(vals) < axles:
            vals.append(vals[-1] if vals else default)
        cfg[key] = vals[:axles]

    for key, default in (
        ("residual_travel", "0.16"), ("suspension_damping_factor", "0.8"),
        ("disc_brake", "true"), ("axle_suspension_type", "wishbone"),
        ("brake_rotor_diameter", "0.30"),
    ):
        fit(key, default)

    # Mass array scales with axle count too; extra axles get the rear figure.
    kg = [int(x) for x in cfg["kerb_weight"]]
    while len(kg) < axles:
        kg.append(kg[-1] if kg else 0)
    cfg["kerb_weight"] = [str(v) for v in kg[:axles]]
    return cfg


def _is_truck_scale(key: str, current: str | list[str] | None) -> bool:
    """True if an existing value is so truck-like it must be replaced.

    Preserving a mod's tuned physics matters: a mod that already drives well
    should come out of the converter unchanged. But some values are so far
    outside car range that leaving them guarantees it still drives like a truck.
    """
    try:
        if key == "air_resistance":
            return current is not None and float(current) >= 1.0
        if key == "cog_height":
            return current is not None and float(current) > 0.85
        if key == "max_steering_angle":
            return current is not None and float(current) > 45
        if key == "sway_bar_stiffness":
            return current is not None and float(current) > 2.0
        if key == "tank_size":
            return current is not None and float(current) > 200
        if key == "kerb_weight" and isinstance(current, list):
            total = sum(float(x) for x in current)
            return total > 4000
        if key == "nominal_voltage":
            # Trucks run a 24V system; every production car is 12V.
            return current is not None and float(current) >= 24
        if key == "residual_travel" and isinstance(current, list):
            # 0.08 is a stiff load-carrying truck value; road cars sit near 0.18.
            return any(float(x) < 0.12 for x in current)
    except (TypeError, ValueError):
        return False
    return False


def tune_chassis(
    text: str,
    vtype: str,
    mass: list[int] | None,
    rep: Report,
    force: bool = False,
) -> str:
    """Rewrite a chassis unit so the vehicle drives like a car, not a truck.

    This is the half the reference tool's README never mentions: chassis-level
    physics. Without these keys a converted vehicle falls back to engine
    defaults and still drives like a truck even with physics.sii removed.

    By default existing car-sane values are PRESERVED -- a mod that already
    drives well should survive conversion untouched. A value is only replaced
    when it is missing or provably truck-scale (see _is_truck_scale).
    `force` overwrites everything.
    """
    lines = text.splitlines()
    axles = count_indexed(lines, "kerb_weight") or count_indexed(lines, "powered_axle") or 2

    cfg = car_physics(vtype, axles, mass)

    # Strip truck-only trailer/cab parameters if the mod carries them inline.
    dropped = [k for k in TRUCK_ONLY_PHYSICS if get_key(lines, k) is not None]
    if dropped:
        lines = remove_keys(lines, set(TRUCK_ONLY_PHYSICS))
        rep.action(f"removed truck-only trailer/cab keys: {', '.join(sorted(dropped))}")

    replaced: list[str] = []
    kept: list[str] = []

    def wants(key: str, current: str | list[str] | None) -> bool:
        if force or current is None:
            return True
        return _is_truck_scale(key, current)

    # NOTE: powered_axle is never written. The reference converter treats the
    # mod's own declaration as authoritative -- copying a rear-drive reference
    # would silently change the drivetrain.
    for key in ("residual_travel", "suspension_damping_factor",
                "disc_brake", "axle_suspension_type", "kerb_weight",
                "brake_rotor_diameter"):
        if key not in cfg:
            continue
        current = get_all_indexed(lines, key) or None
        if current is not None and not current:
            current = None
        if wants(key, current):
            lines = set_indexed(lines, key, cfg[key])
            replaced.append(key)
        else:
            kept.append(key)

    for key in ("air_resistance", "cog_height", "sway_bar_stiffness",
                "max_steering_angle", "suspension_class", "nominal_voltage",
                "air_tank_pressure"):
        if key not in cfg:
            continue
        current = get_key(lines, key)
        if wants(key, current):
            before = current
            lines = set_key(lines, key, cfg[key])
            if before is not None and before != cfg[key]:
                replaced.append(f"{key} ({before} -> {cfg[key]})")
        else:
            kept.append(key)

    # detail_model is deliberately NOT synthesised. An earlier version copied it
    # from `model`; the reference converter does not write it, and adding keys
    # a mod did not declare is exactly the kind of guessing that produced the
    # "Expecting end of file" and phantom-axle failures.

    if replaced:
        rep.action(f"car physics applied: {', '.join(replaced)}")
    if kept:
        rep.action(f"preserved existing tuned values: {', '.join(sorted(kept))}")

    if not force:
        report_action = (
            f"chassis physics for '{vtype}' ({axles} axles); "
            "already-car values were preserved -- use --force-physics to overwrite"
        )
    else:
        report_action = f"chassis physics FORCED for '{vtype}' ({axles} axles)"
    rep.action(report_action)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
# A truck engine is built to move a 40-tonne combination. Reuse one in a 2.2 t
# car and it is absurd overpowered -- the Honda Pilot conversion reached 60 mph
# in about three seconds. Nothing else in the converter can fix that: mass,
# drag and suspension are already car-scaled, and the gearbox ratios the mods
# ship are the same 10R80 pattern SCS uses in its own Road Trip cars. The one
# number left is the engine's output.
#
# The mods already state their true figure in info[], e.g.
#     info[]: "350 @@lb_ft@@ (475 @@nm@@)"
# which for SCS's own Road Trip engines equals the `torque` value exactly
# (the F-150 V8 declares torque: 556 against "410 lb_ft (556 Nm)"). Truck mods
# inflate the same field instead -- 1400 and 4000 -- so the stated figure is a
# trustworthy source and the declared one is not.

# Torque is declared in Nm and the curve is a multiplier on it, so peak output
# is torque * curve_max. Compare the two within this margin before rewriting, so
# a mod that already got it right is left untouched.
_ENGINE_TOLERANCE = 1.15

# Default peak torque per tonne of kerb weight, newton-metres. Measured, not
# assumed: the Mercedes converted this way was timed in-game at 4.9 s against a
# 5.0 s prediction, and the figure holds across masses because it is per tonne.
#
# This is deliberately far above the mod's stated output, and above SCS's own
# cars, which all sit at 250-300 Nm/t. Real-world torque is not the target in
# this simulation -- SCS's own F-150 declares its true 556 Nm and still takes
# about 2.5x its real-world 0-60, because the whole model is tuned for trucks.
# Matching a real engine spec therefore yields a gutless car. See FINDINGS.md
# sections 20-21.
_DEFAULT_NM_PER_TONNE = 760.0

_NM_RE = re.compile(r"\(\s*([\d.]+)\s*@@nm@@\s*\)")
_LBFT_RE = re.compile(r"([\d.]+)\s*@@lb_ft@@")
# Anchored deliberately. "secondary_torque_curve[]" contains the substring
# "torque_curve[]", so an unanchored pattern silently merges both curves and
# reports the secondary peak as the primary one.
_PRIMARY_CURVE_RE = re.compile(
    r"^\s*torque_curve\[\]\s*:\s*\(\s*[\d.]+\s*,\s*([\d.]+)", re.M)


def stated_engine_torque(text: str) -> int | None:
    """The engine's own peak torque in Nm, as declared in its info[] strings.

    Mods spell this inconsistently -- "(475 @@nm@@)" and "(556@@nm@@)" both
    occur -- so the pattern tolerates missing spaces. lb_ft is only a fallback:
    it rounds, and a rounded input would make the rewritten torque wrong by a
    few Nm.
    """
    for line in text.splitlines():
        if "info[]" not in line:
            continue
        m = _NM_RE.search(line)
        if m:
            try:
                return int(round(float(m.group(1))))
            except ValueError:
                continue
        m = _LBFT_RE.search(line)
        if m:
            try:
                return int(round(float(m.group(1)) * 1.35582))
            except ValueError:
                continue
    return None


def _primary_curve_peak(text: str) -> float | None:
    peaks = [float(v) for v in _PRIMARY_CURVE_RE.findall(text)]
    return max(peaks) if peaks else None


def _rescale_in_place(
    lines: list[str], key: str, factor: float,
) -> tuple[list[str], int | None]:
    """Multiply a scalar's number by `factor`, preserving the rest of its line.

    Not set_key: these lines carry author comments such as
    `secondary_torque: 2250 # torque x 1.26 (final drive relation)`, and both
    get_key (which returns the comment with the value) and set_key (which
    rewrites the whole line) mishandle that -- the first makes the value
    unparseable, the second silently discards the comment explaining the key.
    """
    out = list(lines)
    last = None
    for i, line in enumerate(out):
        if re.match(rf"^\s*{re.escape(key)}\s*:", line):
            last = i
    if last is None:
        return out, None
    m = re.match(
        rf"^(\s*{re.escape(key)}\s*:\s*)([+-]?\d+(?:\.\d+)?)(.*)$", out[last])
    if not m:
        return out, None
    scaled = max(1, int(round(float(m.group(2)) * factor)))
    out[last] = f"{m.group(1)}{scaled}{m.group(3)}"
    return out, scaled


def tune_engine(text: str, rep: Report, force: bool = False,
                target_nm: int | None = None,
                nm_per_tonne: float | None = _DEFAULT_NM_PER_TONNE,
                tonnes: float = 1.0,
                target_source: str | None = None) -> str:
    """Rescale an engine's output to the figure its own info[] declares.

    `torque` is multiplied down and `secondary_torque` by the same factor, so
    whatever relationship the author intended between the two survives. The
    curve is left alone: scaling the scalar rescales the whole curve with it,
    and rewriting the points would alter the rev range and shifters too.

    The target is per tonne of kerb weight, never an absolute figure. An
    absolute Nm is a trap: `--engine-torque 1125` was fitted on a car whose
    `classic` preset weighed 1550 kg when the real vehicle was 2069 kg, so the
    number was silently 34% out before any of it was tested. Torque per tonne
    is what actually determines acceleration, and it stays correct when the
    mass preset is wrong, the vehicle is heavier than its type suggests, or the
    same engine is reused on a different car.
    """
    if "accessory_engine_data" not in text:
        return text

    if tonnes <= 0:
        raise ValueError("tonnes must be positive")

    # An explicit --engine-torque replaces the ratio; it is a deliberate
    # override, not a second opinion.
    if target_nm is not None:
        nm_per_tonne = None

    stated = stated_engine_torque(text)
    if stated is None and target_nm is None and nm_per_tonne is None:
        rep.warn(
            "engine declares no torque in info[]; leaving its output alone "
            "(pass --force-physics to rescale blind, or --engine-torque to "
            "give it a target)")
        return text

    lines = text.splitlines()
    declared = get_key(lines, "torque")
    if declared is None:
        return text
    m = re.match(r"^([+-]?\d+(?:\.\d+)?)", declared)
    if not m:
        return text
    declared_nm = float(m.group(1))

    peak = _primary_curve_peak(text)
    if not peak or peak <= 0:
        return text

    actual_nm = declared_nm * peak

    if nm_per_tonne is not None:
        target = nm_per_tonne * tonnes
    elif target_nm is not None:
        target = float(target_nm)
    else:
        target = float(stated)
        if not force and actual_nm <= target * _ENGINE_TOLERANCE:
            rep.action(
                f"engine torque {declared_nm:.0f} Nm (peak {actual_nm:.0f} Nm) "
                f"is within {_ENGINE_TOLERANCE:.2f}x of its stated {stated} Nm; "
                "preserved")
            return text

    factor = target / actual_nm
    lines, new_torque = _rescale_in_place(lines, "torque", factor)
    lines, new_secondary = _rescale_in_place(lines, "secondary_torque", factor)

    if nm_per_tonne is not None:
        source = target_source or f"{nm_per_tonne:g} Nm/t default ({tonnes:.2f} t)"
    elif target_nm is not None:
        source = f"--engine-torque {target:.0f}"
    else:
        source = f"stated {stated} Nm"
    rep.action(
        f"engine rescaled {declared_nm:.0f} -> {new_torque} Nm"
        + (f", secondary_torque -> {new_secondary}"
           if new_secondary is not None else "")
        + f" ({source}; was {actual_nm:.0f} Nm peak -- {factor:.2f}x)"
    )
    return "\n".join(lines) + "\n"


# Every one of SCS's own Road Trip car wheel units (18 of them in def.scs,
# base_vehicle.scs and dlc_rt_ford.scs) declares exactly two grip keys and no
# others: roll_resistance of 11 or 13, and grip_factor of 1.1 or 1.2. The truck
# wheel units declare neither, so trucks inherit the global default.
#
# Truck mods ship the truck side of that split. The Mercedes declares no grip
# key whatsoever, and the Pilot uses `wet_grip`, which no SCS car wheel has
# ever used. Either way the car runs on the truck's tyre calibration, which is
# what leaves it crawling after the drivetrain is corrected. Write the two keys
# SCS actually reads for a car.
_CAR_TYRE = {
    "roll_resistance": "11",   # SCS car range is 11-13
    "grip_factor": "1.1",      # SCS car range is 1.1-1.2
}
# Below the low end of SCS's own car range. A value above it is a mod that has
# already been set for a car and must be left alone.
_TYRE_FLOOR = {"roll_resistance": 11.0, "grip_factor": 1.1}

# Transmission keys, from the four SCS Road Trip transmissions in
# dlc_rt_ford.scs. Every one of them declares all three; every truck-class
# transmission omits them.
#
# stall_torque_ratio is the launch multiplier on engine torque. SCS's cars sit
# at 1.8-2.0. The Honda Pilot mod declares 4.50, a diesel torque-converter
# figure, which multiplied its already-corrected 1672 Nm by 2.84x against the
# Mercedes and made the car genuinely unrecoverable rather than merely quick.
# A stall ratio cannot be left alone: it is the single most violent number in
# the drivetrain and no torque target can compensate for it.
_CAR_TRANSMISSION = {
    "torque_converter_crawl": "true",
    "transmission_class": "standard",
}
_CAR_STALL_CEILING = 2.0
_CAR_LOCK_GEAR = "3"


def tune_transmission(text: str, rep: Report, force: bool = False) -> str:
    """Bring a transmission's converter settings into car range.

    Preserved when already in range, so a mod that already launches sanely
    survives untouched. `force` overwrites.
    """
    if "accessory_transmission_data" not in text:
        return text

    lines = text.splitlines()
    changed: list[str] = []
    kept: list[str] = []

    stall = get_key(lines, "stall_torque_ratio")
    if stall is None:
        lines = set_key(lines, "stall_torque_ratio", "1.9")
        changed.append("stall_torque_ratio (absent -> 1.9)")
    else:
        m = re.match(r"^([+-]?\d+(?:\.\d+)?)", stall)
        try:
            value = float(m.group(1)) if m else 0.0
        except (AttributeError, ValueError):
            value = 0.0
        if force or value > _CAR_STALL_CEILING:
            lines = set_key(lines, "stall_torque_ratio", str(_CAR_STALL_CEILING))
            changed.append(f"stall_torque_ratio ({value:g} -> {_CAR_STALL_CEILING})")
        else:
            kept.append(f"stall_torque_ratio {value:g}")

    for key, value in _CAR_TRANSMISSION.items():
        if get_key(lines, key) is None or force:
            before = get_key(lines, key)
            lines = set_key(lines, key, value)
            changed.append(f"{key} ({before} -> {value})" if before else key)
        else:
            kept.append(key)

    if get_key(lines, "torque_converter_lock_gear") is None and not force:
        lines = set_key(lines, "torque_converter_lock_gear", _CAR_LOCK_GEAR)
        changed.append(f"torque_converter_lock_gear (absent -> {_CAR_LOCK_GEAR})")

    if changed:
        rep.action(f"car transmission applied: {', '.join(changed)}")
    if kept:
        rep.action(f"preserved converter tuning: {', '.join(kept)}")
    return "\n".join(lines) + "\n"


def tune_wheel(text: str, rep: Report, force: bool = False) -> str:
    """Give a car wheel unit the grip keys SCS's own car wheels declare.

    A value already at or above SCS's car floor is preserved; the point is not
    to stomp a tuned wheel, it is to stop the car inheriting truck tyre
    defaults. `force` overwrites even those.
    """
    if "accessory_wheel_data" not in text:
        return text

    lines = text.splitlines()
    replaced: list[str] = []
    kept: list[str] = []

    for key, value in _CAR_TYRE.items():
        current = get_key(lines, key)
        if not force and current is not None:
            m = re.match(r"^([+-]?\d+(?:\.\d+)?)", current)
            try:
                if m and float(m.group(1)) >= _TYRE_FLOOR[key]:
                    kept.append(key)
                    continue
            except ValueError:
                pass
        lines = set_key(lines, key, value)
        replaced.append(f"{key} ({current} -> {value})" if current else key)

    if replaced:
        rep.action(f"car tyre grip applied: {', '.join(replaced)}")
    if kept:
        rep.action(f"preserved tuned tyre grip: {', '.join(kept)}")
    return "\n".join(lines) + "\n"


def convert(
    src: ModSource,
    out_path: Path,
    *,
    vtype: str,
    brand: str,
    slug: str,
    explicit_slug: bool = False,
    asset_root: str | None,
    keep_physics: bool,
    dry_run: bool,
    car_tune: bool = True,
    mass: list[int] | None = None,
    force_physics: bool = False,
    tags: list[str] | None = None,
    logo_dds: bytes | None = None,
    engine_torque: int | None = None,
    engine_nm_per_tonne: float | None = None,
    target_source: str | None = None,
) -> Report:
    rep = Report()
    if src.wrapper_note:
        rep.action(src.wrapper_note)
    if getattr(src, "fake_encryption", False):
        rep.action(
            f"{len(src.local)} entries were flagged encrypted in the archive "
            "index but their data is plain; read directly"
        )
    if src.locked:
        rep.warn(
            f"{len(src.locked)} of {len(src.names)} entries are password-protected "
            "and were skipped"
        )
    vdir, src_slug = find_vehicle_dir(src)

    if vdir is None:
        locked_defs = sorted(n for n in src.locked if n.startswith("def/vehicle/"))
        if locked_defs and not src.readable_defs():
            raise SystemExit(
                "error: none of this mod's definition files could be read.\n"
                f"  {len(locked_defs)} def/vehicle/ entries are password-protected.\n"
                "  Re-run with --password if you have it."
            )
        raise SystemExit(
            "error: no def/vehicle/truck/<slug>/ found.\n"
            "This converter targets truck-classified mods. Road Trip cars keep\n"
            "the def/vehicle/truck/ path, so that directory must exist."
        )
    src_slug = src_slug or slug
    slug = slug or src_slug
    # vdir is the shared parent; each mod has its own subdirectory
    vehicle_dir = f"{vdir}{src_slug}/"

    # Prefer the mod's own vehicle unit name for the registration file names,
    # the way the reference converter does (vehicle.w223_s500 -> w223_s500).
    if not explicit_slug:
        data_file = f"{vehicle_dir}data.sii"
        if src.exists(data_file):
            m = re.search(
                r"^\s*accessory_truck_data\s*:\s*([\w.]+)\s*$",
                src.read(data_file), re.M)
            if m:
                slug = m.group(1).split(".")[-1]

    # Asset path rewriting is OPT-IN.
    #
    # SCS resolves absolute virtual paths (e.g. /vehicle/truck/pilot_2024/car.pmd)
    # against every mounted mod, so the mod author's own paths already work.
    # Rewriting them by default broke real mods: the Pilot's assets live under
    # /model/flare, and blindly forcing /vehicle/car/<slug> pointed the chassis
    # at files that do not exist -> purple square, then a crash on load.
    roots = asset_roots(src)
    mapping: dict[str, str] = {}
    if asset_root:
        new_root = asset_root if asset_root.startswith("/") else "/" + asset_root
        mapping = {r: new_root for r in roots if r != new_root}
        rep.action(
            "asset roots -> " + ", ".join(f"{k} => {v}" for k, v in mapping.items())
        )
    else:
        new_root = ""
        rep.action("asset paths left as the mod author wrote them")

    # discover parts against a rewritten view of the source
    parts, part_rep = discover_parts(src, vehicle_dir, src_slug, new_root)
    rep.gaps.extend(part_rep.gaps)

    files: dict[str, str] = {}
    assets: dict[str, bytes] = {}

    # Carry over every non-definition entry verbatim: models, collision shapes,
    # textures, materials, animations, sounds, icons. Dropping these leaves a
    # mod with definitions that point at nothing, which renders as a purple
    # square and crashes the game when the vehicle is bought.
    stale_binding_re = re.compile(
        r"^def/vehicle/(truck_dealer|car_dealer|car_desktop|car_company)/"
    )
    for name in src.readable:
        if name.endswith((".sii", ".sui")):
            continue
        if stale_binding_re.match(name):
            continue
        data = src.read_bytes(name)
        if data is not None:
            assets[name] = data

    # The registration chain is regenerated below; copying an old one too would
    # leave two competing bindings for the same vehicle. truck_dealer is NOT
    # in this list: the reference converter leaves the mod's own truck dealer
    # entry exactly as the mod shipped it.
    stale_binding = re.compile(
        r"^def/vehicle/(car_dealer|car_desktop|car_company|"
        r"truck_desktop|truck_company)/"
    )

    # Engine torque is targeted per tonne of the mass we actually write, not
    # per tonne of whatever the --type preset assumed. Recompute it from the
    # same input tune_chassis uses, so the two cannot drift apart.
    kerb_kg = sum(mass) if mass else sum(_MASS.get(vtype, _MASS["sedan"]))
    tonnes = kerb_kg / 1000.0

    # 1. existing def files, with asset paths + tags rewritten
    dropped_physics = False
    for name in src.readable:
        if not name.endswith((".sii", ".sui")):
            continue
        if stale_binding.match(name):
            continue
        text = src.read(name)
        new_text, _ = rewrite_asset_paths(text, mapping)

        if name.endswith(f"{vehicle_dir}data.sii"):
            new_text = new_text
        elif car_tune and re.match(
            re.escape(vehicle_dir) + r"chassis/.*\.sii$", name
        ):
            new_text = tune_chassis(new_text, vtype, mass, rep, force=force_physics)
        elif car_tune and re.match(
            re.escape(vehicle_dir) + r"transmission/.*\.sii$", name
        ):
            new_text = tune_transmission(new_text, rep, force=force_physics)
        elif car_tune and re.match(
            re.escape(vehicle_dir) + r"engine/.*\.sii$", name
        ):
            new_text = tune_engine(
                new_text, rep, force=force_physics,
                target_nm=engine_torque,
                # None here means "not supplied on the command line", so it
                # must fall through to tune_engine's own default. Passing the
                # None straight through silently disabled the 760 Nm/t default
                # and fell back to the mod's stated torque.
                nm_per_tonne=(_DEFAULT_NM_PER_TONNE
                              if engine_nm_per_tonne is None
                              else engine_nm_per_tonne),
                target_source=target_source,
                tonnes=tonnes)
        elif car_tune and re.match(
            r"^def/vehicle/[a-z_]*tire/.*\.sii$", name
        ):
            new_text = tune_wheel(new_text, rep, force=force_physics)
        elif name.endswith("def/vehicle/physics.sii"):
            # DROPPED by default. This block is non-namespaced, so it replaces
            # the base game's calibrated physics for EVERY vehicle in the game,
            # not just this one. The mods that ship one carry a stale copy:
            # against the base game they set brake_balance 1.5 (vs 1.1),
            # braking_curve_peak_rel 1.15 (vs 1.5), and roughly double every
            # bristle_stiffness/bristle_damping/slip factor (1.0 -> ~2.1), plus
            # sway_bar_stiffness_factor 3.0 (vs 1.0). Those are what make a
            # converted car brake like a trap and feel feather-light.
            if keep_physics:
                rep.warn(
                    "kept the mod's def/vehicle/physics.sii; it is a stale "
                    "non-namespaced global that overrides the base game's "
                    "brake and tyre physics for EVERY vehicle in the game"
                )
            else:
                dropped_physics = True
                continue

        files[name] = new_text

    # 2. data.sii with the vehicle type tag (create if absent)
    all_tags = tags or [vtype]
    tag_desc = ", ".join(f'"{t}"' for t in all_tags)
    data_path = f"{vehicle_dir}data.sii"
    if data_path not in files:
        data_sii, _ = build_data_sii(src_slug, brand, vtype, parts, all_tags)
        files[data_path] = data_sii
        rep.action(f"created {data_path} with tags[]: {tag_desc}")
    else:
        rep.action(f"injected tags[]: {tag_desc} into {data_path}")

    # 2c. fallback[] -- this is what pre-selects each part, so the car spawns
    # configured instead of showing the shop's first choice. Same syntax the
    # Parati uses: "<def/vehicle subdir>|<file relative to it>".
    if data_path in files:
        lines = files[data_path].split("\n")
        for fb in parts.get("fallbacks", []):
            lines = set_key(lines, "fallback[]", f'"{fb}"')
        # tags[] goes last, after the fallback[] block, as the reference does
        lines = set_tag(lines, all_tags)
        files[data_path] = "\n".join(lines) + "\n"
        if parts.get("fallbacks"):
            rep.action(
                f"pre-selected {len(parts['fallbacks'])} default parts via fallback[]"
            )

    # 3. the registration chain. The reference converter writes five files:
    # three car_* that are byte-identical (unit ".car") plus truck_desktop and
    # truck_company, which differ only in their unit id. It does NOT write a
    # truck_dealer of its own -- the mod's own truck_dealer entry is left
    # exactly as shipped.
    header = (
        "GENERATED by convert_to_car.py.\n"
        "Derived from this mod's own dealer unit.\n"
        "Shape copied from the official Road Trip reference."
    )
    car_binding = build_binding(parts, ".car", header)
    bindings = {
        f"def/vehicle/car_dealer/{DEALER_FOLDER}/{slug}.sii": car_binding,
        f"def/vehicle/car_desktop/{slug}.sii": car_binding,
        f"def/vehicle/car_company/{slug}.sii": car_binding,
        f"def/vehicle/truck_desktop/{slug}.sii": build_binding(
            parts, ".desktop.truck", header),
        f"def/vehicle/truck_company/{slug}.sii": build_binding(
            parts, ".company.truck", header),
    }
    for path, text in bindings.items():
        files[path] = text
    # the mod's own truck_dealer binding is superseded by the chain above
    # truck_dealer is deliberately NOT in this list: the reference converter
    # leaves the mod's own truck dealer entry exactly as shipped.
    stale_binding = re.compile(
        r"^def/vehicle/(car_dealer|car_desktop|car_company|"
        r"truck_desktop|truck_company)/"
    )
    removed = [n for n in files if stale_binding.match(n) and n not in bindings]
    for n in removed:
        del files[n]
    if removed:
        rep.action(f"replaced {len(removed)} pre-existing registration file(s)")
    rep.action(
        "wrote the registration chain: car_dealer, car_desktop, car_company, "
        "truck_desktop, truck_company"
    )

    # manifest -- cars keep the truck category; "Car" is a mod-site convention
    # the game does not read. Whitespace after the colon varies between authors.
    for name in list(files):
        if name == "manifest.sii":
            text = files[name]
            new_text, n = re.subn(
                r'(category\[\]\s*:\s*)"Car"', r'\1"truck"', text
            )
            if n:
                rep.action('manifest category[]: "Car" -> "truck" (cars keep truck category)')
            files[name] = new_text

    # report bindings actually emitted
    rep.bound = list(parts["accessories"]) + list(parts["wheels"])

    if dry_run:
        rep.warn("dry run -- nothing written")
    else:
        # 4. brand logo. Without it the Car Dealer logs "No logo found for
        # brand 'x'!" and cannot load a UI material.
        #
        # Two different lookups are in play:
        #   truck_brand_logo/<brand>   -- what the reference converter adds
        #   car_brand_logo/<dealer>   -- what Car Mode reads; the game asked
        #                                 for "/material/ui/car_brand_logo/mod.mat"
        # The <brand> stem is the folder the mod uses for its own truck dealer
        # ("merc", not "mercedes"), matching the reference.
        logo_brand = brand.lower()
        for n in sorted(src.readable):
            m = re.match(r"^def/vehicle/truck_dealer/([^/]+)/", n)
            if m:
                logo_brand = m.group(1)
                break

        tobj_tpl = next(
            (src.read_bytes(n) for n in sorted(src.readable)
             if re.match(r"^material/ui/(car_|truck_)?brand_logo/[^/]+\.tobj$", n)
             and src.read_bytes(n)),
            None,
        ) or next(
            (src.read_bytes(n) for n in sorted(src.readable)
             if n.endswith(".tobj") and src.read_bytes(n)),
            None,
        )

        # Prefer a logo we were given (--brand-logo / --brand-logo-dir), then
        # the mod's own texture, then a flat placeholder.
        src_dds = None
        if logo_dds is not None:
            src_dds = logo_dds
            logo_source = "selected logo"
        else:
            logo_source = "the mod's own logo"
            src_dds = next(
                (src.read_bytes(n) for n in sorted(src.readable)
                 if re.match(r"^material/ui/(car_|truck_)?brand_logo/[^/]+\.dds$", n)
                 and src.read_bytes(n)),
                None,
            )
            if src_dds is None:
                logo_source = "a flat placeholder"

        logo = build_brand_logo(logo_brand, tobj_tpl, src_dds,
                                dirs=(_TRUCK_LOGO_DIR,))
        logo.update(build_brand_logo(DEALER_FOLDER, tobj_tpl, src_dds,
                                     dirs=(_CAR_LOGO_DIR,)))
        if logo:
            assets.update(logo)
            rep.action(
                f"brand logo ({logo_source}): {_TRUCK_LOGO_DIR}/{logo_brand}.* "
                f"and {_CAR_LOGO_DIR}/{DEALER_FOLDER}.*"
            )

        _write_scs(files, out_path, assets)

    if dropped_physics:
        rep.action(
            "dropped the mod's def/vehicle/physics.sii; the base game's own "
            "brake and tyre calibration now applies (--keep-physics to retain it)"
        )
    return rep


# ---------------------------------------------------------------------------
# Brand logo
# ---------------------------------------------------------------------------
# The Car Dealer logs "No logo found for brand 'x'!" and cannot open a UI
# material without these three files. Car Mode reads them from
# material/ui/car_brand_logo/<dealer folder>.{dds,mat,tobj} -- that directory is
# the one the game actually asks for, and the name it asks for is the *dealer
# folder* ("mod"), not the brand. Writing only brand_logo/ and
# truck_brand_logo/ is why the game logged "Can not open
# '/material/ui/car_brand_logo/mod.mat'".

# Measured from the reference converter's working logo
# (material/ui/brand_logo/merc.dds): 256x64, DXT1, no mipmaps.
_LOGO_W = 256
_LOGO_H = 64
# The header value the game's own logo files advertise. The chain stored is the
# full one (9 levels for 256x64), which is what the reference file does too.
_LOGO_MIPS = 6

# The reference converter adds truck_brand_logo/<brand>.{dds,mat,tobj} and
# leaves the mod's own brand_logo/<brand>.* untouched. Car Mode reads a third
# directory and resolves it by the *dealer folder*, which is why the game asked
# for "/material/ui/car_brand_logo/mod.mat" and found nothing.
_TRUCK_LOGO_DIR = "material/ui/truck_brand_logo"
_CAR_LOGO_DIR = "material/ui/car_brand_logo"


def _dxt1_block(r: int, g: int, b: int) -> bytes:
    """Encode one 4x4 RGB block as DXT1.

    DXT1 stores two endpoint colours plus a 2-bit per-pixel index. A flat block
    is encoded with c0 == c1, which the spec allows and every DXT decoder
    handles: colour0 and colour1 are the same RGB565 value and the indices are
    all zero.
    """
    c = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
    return struct.pack("<HHI", c, c, 0)


def _make_dds(width: int = _LOGO_W, height: int = _LOGO_H,
              rgb: tuple[int, int, int] = (96, 96, 96),
              mips: int = _LOGO_MIPS) -> bytes:
    """A minimal, valid DXT1 DDS of the given size, filled with one colour.

    DXT1 compresses in 4x4 blocks: 8 bytes per block. The full mip chain is
    always stored; `mips` only sets the value the header advertises, which
    matches what the game's own logo files declare (6 for a 256x64 image,
    even though the chain itself runs to 1x1).
    """
    header = bytearray(128)
    header[0:4] = b"DDS "
    struct.pack_into("<I", header, 4, 124)               # dwSize
    # CAPS|HEIGHT|WIDTH|PIXELFORMAT|MIPMAPCOUNT|LINEARSIZE
    struct.pack_into("<I", header, 8, 0x1 | 0x2 | 0x4 | 0x1000 | 0x20000 | 0x80000)
    struct.pack_into("<I", header, 12, height)           # dwHeight
    struct.pack_into("<I", header, 16, width)            # dwWidth
    chain = _mip_chain(width, height)
    struct.pack_into("<I", header, 28, mips)             # dwMipMapCount
    # The game reads the pixel format from offset 76, not the textbook 32.
    struct.pack_into("<I", header, 76, 32)               # ddspf.dwSize
    struct.pack_into("<I", header, 80, 0x4)              # DDPF_FOURCC
    header[84:88] = b"DXT1"                              # ddspf.dwFourCC
    # 0x1000 TEXTURE | 0x40000 | 0x8 -- byte-for-byte what the game's own
    # logo carries.
    struct.pack_into("<I", header, 108, 0x00401008)       # dwCaps

    block = _dxt1_block(*rgb)
    body = bytearray()
    for level_w, level_h in chain:
        bw = max(1, -(-level_w // 4))
        bh = max(1, -(-level_h // 4))
        body += block * (bw * bh)
    return bytes(header) + bytes(body)


def _mip_chain(width: int, height: int) -> list[tuple[int, int]]:
    """The (w, h) levels of a full mip chain, halving until 1x1."""
    levels = [(width, height)]
    while width > 1 or height > 1:
        width = max(1, width // 2)
        height = max(1, height // 2)
        levels.append((width, height))
    return levels


_TOBJ_LEN_AT = 40      # u32 length of the texture path
_TOBJ_PATH_AT = 48     # the path string itself; bytes 44-47 are padding


def _patch_tobj(template: bytes, path: str) -> bytes | None:
    """Point a .tobj at a new texture path.

    A .tobj stores a length-prefixed absolute texture path: u32 at offset 40
    is the string length, four zero bytes follow at 44-47, and the string
    starts at offset 48. The path is absolute, i.e. it starts with "/".

    Writing the string at 44 instead corrupts the padding and the game then
    reports "Failed reading map name from file ...".
    """
    if template is None or len(template) < _TOBJ_PATH_AT:
        return None
    if not path.startswith("/"):
        path = "/" + path
    old_len = struct.unpack("<I", template[_TOBJ_LEN_AT:_TOBJ_LEN_AT + 4])[0]
    if _TOBJ_PATH_AT + old_len > len(template):
        return None

    # template[:48] already carries the header, the length field and its
    # 4 padding bytes, so only the length and the string itself are rewritten.
    data = bytearray(template[:_TOBJ_PATH_AT])
    raw = path.encode("ascii")
    data[_TOBJ_LEN_AT:_TOBJ_LEN_AT + 4] = struct.pack("<I", len(raw))
    data += raw
    data += template[_TOBJ_PATH_AT + old_len:]
    return bytes(data)


def build_brand_logo(name: str, tobj_template: bytes | None,
                     dds: bytes | None = None,
                     dirs: tuple[str, ...] = (_TRUCK_LOGO_DIR,)) -> dict:
    """Return {path: bytes} for the brand-logo files in each given directory.

    `name` is the stem the logo is filed under. `dds` is the real texture when
    the source mod ships one; otherwise a flat placeholder is generated.

    The Car Dealer resolves the logo by the name it asks for, so the caller
    decides the stem: the brand folder for truck_brand_logo/, and the dealer
    folder for car_brand_logo/ (the game asked for
    "/material/ui/car_brand_logo/mod.mat").
    """
    name = re.sub(r"[^a-z0-9]+", "", str(name).lower()) or "mod"
    texture = dds if dds else _make_dds()
    out: dict[str, bytes] = {}
    for directory in dirs:
        root = f"{directory}/{name}"
        out[f"{root}.dds"] = texture
        out[f"{root}.mat"] = (
            'material : "ui"\n'
            "{\n"
            f'\ttexture : "{name}.tobj"\n'
            '\ttexture_name : "texture"\n'
            "}\n"
        ).encode("utf-8")
        tobj = _patch_tobj(tobj_template, f"/{root}.dds")
        if tobj is not None:
            out[f"{root}.tobj"] = tobj
    return out


def _synth_tobj(path: str) -> bytes:
    """Build a minimal .tobj pointing at `path`, for when no template exists.

    Layout copied from a logo file the game itself ships: a 40-byte header, a
    u32 length at offset 40, four zero bytes, then the absolute ASCII path
    from offset 48.
    """
    if not path.startswith("/"):
        path = "/" + path
    raw = path.encode("ascii")
    return (
        bytes(40)
        + struct.pack("<I", len(raw))
        + b"\x00\x00\x00\x00"
        + raw
    )


def find_mod_folders() -> list[Path]:
    """Locate ATS user-data folders, including inside Proton/Wine prefixes.

    A Windows ATS install keeps its data in Documents. Under Proton that path
    lives inside the prefix's fake C: drive, so "Documents\\..." is useless to
    a Linux user -- find the real one instead of telling them to guess.
    """
    found: list[Path] = []
    home = Path.home()

    candidates = [
        home / "Documents" / "American Truck Simulator",
        home / ".wine" / "drive_c" / "users",
        home / ".local" / "share" / "Steam" / "steamapps" / "compatdata",
        home / ".steam" / "steamapps" / "compatdata",
        home / ".steam" / "steam" / "steamapps" / "compatdata",
    ]

    def consider(p: Path) -> None:
        # A real ATS data dir has profiles/ or config.cfg.
        if (p / "profiles").is_dir() or (p / "config.cfg").is_file():
            real = p.resolve()
            if real not in found:
                found.append(real)

    for base in candidates:
        if not base.exists():
            continue
        if base.name == "American Truck Simulator":
            consider(base)
            continue
        # walk one or two levels down (users/<name>/..., compatdata/<id>/...)
        for depth in (1, 2):
            for parent in base.glob("/".join(["*"] * depth)):
                if not parent.is_dir():
                    continue
                for sub in parent.glob("**/American Truck Simulator"):
                    if sub.is_dir():
                        consider(sub)

    return found


def _write_scs(files: dict[str, str], out_path: Path,
               assets: dict[str, bytes] | None = None) -> None:
    """Write the converted mod.

    SCS's zipfs is stricter than a general ZIP reader. Archives it accepts
    consistently use:
      * create_system = 0 (MS-DOS), not 3 (Unix) -- Python's default
      * explicit directory entries for every parent folder
      * external_attr 0x81b60000 on files, 0x41ff0010 on directories

    Getting these wrong makes the game reject every entry with
    "Failed to process incompatible zip entry", which surfaces as
    "mod pool: 1 packs, 0 files" -- the mod appears in the list but nothing
    it contains is ever loaded.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    FILE_ATTR = 0x81B60000
    DIR_ATTR = 0x41FF0010

    assets = assets or {}

    # Collect every parent directory so we can emit directory entries.
    dirs: set[str] = set()
    for name in list(files) + list(assets):
        parts = name.split("/")[:-1]
        for i in range(len(parts)):
            dirs.add("/".join(parts[: i + 1]) + "/")

    stamp = (2026, 1, 1, 0, 0, 0)

    def entry(arcname: str, data: bytes, is_dir: bool) -> zipfile.ZipInfo:
        zi = zipfile.ZipInfo(arcname, date_time=stamp)
        zi.compress_type = (
            zipfile.ZIP_STORED if is_dir else zipfile.ZIP_DEFLATED
        )
        zi.create_system = 0          # MS-DOS, as SCS archives use
        zi.create_version = 20
        zi.extract_version = 20
        zi.external_attr = DIR_ATTR if is_dir else FILE_ATTR
        zi.internal_attr = 0
        return zi

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        for d in sorted(dirs):
            z.writestr(entry(d, b"", True), b"")
        for name in sorted(files):
            data = files[name].encode("utf-8")
            z.writestr(entry(name, data, False), data)
        for name in sorted(assets):
            data = assets[name]
            z.writestr(entry(name, data, False), data)


def print_install_help(out: Path) -> None:
    """Print the output path and where to copy it, with a real mod folder if found."""
    print()
    print(_ui.paint("  " + _ui.rule("=", 62), "green"))
    print()
    print("  " + _ui.paint("DONE", "green", "bold")
          + " -- your Road Trip mod is here:")
    print()
    print("      " + _ui.paint(str(out.resolve()), "bold"))
    print()
    print("  " + _ui.paint("To install it:", "bold"))
    print("    " + _ui.paint("1.", "magenta") + " Close American Truck Simulator")

    mod_dirs = [d / "mod" for d in find_mod_folders()]
    if mod_dirs:
        print("    " + _ui.paint("2.", "magenta")
              + " Copy that .scs into your ATS mod folder:")
        for d in mod_dirs:
            marker = "" if d.exists() else "  (will be created)"
            print("       " + _ui.paint(str(d), "cyan") + marker)
    else:
        print("    " + _ui.paint("2.", "magenta")
              + " Copy that .scs into your ATS mod folder:")
        print("       Windows:  Documents\\American Truck Simulator\\mod")
        print("       Proton:   ~/.steam/steamapps/compatdata/<appid>/pfx/drive_c/")
        print("                 users/<user>/Documents/American Truck Simulator/mod")
        print("       " + _ui.paint("(I could not find one on this machine -- search for",
                                        "dim"))
        print("        " + _ui.paint("'American Truck Simulator' under compatdata/ if unsure)",
                                        "dim"))
    print("    " + _ui.paint("3.", "magenta")
          + " Start the game, then Mod Manager -> enable -> Save & Play")
    print()
    print("  " + _ui.paint("Car Mode needs the Road Trip DLC. Once it is unlocked,",
                            "dim"))
    print("  " + _ui.paint("look for this car under Car Dealer.", "dim"))
    print(_ui.paint("  " + _ui.rule("=", 62), "green"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Convert a truck-classified ATS mod into a Road Trip car.\n"
            "Run with no arguments for a guided walkthrough."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python3 convert_to_car.py                       # guided, asks everything\n"
            "  python3 convert_to_car.py mod.scs --type suv --brand porsche\n"
            "  python3 convert_to_car.py mod.scs --type sedan --brand mercedes "
            "--mass 1900\n"
        ),
    )
    ap.add_argument("input", nargs="?", type=Path, help="mod folder or plain-ZIP .scs")
    ap.add_argument(
        "--type",
        dest="vtype",
        help="Road Trip vehicle type (writes tags[] into data.sii). One of: "
             + ", ".join(VEHICLE_TYPES)
             + ". Unlisted spellings are accepted and normalised, so an "
               "alternative token can be tested without editing the script.",
    )
    ap.add_argument(
        "--brand",
        help="brand, e.g. porsche. Omit to pick from the shipped logo list. "
             "See brands.py for the recognised names.",
    )
    ap.add_argument(
        "--brand-logo",
        help="logo image for this brand (PNG/WebP/SVG). Converted to the "
             "256x64 DDS the game needs. Overrides the mod's own logo.",
    )
    ap.add_argument(
        "--brand-logo-dir",
        help="directory to look for <brand>.png instead of the bundled logos/.",
    )
    ap.add_argument(
        "--extra-tags",
        help="comma-separated extra vehicle types to add to tags[], e.g. "
             "'pickup,sedan'. Extra tags let the car satisfy more than one "
             "car_job category.",
    )
    ap.add_argument("--slug", help="vehicle slug; defaults to source folder name")
    ap.add_argument("--asset-root", help="rewrite model roots to this, e.g. /vehicle/car/my_car")
    ap.add_argument(
        "--keep-physics",
        action="store_true",
        help="retain the mod's own def/vehicle/physics.sii instead of dropping "
             "it. It is a non-namespaced global, so keeping it also overrides "
             "the base game's brake and tyre calibration for every other "
             "vehicle in the game",
    )
    ap.add_argument(
        "--engine-torque",
        type=int,
        metavar="NM",
        help="peak engine torque in Nm, overriding the figure the mod states "
             "in info[]. Use this to match the feel of an SCS Road Trip car, "
             "whose target is not the real engine's output",
    )
    ap.add_argument(
        "--acceleration",
        type=float,
        metavar="SECONDS",
        help="target 0-60 mph time; the engine is tuned to hit it at the "
             "given --mass. Preferred over --engine-nm-per-tonne, which is a "
             "constant fitted from one car",
    )
    ap.add_argument(
        "--engine-nm-per-tonne",
        type=float,
        metavar="NM/T",
        help="peak engine torque per tonne of kerb weight, e.g. "
             "--engine-nm-per-tonne 760. Preferred over --engine-torque: an "
             "absolute figure silently breaks when the real car weighs more "
             "than its --type preset assumes",
    )
    ap.add_argument(
        "--no-car-tune",
        dest="car_tune",
        action="store_false",
        help="leave chassis physics alone (still writes tags[] + dealer bindings)",
    )
    ap.add_argument(
        "--mass",
        help="kerb weight: TOTAL kg (e.g. 1650) or FRONT,REAR (e.g. 1150,1050). "
             "Skips the interactive prompt.",
    )
    ap.add_argument(
        "--no-prompt",
        action="store_true",
        help="never prompt, even on a terminal",
    )
    ap.add_argument(
        "--force-physics",
        action="store_true",
        help="overwrite chassis physics even when the mod already has car-sane values",
    )
    ap.add_argument(
        "--strict-encryption",
        action="store_true",
        help="refuse to read entries whose index claims encryption; only a real "
             "--password will open them",
    )
    ap.add_argument(
        "--password",
        help="password for a protected mod archive",
    )
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument(
        "-o", "--output", type=Path,
        help="output .scs (default: rt_<slug>.scs next to the input)",
    )
    args = ap.parse_args(argv)

    if _tty() and not args.no_prompt and args.input is None:
        print()
        print(_ui.banner())

    guided = _tty() and not args.no_prompt
    total = 6

    # Optional but wanted-by-default: offer to install ImageMagick before the
    # user invests answers in the walkthrough. Cheap when it is already there.
    if not args.no_prompt:
        check_dependencies()

    # ---- 1. source -------------------------------------------------------
    if args.input is None:
        if not guided:
            raise SystemExit(
                "error: no input given.\n"
                "  Guided mode: run with no arguments and answer the prompts.\n"
                "  Scripted: pass the path, e.g. python3 convert_to_car.py mod.scs \\\n"
                "            --type suv --brand porsche"
            )
        _ui.step(1, total, "Find your mod")
        args.input = prompt_source()
    if not args.input.exists():
        raise SystemExit(f"error: no such file or folder: {args.input}")

    src = ModSource(args.input, password=args.password,
                     plain_read=not args.strict_encryption)
    src.check_readable()
    slug = args.slug or slug_from(args.input)

    # ---- 2. type ---------------------------------------------------------
    if args.vtype is None:
        if not guided:
            raise SystemExit("error: --type is required in scripted mode")
        _ui.step(2, total, "Choose the body type")
        args.vtype = prompt_type()
    args.vtype = normalise_type(args.vtype)

    # Secondary tags: a comma-separated list adds further vehicle types to
    # tags[] so the car can satisfy more than one car_job category.
    tags = [args.vtype]
    if args.extra_tags:
        for extra in args.extra_tags.split(","):
            extra = normalise_type(extra)
            if extra not in tags:
                tags.append(extra)

    # ---- 3. brand --------------------------------------------------------
    if args.brand is None:
        if not guided:
            raise SystemExit("error: --brand is required in scripted mode")
        _ui.step(3, total, "Choose the brand")
        args.brand = prompt_brand(slug)
    args.brand = _brands.normalise(args.brand) or "other"

    # ---- 3b. brand logo -------------------------------------------------
    logo_dds = None
    if args.brand_logo:
        try:
            logo_dds = _logo.image_to_dds(
                Path(args.brand_logo).read_bytes()
            )
        except (_logo.LogoError, OSError) as exc:
            raise SystemExit(f"error: --brand-logo: {exc}")
    else:
        path = None
        if args.brand_logo_dir:
            for ext in (".png", ".webp", ".svg", ".jpg", ".jpeg"):
                cand = Path(args.brand_logo_dir) / f"{args.brand}{ext}"
                if cand.exists():
                    path = cand
                    break
        else:
            path = _brands.logo_path(args.brand)
        if path is not None:
            if _logo.imagemagick() is None:
                # check_dependencies() already explained this and offered an
                # install. Declining must not then abort the conversion, so
                # carry on with the mod's own texture or the flat placeholder.
                print()
                _ui.warn(
                    f"no ImageMagick, so {path.name} was not converted; the car "
                    "keeps the mod's own logo or a flat placeholder"
                )
            else:
                try:
                    logo_dds = _logo.image_to_dds(path.read_bytes())
                except (_logo.LogoError, OSError) as exc:
                    raise SystemExit(f"error: {path.name}: {exc}")

    # ---- 4. weight -------------------------------------------------------
    mass = None
    if args.mass:
        raw = args.mass.replace(" ", "").replace("_", "")
        try:
            if "," in raw:
                parts = raw.split(",")
                if len(parts) != 2:
                    raise ValueError
                mass = [int(float(x)) for x in parts]
            else:
                mass = split_mass(int(float(raw.rstrip("kgt."))), args.vtype, 2)
        except ValueError:
            raise SystemExit(
                f"error: --mass must be TOTAL kg (e.g. 1650) or FRONT,REAR "
                f"(e.g. 1150,1050), got {args.mass!r}"
            )
        if len(mass) != 2 or any(m < 1 for m in mass):
            raise SystemExit("error: --mass needs two positive weights")

    # Every path must bind `wanted`: --mass skips the guided prompts entirely,
    # and an unbound name here would only surface at the end of the run.
    wanted = args.acceleration
    if guided:
        # Interactive: only when a terminal is attached, so scripted runs
        # and CI never block on stdin.
        _ui.step(4, total, "Set the kerb weight")
        mass = prompt_weight(args.vtype, None)
        if wanted is None:
            _ui.step(5, total, "Set the acceleration")
            wanted = prompt_acceleration(
                sum(mass) if mass else default_total(args.vtype))

    # A target 0-60 supersedes the fitted Nm/t constant: it is measured from
    # the user's own two numbers instead of extrapolated from one car. Seeded
    # from argparse so the no-target path still honours an explicit flag.
    engine_nm_per_tonne = args.engine_nm_per_tonne
    target_source = (f"--engine-nm-per-tonne {engine_nm_per_tonne:g}"
                     if engine_nm_per_tonne is not None else None)
    if wanted is not None:
        kerb = sum(mass) if mass else default_total(args.vtype)
        engine_nm_per_tonne = torque_for_time(kerb, wanted) / (kerb / 1000.0)
        target_source = f"{wanted:.1f}s 0-60 at {kerb:.0f} kg"

    # ---- 6. output -------------------------------------------------------
    if args.output is not None:
        out = args.output
    elif args.input.is_file():
        # next to the source mod
        out = args.input.parent / f"{RT_PREFIX}_{slug}.scs"
    else:
        # input was a folder: write inside it
        out = args.input / f"{RT_PREFIX}_{slug}.scs"

    if guided:
        _ui.step(6, total, "Convert")
        print()

    rep = convert(
        src,
        out,
        vtype=args.vtype,
        brand=args.brand,
        slug=slug,
        explicit_slug=args.slug is not None,
        asset_root=args.asset_root,
        keep_physics=args.keep_physics,
        engine_torque=args.engine_torque,
        engine_nm_per_tonne=engine_nm_per_tonne,
        target_source=target_source,
        dry_run=args.dry_run,
        car_tune=args.car_tune,
        mass=mass,
        force_physics=args.force_physics,
        tags=tags,
        logo_dds=logo_dds,
    )

    if mass:
        suffix = " (dry run)" if args.dry_run else ""
        rep.action(
            f"kerb weight set by you: {mass[0]} kg front + {mass[1]} kg rear "
            f"= {sum(mass)} kg total{suffix}"
        )

    print()
    print("  " + _ui.paint(str(args.input), "dim") + "  ->  "
          + _ui.paint(str(out), "bold", "green"))
    print()
    if rep.actions:
        _ui.heading("changes")
        for a in rep.actions:
            _ui.ok(a)
    if rep.bound:
        _ui.heading("parts bound")
        for b in rep.bound:
            _ui.bullet(b)
    if rep.warnings:
        _ui.heading("warnings")
        for w in rep.warnings:
            _ui.warn(w)
    if rep.gaps:
        _ui.heading("not present in source (needs manual work)")
        for g in rep.gaps:
            print("  " + _ui.paint("?", "yellow") + " " + g)

    if args.dry_run:
        print("\n(dry run -- nothing was written)")
        return 0

    # ---- final: where the mod is ----------------------------------------
    print_install_help(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())