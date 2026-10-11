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
import re
import shutil
import struct
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


def _tty() -> bool:
    return sys.stdin.isatty()


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(1)


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
    print("  What kind of car is it?")
    for i, t in enumerate(VEHICLE_TYPES, 1):
        print(f"    {i}. {t:9s} {TYPE_HELP[t]}")
    print()
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
    """Ask which brand to file it under."""
    # Skip leading year/version noise: "2021_porsche_cayenne" -> "porsche".
    guess = ""
    for tok in re.split(r"[_\s]+", slug or ""):
        if tok and not tok.isdigit() and not re.fullmatch(r"v?\d+[a-z]?", tok):
            guess = re.sub(r"[^a-z0-9]", "", tok.lower())
            break
    guess = guess or "custom"

    print()
    print(f"  Brand folder to file it under? [{guess}]")
    print("    e.g. porsche, volkswagen, ford -- custom is fine.")
    print()
    raw = _ask("  Brand: ").strip().lower()
    if raw in ("q", "quit", "exit"):
        raise SystemExit("cancelled")
    return re.sub(r"[^a-z0-9]+", "", raw) or guess


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
        ("disc_brake", "true"),
        ("axle_suspension_type", "wishbone"),
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
                "disc_brake", "axle_suspension_type", "kerb_weight"):
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


def convert(
    src: ModSource,
    out_path: Path,
    *,
    vtype: str,
    brand: str,
    slug: str,
    explicit_slug: bool = False,
    asset_root: str | None,
    drop_physics: bool,
    dry_run: bool,
    car_tune: bool = True,
    mass: list[int] | None = None,
    force_physics: bool = False,
    tags: list[str] | None = None,
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

    # 1. existing def files, with asset paths + tags rewritten
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
        elif name.endswith("def/vehicle/physics.sii"):
            # KEEP by default. The reference converter keeps the mod's own
            # global physics block and reports that overriding it would change
            # every vehicle in the game, not just this one. It is a documented
            # pre-existing risk rather than something a converter should
            # silently delete.
            if drop_physics:
                continue
            rep.warn(
                "kept the mod's def/vehicle/physics.sii; it overrides the base "
                "game block for EVERY vehicle, not just this one"
            )

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
        f"def/vehicle/car_dealer/mod/{slug}.sii": car_binding,
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
        # 4. brand logo. Without it the Car Dealer logs
        # "No logo found for brand 'x'!" and cannot load a UI material.
        # The brand folder the mod already uses for its own truck dealer is
        # what the reference converter reuses for the logo ("merc", not
        # "mercedes").
        logo_brand = brand.lower()
        for n in sorted(src.readable):
            m = re.match(r"^def/vehicle/truck_dealer/([^/]+)/", n)
            if m:
                logo_brand = m.group(1)
                break

        tobj_tpl = next(
            (src.read_bytes(n) for n in sorted(src.readable)
             if n.endswith(".tobj") and src.read_bytes(n)),
            None,
        )
        logo = build_brand_logo(logo_brand, tobj_tpl)
        if logo:
            assets.update(logo)
            rep.action(
                f"generated material/ui/truck_brand_logo/{logo_brand}"
                ".{dds,mat,tobj}"
            )

        _write_scs(files, out_path, assets)

    if drop_physics:
        rep.warn("dropped def/vehicle/physics.sii (--drop-physics)")
    return rep


# ---------------------------------------------------------------------------
# Brand logo
# ---------------------------------------------------------------------------
# The Car Dealer logs "No logo found for brand 'x'!" and cannot open a UI
# material without these three files. The reference converter emits them under
# material/ui/truck_brand_logo/<brand>.{dds,mat,tobj}.

_LOGO_SIZE = 64


def _make_dds(size: int = _LOGO_SIZE) -> bytes:
    """A minimal, valid 64x64 RGBA DDS filled with mid-grey."""
    import struct as _s

    header = bytearray()
    header += b"DDS "
    header += _s.pack("<I", 124)                       # dwSize
    header += _s.pack("<I", 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000)  # caps,size,pf,pitch
    header += _s.pack("<I", size)                      # height
    header += _s.pack("<I", size)                      # width
    header += _s.pack("<I", size * 4)                  # pitch
    header += _s.pack("<I", 0)                         # depth
    header += _s.pack("<I", 0)                         # mipmaps
    header += _s.pack("<I", 32)                        # ddspf size
    header += _s.pack("<I", 0x1 | 0x40)                # DDPF_ALPHAPIXELS|DDPF_RGB
    header += _s.pack("<I", 0)                         # fourCC
    header += _s.pack("<I", 32)                        # RGBBitCount
    header += _s.pack("<I", 0x00FF0000)                # R mask
    header += _s.pack("<I", 0x0000FF00)                # G mask
    header += _s.pack("<I", 0x000000FF)                # B mask
    header += _s.pack("<I", 0xFF000000)                # A mask
    header += _s.pack("<I", 0x1000)                    # dwCaps = TEXTURE
    header += _s.pack("<5I", 0, 0, 0, 0, 0)           # caps2..reserved
    while len(header) < 128:        # DDSURFACEDESC2 padding
        header += b"\x00"
    return bytes(header) + bytes([96, 96, 96, 255]) * (size * size)


def _patch_tobj(template: bytes, path: str) -> bytes | None:
    """Point a .tobj at a new texture path.

    A .tobj stores a length-prefixed absolute texture path: u32 at offset 40
    is the string length, the string starts at 44. Only that changes when the
    texture is renamed, which is all the reference converter does.
    """
    if template is None or len(template) < 48:
        return None
    old_len = struct.unpack("<I", template[40:44])[0]
    if 44 + old_len > len(template):
        return None
    data = bytearray(template)
    raw = path.encode("ascii")
    data[40:44] = struct.pack("<I", len(raw))
    data[44:44 + len(raw)] = raw
    if len(raw) != old_len:
        data = data[:44 + len(raw)] + data[44 + old_len:]
    return bytes(data)


def build_brand_logo(brand: str, tobj_template: bytes | None) -> dict:
    """Return {path: bytes} for the three brand-logo files."""
    name = re.sub(r"[^a-z0-9]+", "", brand.lower()) or "mod"
    root = f"material/ui/truck_brand_logo/{name}"
    out = {
        f"{root}.dds": _make_dds(),
        f"{root}.mat": (
            'material : "ui"\n'
            "{\n"
            f'\ttexture : "{name}.tobj"\n'
            '\ttexture_name : "texture"\n'
            "}\n"
        ).encode("utf-8"),
    }
    tobj = _patch_tobj(tobj_template, f"{root}.dds")
    if tobj is None:
        return out
    out[f"{root}.tobj"] = tobj
    return out


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
    print("=" * 62)
    print(f"  DONE -- your Road Trip mod is here:\n")
    print(f"      {out.resolve()}\n")
    print("  To install it:")
    print("    1. Close American Truck Simulator")

    mod_dirs = [d / "mod" for d in find_mod_folders()]
    if mod_dirs:
        print("    2. Copy that .scs into your ATS mod folder:")
        for d in mod_dirs:
            marker = "" if d.exists() else "  (will be created)"
            print(f"       {d}{marker}")
    else:
        print("    2. Copy that .scs into your ATS mod folder:")
        print("       Windows:  Documents\\American Truck Simulator\\mod")
        print("       Proton:   ~/.steam/steamapps/compatdata/<appid>/pfx/drive_c/")
        print("                 users/<user>/Documents/American Truck Simulator/mod")
        print("       (I could not find one on this machine -- search for")
        print("        'American Truck Simulator' under compatdata/ if unsure)")
    print("    3. Start the game, then Mod Manager -> enable -> Save & Play")
    print()
    print("  Car Mode needs the Road Trip DLC. Once it is unlocked, look for")
    print("  this car under Car Dealer.")
    print("=" * 62)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Convert a truck-classified ATS mod into a Road Trip car.\n"
            "Run with no arguments for a guided walkthrough."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  convert_to_car.py                       # guided, asks everything\n"
            "  convert_to_car.py mod.scs --type suv --brand porsche\n"
            "  convert_to_car.py mod.scs --type sedan --brand vw --mass 1650\n"
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
    ap.add_argument("--brand", help="brand, e.g. porsche")
    ap.add_argument(
        "--extra-tags",
        help="comma-separated extra vehicle types to add to tags[], e.g. "
             "'pickup,sedan'. Extra tags let the car satisfy more than one "
             "car_job category.",
    )
    ap.add_argument("--slug", help="vehicle slug; defaults to source folder name")
    ap.add_argument("--asset-root", help="rewrite model roots to this, e.g. /vehicle/car/my_car")
    ap.add_argument(
        "--drop-physics",
        action="store_true",
        help="delete the mod's def/vehicle/physics.sii (kept by default: it is "
             "a non-namespaced global that overrides the base game for all "
             "vehicles, so removing it is a global change)",
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
        print("=" * 62)
        print("  ATS ROAD TRIP MOD CONVERTER")
        print("  Turn a truck-classified car mod into a Car Mode vehicle.")
        print("=" * 62)

    guided = _tty() and not args.no_prompt

    # ---- 1. source -------------------------------------------------------
    if args.input is None:
        if not guided:
            raise SystemExit(
                "error: no input given.\n"
                "  Guided mode: run with no arguments and answer the prompts.\n"
                "  Scripted: pass the path, e.g. convert_to_car.py mod.scs --type suv --brand porsche"
            )
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
        args.brand = prompt_brand(slug)

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
    elif guided:
        # Interactive: only when a terminal is attached, so scripted runs
        # and CI never block on stdin.
        mass = prompt_weight(args.vtype, None)

    # ---- 5. output -------------------------------------------------------
    if args.output is not None:
        out = args.output
    elif args.input.is_file():
        # next to the source mod
        out = args.input.parent / f"{RT_PREFIX}_{slug}.scs"
    else:
        # input was a folder: write inside it
        out = args.input / f"{RT_PREFIX}_{slug}.scs"

    rep = convert(
        src,
        out,
        vtype=args.vtype,
        brand=args.brand,
        slug=slug,
        explicit_slug=args.slug is not None,
        asset_root=args.asset_root,
        drop_physics=args.drop_physics,
        dry_run=args.dry_run,
        car_tune=args.car_tune,
        mass=mass,
        force_physics=args.force_physics,
        tags=tags,
    )

    if mass:
        suffix = " (dry run)" if args.dry_run else ""
        rep.action(
            f"kerb weight set by you: {mass[0]} kg front + {mass[1]} kg rear "
            f"= {sum(mass)} kg total{suffix}"
        )

    print(f"\n=== {args.input} -> {out} ===")
    if rep.actions:
        print("\nchanges:")
        for a in rep.actions:
            print(f"  - {a}")
    if rep.bound:
        print("\nparts bound:")
        for b in rep.bound:
            print(f"  * {b}")
    if rep.warnings:
        print("\nwarnings:")
        for w in rep.warnings:
            print(f"  ! {w}")
    if rep.gaps:
        print("\nnot present in source (needs manual work):")
        for g in rep.gaps:
            print(f"  ? {g}")

    if args.dry_run:
        print("\n(dry run -- nothing was written)")
        return 0

    # ---- final: where the mod is ----------------------------------------
    print_install_help(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())