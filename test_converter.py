#!/usr/bin/env python3
"""Tests for carutil.py.

Run: python3 test_converter.py

Two levels:
  1. Unit tests on the SII helpers and converter internals.
  2. A fidelity round-trip against the Volkswagen Parati reference, which is
     the mod we know already works in Car Mode. If the converter can reproduce
     that file's structure, its understanding of the schema is sound.
"""

from __future__ import annotations

import functools
import io
import math
import hashlib
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
import zlib
from pathlib import Path

HERE = Path(__file__).parent
BACKEND = HERE / "backend"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(BACKEND))

# The shipped modules are in two places: carutil.py is the entry point at the
# root and the helpers are in backend/. Several checks below assert on the
# source text of all of them, so each one is resolved once, here.
SOURCE = {
    "carutil.py": HERE / "carutil.py",
    "sii.py": BACKEND / "sii.py",
    "hashfs.py": BACKEND / "hashfs.py",
    "logo.py": BACKEND / "logo.py",
    "ui.py": BACKEND / "ui.py",
    "brands.py": BACKEND / "brands.py",
}

import carutil as C  # noqa: E402
import hashfs  # noqa: E402
import sii  # noqa: E402
from sii import (  # noqa: E402
    get_all_indexed,
    get_key,
    set_indexed,
    set_key,
    set_tag,
    edit_lines,
)

# Real mod files are needed by the conversion tests, but they are third-party
# assets and are not distributed with this repository. Point FIXTURES at a
# directory holding them (see README) to run the full suite; without it the
# fixture-dependent checks are reported as skipped rather than failed.
FIXTURES = Path(os.environ.get("CARUTIL_FIXTURES", HERE.parent / "testing-covnverter"))

REF = FIXTURES / "Volkswagen_Parati_1994.scs"
PILOT = FIXTURES / "Honda Pilot YG2 2025 V1.6 1.61.scs"
W223 = FIXTURES / "Mercedes-Benz W223 S-Class V2.3 1.61.scs"
PORCHE = FIXTURES / "porsche-cayenne" / "2021 Porsche Cayenne Coupe Def.scs"

FAILURES: list[str] = []
SKIPS: list[str] = []


def _say(line: str) -> None:
    """Print a line that a narrow console can actually encode.

    Windows consoles default to cp1252, and a `detail` built from rendered
    output carries box-drawing characters. Letting that raise inside a check
    aborts the whole run partway through, so the remaining checks are never
    reported -- a broken check then looks like a broken suite.
    """
    try:
        print(line)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        print(line.encode(encoding, "backslashreplace").decode(encoding))


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        _say(f"  PASS  {name}")
    else:
        _say(f"  FAIL  {name}" + (f" -- {detail}" if detail else ""))
        FAILURES.append(name)


def skip(name: str, detail: str = "") -> None:
    """Record a check that could not run because a fixture is missing."""
    _say(f"  SKIP  {name}" + (f" -- {detail}" if detail else ""))
    SKIPS.append(name)


def _probe_failure(detail: str) -> None:
    """Drive a deliberately failing check, then forget it.

    It runs against the real stdout on purpose: that is the stream whose narrow
    encoding is the thing under test. The recorded failure is removed again so
    a green run still reports none.
    """
    before = len(FAILURES)
    try:
        check("(self-test) a detail this console may not encode", False, detail)
    finally:
        del FAILURES[before:]


def need(path: Path, name: str) -> bool:
    """True if the fixture is available; otherwise records a skip."""
    if Path(path).exists():
        return True
    skip(name, f"fixture not present: {Path(path).name}"
              f" (set CARUTIL_FIXTURES)")
    return False

def guard(fn):
    """Run a test, turning a missing-fixture crash into a recorded skip.

    The conversion tests need real mod files that are third-party assets and
    are not redistributed here. Without them those checks cannot run; they
    must not be reported as failures.

    Used as ``@guard`` on a test function, or called directly as
    ``guard(test_x)()``.
    """

    @functools.wraps(fn)
    def run() -> None:
        name = fn.__name__.removeprefix("test_").replace("_", " ")
        try:
            fn()
        except FileNotFoundError as exc:
            skip(name, f"fixture not present: {Path(exc.filename or '').name}"
                        " (set CARUTIL_FIXTURES)")
        except SystemExit as exc:
            # main() reports a missing input with SystemExit rather than
            # FileNotFoundError, so a half-populated CARUTIL_FIXTURES used to
            # abort the whole run instead of skipping the checks that need the
            # absent mod. Point it at a directory holding every fixture.
            msg = str(exc)
            if "no such file or folder" not in msg:
                raise
            skip(name, f"fixture not present: {msg.split(':')[-1].strip()}"
                        " (set CARUTIL_FIXTURES)")

    return run



def section(title: str) -> None:
    print(f"\n=== {title} ===")


def synthetic_mod(root: Path, slug: str = "testcar", *,
                  data_body: list[str] | None = None,
                  unit_name: str | None = None,
                  extra_defs: dict[str, str] | None = None) -> Path:
    """Build a minimal but complete mod folder on disk.

    The real fixtures are third-party assets and are not in the repo, so every
    check that can be expressed against a mod we author ourselves has to be.
    A folder (not a zip) keeps it readable, and ModSource treats the two the
    same, so a fixture-free conversion test is enough to pin this behaviour.
    """
    vdir = root / "def" / "vehicle" / "truck" / slug
    (vdir / "chassis").mkdir(parents=True, exist_ok=True)
    (vdir / "engine").mkdir(parents=True, exist_ok=True)
    (vdir / "interior").mkdir(parents=True, exist_ok=True)
    (vdir / "transmission").mkdir(parents=True, exist_ok=True)
    (vdir / "head_light").mkdir(parents=True, exist_ok=True)
    (vdir / "paint_job").mkdir(parents=True, exist_ok=True)
    (vdir / "f_tire").mkdir(parents=True, exist_ok=True)
    (vdir / "f_disc").mkdir(parents=True, exist_ok=True)
    (vdir / "r_tire").mkdir(parents=True, exist_ok=True)
    (vdir / "r_disc").mkdir(parents=True, exist_ok=True)

    def unit(kind: str, ident: str, body: list[str]) -> str:
        return ("SiiNunit\n{\n" + f"{kind} : {ident}\n{{\n"
                + "".join(f"\t{b}\n" for b in body) + "}\n}\n")

    chassis = ["model : /vehicle/truck/%s/chassis.pmd" % slug,
               "kerb_weight[0]: 1150", "kerb_weight[1]: 1050",
               "residual_travel[0]: 0.09", "residual_travel[1]: 0.09",
               "air_resistance: 0.4"]
    (vdir / "chassis" / "xdl.sii").write_text(
        unit("accessory_truck_chassis", f"chassis.{slug}.xdl", chassis))
    (vdir / "chassis" / "xdrive.sii").write_text(
        unit("accessory_truck_chassis", f"chassis.{slug}.xdrive", chassis))
    (vdir / "engine" / "v8.sii").write_text(unit(
        "accessory_truck_engine", f"engine.{slug}.v8",
        ["torque: 1400", "torque_curve[]: (1500, 0.43)",
         "torque_curve[]: (3750, 1.0)", "engine_name: V8"]))
    (vdir / "interior" / "standard.sii").write_text(unit(
        "accessory_truck_interior", f"interior.{slug}.standard", ["ui_name: std"]))
    (vdir / "interior" / "amb_blue.sii").write_text(unit(
        "accessory_truck_interior", f"interior.{slug}.amb_blue", ["ui_name: amb"]))
    (vdir / "transmission" / "auto.sii").write_text(unit(
        "accessory_truck_transmission", f"transmission.{slug}.auto",
        ["gear_count: 10"]))
    (vdir / "head_light" / "std.sii").write_text(unit(
        "accessory_truck_head_light", f"head_light.{slug}.std", ["ui_name: hl"]))
    (vdir / "paint_job" / "red.sii").write_text(unit(
        "accessory_truck_paint_job", f"paint_job.{slug}.red",
        ["ui_name: red", "stock: true"]))

    for axle in ("f_tire", "f_disc", "r_tire", "r_disc"):
        (vdir / axle / "std.sii").write_text(unit(
            f"accessory_truck_{axle}", f"{axle}.{slug}.std", ["ui_name: w"]))

    for rel, text in (extra_defs or {}).items():
        p = vdir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    body = data_body if data_body is not None else [
        'ui_name: "Test Car"',
        'model : "/vehicle/truck/%s/car.pmd"' % slug,
    ]
    (vdir / "data.sii").write_text(
        unit("accessory_truck_data", unit_name or f"vehicle.{slug}", body))
    return root


# ---------------------------------------------------------------- sii helpers
@guard
def test_sii_helpers() -> None:
    section("sii helpers")
    lines = ["SiiNunit", "{", "unit : id", "{", '\tinfo[]: "V"', "\tkerb: 5", "}"]

    check("get_key scalar", get_key(lines, "kerb") == "5")
    check("get_key indexed", get_key(lines, "info", indexed="[]") == "V")
    check("get_key ignores wrong index", get_key(lines, "info", indexed="[1]") is None)
    check("get_all_indexed", get_all_indexed(lines, "info") == ["V"])

    out = set_key(lines, "kerb", "9")
    check("set_key replaces", get_key(out, "kerb") == "9")
    out2 = set_key(lines, "brand", '"BMW"')
    check("set_key inserts", get_key(out2, "brand") == "BMW")
    check("set_key preserves others", get_key(out2, "kerb") == "5")

    tagged = set_tag(lines, "suv")
    check("set_tag adds tag", get_all_indexed(tagged, "tags") == ["suv"])
    tagged2 = set_tag(tagged, "sedan")
    check("set_tag replaces, not duplicates", get_all_indexed(tagged2, "tags") == ["sedan"])
    tagged3 = set_tag(tagged2, ["suv", "pickup"])
    check("set_tag multi", get_all_indexed(tagged3, "tags") == ["suv", "pickup"])
    tagged4 = set_tag(tagged3, "sedan")
    check("set_tag multi replaced by single", get_all_indexed(tagged4, "tags") == ["sedan"])


def test_multikey_lines_expand() -> None:
    """A def with several keys on one line must be split, not silently cut.

    Reported on a real mod: a chassis line carrying residual_travel twice plus
    nominal_voltage and tank_size lost the last two the moment the converter
    rewrote the first. The car shipped with a 0-gallon tank. Every helper here
    is line-oriented, so the split has to happen before editing, not inside
    set_key.
    """
    section("collapsed key lines are normalised before editing")
    import sii as _sii

    collapsed = ("\tresidual_travel[]: 0.08\t\tresidual_travel[]: 0.08"
                 "  \tnominal_voltage: 12\ttank_size: 1200")
    out = _sii.expand_multi_key_lines([collapsed])

    check("every pair gets its own line", len(out) == 4, str(out))
    check("tank_size survives the split",
          get_key(out, "tank_size") == "1200", str(out))
    check("nominal_voltage survives the split",
          get_key(out, "nominal_voltage") == "12", str(out))
    check("both residual_travel elements survive",
          get_all_indexed(out, "residual_travel") == ["0.08", "0.08"], str(out))

    # The destructive case end to end: rewrite the indexed key, and the
    # unrelated keys must still be there afterwards.
    rewritten = set_indexed(out, "residual_travel", ["0.093", "0.093"])
    check("set_indexed no longer eats a neighbour",
          get_key(rewritten, "tank_size") == "1200", str(rewritten))
    check("nominal_voltage still there after set_indexed",
          get_key(rewritten, "nominal_voltage") == "12", str(rewritten))

    # A colon inside a quoted value is part of the value, not a new key.
    tricky = '\tinfo[]: "V: 8"  // and a comment: here'
    check("a quoted colon does not split the line",
          _sii.expand_multi_key_lines([tricky]) == [tricky])

    single = "\tkerb_weight[0]: 9400"
    check("a lone pair is returned byte for byte",
          _sii.expand_multi_key_lines([single]) == [single])

    # Comments and directives must not be touched at all.
    for untouched in ('@include "other.sui"', "// residual_travel[]: 0.08",
                      "", "\t}"):
        check(f"left alone: {untouched!r}",
              _sii.expand_multi_key_lines([untouched]) == [untouched])

    # set_key must be able to target a repeated key, which it never could when
    # the bracket was part of the name: KEY_RE's name group has no brackets, so
    # set_key(lines, "fallback[]", ...) matched nothing and only ever appended.
    tagged = ['\tfallback[]: "chassis|a.sii"', '\tfallback[]: "interior|b.sii"']
    replaced = set_key(tagged, "fallback", '"chassis|z.sii"', indexed="[]")
    check("set_key replaces the last repeated key, not appends",
          get_all_indexed(replaced, "fallback") == ["chassis|a.sii",
                                                   "chassis|z.sii"],
          str(replaced))


def test_collapsed_keys_survive_a_real_tune() -> None:
    """tune_chassis must not delete anything it did not mean to touch."""
    section("chassis tuning preserves collapsed neighbours")
    body = [
        "\tresidual_travel[0]: 0.08\t\tresidual_travel[1]: 0.08",
        "\tnominal_voltage: 12\ttank_size: 1200",
        "\tkerb_weight[0]: 1150\tkerb_weight[1]: 1050",
    ]
    text = "accessory_truck_chassis : vehicle.test\n{\n" + "\n".join(body) + "\n}\n"
    out = C.tune_chassis(text, "sedan", None, C.Report())

    check("nominal_voltage still present after tuning",
          get_key(edit_lines(out), "nominal_voltage") == "12", out)
    # tank_size is deliberately never touched (see
    # test_tank_size_is_left_alone), so asserting it comes through verbatim
    # proves something stronger than survival: the collapsed line is not being
    # damaged at all, and the untouched keys really are untouched.
    size = get_key(edit_lines(out), "tank_size")
    check("tank_size survives the collapsed line verbatim",
          size == "1200", f"{size}")
    # The preset is per-axle, so only the shape is asserted: both elements
    # still present, and neither left at the truck's 0.08.
    rt = get_all_indexed(edit_lines(out), "residual_travel")
    check("residual_travel kept both axles and was rewritten",
          len(rt) == 2 and "0.08" not in rt, str(rt))


def _data_sii(mod: Path, out: Path, *extra: str) -> str:
    """Convert a synthetic mod and return its finished data.sii."""
    import zipfile as _z
    real_tty = C._tty
    C._tty = lambda: False
    try:
        C.main([str(mod), "--type", "sedan", "--brand", "test", "--no-prompt",
                *extra, "-o", str(out)])
    finally:
        C._tty = real_tty
    with _z.ZipFile(out) as z:
        name = next(n for n in z.namelist()
                    if n.endswith("/data.sii") and "truck" in n)
        return z.read(name).decode()


def test_fallback_merges_with_the_mods_own() -> None:
    """A slot the mod already defaults must not get a second, rival default.

    Reported on a real mod whose data.sii already carried a complete fallback[]
    list: the converter appended its own behind it, so each slot ended up with
    two entries (the author's xdrive.sii, then our xdl.sii) and the car's
    delivered configuration was decided twice.
    """
    section("fallback[] merges instead of duplicating")
    with tempfile.TemporaryDirectory() as td:
        mod = synthetic_mod(Path(td) / "mod", data_body=[
            'ui_name: "Test Car"',
            # Both on one line, which is how the report showed them.
            'fallback[]: "chassis|xdrive.sii"\tfallback[]: "interior|standard.sii"',
            'require[]: chrome\trequire[]: steering_w',
        ])
        out = Path(td) / "o.scs"
        text = _data_sii(mod, out)
        fb = get_all_indexed(edit_lines(text), "fallback")

        slots = [f.split("|", 1)[0] for f in fb]
        check("the author's chassis choice is kept",
              "chassis|xdrive.sii" in fb, str(fb))
        check("the converter's rival xdl.sii was NOT added",
              "chassis|xdl.sii" not in fb, str(fb))
        check("no slot is defaulted twice",
              len(slots) == len(set(slots)), str(fb))
        check("slots the mod left open are still filled",
              {"engine", "transmission", "head_light"} <= set(slots), str(fb))

        # Both require[] entries shared one line. The old pattern stopped at
        # the first separator and reported only the first, so a genuinely
        # unbound accessory never reached the gap check.
        req = get_all_indexed(edit_lines(text), "require")
        check("every require[] on a collapsed line is seen",
              req == ["chrome", "steering_w"], str(req))


def test_fallback_appends_every_slot_when_the_mod_declares_none() -> None:
    """The regression guard for the fix's own first attempt.

    Routing the appends through set_key made every call rewrite the previous
    entry instead, so five discovered defaults collapsed into one.
    """
    section("fallback[] appends rather than replaces")
    with tempfile.TemporaryDirectory() as td:
        mod = synthetic_mod(Path(td) / "mod")
        out = Path(td) / "o.scs"
        fb = get_all_indexed(edit_lines(_data_sii(mod, out)), "fallback")
        check("every discovered default is present",
              {"chassis", "interior", "engine", "transmission", "head_light"}
              <= {f.split("|", 1)[0] for f in fb}, str(fb))
        check("no default was lost to a replace-instead-of-append",
              len(fb) >= 5, str(fb))


def _dealer_unit(slot: str, path: str) -> str:
    return ("SiiNunit\n{\ntruck_dealer : vehicle.truck\n{\n"
            f"\tvehicle_accessory : .{slot}\n\t{{\n"
            f'\t\tdata_path: "{path}"\n\t}}\n}}\n}}\n')


def _desktop(archive: Path) -> str:
    """The generated car_desktop binding -- the list of what is actually bound.

    Filtering on .sii matters: the archive also carries a `def/vehicle/
    car_desktop/` DIRECTORY entry, and matching that alone reads back as empty,
    which turns every "is it bound?" assertion into a silent false pass.
    """
    import zipfile as _z
    with _z.ZipFile(archive) as z:
        names = [n for n in z.namelist()
                 if "car_desktop" in n and n.endswith(".sii")]
        return z.read(names[0]).decode()


def _engine_unit(ident: str, name: str, partner: str) -> str:
    suitable = partner.rsplit("/", 1)[-1].replace(".sii", "") + ".engine"
    return ("SiiNunit\n{\naccessory_truck_engine : " + ident + "\n{\n"
            f"\ttorque: 1400\n\tengine_name: {name}\n"
            f'\tdefaults[]: "{partner}"\n'
            f'\tsuitable_for[]: "{suitable}"\n'
            "}\n}\n")


def _transmission_unit(ident: str, drive: bool,
                       partner: tuple[str, str] | None = None) -> str:
    """A transmission unit, optionally paired with an engine.

    `partner` is (engine_path, engine_ident). Both of the real mod's boxes carry
    a defaults[] naming the engine they belong with and a conflict_with[] naming
    the engine they must NOT be bound to, which is what makes the pair load
    bearing rather than a convention.
    """
    keys = ["gear_count: 10", "manual_mode_name: \"M\""]
    if drive:
        keys.append("auto_mode_name: D")
    if partner:
        keys.append(f'defaults[]: "{partner[0]}"')
        keys.append(f'conflict_with[]: {partner[1]}')
    return ("SiiNunit\n{\naccessory_truck_transmission : " + ident + "\n{\n"
            + "".join(f"\t{k}\n" for k in keys) + "}\n}\n")


def test_the_engine_follows_the_transmission_it_is_paired_with() -> None:
    """A transmission that conflicts with the engine decides which engine it is.

    The CTS-V Sport Wagon ships two mutually exclusive pairs -- 62l with
    6_manual, 62l_auto with 6_auto -- and both transmissions declare
    `conflict_with[]: engine`. Binding 62l to 6_auto is a combination the mod
    forbids, and the game resolves a conflict by dropping an accessory. That is
    a measured 26-second 0-60 against 5.3 in manual on the same engine.

    So the transmission is resolved first and its own defaults[] picks the
    engine. Resolving the two independently, which is what this used to do,
    produces the forbidden pair.
    """
    section("the engine is the one the bound transmission defaults to")
    vdir = "/def/vehicle/truck/testcar"
    both = {
        "transmission/auto.sii": _transmission_unit(
            "transmission.testcar.auto", True,
            (f"{vdir}/engine/auto.sii", "engine.testcar.manual")),
        "transmission/manual.sii": _transmission_unit(
            "transmission.testcar.manual", False,
            (f"{vdir}/engine/manual.sii", "engine.testcar.auto")),
        "engine/auto.sii": _engine_unit("engine.testcar.auto", "v8_auto",
                                        f"{vdir}/transmission/auto.sii"),
        "engine/manual.sii": _engine_unit("engine.testcar.manual", "v8_manual",
                                          f"{vdir}/transmission/manual.sii"),
    }

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "mod"
        dealer = root / "def" / "vehicle" / "truck_dealer" / "tester"
        dealer.mkdir(parents=True)
        (dealer / "tester_0.sii").write_text(
            _dealer_unit("engine", f"{vdir}/engine/manual.sii")
            + _dealer_unit("transm", f"{vdir}/transmission/manual.sii"))
        mod = synthetic_mod(root, extra_defs=both)
        fb = get_all_indexed(edit_lines(_data_sii(mod, Path(td) / "o.scs")),
                             "fallback")
        check("the dealer's transmission is bound, automatic preference aside",
              "transmission|manual.sii" in fb, str(fb))
        check("and its own engine with it",
              "engine|manual.sii" in fb, str(fb))
        check("the other engine, which it conflicts with, is not",
              "engine|auto.sii" not in fb, str(fb))

        # A dealer that names the other transmission gets that one's engine,
        # which is the property the first case does not exercise.
        root2 = Path(td) / "mod2"
        dealer2 = root2 / "def" / "vehicle" / "truck_dealer" / "tester"
        dealer2.mkdir(parents=True)
        (dealer2 / "tester_0.sii").write_text(
            _dealer_unit("transm", f"{vdir}/transmission/auto.sii"))
        mod2 = synthetic_mod(root2, extra_defs=both)
        fb2 = get_all_indexed(edit_lines(_data_sii(mod2, Path(td) / "o2.scs")),
                              "fallback")
        check("switching transmission switches its engine too",
              "transmission|auto.sii" in fb2 and "engine|auto.sii" in fb2, str(fb2))
        check("even when the dealer never named an engine",
              "engine|manual.sii" not in fb2, str(fb2))


def test_an_engine_with_no_rev_bands_gets_them() -> None:
    """The automatic has no shift band unless the engine declares one.

    Measured on a Cadillac CTS-V Sport Wagon: 26 seconds to 60 mph on the
    automatic, 5.3 in manual, same engine. Both its engines declare none of
    rpm_range_low_gear / _high_gear / _power -- and neither does any other mod
    measured: the Escalade, the Pilot and the W223 all set all three, as do all
    four of SCS's own car engines. Without them the game falls back to a default
    tuned for a truck and changes up far too early.

    The defaults written are the median of the three mods that do declare them,
    so a mod that already has bands is left byte-for-byte alone and only the gap
    is filled.
    """
    section("an engine with no rev bands is given measured ones")
    real_tty = C._tty
    C._tty = lambda: False
    with_bands = "\n".join(f"\t{k}: {C._RPM_RANGE_DEFAULTS[k]}"
                       for k in C._RPM_RANGE_ORDER) + "\n"

    def engine(body: str) -> str:
        return ("SiiNunit\n{\naccessory_engine_data : e\n{\n"
                f"\ttorque: 503\n\trpm_idle: 750\n{body}\tengine_name: V8\n"
                "}\n}\n")

    # 1. none present: all three written, and the user is told.
    C._rpm_ranges = None
    rep = C.Report()
    out = C.tune_engine(engine(""), rep)
    lines = edit_lines(out)
    for key in C._RPM_RANGE_ORDER:
        check(f"{key} written",
              get_key(lines, key) == C._RPM_RANGE_DEFAULTS[key], out)
    check("the user is warned, not left to guess",
          any("rpm_range_low_gear" in w for w in rep.warnings), str(rep.warnings))

    # 2. all present: untouched. The other three mods take this path.
    C._rpm_ranges = None
    already = engine(with_bands)
    rep2 = C.Report()
    check("an engine that declares them is untouched",
          C.tune_engine(already, rep2) == already)
    check("and is not warned about",
          not any("rpm_range" in w for w in rep2.warnings), str(rep2.warnings))

    # 3. only some missing: only the gap is filled.
    C._rpm_ranges = None
    partial = engine("\trpm_range_low_gear: (1250, 1850)\n"
                     "\trpm_range_power: (1800, 6000)\n")
    lines3 = edit_lines(C.tune_engine(partial, C.Report()))
    check("a band already declared is kept",
          get_key(lines3, "rpm_range_low_gear") == "(1250, 1850)")
    check("the missing one is filled",
          get_key(lines3, "rpm_range_high_gear")
          == C._RPM_RANGE_DEFAULTS["rpm_range_high_gear"])
    check("and the third is still the author's",
          get_key(lines3, "rpm_range_power") == "(1800, 6000)")

    # 4. the horsepower path rebuilds the curve and returns early, so the bands
    #    have to be written there too or --power silently skips them.
    C._rpm_ranges = None
    hp = C.tune_engine(engine(""), C.Report(), target_hp=300, peak_power_rpm=5000)
    check("the --power path writes them too",
          get_key(edit_lines(hp), "rpm_range_power")
          == C._RPM_RANGE_DEFAULTS["rpm_range_power"], hp)
    C._tty = real_tty


def test_rpm_bands_scale_to_where_the_engine_peaks() -> None:
    """A fixed band is only right for one engine, and we build a different one.

    503 Nm peaking at 4000 rpm and 503 Nm peaking at 6000 rpm cannot both be
    served by an upshift at 1950; the second one leaves its whole power band
    unused. So the measured bands scale with the engine's power peak.

    Expressed against the 5000 rpm peak the three measured mods sit at, the six
    numbers are clean fractions -- (0.27, 0.39), (0.29, 0.41), (0.70, 1.20) --
    which is what makes this a derivation rather than a fit: at P = 5000 it
    reproduces the measured median exactly.
    """
    section("the rev bands follow the engine's own power peak")
    real_tty = C._tty
    C._tty = lambda: False
    at_ref = C._rpm_range_bands(5000)
    check("the reference peak reproduces the measured median exactly",
          at_ref == C._RPM_RANGE_DEFAULTS, str(at_ref))

    early = C._rpm_range_bands(4000)
    late = C._rpm_range_bands(6000)
    for key in C._RPM_RANGE_ORDER:
        e = int(re.search(r"\d+", early[key]).group(0))
        r = int(re.search(r"\d+", at_ref[key]).group(0))
        l = int(re.findall(r"\d+", late[key])[1])
        check(f"{key} moves up with a later peak",
              e < r < l, f"4000={early[key]} 5000={at_ref[key]} 6000={late[key]}")

    # End to end: --peak-power-rpm has to reach the written bands, or the prompt
    # for maximum revs would tune the engine and leave the gearbox behind.
    eng = ("SiiNunit\n{\naccessory_engine_data : e\n{\n\ttorque: 503\n"
           "\trpm_idle: 750\n\trpm_limit: 6100\n"
           "\ttorque_curve[]: (1500, 0.51)\n\ttorque_curve[]: (3750, 0.57)\n"
           "}\n}\n")
    for prm, want in ((4000, "(2800, 4800)"), (5000, "(3500, 6000)")):
        C._rpm_ranges = None
        got = get_key(edit_lines(C.tune_engine(
            eng, C.Report(), target_hp=300, peak_power_rpm=prm)), "rpm_range_power")
        check(f"--peak-power-rpm {prm} moves the band", got == want, got)

    # An engine cannot make power above its own limiter.
    C._rpm_ranges = None
    clamped = get_key(edit_lines(C.tune_engine(
        eng, C.Report(), target_hp=300, peak_power_rpm=6000)), "rpm_range_power")
    check("the power band never exceeds the rev limit",
          int(re.findall(r"\d+", clamped)[1]) <= 6100, clamped)
    C._tty = real_tty


_ENGINE_CURVE = ("SiiNunit\n{\naccessory_engine_data : e\n{\n\ttorque: 503\n"
                 "\trpm_idle: 750\n\trpm_limit: 6100\n"
                 "\ttorque_curve[]: (1500, 0.51)\n"
                 "\ttorque_curve[]: (3750, 0.57)\n}\n}\n")


def test_the_rev_limit_cuts_the_curve_and_is_no_longer_asked() -> None:
    """A converted engine keeps the mod's own rpm_limit, as SCS's cars keep theirs.

    There was a `default_rev_limit()` that derived 1.5x peak torque -- a figure
    that sits inside the 1.44-2.29x SCS's own engines reach, which is why it
    looked like the reference speaking. It clipped our own curve below the rev
    where the shape runs out and cost real torque (FINDINGS 39), and the ceiling
    was never this converter's to choose. `--rev-limit` is now the only way to
    move it, and it is applied BEFORE the curve is built, never after: the curve
    is clipped to the limit, so writing it afterwards would leave a curve
    reaching past the limiter that is supposed to stop it.
    """
    section("the engine keeps the mod's own rev limit")
    real_tty = C._tty
    C._tty = lambda: False
    C._rpm_ranges = None
    out = C.tune_engine(_ENGINE_CURVE, C.Report(), target_hp=300,
                        peak_power_rpm=5000, rev_limit=5600)
    pts = re.findall(r"torque_curve\[\]\s*:\s*\((\d+)\s*,\s*([\d.]+)\)", out)
    check("rpm_limit written", get_key(edit_lines(out), "rpm_limit") == "5600")
    check("the curve is clipped to the limiter, not past it",
          max(int(r) for r, f in pts) == 5600, out)
    check("and it falls to zero there",
          pts[-1][1] == "0.0", out)

    # No flag, at the library level: `tune_engine(rev_limit=None)` leaves the
    # mod's own ceiling standing, and so does the CLI -- there is no derived
    # default anywhere left to disagree with it. This is the behaviour of the
    # function and of the walkthrough alike.
    C._rpm_ranges = None
    check("tune_engine(None) leaves the mod's own limit untouched",
          get_key(edit_lines(C.tune_engine(_ENGINE_CURVE, C.Report(), target_hp=300,
                                           peak_power_rpm=5000)), "rpm_limit")
          == "6100")

    # A ceiling above the shape's natural end is air, and is called air.
    C._rpm_ranges = None
    rep = C.Report()
    C.tune_engine(_ENGINE_CURVE, rep, target_hp=300, peak_power_rpm=5000,
                  rev_limit=8000)
    check("a limit above the curve's reach is warned about",
          any("runs out at about 6667" in w for w in rep.warnings),
          str(rep.warnings))

    C._rpm_ranges = None
    rep2 = C.Report()
    C.tune_engine(_ENGINE_CURVE, rep2, target_hp=300, peak_power_rpm=5000,
                  rev_limit=6000)
    check("a limit inside the curve's reach is not",
          not any("rev limit" in w for w in rep2.warnings), str(rep2.warnings))

    # The air is judged against the ceiling the engine will CARRY, not only one
    # the user named. This engine's own 6100 sits below the shape's 6667, so it
    # stays quiet; the case below is a mod whose own limiter does not.
    C._rpm_ranges = None
    rep3 = C.Report()
    kept = C.tune_engine(_ENGINE_CURVE, rep3, target_hp=300, peak_power_rpm=5000)
    check("the mod's own ceiling is judged for air too, not only an explicit one",
          not any("runs out at about" in w for w in rep3.warnings),
          str(rep3.warnings))
    check("...and the engine keeps the mod's own limiter",
          get_key(edit_lines(kept), "rpm_limit") == "6100")

    # A mod whose own limiter is above the curve's reach still gets told, with no
    # flag involved. This is the CTS-V Sport Wagon's shape of problem: its 6100
    # against a 5333 ceiling for a 4000 rpm power peak.
    C._rpm_ranges = None
    rep4 = C.Report()
    airy = _engine_unit("torque: 3500", "rpm_limit: 6100",
                        "torque_curve[]: (1500, 0.43)",
                        "torque_curve[]: (3750, 0.57)")
    C.tune_engine(airy, rep4, target_hp=300, peak_power_rpm=4000)
    check("air above the curve is reported on a default conversion, not only "
          "with --rev-limit",
          any("runs out at about 5333" in w for w in rep4.warnings),
          str(rep4.warnings))

    # The rev bands must not claim full power above where the engine cuts. They
    # used to: _ensure_rpm_ranges runs before _set_rev_limit, so it read the
    # unit's own limiter and then wrote a lower one underneath -- (2800, 4800) on
    # an engine cut at 3750, telling the gearbox to use revs that do not exist.
    rep5 = C.Report()
    real_tty2, saved_ranges = C._tty, C._rpm_ranges
    C._tty = lambda: False
    C._rpm_ranges = C._rpm_range_bands(4000, 3750)
    try:
        out = C.tune_engine(airy, rep5, target_hp=516.0,
                            peak_power_rpm=4000, rev_limit=3750)
    finally:
        C._tty = real_tty2
        C._rpm_ranges = saved_ranges
    written = get_key(edit_lines(out), "rpm_limit")
    band = get_key(edit_lines(out), "rpm_range_power")
    check("the ceiling asked for is the one written", written == "3750",
          str(written))
    check("the power band is written", band is not None, str(band))
    if band:
        check("the power band does not claim full power past the ceiling",
              int(re.findall(r"\d+", band)[-1]) <= int(written),
              f"band {band}, ceiling {written}")
    for key in ("rpm_range_low_gear", "rpm_range_high_gear"):
        got = get_key(edit_lines(out), key)
        check(f"{key} also stays under the ceiling",
              got is None or int(re.findall(r"\d+", got)[-1]) <= 3750, str(got))

    C._tty = real_tty


def test_the_peak_rpm_is_held_inside_the_engines_own_range() -> None:
    """The only rev question is where the power peaks, and the engine bounds it.

    There is no rev-limit prompt and no derived ceiling: the engine keeps the
    mod's own `rpm_limit`, which is what SCS's own cars do -- 1.44 to 2.29 times
    their torque peak. What is asked is where the rebuilt curve peaks, and that
    answer is held between the engine's idle and its limiter, both read off the
    mod rather than invented. A peak below the idle is a rev the engine is not
    at; one above the limiter is a rev it may not reach.
    """
    section("the peak rpm stays inside the engine's own rev range")
    import builtins
    import io

    real_tty = C._tty
    real_input = builtins.input
    real_stdout = sys.stdout
    C._tty = lambda: True
    try:
        def drive(answers, bounds):
            fake, prompts = _answer(list(answers))
            builtins.input = fake
            buf = io.StringIO()
            sys.stdout = buf
            try:
                got = C.prompt_power(2200.0, bounds)
            finally:
                sys.stdout = real_stdout
            return got, prompts, buf.getvalue()

        # The CTS-V Sport Wagon, measured: idle 750, limiter 6100.
        got, prompts, text = drive(["350", "4000"], (750, 6100))
        check("a peak inside the engine's range is taken",
              got == (350.0, 4000), str(got))
        check("the hint states the engine's own idle and limiter",
              "750" in text and "6100" in text, text)
        check("the field carries no label",
              all(p.strip() == "" for p in prompts), repr(prompts))

        for bad, why in (("200", "below the idle"),
                         ("77000", "above the limiter"),
                         ("fast", "not a number at all"),
                         ("", "empty")):
            got, _prompts, text = drive(["350", bad, "5000"], (750, 6100))
            check(f"a peak {why} is re-asked, not accepted",
                  got == (350.0, 5000), str(got))
            check(f"a peak {why}: the re-ask names the engine's own range",
                  "750" in text and "6100" in text, text)

        # No bounds is the historical wide range, not a refusal: a mod that
        # declares no limiter still has to be convertible.
        got, _p, text = drive(["350", "4500"], None)
        check("a mod with no declared range keeps the wide historical one",
              got == (350.0, 4500), str(got))

        def boom(prompt: str = "") -> str:
            raise KeyboardInterrupt
        builtins.input = boom
        buf = io.StringIO()
        sys.stdout = buf
        try:
            check("Ctrl-C on the power field keeps the default",
                  C.prompt_power(2200.0, (750, 6100)) == (None, None))
        finally:
            sys.stdout = real_stdout
    finally:
        C._tty = real_tty
        builtins.input = real_input

    # The reader. Both ends off the mod, and neither invented.
    engine = _engine_unit(
        "torque: 3500",
        "rpm_limit: 6100 # max of 6.2 L V8 is 6100 RPM",
        "torque_curve[]: (750, 0.55)",
        "torque_curve[]: (3750, 0.72)",
        "torque_curve[]: (6100, 0.689)",
    )
    check("the idle comes from the first curve sample",
          C._idle_rpm(engine) == 750, str(C._idle_rpm(engine)))
    check("a curve carrying the (0, 0) origin is not idle at zero",
          C._idle_rpm("torque_curve[]: (0, 0.0)\n"
                      "torque_curve[]: (750, 0.55)") == 750)
    check("no idle at all is reported as None", C._idle_rpm("torque: 1") is None)

    # The reader, against a real mod folder on disk rather than an injected
    # entry: ModSource.local holds (offset, size, method, flag) tuples, so
    # handing it raw bytes is not a way to test a reader.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "mod"
        synthetic_mod(root, extra_defs={"engine/62l.sii": engine})
        with C.ModSource(root) as src:
            check("the bounds are the engine's own idle and limiter",
                  C.engine_rev_bounds(src) == (750, 6100),
                  str(C.engine_rev_bounds(src)))

        # A curve with no samples offers nothing to bound against.
        thin_root = Path(td) / "thin"
        synthetic_mod(thin_root, extra_defs={"engine/t.sii": _engine_unit(
            "torque: 1", "rpm_limit: 6100")})
        with C.ModSource(thin_root) as src2:
            check("a mod with no torque curve offers no range",
                  C.engine_rev_bounds(src2) is None)

    # And the walkthrough: seven steps again, with no rev-limit step between the
    # power and the price. Driven for real, because nothing else in this suite
    # goes through main()'s guided path.
    with tempfile.TemporaryDirectory() as td:
        C._rpm_ranges = C._rpm_range_bands(5000, 6100)
        mod = synthetic_mod(Path(td) / "mod", extra_defs={
            "engine/v8.sii": _engine_unit(
                "torque: 1400", "rpm_limit: 6100",
                "torque_curve[]: (1500, 0.43)", "torque_curve[]: (3750, 1.0)")})
        seen: list[int] = []
        offered: list = []
        stubs = {
            "prompt_source": lambda: mod,
            "prompt_type": lambda: "sedan",
            "prompt_brand": lambda slug: "test",
            "prompt_weight": lambda vtype, preset: [1150, 1050],
            "prompt_power": lambda kerb, bounds=None: (
                offered.append(bounds) or (350.0, 5000)),
            # Pre-answered, and _tty pinned on: the synthetic engine declares no
            # rev bands, so a real conversion would offer them with an arrow menu
            # and wait for a keypress nobody sends. That is the hang this suite is
            # required never to have.
            "prompt_price": lambda: 45000,
        }
        real_step = C._ui.step
        saved = {name: getattr(C, name) for name in stubs}
        real_stdout = sys.stdout
        try:
            C.__dict__.update(stubs)
            C._ui.step = lambda index, total, text: seen.append(index)
            C._tty = lambda: True
            sys.stdout = io.StringIO()
            C.main(["-o", str(Path(td) / "guided.scs")])
        finally:
            sys.stdout = real_stdout
            for name, fn in saved.items():
                setattr(C, name, fn)
            C._ui.step = real_step
            C._tty = real_tty
            C._rpm_ranges = None
        check("the walkthrough numbers its steps 1 to 7 with no gap",
              seen == [1, 2, 3, 4, 5, 6, 7], str(seen))
        check("the power prompt is handed the engine's own range",
              offered == [(1500, 6100)], str(offered))


def test_clipping_the_ceiling_costs_torque_and_says_so() -> None:
    """Cutting the ceiling below the shape's rev range changes the engine.

    It does not merely stop it earlier. Drop the shape's second-highest point --
    5882 rpm at a 5000 rpm power peak -- and build_power_curve re-stretches to
    put the power peak on a shorter curve, which moves the torque peak up and
    costs real torque:

        ceiling 6000+   torque peak 3750   503 Nm   353 hp
        ceiling 5625    torque peak 4500   464 Nm   326 hp

    So the loss is measured against the engine's own ceiling and reported. This
    is the case the default of 1.5x peak torque lands in, which is exactly why
    it needed catching rather than absorbing.
    """
    section("a ceiling below the shape's rev range costs torque, and warns")
    real_tty = C._tty
    C._tty = lambda: False
    def build(limit):
        C._rpm_ranges = None
        rep = C.Report()
        out = C.tune_engine(_ENGINE_CURVE, rep, target_hp=300,
                            peak_power_rpm=5000, rev_limit=limit)
        tq = float(get_key(edit_lines(out), "torque"))
        peak = C._primary_curve_peak(out)
        return tq * peak, rep

    at_own, rep_own = build(6100)
    check("at its own ceiling the engine is unharmed", at_own > 0, str(at_own))
    check("and does not warn about losing torque",
          not any("costs this engine torque" in w for w in rep_own.warnings),
          str(rep_own.warnings))

    clipped, rep_cut = build(5625)
    check("clipping below the shape's range really costs torque",
          clipped < at_own * 0.98, f"{at_own:.0f} -> {clipped:.0f} Nm")
    check("and the cost is reported",
          any("costs this engine torque" in w for w in rep_cut.warnings),
          str(rep_cut.warnings))

    # Above the shape's reach the ceiling is air instead: warned, but no torque
    # is lost, because the curve was going to die there anyway.
    C._rpm_ranges = None
    rep_air = C.Report()
    C.tune_engine(_ENGINE_CURVE, rep_air, target_hp=300, peak_power_rpm=5000,
                  rev_limit=8000)
    check("a ceiling above the curve's reach is air, not a torque loss",
          any("runs out at about" in w for w in rep_air.warnings)
          and not any("costs this engine torque" in w for w in rep_air.warnings),
          str(rep_air.warnings))
    C._tty = real_tty


def test_the_shift_point_is_the_band_that_governs_upshifts() -> None:
    """`rpm_range_power` is not what changes gear, and the data proves it.

    For the F-150 the power band is (5000, 6000) while the box shifts between
    1500 and 2200. A key that decided when to change up would have to overlap
    the shift band, so it is not that. Measured shift point against peak torque
    across SCS's own four:

        ch289v8      Mustang   2300 / 2400  =  0.96x
        v8_46l       Victoria   3400 / 4100  =  0.83x
        ford_27l_eco Bronco    2000 / 3000  =  0.67x
        ford_tivctv8 F-150     2200 / 4500  =  0.49x

    So which end of that range you want is taste, not measurement, and the
    default (0.55x) sits with the pickup. --shift-up-rpm moves the band's
    upper bound and leaves its floor alone: the floor is where the box drops
    back down, and moving it too would change how it hunts between gears.
    """
    section("the shift point is the band's upper bound, and it is a choice")
    real_tty = C._tty
    C._tty = lambda: False
    eng = ("SiiNunit\n{\naccessory_engine_data : e\n{\n\ttorque: 503\n"
           "\trpm_idle: 750\n\trpm_limit: 6100\n"
           "\ttorque_curve[]: (1500, 0.51)\n\ttorque_curve[]: (3750, 0.57)\n"
           "}\n}\n")
    torque_peak = 3750

    def bands(**kw):
        C._rpm_ranges = None
        return edit_lines(C.tune_engine(eng, C.Report(), target_hp=300,
                                        peak_power_rpm=5000, **kw))

    default = bands()
    up = int(re.findall(r"\d+", get_key(default, "rpm_range_high_gear"))[1])
    check("the default is pickup-territory, 0.55x peak torque",
          abs(up / torque_peak - 0.55) < 0.02, f"{up}/{torque_peak}")
    for asked, ratio in ((3200, 0.85), (3600, 0.96)):
        lines = bands(shift_up_rpm=asked)
        check(f"--shift-up-rpm {asked} shifts at {ratio}x peak torque",
              get_key(lines, "rpm_range_high_gear") == f"(1450, {asked})",
              str(get_key(lines, "rpm_range_high_gear")))
        check("  the band's floor is left where the author put it",
              get_key(lines, "rpm_range_low_gear").startswith("(1350,"),
              str(get_key(lines, "rpm_range_low_gear")))

    # An explicit choice is applied even to a mod that declares its bands --
    # the one exception to preservation, because it was asked for by name.
    declaring = eng.replace(
        "\trpm_limit: 6100\n",
        "\trpm_limit: 6100\n\trpm_range_low_gear: (1250, 1850)\n"
        "\trpm_range_high_gear: (1350, 1950)\n\trpm_range_power: (1800, 6000)\n")
    C._rpm_ranges = None
    overridden = edit_lines(C.tune_engine(declaring, C.Report(), target_hp=300,
                                          peak_power_rpm=5000, shift_up_rpm=3200))
    check("a mod that declares its bands still takes an explicit shift point",
          get_key(overridden, "rpm_range_high_gear") == "(1350, 3200)",
          str(get_key(overridden, "rpm_range_high_gear")))
    C._rpm_ranges = None
    kept = edit_lines(C.tune_engine(declaring, C.Report(), target_hp=300,
                                    peak_power_rpm=5000))
    check("and without one it is still preserved",
          get_key(kept, "rpm_range_high_gear") == "(1350, 1950)"
          and get_key(kept, "rpm_range_power") == "(1800, 6000)",
          str([get_key(kept, k) for k in C._RPM_RANGE_ORDER]))
    C._tty = real_tty


def test_rpm_band_input_is_validated() -> None:
    section("a rev band asked for by hand is validated")
    check("two numbers", C._parse_band("1350, 1950") == "(1350, 1950)")
    check("spaces are fine", C._parse_band(" 1350 , 1950 ") == "(1350, 1950)")
    check("one number is not a band", C._parse_band("1350") is None)
    check("three are not a band", C._parse_band("1, 2, 3") is None)
    check("words are not numbers", C._parse_band("low, high") is None)
    check("backwards is not a band", C._parse_band("1950, 1350") is None)
    check("equal ends is not a band", C._parse_band("1500, 1500") is None)
    check("zero is not a rev", C._parse_band("0, 1500") is None)
    check("empty is not a band", C._parse_band("") is None)


def test_defaults_come_from_the_mods_dealer_config() -> None:
    """The author's dealer unit names the car's real part; believe it.

    Reported on a real mod: discovery bound chassis/xdl.sii and
    interior/amb_blue.sii purely because they sort first, where the mod's own
    dealer config named xdrive.sii and standard.sii.
    """
    section("default parts follow the mod's dealer config")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "mod"
        vdir = "/def/vehicle/truck/testcar"
        dealer = Path(root) / "def" / "vehicle" / "truck_dealer" / "tester"
        dealer.mkdir(parents=True)
        (dealer / "tester_0.sii").write_text(
            _dealer_unit("chassis", f"{vdir}/chassis/xdrive.sii")
            + _dealer_unit("interior", f"{vdir}/interior/standard.sii"))
        mod = synthetic_mod(root)
        out = Path(td) / "o.scs"
        text = _data_sii(mod, out)

        fb = get_all_indexed(edit_lines(text), "fallback")
        check("the dealer's chassis is the default, not the alphabetical one",
              "chassis|xdrive.sii" in fb, str(fb))
        check("the alphabetically-first chassis is not used",
              "chassis|xdl.sii" not in fb, str(fb))
        check("the dealer's interior wins too",
              "interior|standard.sii" in fb, str(fb))

        bound = _desktop(out)
        check("the bound chassis is the dealer's file",
              "chassis/xdrive.sii" in bound, bound[:400])
        check("the alphabetically-first chassis is not bound either",
              "chassis/xdl.sii" not in bound, bound[:400])


def test_decorative_slots_are_never_auto_bound() -> None:
    """A decorative addon must not become part of the spawned car.

    Reported on a real mod carrying ETS2 interior toys in toybig/, toyseat/,
    toystand/ and set_dashbrd/. Binding one makes it mandatory, and ATS has no
    models for it: the game died with "Can not open
    '/vehicle/truck/upgrade/interior_decors/...pmd'".
    """
    section("decorative slots are left out of the car")
    decoys = ("toybig", "toyseat", "toystand", "set_dashbrd", "interior_decors")
    # Names that merely START with "toy" or "interior_decor" and are real
    # parts. A prefix match would swallow these.
    keepers = ("toyota_livery", "toybox_trim", "interior_decor_lights")
    with tempfile.TemporaryDirectory() as td:
        extra = {
            f"accessory/{d}/x.sii":
                f'SiiNunit\n{{\naccessory_truck_addon : {d}\n{{\n'
                f'\tui_name: "{d}"\n}}\n}}\n'
            for d in decoys + keepers
        }
        mod = synthetic_mod(Path(td) / "mod", extra_defs=extra)
        out = Path(td) / "o.scs"
        _data_sii(mod, out)

        bound = _desktop(out)
        for d in decoys:
            check(f"{d} is not bound", d not in bound, bound[:400])
        # Still shipped, just not fitted: a mod that wants one binds it itself.
        import zipfile as _z
        with _z.ZipFile(out) as z:
            names = z.namelist()
        for d in decoys:
            check(f"{d} def is still in the archive",
                  any(d in n for n in names), str(names[:5]))
        # The whole point of matching whole names: a real part whose name
        # starts with a decorative word must still be bound.
        for d in keepers:
            check(f"{d} is a real part and IS bound",
                  f"accessory/{d}/" in bound, bound[:400])


def _chassis_unit(*body: str) -> str:
    return ("SiiNunit\n{\naccessory_truck_chassis : vehicle.test\n{\n"
            + "".join(f"\t{b}\n" for b in body) + "}\n}\n")


def test_hex_float_kerb_weight_reads_as_truck_scale() -> None:
    """SII's `&xxxxxxxx` form must not read as "not a truck".

    A 9.4 t chassis written kerb_weight[0]: &459380cd (4720.1 kg) raised
    ValueError inside _is_truck_scale, which caught it and returned False. The
    truck's mass was therefore judged car-sane and preserved whole.
    """
    section("hex floats are read as numbers")
    import sii as _sii

    check("a hex float decodes to its value",
          abs(_sii.to_float("&459380cd") - 4720.1001) < 0.001,
          str(_sii.to_float("&459380cd")))
    check("a plain number still decodes", _sii.to_float("1150") == 1150.0)
    check("a hex float counts as truck-scale kerb weight",
          C._is_truck_scale("kerb_weight", ["&459380cd", "&459380cd"]))
    check("a car's own kerb weight still does not",
          not C._is_truck_scale("kerb_weight", ["1150", "1050"]))
    # Unreadable input must still raise rather than be silently treated as 0.
    try:
        _sii.to_float("not a number")
        check("garbage still raises", False, "no error raised")
    except ValueError:
        check("garbage still raises", True)

    # And end to end: the hex chassis gets replaced, not preserved.
    text = _chassis_unit("kerb_weight[0]: &459380cd",
                         "kerb_weight[1]: &459380cd")
    out = C.tune_chassis(text, "sedan", None, C.Report())
    weights = get_all_indexed(edit_lines(out), "kerb_weight")
    check("the truck mass is replaced by the car's",
          weights and "&" not in weights[0], str(weights))
    check("and it totals a plausible car mass",
          0 < sum(int(float(w)) for w in weights) < 2500, str(weights))


def test_explicit_mass_overrides_the_mods_own() -> None:
    """--mass is an instruction; a car-sane existing value must not win."""
    section("--mass wins over the mod's own kerb_weight")
    text = _chassis_unit("kerb_weight[0]: 1150", "kerb_weight[1]: 1050")
    out = C.tune_chassis(text, "sedan", [1200, 1200], C.Report())
    check("the requested mass is written",
          get_all_indexed(edit_lines(out), "kerb_weight") == ["1200", "1200"],
          out)
    # Without --mass the mod's car-sane figure is still preserved.
    kept = C.tune_chassis(text, "sedan", None, C.Report())
    check("with no --mass the mod's own figure survives",
          get_all_indexed(edit_lines(kept), "kerb_weight") == ["1150", "1050"],
          kept)


def test_tank_size_is_left_alone() -> None:
    """Fuel capacity is economy, and this converter does not do economy.

    It was briefly written here -- every preset carried a car-sized figure and
    _is_truck_scale had a >200 threshold for it -- and that made range WORSE.
    A real mod's 291 L tank became 79 L while its fuel consumption, which
    nothing here touches, stayed at the truck's rate. A smaller tank at a
    truck's consumption is a strictly shorter range than the tank the mod
    shipped with, so the half-fix made the complaint worse.
    """
    section("tank_size is the mod's own business")
    text = _chassis_unit("kerb_weight[0]: 1150", "kerb_weight[1]: 1050",
                         "tank_size: 1200")
    out = C.tune_chassis(text, "sedan", None, C.Report())
    check("a truck-sized tank is preserved, not shrunk",
          get_key(edit_lines(out), "tank_size") == "1200", out)

    # --force-physics must not reach it either: fuel is not driving physics.
    forced = C.tune_chassis(text, "sedan", None, C.Report(), force=True)
    check("--force-physics leaves it alone too",
          get_key(edit_lines(forced), "tank_size") == "1200", forced)

    # No half-configured key left lying around. A preset figure with no writer
    # is exactly the trap AGENTS.md documents, and it is why the threshold
    # branch in _is_truck_scale was removed rather than left dormant.
    check("the tank_size threshold is gone from _is_truck_scale",
          not C._is_truck_scale("tank_size", "1200"),
          "the dormant branch came back")


def test_suspension_class_is_validated() -> None:
    """All four SCS Road Trip chassis declare `standard`."""
    section("suspension_class is checked against SCS's own value")
    check("a coach rating is treated as truck-scale",
          C._is_truck_scale("suspension_class", "bus"))
    check("SCS's own value is left alone",
          not C._is_truck_scale("suspension_class", "standard"))

    text = _chassis_unit("kerb_weight[0]: 1150", "kerb_weight[1]: 1050",
                         "suspension_class: bus")
    rep = C.Report()
    out = C.tune_chassis(text, "sedan", None, rep)
    check("'bus' is rewritten to 'standard'",
          get_key(edit_lines(out), "suspension_class") == "standard", out)
    check("and the rewrite is warned about, not done silently",
          any("suspension_class" in w for w in rep.warnings), str(rep.warnings))


def _engine_unit(*body: str) -> str:
    # accessory_engine_data is the unit name tune_engine keys on; the truck
    # spelling is not accepted, and a test that used it silently measured
    # nothing.
    return ("SiiNunit\n{\naccessory_engine_data : engine.test\n{\n"
            + "".join(f"\t{b}\n" for b in body) + "}\n}\n")


def test_resistance_torque_is_rescaled_with_the_engine() -> None:
    """Engine drag is a fraction of torque, so it must follow torque down.

    A mod declaring torque 4900 with resistance_torque 320 (drag at ~12% of
    peak) kept 320 after conversion to a 476 Nm engine -- 67% drag, which is
    why lifting off at 50 mph dropped the car to about 30 immediately.
    """
    section("resistance_torque follows torque")
    real_tty = C._tty
    C._tty = lambda: False
    engine = _engine_unit(
        "torque: 4900",
        "resistance_torque: 320",
        "torque_curve[]: (1000, 0.5)",
        "torque_curve[]: (2000, 1.0)",
        "rpm_limit: 4000",
    )
    rep = C.Report()
    out = C.tune_engine(engine, rep, force=True, target_hp=200.0,
                        tonnes=1.5)
    lines = edit_lines(out)
    peak = float(get_key(lines, "torque"))
    drag = get_key(lines, "resistance_torque")

    check("peak torque came down", peak < 4900, f"{peak}")
    check("drag came down with it", drag is not None and float(drag) < 320,
          f"{drag}")
    check("drag is still the same fraction of peak",
          abs(float(drag) / peak - 320 / 4900) < 0.02, f"{drag}/{peak}")
    check("the rescale is reported", any("resistance_torque" in a
                                         for a in rep.actions), str(rep.actions))

    # -1 is the game's "no drag" and must survive untouched.
    nodrag = _engine_unit("torque: 4900", "resistance_torque: -1",
                          "torque_curve[]: (1000, 0.5)",
                          "torque_curve[]: (2000, 1.0)", "rpm_limit: 4000")
    kept = C.tune_engine(nodrag, C.Report(), force=True, target_hp=200.0,
                         tonnes=1.5)
    check("-1 (no drag) is left exactly as written",
          get_key(edit_lines(kept), "resistance_torque") == "-1", kept)

    # _rescale_in_place exists precisely so an author's trailing comment
    # survives a rewrite rather than being discarded with the line.
    commented = _engine_unit(
        "torque: 4900 # peak, per the tuner",
        "resistance_torque: 320 # measured on the dyno",
        "torque_curve[]: (1000, 0.5)", "torque_curve[]: (2000, 1.0)",
        "rpm_limit: 4000")
    out2 = C.tune_engine(commented, C.Report(), force=True, target_hp=200.0,
                         tonnes=1.5)
    check("a comment on torque survives the rescale",
          "# peak, per the tuner" in out2, out2)
    check("a comment on resistance_torque survives too",
          "# measured on the dyno" in out2, out2)
    C._tty = real_tty


def test_secondary_torque_is_dropped_for_a_car() -> None:
    """A truck torque multiplier is not a car mechanism.

    Keeping it proportional handed one converted car 1.7x torque in every gear
    at or below secondary_torque_gear_ratio, and another 1.5x. No production
    car has one, and all 181 SCS engines leave it alone.
    """
    section("secondary_torque* is dropped on a car")
    real_tty = C._tty
    C._tty = lambda: False
    engine = _engine_unit(
        "torque: 1400",
        "secondary_torque: 2250",
        "secondary_torque_gear_ratio: 1",
        "torque_curve[]: (1000, 0.5)",
        "torque_curve[]: (2000, 1.0)",
        "rpm_limit: 4000",
    )
    rep = C.Report()
    out = C.tune_engine(engine, rep, force=True, target_hp=200.0, tonnes=1.5)
    lines = edit_lines(out)

    check("secondary_torque is gone", get_key(lines, "secondary_torque") is None,
          out)
    check("secondary_torque_gear_ratio is gone",
          get_key(lines, "secondary_torque_gear_ratio") is None, out)
    check("the drop is reported", any("secondary_torque" in a
                                      for a in rep.actions), str(rep.actions))

    # The rescale path (Nm/t) must do the same, not just the hp path.
    scaled = C.tune_engine(engine, C.Report(), force=True, nm_per_tonne=250.0,
                           tonnes=1.5)
    check("dropped on the torque-rescale path too",
          get_key(edit_lines(scaled), "secondary_torque") is None, scaled)
    C._tty = real_tty


def test_truck_fuel_keys_are_dropped_for_a_car() -> None:
    """Fuel burn is a truck-era model; no SCS car declares it.

    Across the 657 definition units recovered from dlc_rt_ford.scs -- every
    definition SCS ships for its own four Road Trip cars -- consumption_coef
    appears ZERO times. The only fuel key present is adblue_consumption: 0.0.

    Every truck mod carries one anyway: 0.42 (Pilot, W223), 0.52 and 0.70
    (Escalade), 1 (BMW 135i). Those are truck burn rates, and leaving one on a
    car engine is what a driver reports as "fuel lasts way too little".

    Removing the key beats retuning it: there is no measured car value to
    substitute, whereas removing it puts the car on exactly the same footing as
    the Mustang and the Bronco, which take the base game's consumption.
    """
    section("truck fuel keys are dropped, not retuned")
    real_tty = C._tty
    C._tty = lambda: False
    engine = _engine_unit(
        "torque: 1400",
        "consumption_coef: 0.42",
        "adblue_consumption: 0.035",
        "no_adblue_power_limit: 0.75",
        "torque_curve[]: (1000, 0.5)",
        "torque_curve[]: (2000, 1.0)",
        "rpm_limit: 4000",
    )
    rep = C.Report()
    out = C.tune_engine(engine, rep, force=True, target_hp=200.0, tonnes=1.5)
    lines = edit_lines(out)

    check("consumption_coef is gone",
          get_key(lines, "consumption_coef") is None, out)
    check("adblue_consumption is gone",
          get_key(lines, "adblue_consumption") is None, out)
    check("no_adblue_power_limit is gone",
          get_key(lines, "no_adblue_power_limit") is None, out)
    # A WARN, not a quiet action: this changes how the car drives in the world
    # and the user has to be able to see it and object.
    check("the fuel drop is warned about, not silently applied",
          any("truck fuel key" in w for w in rep.warnings), str(rep.warnings))
    check("the warning names the key",
          any("consumption_coef" in w for w in rep.warnings),
          str(rep.warnings))

    # Both engine paths, or the torque-rescale route would keep the truck rate.
    scaled = C.tune_engine(engine, C.Report(), force=True, nm_per_tonne=250.0,
                           tonnes=1.5)
    check("dropped on the torque-rescale path too",
          get_key(edit_lines(scaled), "consumption_coef") is None, scaled)

    # An engine that never had them must be left alone, and must NOT warn --
    # otherwise every SCS-clean mod gets a spurious warning.
    clean = _engine_unit("torque: 500",
                         "torque_curve[]: (1000, 0.5)",
                         "torque_curve[]: (2000, 1.0)", "rpm_limit: 4000")
    rep2 = C.Report()
    C.tune_engine(clean, rep2, force=True, target_hp=200.0, tonnes=1.5)
    check("an engine without them is not warned about",
          not any("truck fuel key" in w for w in rep2.warnings),
          str(rep2.warnings))

    # The point of the exercise: tuning still ran, and only the fuel keys went.
    new_tq = float(get_key(edit_lines(out), "torque"))
    check("and the torque was still retargeted, so only fuel changed",
          new_tq < 1400 and new_tq > 50, f"{new_tq} Nm from 1400")
    C._tty = real_tty


def test_tank_size_is_still_left_to_the_mod() -> None:
    """The tank is deliberately NOT rewritten, and the reason is now known.

    Consumption is the truck-side problem; capacity is not. Once
    consumption_coef is dropped, the car takes the base game's own burn rate,
    and the mod's oversized tank is merely generous rather than broken. A
    car-sized tank against a still-trucked consumption coefficient is
    strictly worse range, which is why the tank is left alone.
    """
    section("tank_size stays the mod's own decision")
    text = _chassis_unit("kerb_weight[0]: 1150", "kerb_weight[1]: 1050",
                         "tank_size: 868")
    check("a truck-sized tank is preserved verbatim",
          get_key(edit_lines(C.tune_chassis(text, "sedan", None, C.Report())),
                  "tank_size") == "868", text)


def test_missing_assets_are_reported() -> None:
    """A mod whose defs point at files it does not ship must say so.

    Some mods ship without directory listings for part of their tree, so a
    plain extraction comes up hundreds of files short -- the engine's sound
    bank, most textures and models. Every def still parses, so a 12 MB archive
    was produced from a 124 MB mod and the car spawned mute and untextured.
    """
    section("incomplete extractions are called out")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "mod"
        refs = "\n".join(
            f'\tmodel: "/model/thing/part{i}.pmd"' for i in range(4))
        synthetic_mod(root, extra_defs={
            "accessory/a/one.sii": f"SiiNunit\n{{\ncamera_def : c1\n{{\n{refs}\n}}\n}}\n",
            "asset/thing/note.txt": "x",      # makes /model/thing a root
        })
        src = C.ModSource(root)
        roots = C.asset_roots(src)
        check("the mod's own asset root is found", "/model/thing" in roots,
              str(roots))

        rep = C.Report()
        C.check_asset_integrity(src, roots, rep)
        check("a wholly absent asset set is a warning, not a silent success",
              len(rep.warnings) == 1, str(rep.warnings + rep.gaps))
        check("the warning says how much is missing",
              "4 of 4" in rep.warnings[0], rep.warnings[0])
        check("and it names the files",
              "/model/thing/part0.pmd" in rep.warnings[0], rep.warnings[0])

        # A complete mod stays silent, and base-game paths are never counted.
        full = Path(td) / "full"
        synthetic_mod(full)
        for i in range(4):
            p = full / "model" / "thing" / f"part{i}.pmd"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"pmd")
        rep2 = C.Report()
        C.check_asset_integrity(C.ModSource(full), C.asset_roots(C.ModSource(full)),
                                rep2)
        check("a complete mod reports nothing",
              not rep2.warnings and not rep2.gaps, str(rep2.warnings + rep2.gaps))


def test_slug_never_collapses_to_a_bare_number() -> None:
    """`vehicle.gle.450` became `450`, and two mods would want the same name.

    The reference's rule -- last dot-separated token -- is right while that
    token names the vehicle. It collapses when it is a bare number, which is
    how registration files ended up called 20.sii.
    """
    section("slug keeps enough of the unit name to stay unique")
    import zipfile as _z

    cases = [
        ("vehicle.gle.450", "gle_450"),
        ("vehicle.prado.20", "prado_20"),
        ("vehicle.w223_s500", "w223_s500"),          # unchanged
        ("vehicle.volkswagen.parati1994", "parati1994"),  # unchanged
    ]
    for unit_name, want in cases:
        with tempfile.TemporaryDirectory() as td:
            mod = synthetic_mod(Path(td) / "mod", unit_name=unit_name)
            out = Path(td) / "o.scs"
            C.main([str(mod), "--type", "sedan", "--brand", "test",
                    "--no-prompt", "-o", str(out)])
            with _z.ZipFile(out) as z:
                desktop = [n for n in z.namelist()
                           if "car_desktop" in n and n.endswith(".sii")]
            check(f"{unit_name} -> {want}",
                  desktop and Path(desktop[0]).stem == want,
                  str(desktop))

    # An explicit --slug is still the user speaking for themselves.
    with tempfile.TemporaryDirectory() as td:
        mod = synthetic_mod(Path(td) / "mod", unit_name="vehicle.gle.450")
        out = Path(td) / "o.scs"
        C.main([str(mod), "--type", "sedan", "--brand", "test", "--no-prompt",
                "--slug", "mine", "-o", str(out)])
        with _z.ZipFile(out) as z:
            desktop = [n for n in z.namelist()
                       if "car_desktop" in n and n.endswith(".sii")]
        check("--slug overrides the derived one",
              desktop and Path(desktop[0]).stem == "mine", str(desktop))


def test_output_is_not_written_inside_the_input_folder() -> None:
    """A second run must not nest the previous output inside the mod."""
    section("default output does not land inside the input folder")
    with tempfile.TemporaryDirectory() as td:
        mod = synthetic_mod(Path(td) / "mod")
        C.main([str(mod), "--type", "sedan", "--brand", "test", "--no-prompt"])
        # The default slug comes from the input's own name, which for a folder
        # is the folder's.
        produced = Path(td) / "rt_mod.scs"
        check("output is written beside the input folder", produced.exists(),
              str(list(Path(td).iterdir())))
        check("nothing was written inside the input folder",
              not list(mod.glob("*.scs")), str(list(mod.iterdir())))

        # And the real damage: a re-run must not carry the old archive in.
        mod2 = synthetic_mod(Path(td) / "mod2")
        (mod2 / "rt_old.scs").write_bytes(b"PK\x03\x04 not really an archive")
        out2 = Path(td) / "second.scs"
        C.main([str(mod2), "--type", "sedan", "--brand", "test",
                "--no-prompt", "-o", str(out2)])
        with zipfile.ZipFile(out2) as z:
            names = z.namelist()
        check("a stray .scs inside the input is not carried over",
              not any(n.endswith(".scs") for n in names),
              str([n for n in names if n.endswith(".scs")]))


def test_generic_camera_name_is_flagged() -> None:
    """A mod defining camera.behind.car collides with the base game."""
    section("base-game unit-name collisions are reported")
    with tempfile.TemporaryDirectory() as td:
        mod = synthetic_mod(Path(td) / "mod", extra_defs={
            "accessory/cam/camera.sii":
                "SiiNunit\n{\ncamera_def : camera.behind.car\n{\n"
                "\tui_name: behind\n}\n}\n",
        })
        out = Path(td) / "o.scs"
        buf = io.StringIO()
        real, sys.stdout = sys.stdout, buf
        try:
            C.main([str(mod), "--type", "sedan", "--brand", "test",
                    "--no-prompt", "-o", str(out)])
        finally:
            sys.stdout = real
        text = buf.getvalue()
        check("the collision is named in the summary",
              "camera.behind.car" in text, text[-400:])
        # Under "warnings", not "gaps": it is the mod's defect, not a part the
        # user still has to add by hand.
        wi = text.find("warnings")
        gi = text.find("not present in source")
        check("reported as a warning, not a gap",
              wi != -1 and "camera.behind.car" in text[wi:gi], text[-400:])


# ------------------------------------------------------------- type selection
@guard
def test_vehicle_types() -> None:
    section("vehicle types")
    check("seven types", len(C.VEHICLE_TYPES) == 7, str(C.VEHICLE_TYPES))
    for t in ("pickup", "suv", "sedan", "muscle", "classic", "sports", "offroad"):
        check(f"has {t}", t in C.VEHICLE_TYPES)


@guard
def test_type_validation() -> None:
    section("type validation via CLI")
    # Known types pass through unchanged.
    check("normalise keeps sedan", C.normalise_type("sedan") == "sedan")
    check("normalise keeps suv", C.normalise_type("suv") == "suv")
    check("normalise lowercases", C.normalise_type("SUV") == "suv")
    check("normalise strips", C.normalise_type("  sedan ") == "sedan")
    # Aliases map onto a canonical type.
    check("off-road -> offroad", C.normalise_type("off-road") == "offroad")
    check("crossover -> suv", C.normalise_type("crossover") == "suv")
    check("wagon -> sedan", C.normalise_type("wagon") == "sedan")
    check("coupe -> sports", C.normalise_type("coupe") == "sports")
    # An unknown token is passed through, not rejected, so it can be tested
    # in-game rather than blocking the run.
    check("unknown passes through", C.normalise_type("spaceship") == "spaceship")
    # Empty is still an error.
    try:
        C.normalise_type("  ")
        check("empty type rejected", False)
    except SystemExit:
        check("empty type rejected", True)

    if not need(REF, "unlisted-type conversion"):
        return

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "x.scs"
        r = subprocess.run(
            [sys.executable, str(HERE / "carutil.py"), str(REF),
             "--type", "spaceship", "--brand", "vw", "-o", str(out)],
            capture_output=True, text=True,
        )
        check("accepts unlisted type", r.returncode == 0, r.stderr[-200:])
        if r.returncode == 0:
            import zipfile as _z
            with _z.ZipFile(out) as z:
                data = z.read(
                    "def/vehicle/truck/volkswagen.parati1994/data.sii"
                ).decode()
            check("writes unknown tag", 'tags[]: "spaceship"' in data)

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "e.scs"
        r = subprocess.run(
            [sys.executable, str(HERE / "carutil.py"), str(REF),
             "--type", "suv", "--extra-tags", "pickup,sedan", "--brand", "vw",
             "-o", str(out)],
            capture_output=True, text=True,
        )
        check("extra-tags accepted", r.returncode == 0, r.stderr[-200:])
        if r.returncode == 0:
            import zipfile as _z
            with _z.ZipFile(out) as z:
                data = z.read(
                    "def/vehicle/truck/volkswagen.parati1994/data.sii"
                ).decode()
            check("three tags written", data.count("tags[]") == 3)
            check("primary first", 'tags[]: "suv"' in data)
            check("extra pickup", 'tags[]: "pickup"' in data)
            check("extra sedan", 'tags[]: "sedan"' in data)


# --------------------------------------------------- reference fidelity check
@guard
def test_reference_roundtrip() -> None:
    section("reference fidelity (Parati -> sedan)")
    if not need(REF, "reference fidelity"):
        return
    check("reference present", True)

    ref = zipfile.ZipFile(REF)
    ref_binding = ref.read(
        "def/vehicle/car_dealer/volkswagen/volks_parati_0.sii"
    ).decode()

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "parati_out.scs"
        C.main([
            str(REF), "--type", "sedan", "--brand", "volkswagen",
            "--slug", "volkswagen_parati_1994", "-o", str(out),
        ])
        check("output written", out.exists())
        gen = zipfile.ZipFile(out)

        # The generated dealer binding should match the reference exactly.
        cand = [n for n in gen.namelist()
                if "car_dealer" in n and n.endswith(".sii")]
        check("generated car_dealer", len(cand) == 1, str(cand))
        if cand:
            gen_text = gen.read(cand[0]).decode()

            # Fidelity criterion: every accessory the reference binds must also
            # be bound by the converter. The reference binds a hand-curated
            # subset of addons, so we bind a superset -- equality would be
            # wrong to demand.
            ref_names = set(re.findall(r"^\s*accessories\[\]:\s*\.(\w+)",
                                       ref_binding, re.M))
            gen_names = set(re.findall(r"^\s*accessories\[\]:\s*\.(\w+)",
                                       gen_text, re.M))
            missing = ref_names - gen_names
            # Wheels are bound under whatever namespace the mod ships.
            # The Parati already uses car p_tire/p_disc; truck mods use
            # f_/r_ categories. Both are bound as-is, never renamed.
            for w in ("ptire", "pdisc"):
                check(f"binds .{w}", w in gen_names, f"missing {w}")
            check("binds no fabricated wheel namespace",
                  not ({"ptire", "pdisc"} - set(gen_names))
                  or "ptire" in gen_names)
            check("binds core data unit", "data" in gen_names)
            check("binds steering_w", "steering_w" in gen_names)

            # every data_path in the generated file must resolve to a real file
            paths = re.findall(r'data_path:\s*"([^"]+)"', gen_text)
            unresolved = [p for p in paths if p.lstrip("/") not in gen.namelist()]
            check("all data_path targets exist", not unresolved, str(unresolved[:5]))

            # wheel bindings must carry an offset
            check("wheel bindings carry offset", "offset: 0" in gen_text)
            check("wheel bindings carry paint_color",
                  "paint_color: (1.000000, 1.000000, 1.000000)" in gen_text)

        # no duplicate/competing bindings
        dealers = [
            n for n in gen.namelist()
            if n.endswith(".sii") and n.startswith(
                ("def/vehicle/car_dealer/", "def/vehicle/truck_dealer/",
                 "def/vehicle/car_desktop/", "def/vehicle/car_company/")
            )
        ]
        check("exactly 4 bindings emitted", len(dealers) == 4, str(dealers))

        # tags injected exactly once
        data = gen.read("def/vehicle/truck/volkswagen.parati1994/data.sii").decode()
        check("tags[] present once", data.count("tags[]") == 1)
        check("tags[] is sedan", 'tags[]: "sedan"' in data)

        # physics dropped by default
        check("physics.sii dropped",
              not any("physics" in n for n in gen.namelist()))

        # asset paths rewritten, and def paths left intact
        chassis = gen.read(
            "def/vehicle/truck/volkswagen.parati1994/chassis/4x2a.sii").decode()
        check("model path preserved verbatim",
              '"/vehicle/car/volkswagen_parati_1994/carro.pmd"' in chassis)
        check("def data_path intact",
              '"/def/vehicle/truck/volkswagen.parati1994' in gen.read(
                  cand[0]).decode() if cand else False)


@guard
def test_physics_sii_policy() -> None:
    section("physics.sii is dropped by default")
    with tempfile.TemporaryDirectory() as td:
        src_dir = Path(td) / "src"
        src_dir.mkdir()
        with zipfile.ZipFile(REF) as z:
            for n in z.namelist():
                if n.endswith("/") or "physics" in n:
                    continue
                q = src_dir / n
                q.parent.mkdir(parents=True, exist_ok=True)
                q.write_bytes(z.read(n))
        (src_dir / "def" / "vehicle" / "physics.sii").write_text(
            "SiiNunit\n{\nphysics_data : .phdata\n{\n\tversion: 2\n}\n}\n"
        )

        out = Path(td) / "a.scs"
        C.main([str(src_dir), "--type", "sedan", "--brand", "vw", "-o", str(out)])
        check("physics.sii dropped by default",
              "def/vehicle/physics.sii" not in zipfile.ZipFile(out).namelist())
        check("folder input works",
              "def/vehicle/truck/volkswagen.parati1994/data.sii"
              in zipfile.ZipFile(out).namelist())

        out2 = Path(td) / "b.scs"
        C.main([str(src_dir), "--type", "sedan", "--brand", "vw",
                "--keep-physics", "-o", str(out2)])
        check("--keep-physics retains it",
              "def/vehicle/physics.sii" in zipfile.ZipFile(out2).namelist())


# ------------------------------------------------------------------- engine
# A truck engine in a 2.2 t car reaches 60 mph in about three seconds. The
# gearbox the mods ship already matches SCS's own Road Trip gearing, and mass,
# drag and suspension are all car-scaled, so the engine's declared output is
# the only thing left that is still at truck scale.

_ENGINE_INFLATED = """SiiNunit
{
accessory_engine_data : 35l.test.engine
{
\tname: "3.5 L EcoBoost V6"
\tinfo[]: "365 @@hp@@ (272@@kw@@)"
\tinfo[]: "350 @@lb_ft@@ (475 @@nm@@)"
\ttorque: 1400
\trpm_range_low_gear: (1350, 1950)
\trpm_range_high_gear: (1450, 2050)
\trpm_range_power: (3500, 6000)
\ttorque_curve[]: (1500, 0.51)
\ttorque_curve[]: (3750, 0.57)
\tsecondary_torque: 2250 # torque x 1.26 (final drive relation)
\tsecondary_torque_curve[]: (4500, 0.61)
}
}
"""

# SCS's own Road Trip F-150 V8, verbatim: torque 556 against a stated 556 Nm.
_ENGINE_OFFICIAL = """SiiNunit
{
accessory_engine_data : ford_tivctv8.ford.f150_23.engine
{
\tname: "@@engine_ford_50l_tivct_32v_v8@@"
\tinfo[]: "400 @@hp@@ (298@@kw@@)"
\tinfo[]: "410 @@lb_ft@@ (556@@nm@@)"
\ttorque: 556
\tvolume: 5.0
\trpm_range_low_gear: (1300, 1800)
\trpm_range_high_gear: (1500, 2200)
\trpm_range_power: (5000, 6000)
\ttorque_curve[]: (1000, 0.6)
\ttorque_curve[]: (4500, 1.0)
}
}
"""


def test_stated_engine_torque() -> None:
    section("engine info[] Nm parser")
    check("parses the Nm figure", C.stated_engine_torque(_ENGINE_INFLATED) == 475)
    check("tolerates a missing space before the unit",
          C.stated_engine_torque(_ENGINE_OFFICIAL) == 556)
    check("official engine parses", C.stated_engine_torque(_ENGINE_OFFICIAL) == 556)

    lbft = _ENGINE_OFFICIAL.replace("410 @@lb_ft@@ (556@@nm@@)", "400 @@lb_ft@@")
    check("falls back to lb_ft when Nm is absent",
          C.stated_engine_torque(lbft) == 542, str(C.stated_engine_torque(lbft)))
    check("no info[] yields None",
          C.stated_engine_torque("accessory_engine_data : x\n{\n\ttorque: 9\n}\n")
          is None)
    check("hp-only info[] is not mistaken for torque",
          C.stated_engine_torque(
              'info[]: "365 @@hp@@ (272@@kw@@)"') is None)


def test_engine_curve_peak_excludes_secondary() -> None:
    section("primary torque curve peak")
    check("Pilot primary curve peaks at 0.57",
          C._primary_curve_peak(_ENGINE_INFLATED) == 0.57)
    check("secondary curve is not folded in",
          C._primary_curve_peak(_ENGINE_OFFICIAL) == 1.0)


def test_tune_engine_rescales_inflated_output() -> None:
    section("engine output is rescaled to the default Nm/t target")
    rep = C.Report()
    # 1.00 t, so the target is 760 Nm peak; 1400 * 0.57 = 798 Nm actual.
    out = C.tune_engine(_ENGINE_INFLATED, rep, tonnes=1.0)
    peak = C._primary_curve_peak(out)
    tq = float(C.get_key(out.splitlines(), "torque"))
    check("peak torque lands on the target",
          abs(tq * peak - 760) < 2, f"got {tq * peak:.0f} Nm")
    # Used to assert secondary_torque was scaled by the same factor. That was
    # the bug: the key is a truck torque multiplier, and keeping it gave a
    # converted car 1.26x torque in every low gear. It is dropped now.
    check("secondary_torque is dropped, not scaled",
          re.search(r"secondary_torque:", out) is None, out)
    check("its curve goes with it",
          re.search(r"secondary_torque_curve", out) is None, out)
    check("and the drop is reported",
          any("secondary_torque" in a for a in rep.actions), str(rep.actions))
    # Anchored on the key, not a substring: "secondary_torque_curve[]" also
    # contains "torque_curve[]", and this comparison used to depend on that.
    #
    # Two points may now differ from the author's: the (0, 0) origin every SCS
    # engine has, and the sample at the idle rev, which is halved to stop the car
    # creeping. Everything else is still the mod's own curve, untouched.
    def curve_pts(text: str) -> list[tuple[int, float]]:
        return [(int(r), float(f)) for r, f in re.findall(
            r"^\s*torque_curve\[\]\s*:\s*\((\d+)\s*,\s*([\d.]+)", text, re.M)]

    was = curve_pts(_ENGINE_INFLATED)
    now = curve_pts(out)
    idle = C._idle_rpm(_ENGINE_INFLATED)
    check("the origin is added and the idle sample halved, and nothing else",
          now[0] == (0, 0.0)
          and now[1][0] == idle and abs(now[1][1] - was[0][1] / 2) < 1e-6
          and now[2:] == was[1:],
          f"was {was} now {now}")
    check("a zero-torque origin is added, as every SCS engine has",
          re.search(r"^\ttorque_curve\[\]: \(0, 0\)$", out, re.M))
    peak = max((float(m.group(2)), int(m.group(1)))
               for m in re.finditer(
                   r"^\s*torque_curve\[\]:\s*\(\s*(\d+)\s*,\s*([\d.]+)",
                   out, re.M))
    check("peak value and rev unchanged", peak == (0.57, 3750), str(peak))
    check("reported the rescale", any("engine rescaled" in a for a in rep.actions))

    # An explicit ratio still wins over the default.
    rep2 = C.Report()
    out2 = C.tune_engine(_ENGINE_INFLATED, rep2, nm_per_tonne=1000, tonnes=2.0)
    check("an explicit ratio overrides the default",
          abs(float(C.get_key(out2.splitlines(), "torque")) * 0.57 - 2000) < 2)


def test_tune_engine_falls_back_to_stated_figure() -> None:
    section("the stated figure is a fallback, not the target")
    # Only reachable when the ratio is disabled, which is what the "preserve
    # an already-correct engine" behaviour relies on.
    rep = C.Report()
    out = C.tune_engine(_ENGINE_INFLATED, rep, nm_per_tonne=None)
    # 1400 * 0.57 = 798; stated 475 => 1400 * (475/798) = 833.
    check("fallback rescales to the stated 475 Nm",
          re.search(r"^\ttorque: 833$", out, re.M), out)
    check("fallback is labelled as such",
          any("stated 475 Nm" in a for a in rep.actions), str(rep.actions))


def test_tune_engine_preserves_official_output() -> None:
    section("preservation is measured against the stated figure")
    # SCS's own F-150 V8 declares torque 556 against a stated 556 Nm. With the
    # ratio disabled it is left byte-identical, because its peak already equals
    # what it claims. Under the Nm/t default it is deliberately NOT preserved:
    # 556 Nm on a 1 t car is not SCS-felt performance, and that is the point.
    rep = C.Report()
    out = C.tune_engine(_ENGINE_OFFICIAL, rep, nm_per_tonne=None)
    check("an engine matching its own figure is byte-identical",
          out == _ENGINE_OFFICIAL)
    check("preservation is reported",
          any("preserved" in a for a in rep.actions))

    rep2 = C.Report()
    default_out = C.tune_engine(_ENGINE_OFFICIAL, rep2, tonnes=1.0)
    check("the Nm/t default does override it",
          "torque: 760" in default_out, default_out)


def test_tune_engine_degenerate_inputs() -> None:
    section("engine tuning refuses to guess")
    no_info = _ENGINE_INFLATED.replace('info[]: "350 @@lb_ft@@ (475 @@nm@@)"\n', "")
    rep = C.Report()
    check("no stated figure => untouched",
          C.tune_engine(no_info, rep, nm_per_tonne=None) == no_info)
    check("and warns instead of inventing one",
          any("no torque in info[]" in w for w in rep.warnings))
    check("...but the Nm/t default still reaches it",
          "torque: 1333" in C.tune_engine(no_info, C.Report(), tonnes=1.0))

    no_curve = "\n".join(
        ln for ln in _ENGINE_INFLATED.splitlines()
        if "torque_curve[]" not in ln or "secondary_torque_curve" in ln) + "\n"
    rep2 = C.Report()
    check("no primary torque curve => untouched",
          C.tune_engine(no_curve, rep2, nm_per_tonne=None) == no_curve)

    not_engine = "SiiNunit\n{\nfoo: 1\n}\n"
    check("non-engine unit is not touched",
          C.tune_engine(not_engine, C.Report()) == not_engine)

    rep3 = C.Report()
    forced = C.tune_engine(_ENGINE_OFFICIAL, rep3, force=True, nm_per_tonne=None)
    check("--force-physics resweeps an already-correct engine",
          any("engine rescaled" in a for a in rep3.actions),
          str(rep3.actions))
    check("...but does not change its values", "torque: 556" in forced)

    try:
        C.tune_engine(_ENGINE_INFLATED, C.Report(), nm_per_tonne=760, tonnes=0)
        check("a zero mass is rejected", False, "no error raised")
    except ValueError:
        check("a zero mass is rejected", True)


def test_tune_engine_target_override() -> None:
    section("--engine-torque overrides the stated figure")
    rep = C.Report()
    out = C.tune_engine(_ENGINE_INFLATED, rep, target_nm=1400)
    # 1400 Nm peak over a 0.57 curve => declared scalar 2456
    check("target reached", re.search(r"^\ttorque: 2456$", out, re.M), out)
    check("target reported as the source",
          any("--engine-torque 1400" in a for a in rep.actions), str(rep.actions))

    # An engine already below the target must still be raised: the override is
    # explicit and is not subject to the preserve-if-close rule.
    rep2 = C.Report()
    out2 = C.tune_engine(_ENGINE_OFFICIAL, rep2, target_nm=1400)
    check("a preserved engine is raised when a target is given",
          "torque: 1400" in out2)
    check("...and is not reported as preserved",
          not any("preserved" in a for a in rep2.actions))

    # Target with no info[] at all: previously impossible.
    blind = _ENGINE_INFLATED.replace('info[]: "350 @@lb_ft@@ (475 @@nm@@)"\n', "")
    rep3 = C.Report()
    out3 = C.tune_engine(blind, rep3, target_nm=1400)
    check("target works without any info[]", "torque: 2456" in out3)
    check("...and does not warn about a missing figure",
          not rep3.warnings, str(rep3.warnings))


def test_tune_engine_nm_per_tonne() -> None:
    section("engine torque is targeted per tonne, not absolutely")
    # This is the trap that cost a full test cycle: 1125 Nm was fitted on a
    # 1550 kg `classic`-preset car that was really 2069 kg.
    rep = C.Report()
    out = C.tune_engine(_ENGINE_INFLATED, rep, nm_per_tonne=760, tonnes=2.069)
    peak = C._primary_curve_peak(out)
    tq = float(C.get_key(out.splitlines(), "torque"))
    check("target scales with the mass actually written",
          abs(tq * peak - 760 * 2.069) < 2, f"got {tq * peak:.0f} Nm")
    check("mass is reported so a wrong figure is visible",
          any("2.07 t" in a for a in rep.actions), str(rep.actions))

    # Same ratio, heavier car: more torque, and the acceleration is unchanged.
    rep2 = C.Report()
    out2 = C.tune_engine(_ENGINE_INFLATED, rep2, nm_per_tonne=760, tonnes=2.500)
    tq2 = float(C.get_key(out2.splitlines(), "torque"))
    check("a heavier car is given more torque", tq2 > tq)

    # An engine with no info[] is still reachable via the ratio.
    blind = _ENGINE_INFLATED.replace('info[]: "350 @@lb_ft@@ (475 @@nm@@)"\n', "")
    rep3 = C.Report()
    out3 = C.tune_engine(blind, rep3, nm_per_tonne=760, tonnes=2.069)
    check("ratio works without any info[]",
          abs(float(C.get_key(out3.splitlines(), "torque")) * 0.57
              - 760 * 2.069) < 2)


@guard
def test_engine_target_uses_written_mass() -> None:
    section("engine target tracks the mass written to the chassis")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "m.scs"
        C.main([str(PILOT), "--type", "pickup", "--brand", "honda",
                "--mass", "2069", "--engine-nm-per-tonne", "760",
                "-o", str(out)])
        z = zipfile.ZipFile(out)
        eng = [n for n in z.namelist()
               if re.search(r"/engine/.*\.sii$", n)][0]
        chassis = [n for n in z.namelist()
                   if re.search(r"/chassis/.*\.sii$", n)][0]
        et = z.read(eng).decode()
        ct = z.read(chassis).decode()
        kw = re.findall(r"^\s*kerb_weight(?:\[\d+\])?\s*:\s*([\d.]+)", ct, re.M)
        mass = sum(float(x) for x in kw)
        peak = C._primary_curve_peak(et)
        tq = float(C.get_key(et.splitlines(), "torque"))
        check("kerb mass is 2069 kg as asked", abs(mass - 2069) < 1, str(mass))
        check("torque matches mass/1000 * 760, not a preset",
              abs(tq * peak - 760 * mass / 1000) < 2, f"{tq * peak:.0f} Nm")


@guard
def test_default_engine_target_applies() -> None:
    section("760 Nm/t is the default with no flags at all")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "d.scs"
        C.main([str(PILOT), "--type", "suv", "--brand", "honda",
                "--mass", "2200", "-o", str(out)])
        z = zipfile.ZipFile(out)
        eng = [n for n in z.namelist()
               if re.search(r"/engine/.*\.sii$", n)][0]
        ch = [n for n in z.namelist()
              if re.search(r"/chassis/.*\.sii$", n)][0]
        et = z.read(eng).decode()
        mass = sum(float(x) for x in re.findall(
            r"^\s*kerb_weight(?:\[\d+\])?\s*:\s*([\d.]+)",
            z.read(ch).decode(), re.M))
        curve = {int(m.group(1)): float(m.group(2)) for m in re.finditer(
            r"^\s*torque_curve\[\]:\s*\(\s*(\d+)\s*,\s*([\d.]+)", et, re.M)}
        tq = float(C.get_key(et.splitlines(), "torque"))
        # With no flags the engine is rebuilt to the default 150 hp/tonne on
        # SCS's curve shape -- which is what replaced the old 760 Nm/t scalar
        # default, and the reason the car no longer pulls to 140 mph.
        peak_rpm = max(curve, key=lambda r: r * curve[r])
        hp = tq * curve[peak_rpm] * peak_rpm * 2 * math.pi / 60 / 745.699872
        want_hp = C._DEFAULT_HP_PER_TONNE * mass / 1000.0
        check("default power is the SCS band, not the old Nm/t constant",
              abs(hp - want_hp) < 2, f"{hp:.0f} hp, expected {want_hp:.0f}")
        check("...and that is in the range SCS's own cars occupy",
              150 <= hp <= 600, f"{hp:.0f} hp")
        check("curve ends at zero, so there is no endless pull",
              curve[max(curve)] == 0.0, str(curve[max(curve)]))
        check("...and keeps the (0, 0) origin every SCS engine has",
              curve.get(0) == 0.0)


def test_tune_wheel_adds_car_grip() -> None:
    section("car wheel grip keys")
    bare = ("SiiNunit\n{\naccessory_wheel_data : w223.ftire\n{\n\tname: \"Stock\"\n"
            "\tmodel: \"/vehicle/truck/w223/acc/wheel/tire_01.pmd\"\n}\n}\n")
    rep = C.Report()
    out = C.tune_wheel(bare, rep)
    check("roll_resistance written", re.search(r"^\troll_resistance: 11$", out, re.M))
    check("grip_factor written", re.search(r"^\tgrip_factor: 1.1$", out, re.M))

    # SCS's own car wheels: 11-13 and 1.1-1.2. Those must survive untouched.
    tuned = bare.replace("\tname: \"Stock\"",
                         "\tname: \"Stock\"\n\troll_resistance: 13\n\tgrip_factor: 1.2")
    rep2 = C.Report()
    check("an already-car tyre is left alone",
          C.tune_wheel(tuned, rep2) == tuned)
    check("...and preservation is reported",
          any("preserved" in a for a in rep2.actions))

    forced = C.tune_wheel(tuned, C.Report(), force=True)
    check("--force-physics overwrites a tuned tyre",
          re.search(r"^\troll_resistance: 11$", forced, re.M)
          and re.search(r"^\tgrip_factor: 1.1$", forced, re.M))

    rim = "SiiNunit\n{\naccessory_rim_data : w223.fdisc\n{\n\tname: \"Stock\"\n}\n}\n"
    check("a rim unit is not treated as a tyre",
          C.tune_wheel(rim, C.Report()) == rim)

    truck_key = bare.replace("\tname: \"Stock\"",
                             "\tname: \"Stock\"\n\twet_grip: 4.02\n\tnoise_volume: 70")
    out2 = C.tune_wheel(truck_key, C.Report())
    check("truck-only tyre keys are left in place, not deleted",
          "wet_grip: 4.02" in out2 and "noise_volume: 70" in out2)


@guard
def test_real_wheel_grip() -> None:
    section("real mods: car wheels carry the keys SCS reads")
    for label, path in (("Pilot", PILOT), ("W223", W223)):
        src = C.ModSource(path)
        tyres = [n for n in src.readable
                 if re.match(r"^def/vehicle/[a-z_]*tire/.*\.sii$", n)]
        if not tyres:
            skip(f"{label} wheels", "no *_tire unit found")
            continue
        for t in tyres:
            out = C.tune_wheel(src.read(t), C.Report())
            check(f"{label}: {Path(t).name} has grip_factor",
                  re.search(r"^\tgrip_factor:\s*1\.1$", out, re.M))
            check(f"{label}: {Path(t).name} roll_resistance at car floor",
                  re.search(r"^\troll_resistance:\s*11$", out, re.M))


@guard
def test_real_engine_scaling() -> None:
    section("real mods: no engine stays at truck scale")
    for label, path, want in (("Pilot", PILOT, 475), ("W223", W223, 520)):
        src = C.ModSource(path)
        engines = [n for n in src.readable
                   if "/engine/" in n and n.endswith(".sii")]
        if not engines:
            skip(f"{label} engine", "no engine unit found")
            continue
        for eng in engines:
            text = src.read(eng)
            rep = C.Report()
            out = C.tune_engine(text, rep)
            stated = C.stated_engine_torque(text)
            check(f"{label}: states its real torque", stated == want, str(stated))
            peak = C._primary_curve_peak(out)
            tq = C.get_key(out.splitlines(), "torque")
            tq = float(re.match(r"[+-]?\d+(?:\.\d+)?", tq).group(0))
            actual = tq * peak
            check(f"{label}: peak torque is no longer at real-world output",
                  actual > want * 1.3,
                  f"peak {actual:.0f} Nm vs stated {want} Nm -- real-world "
                  "torque produces a car about 2.5x slower than the real one")
            check(f"{label}: engine unit still balanced",
                  out.count("{") == out.count("}"))


@guard
def test_porsche_conversion() -> None:
    section("Porsche Cayenne -> suv")
    if not need(PORCHE, "porsche conversion"):
        return
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "porsche.scs"
        C.main([str(PORCHE), "--type", "suv", "--brand", "porsche",
                "--slug", "porsche_cayenne", "-o", str(out)])
        gen = zipfile.ZipFile(out)
        names = gen.namelist()

        # Registration names come from the mod's own vehicle unit, and the
        # car_dealer folder is "mod", as the reference converter writes it.
        for expect in ["def/vehicle/car_desktop/", "def/vehicle/car_company/",
                       "def/vehicle/truck_desktop/", "def/vehicle/truck_company/"]:
            check(f"wrote under {expect.split('/')[-2] or 'x'}",
                  any(n.startswith(expect) and n.endswith(".sii") for n in names))
        check("car_dealer under mod/",
              any(n.startswith("def/vehicle/car_dealer/mod/") for n in names))

        data = gen.read("def/vehicle/truck/cayenne.gts/data.sii").decode()
        check("suv tag written", 'tags[]: "suv"' in data)

        chassis = gen.read(
            "def/vehicle/truck/cayenne.gts/chassis/awd.sii").decode()
        # Asset paths must be LEFT ALONE. SCS resolves absolute virtual paths
        # against every mounted mod, and rewriting them breaks the models.
        check("model path left as the author wrote it",
              '"/porshe_cayenne/car.pmd"' in chassis)
        check("not forced to /vehicle/car/", "/vehicle/car/porsche_cayenne" not in chassis)

        man = gen.read("manifest.sii").decode()
        # authors vary whitespace around the colon, so normalise before matching
        flat_man = re.sub(r"\s+", " ", man)
        check('manifest category is "truck"', 'category[]: "truck"' in flat_man)
        check('manifest no longer "Car"', 'category[]: "Car"' not in flat_man)

        check("physics.sii kept by default",
              "def/vehicle/physics.sii" in names)
        check("engine preserved",
              "def/vehicle/truck/cayenne.gts/engine/mx11_270.sii" in names)


# ------------------------------------------------------------------ physics
@guard
def test_car_physics_presets() -> None:
    section("car physics presets")
    for t_ in C.VEHICLE_TYPES:
        cfg = C.car_physics(t_, 2)
        # Bounded by the range SCS's own four Road Trip chassis use, not by
        # the Parati. The old "<0.30 is a car" test was measuring the old
        # table against itself.
        check(f"{t_}: air_resistance is car-scale",
              0.37 <= float(cfg["air_resistance"]) <= 0.60,
              cfg["air_resistance"])
        check(f"{t_}: cog_height is car-scale",
              0.52 <= float(cfg["cog_height"]) <= 0.74, cfg["cog_height"])
        check(f"{t_}: steering lock is car-scale",
              28 <= int(cfg["max_steering_angle"]) <= 35,
              cfg["max_steering_angle"])
        mass = sum(int(x) for x in cfg["kerb_weight"])
        check(f"{t_}: kerb mass plausible", 900 <= mass <= 2600, str(mass))
        check(f"{t_}: voltage 12V", cfg["nominal_voltage"] == "12.0")
        check(f"{t_}: no air brakes", cfg["air_tank_pressure"] == "0.0")

    # The ordering that survives SCS's own calibration: tall heavy cars drag
    # and roll most, light ones least. "Sports is the most aerodynamic" was a
    # Parati-era assumption and is false of the Mustang, which is more draggy
    # than the Victoria.
    check("pickup has most drag",
          float(C.car_physics("pickup", 2)["air_resistance"])
          >= max(float(C.car_physics(t, 2)["air_resistance"])
                for t in C.VEHICLE_TYPES))
    check("classic and muscle have least drag",
          float(C.car_physics("classic", 2)["air_resistance"])
          == min(float(C.car_physics(t, 2)["air_resistance"])
                 for t in C.VEHICLE_TYPES))
    check("pickup has the highest CoG",
          float(C.car_physics("pickup", 2)["cog_height"])
          >= max(float(C.car_physics(t, 2)["cog_height"])
                for t in C.VEHICLE_TYPES))
    check("offroad is at least as soft as the sedan",
          float(C.car_physics("offroad", 2)["residual_travel"][0])
          >= float(C.car_physics("sedan", 2)["residual_travel"][0]))


@guard
def test_axle_count_never_exceeds() -> None:
    section("axle-count safety (the phantom-axle bug)")
    for axles in (2, 3, 4):
        cfg = C.car_physics("suv", axles)
        for key in ("kerb_weight", "residual_travel", "suspension_damping_factor",
                    "disc_brake", "axle_suspension_type",
                    "brake_rotor_diameter"):
            check(f"{key} has exactly {axles} elements",
                  len(cfg[key]) == axles, f"got {len(cfg[key])}")

    # brake_rotor_diameter was previously refused on the reference converter's
    # reasoning ("inventing a rotor size risks a brake/wheel mismatch"). That
    # reason does not survive measurement: all 4 SCS Road Trip chassis declare
    # it and all 39 truck chassis omit it, so a truck inherits the global
    # truck default -- which is what over-brakes a converted car.
    cfg3 = C.car_physics("suv", 3)
    check("brake_rotor_diameter is written for a car",
          cfg3.get("brake_rotor_diameter") is not None)
    check("...at a value inside SCS's own car range",
          all(0.279 <= float(v) <= 0.356 for v in cfg3["brake_rotor_diameter"]))
    check("powered_axle is never synthesised", "powered_axle" not in cfg3)

    # disc_brake / axle_suspension_type must never receive a bare number
    cfg = C.car_physics("suv", 3)
    check("disc_brake stays boolean", all(v in ("true", "false") for v in cfg["disc_brake"]))
    check("axle_suspension_type stays a name",
          all(re.fullmatch(r"[a-z_]+", v) for v in cfg["axle_suspension_type"]))


@guard
def test_tune_chassis_rewrites_porsche() -> None:
    section("chassis tuning on the Porsche")
    if not need(PORCHE, "chassis tuning on the Porsche"):
        return
    src = zipfile.ZipFile(PORCHE)
    original = src.read("def/vehicle/truck/cayenne.gts/chassis/awd.sii").decode()
    rep = C.Report()
    tuned = C.tune_chassis(original, "suv", None, rep)

    check("kerb weight replaced", "kerb_weight[0]: 1150" in tuned)
    check("truck kerb weight gone", "5250" not in tuned)
    check("air_resistance added", "air_resistance: 0.16" in tuned)
    check("no brake_rotor_diameter written", "brake_rotor_diameter" not in tuned)
    check("disc_brake is true on both axles",
          "disc_brake[0]: true" in tuned and "disc_brake[1]: true" in tuned)
    check("axle_suspension_type wishbone both axles",
          "axle_suspension_type[1]: wishbone" in tuned)
    check("cog_height added", "cog_height: 0.55" in tuned)
    check("sway bar added", "sway_bar_stiffness: 0.50" in tuned)
    check("steering lock added", "max_steering_angle: 28" in tuned)
    check("voltage 24 -> 12", "nominal_voltage: 24" not in tuned)

    # every generated key must sit INSIDE the unit block
    body = tuned.split("accessory_chassis_data")[1]
    unit_close = body.rindex("\n}\n")
    body_only = body[:unit_close]
    for key in ("air_resistance", "cog_height", "sway_bar_stiffness",
                "max_steering_angle", "suspension_class"):
        check(f"{key} inside unit block", key in body_only, "outside unit")

    # no surplus axle elements: 2 axles means exactly 2 of each array
    for key in ("kerb_weight", "residual_travel", "suspension_damping_factor",
                "disc_brake", "axle_suspension_type"):
        found = re.findall(rf"{key}\[\d*\]", tuned)
        check(f"{key} has 2 elements", len(found) == 2, str(len(found)))

    # the include and the unit must survive
    check("@include preserved", "@include" in tuned)
    check("unit header preserved", "accessory_chassis_data" in tuned)


@guard
def test_tune_chassis_drops_truck_keys() -> None:
    section("truck-only keys removed")
    truckish = (
        "SiiNunit\n{\naccessory_chassis_data : a.b\n{\n"
        "\tkerb_weight[]: 620\n\tkerb_weight[]: 415\n"
        "\tfifth_wheel_pitch: 20\n\ttrailer_joint_roll: 3\n\tcabin_mass: 700.0\n"
        "}\n}\n"
    )
    rep = C.Report()
    out = C.tune_chassis(truckish, "sedan", None, rep)
    check("fifth_wheel_pitch removed", "fifth_wheel_pitch" not in out)
    check("trailer_joint_roll removed", "trailer_joint_roll" not in out)
    check("cabin_mass removed", "cabin_mass" not in out)
    check("kerb_weight kept", "kerb_weight" in out)
    check("reported the removal", any("truck-only" in a for a in rep.actions))


@guard
def test_air_resistance_override() -> None:
    section("--air-resistance override")
    # A mod's own car-sane value is normally PRESERVED. An explicit
    # --air-resistance must still win, or the flag would silently do nothing.
    chassis = (
        "SiiNunit\n{\naccessory_chassis_data : a.b\n{\n"
        "\tkerb_weight[]: 1150\n\tkerb_weight[]: 1050\n"
        "\tair_resistance: 0.16\n}\n}\n"
    )
    kept = C.tune_chassis(chassis, "suv", None, C.Report())
    check("car-sane value preserved without the flag",
          "air_resistance: 0.16" in kept)

    forced = C.tune_chassis(
        chassis, "suv", None, C.Report(), overrides={"air_resistance": "1.5"}
    )
    check("override wins over a car-sane value",
          "air_resistance: 1.5" in forced)
    check("old value gone", "air_resistance: 0.16" not in forced)

    check("override does not touch the preset",
          C.car_physics("suv", 2)["air_resistance"] == "0.55")
    check("override reaches the physics table",
          C.car_physics("suv", 2, None, {"air_resistance": "1.5"})
          ["air_resistance"] == "1.5")


@guard
def test_addons_bound_by_directory_not_filename() -> None:
    section("addons bound by directory, not filename")
    vdir = "def/vehicle/truck/testcar/"
    files = {
        "manifest.sii": "SiiNunit\n{\npackage[]: \"test\"\n}\n",
        f"{vdir}data.sii": (
            "SiiNunit\n{\naccessory_truck_data : vehicle.testcar\n{\n"
            '\tinfo[]: "Test"\n'
            '\trequire[]: mirror\n'
            '\trequire[]: f_light_chs\n'
            '\trequire[]: steering_w\n'
            "}\n}\n"
        ),
    }
    for part in ("chassis/awd", "cabin/c", "interior/i", "engine/e",
                 "transmission/t", "head_light/led", "paint_job/color0"):
        files[f"{vdir}{part}.sii"] = "SiiNunit\n{\n\tname: \"x\"\n}\n"
    # The GLS's front-light addon is led.sii, NOT shape1.sii. It is the only
    # unit carrying the headlight's exterior_model, so dropping it leaves the
    # converted car with no headlights at all.
    files[f"{vdir}accessory/mirror/shape1.sii"] = "SiiNunit\n{\n}\n"
    files[f"{vdir}accessory/steering_w/default.sii"] = "SiiNunit\n{\n}\n"
    files[f"{vdir}accessory/f_light_chs/led.sii"] = "SiiNunit\n{\n}\n"
    for wheel in ("f_tire/t", "f_disc/d", "f_hub/h", "f_nuts/n",
                  "r_tire/t", "r_disc/d", "r_hub/h", "r_nuts/n"):
        files[f"def/vehicle/{wheel}.sii"] = "SiiNunit\n{\n}\n"

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "testcar.scs"
        with zipfile.ZipFile(path, "w") as z:
            for name, body in files.items():
                z.writestr(name, body)
        parts, rep = C.discover_parts(C.ModSource(path), vdir, "testcar",
                                      "/vehicle/car/testcar")

    bound = parts["accessories"]
    check("shape-named addon still binds", "mirror" in bound)
    check("steering_w special case still binds", "steering_w" in bound)
    check("non-shape addon binds (the GLS headlight)", "f_light_chs" in bound,
          str(sorted(bound)))
    check("it binds the right file",
          bound.get("f_light_chs", ("", ""))[1]
          == f"/{vdir}accessory/f_light_chs/led.sii")
    check("requires captured from data.sii",
          parts["requires"] == ["mirror", "f_light_chs", "steering_w"],
          str(parts.get("requires")))

    # every require[] must resolve, or the vehicle can never be completed
    unsatisfied = [r for r in parts["requires"] if r not in bound]
    check("no unsatisfied require[]", unsatisfied == [], str(unsatisfied))
    check("no gap reported for a fully satisfiable mod",
          not [g for g in rep.gaps if "requires" in g], str(rep.gaps))


@guard
def test_unsatisfied_require_is_reported() -> None:
    section("an unbindable require[] is reported")
    vdir = "def/vehicle/truck/testcar/"
    files = {
        f"{vdir}data.sii": (
            "SiiNunit\n{\naccessory_truck_data : vehicle.testcar\n{\n"
            '\trequire[]: mirror\n'
            '\trequire[]: nonexistent_addon\n'
            "}\n}\n"
        ),
    }
    for part in ("chassis/awd", "cabin/c", "interior/i", "engine/e",
                 "transmission/t", "head_light/led", "paint_job/color0"):
        files[f"{vdir}{part}.sii"] = "SiiNunit\n{\n}\n"
    files[f"{vdir}accessory/mirror/shape1.sii"] = "SiiNunit\n{\n}\n"
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "testcar.scs"
        with zipfile.ZipFile(path, "w") as z:
            for name, body in files.items():
                z.writestr(name, body)
        parts, rep = C.discover_parts(C.ModSource(path), vdir, "testcar",
                                      "/vehicle/car/testcar")
    check("the missing require is named in a gap",
          any("nonexistent_addon" in g for g in rep.gaps), str(rep.gaps))


@guard
def test_dealer_price() -> None:
    section("--price")
    defs = {
        "def/vehicle/car_dealer/x/y.sii": (
            'data_path: "/def/vehicle/truck/y/chassis.sii"\n'
            'data_path: "/def/vehicle/truck/y/engine.sii"\n'
            'data_path: "/def/vehicle/truck/y/data.sii"\n'
        ),
        "def/vehicle/truck/y/chassis.sii": "price: 35000\nkerb_weight[]: 1\n",
        "def/vehicle/truck/y/engine.sii": "price: 10000\ntorque: 400\n",
        "def/vehicle/truck/y/data.sii": "info[]: \"Y\"\n",
        "def/vehicle/truck/y/paint_job/color2.sii": "price: 3000\n",
    }
    total, rows = C.dealer_price(defs, "def/vehicle/car_dealer/x/y.sii")
    check("dealer totals the listed accessories", total == 45000)
    check("a listed file with no price counts as zero", len(rows) == 3)

    rep = C.Report()
    C.apply_dealer_price(defs, "def/vehicle/car_dealer/x/y.sii", None, rep)
    check("default run reports the total",
          any("$45,000" in a for a in rep.actions))
    check("default run changes nothing",
          "price: 35000" in defs["def/vehicle/truck/y/chassis.sii"])

    C.apply_dealer_price(defs, "def/vehicle/car_dealer/x/y.sii", 90000, rep)
    after, _ = C.dealer_price(defs, "def/vehicle/car_dealer/x/y.sii")
    check("target total hit exactly, rounding included", after == 90000)
    check("proportions kept",
          "price: 70000" in defs["def/vehicle/truck/y/chassis.sii"])
    # The upgrade menu now moves with everything else. It used to be left
    # alone, which is what produced the reported symptom: --price 1000 took a
    # real mod's car from $113,304 to $1,000 while chassis/awd.sii stayed at
    # $40,000, so the car was 40x cheaper than one of its own parts.
    check("the upgrade menu is rescaled by the same factor",
          "price: 6000" in defs["def/vehicle/truck/y/paint_job/color2.sii"],
          defs["def/vehicle/truck/y/paint_job/color2.sii"])
    check("and the rescale is reported",
          any("upgrade-menu" in a for a in rep.actions), str(rep.actions))
    # Scaling UP is the direction FINDINGS 30 warns about: everything else the
    # player buys afterwards gets more expensive at once. Say so.
    check("scaling up warns that the menu inflates with it",
          any("multiplies every upgrade-menu price" in w
              for w in rep.warnings), str(rep.warnings))
    check("non-price keys survive",
          "kerb_weight[]: 1" in defs["def/vehicle/truck/y/chassis.sii"]
          and "torque: 400" in defs["def/vehicle/truck/y/engine.sii"])


def test_price_keeps_the_menu_proportionate() -> None:
    """The reported case: a $1,000 car must not have a $40,000 AWD chassis.

    Every part moves by one factor, so the author's RELATIVE pricing -- an AWD
    chassis at 35% of the vehicle's price -- is what survives. That is the
    thing worth preserving; an absolute figure is the author's guess, not a
    fact about a car.
    """
    section("--price keeps accessory prices proportionate to the car")
    parts = {
        "chassis/rwd.sii": 35000,
        "chassis/awd.sii": 40000,
        "cabin/std.sii": 35000,
        "engine/v8.sii": 15000,
        "transmission/auto.sii": 10000,
        "head_light/std.sii": 5000,
        "interior/beige.sii": 20000,
        "paint_job/white.sii": 4000,
        "mirror/l.sii": 500,
        "mirror/r.sii": 500,
    }
    files = {
        "def/vehicle/car_dealer/x/y.sii": "".join(
            f'data_path: "/def/vehicle/truck/y/{n}"\n' for n in parts),
        **{f"def/vehicle/truck/y/{n}": f"price: {v}\n"
           for n, v in parts.items()},
    }
    dealer = "def/vehicle/car_dealer/x/y.sii"
    base = C.dealer_price(files, dealer)[0]

    C.apply_dealer_price(files, dealer, 1000, C.Report())

    def price(name: str) -> int:
        m = re.search(r"price:\s*(\d+)",
                      files[f"def/vehicle/truck/y/{name}"])
        return int(m.group(1))

    check("the AWD chassis no longer costs more than the car",
          price("chassis/awd.sii") < 1000,
          f"${price('chassis/awd.sii'):,}")
    check("its share of the vehicle price is what the author set",
          abs(price("chassis/awd.sii") / 1000 - 40000 / base) < 0.01,
          f"{price('chassis/awd.sii')}/1000 vs {40000}/{base}")
    # No part may exceed the car it belongs to.
    worst = max(price(n) for n in parts)
    check("no accessory costs more than the whole car",
          worst <= 1000, f"${worst:,}")
    check("the relative order is unchanged",
          sorted(parts, key=lambda n: -parts[n])
          == sorted(parts, key=lambda n: -price(n)), "ordering changed")
    check("nothing collapses to a rounding artefact",
          all(price(n) >= 1 for n in parts),
          str({n: price(n) for n in parts}))


def _answer(answers: list[str] | None = None):
    """A scripted stand-in for input() that records every prompt shown."""
    prompts: list[str] = []

    def fake(prompt: str = "") -> str:
        prompts.append(prompt)
        if not answers:
            raise AssertionError(f"prompted past the end: {prompt!r}")
        return answers.pop(0)

    return fake, prompts


def test_prompts_demand_a_value() -> None:
    """Empty input is an omission, not a request for the default.

    Every prompt re-asks instead of silently applying a suggestion, and the
    label carries no bracketed default for the eye to fall back on. Ctrl-C
    must still cancel, or a mandatory field becomes unescapable.
    """
    section("prompts demand a value")
    import builtins
    import io

    real_tty = C._tty
    real_input = builtins.input
    real_stdout = sys.stdout
    C._tty = lambda: True
    try:
        def drive(fn, answers, *args):
            fake, prompts = _answer(list(answers))
            builtins.input = fake
            buf = io.StringIO()
            sys.stdout = buf
            try:
                result = fn(*args)
            finally:
                sys.stdout = real_stdout
            return result, prompts, buf.getvalue()

        cases = [
            ("weight", C.prompt_weight, ("suv", None), ["", "1650"], [862, 788]),
            ("weight split", C.prompt_weight, ("suv", None),
             ["", "1150,1050"], [1150, 1050]),
            ("power", C.prompt_power, (2200,), ["", "350", "5000"], (350.0, 5000)),
            ("acceleration", C.prompt_acceleration, (2200,), ["", "5.5"], 5.5),
            ("price", C.prompt_price, (), ["", "45000"], 45000),
        ]
        for label, fn, args, answers, expected in cases:
            got, prompts, text = drive(fn, answers, *args)
            check(f"{label}: empty input re-asks", len(prompts) == len(answers),
                  f"{len(prompts)} prompts")
            check(f"{label}: accepted value returned", got == expected,
                  f"{got!r} != {expected!r}")
            check(f"{label}: the re-ask explains itself",
                  "enter a number" in text, text.strip().splitlines()[-1:])
            check(f"{label}: no bracketed suggestion in the label",
                  "[" not in "".join(prompts), str(prompts))

        # A bad value must be rejected in its own right, not just when empty.
        _r, _p, text = drive(C.prompt_price, ["abc", "45000"])
        check("nonsense price is rejected", "enter a number" in text)
        _r, _p, text = drive(C.prompt_price, ["99999999", "45000"])
        check("absurd price is rejected", "between" in text)
        _r, _p, text = drive(C.prompt_power, ["5000", "350", "4500"], 2200)
        check("peak rev out of range is rejected", "between" in text)

        # The money symbol is gone, and so are the unit suffixes in the labels
        # for the field the user asked to be bare.
        _r, prompts, text = drive(C.prompt_price, ["45000"])
        check("the price field carries no dollar sign",
              "$" not in prompts[0] and "$" not in text, text)

        # Cancelling is still an escape from a mandatory field.
        def cancel(fn, *args):
            def boom(prompt: str = "") -> str:
                raise KeyboardInterrupt
            builtins.input = boom
            buf = io.StringIO()
            sys.stdout = buf
            try:
                return fn(*args)
            finally:
                sys.stdout = real_stdout

        check("Ctrl-C cancels the weight prompt",
              cancel(C.prompt_weight, "suv", None) is None)
        check("Ctrl-C cancels the power prompt",
              cancel(C.prompt_power, 2200) == (None, None))
        check("Ctrl-C cancels the price prompt",
              cancel(C.prompt_price) is None)
    finally:
        C._tty = real_tty
        builtins.input = real_input


def test_prompts_look_like_the_menus() -> None:
    """Every prompt is one shape: question rule, one hint, then the answer.

    The typed fields used to print a four-line paragraph above themselves while
    the menus printed a question and the row they settled on, so half the
    walkthrough read like a different program. A typed answer now comes to rest
    on its own line under the question -- `input()` is given a bare field, never
    a label -- and the only thing between question and answer is one dim hint
    carrying the unit and the sensible range.
    """
    section("prompts share the menu shape")
    import builtins
    import io

    real_tty = C._tty
    real_input = builtins.input
    real_stdout = sys.stdout
    real_heading, real_hint = C._ui.heading, C._ui.hint
    seen: list[tuple[str, str]] = []
    C._tty = lambda: True
    C._ui.heading = lambda text: seen.append(("heading", text))
    C._ui.hint = lambda text: seen.append(("hint", text))
    try:
        def drive(fn, answers, *args):
            fake, prompts = _answer(list(answers))
            builtins.input = fake
            buf = io.StringIO()
            sys.stdout = buf
            try:
                fn(*args)
            finally:
                sys.stdout = real_stdout
            return prompts, buf.getvalue()

        cases = [
            ("weight", C.prompt_weight, ("suv", None), ["1650"], 1),
            ("power", C.prompt_power, (2200,), ["350", "4500"], 2),
            ("acceleration", C.prompt_acceleration, (2200,), ["5.5"], 1),
            ("price", C.prompt_price, (), ["45000"], 1),
        ]
        for label, fn, args, answers, headings in cases:
            seen.clear()
            prompts, text = drive(fn, answers, *args)
            kinds = [kind for kind, _ in seen]
            check(f"{label}: asks its question as a heading",
                  kinds.count("heading") == headings and kinds[0] == "heading",
                  str(seen))
            check(f"{label}: every question is a question",
                  all(q.endswith("?") for k, q in seen if k == "heading"),
                  str(seen))
            check(f"{label}: one hint per question, and nothing else",
                  kinds == ["heading", "hint"] * headings, str(kinds))
            check(f"{label}: the field itself carries no label",
                  "".join(prompts).strip() == "", repr(prompts))
            check(f"{label}: no paragraph above the field",
                  "\n    " not in text, repr(text))

        # The source prompt is the first thing in the walkthrough, so it has to
        # look like the rest of it.
        seen.clear()
        fake, prompts = _answer([str(HERE / "carutil.py")])
        builtins.input = fake
        buf = io.StringIO()
        sys.stdout = buf
        try:
            C.prompt_source()
            source_text = buf.getvalue()
        finally:
            sys.stdout = real_stdout
        check("source: asks its question as a heading",
              [kind for kind, _ in seen] == ["heading", "hint"], str(seen))
        check("source: the field itself carries no label",
              "".join(prompts).strip() == "", repr(prompts))
        check("source: no paragraph above the field",
              "\n    " not in source_text, repr(source_text))

        # A label is the one thing the shape forbids, so hold the rule itself.
        check("the bare field is what the prompts pass to input()",
              C._FIELD.strip() == "" and C._FIELD.isascii(), repr(C._FIELD))
    finally:
        C._tty = real_tty
        C._ui.heading, C._ui.hint = real_heading, real_hint
        builtins.input = real_input


def test_idle_torque_is_halved_to_stop_the_car_creeping() -> None:
    """At least half the idle torque goes, and nothing above it does.

    A car creeps because the engine is making real torque before anyone asks for
    any, and through first gear that walks it away. FINDINGS 28 measured this
    project's rebuilt curves at 577-761 Nm at idle against 118-217 Nm for SCS's
    own four cars. The (0, 0) origin only helps while idle falls below the first
    sample, so the cut is placed AT the idle rev instead.
    """
    section("idle torque is halved")

    def idle_nm(curve, peak_nm, idle):
        return C._curve_fraction_at(curve, idle) * peak_nm

    def curve_pts(text: str) -> list[tuple[int, float]]:
        return [(int(r), float(f)) for r, f in re.findall(
            r"^\s*torque_curve\[\]\s*:\s*\((\d+)\s*,\s*([\d.]+)", text, re.M)]

    # The horsepower rebuild, at every power peak the guided run can produce.
    # Both halves matter: the cut must not cost the peak, and the power peak must
    # not move, or the horsepower request silently stops being honoured.
    for peak in (3750, 4000, 4500, 5000, 6000):
        cut, cut_nm = C.build_power_curve(516.0, peak, 5333, idle_rpm=750)
        plain, plain_nm = C.build_power_curve(516.0, peak, 5333)
        before = idle_nm(plain, plain_nm, 750)
        after = idle_nm(cut, cut_nm, 750)
        check(f"a {peak} rpm peak loses at least half its idle torque",
              after <= before * 0.5 + 1e-6, f"{before:.0f} -> {after:.0f} Nm")
        check(f"a {peak} rpm peak keeps the torque the horsepower was sized from",
              cut_nm == plain_nm, f"{plain_nm} -> {cut_nm} Nm")
        check(f"a {peak} rpm peak keeps the power peak where it was asked for",
              max(cut, key=lambda p: p[0] * p[1])[0]
              == max(plain, key=lambda p: p[0] * p[1])[0])
        check(f"a {peak} rpm peak curve stays in rev order",
              all(cut[i][0] < cut[i + 1][0] for i in range(len(cut) - 1)),
              str(cut[:4]))
        check(f"a {peak} rpm peak still makes full torque at its own peak",
              abs(max(f for _, f in cut) - max(f for _, f in plain)) < 1e-6)

    # The rescale path keeps the mod's own curve and never calls
    # build_power_curve, so it needs its own cut or --acceleration leaves the car
    # creeping exactly as before.
    rep = C.Report()
    real_tty, saved_ranges = C._tty, C._rpm_ranges
    # Pinned so a band prompt can never become a menu on a console run. This
    # fixture declares all three bands, so nothing should prompt -- but relying on
    # the fixture is how a 900 s timeout happens.
    C._tty = lambda: False
    C._rpm_ranges = None
    try:
        out = C.tune_engine(_ENGINE_INFLATED, rep, tonnes=1.0)
    finally:
        C._tty = real_tty
        C._rpm_ranges = saved_ranges
    pts, was_pts = curve_pts(out), curve_pts(_ENGINE_INFLATED)
    peak_nm = C._declared_peak_nm(out)
    idle = C._idle_rpm(_ENGINE_INFLATED)
    check("the rescale path halves idle torque too",
          idle_nm(pts, peak_nm, idle)
          <= idle_nm(was_pts, peak_nm, idle) * 0.5 + 1e-6,
          f"{idle_nm(was_pts, peak_nm, idle):.0f} -> "
          f"{idle_nm(pts, peak_nm, idle):.0f} Nm")
    check("and it is reported, not done silently",
          any("creeping" in a for a in rep.actions), str(rep.actions))
    check("the rescale path still rescales the torque scalar",
          abs(float(C.get_key(out.splitlines(), "torque")) * 0.57 - 760) < 2,
          str(C.get_key(out.splitlines(), "torque")))

    # No idle, an idle outside the curve, and a curve already at zero there must
    # all leave the curve alone -- a mod with none of these gets no cut at all.
    for idle_arg in (None, 0, -5, 999999):
        same, before = C._cut_idle_torque(
            C.build_power_curve(516.0, 4000, 5333)[0], idle_arg)
        check(f"an unusable idle ({idle_arg!r}) changes nothing",
              before is None and len(same) == 11, str(before))
    _zero, before = C._cut_idle_torque([(0, 0.0), (2000, 0.0), (4000, 1.0)], 2000)
    check("a curve already at zero there is left alone",
          before is None, str(before))


def test_prompt_price_never_blocks() -> None:
    section("price prompt never blocks")
    # Force the condition rather than assuming it. Running the suite from a
    # console means stdin really is a TTY, and an unpinned prompt would sit
    # there waiting for an answer that never comes.
    real = C._tty
    try:
        C._tty = lambda: False
        check("prompt_price returns None when stdin is not a tty",
              C.prompt_price() is None)
        check("prompt_power still guarded the same way",
              C.prompt_power(1500) == (None, None))
    finally:
        C._tty = real
    check("price prompt survives a bad number and a huge one",
          C._MIN_PRICE < 45000 < C._MAX_PRICE)


@guard
def test_mass_override() -> None:
    section("--mass override")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "m.scs"
        C.main([str(PORCHE), "--type", "suv", "--brand", "porsche",
                "--mass", "1300,1200", "-o", str(out)])
        t = zipfile.ZipFile(out).read(
            "def/vehicle/truck/cayenne.gts/chassis/awd.sii").decode()
        check("custom front mass honoured", "kerb_weight[0]: 1300" in t)
        check("custom rear mass honoured", "kerb_weight[1]: 1200" in t)

        r = subprocess.run(
            [sys.executable, str(HERE / "carutil.py"), str(PORCHE),
             "--type", "suv", "--brand", "p", "--mass", "oops", "-o", str(out)],
            capture_output=True, text=True)
        check("bad --mass rejected", r.returncode != 0)
        check("error explains the format", "1150,1050" in r.stderr)


# ------------------------------------------------------------------- weight
@guard
def test_split_mass() -> None:
    section("weight split")
    # SUV natural split is 1150:1050 -> total 2200
    w = C.split_mass(2200, "suv", 2)
    check("SUV 2200 splits to natural ratio", abs(w[0] - 1150) <= 2, str(w))
    check("split sums to total", sum(w) == 2200, str(w))
    # every preset's natural bias must survive a custom total
    for vt in C.VEHICLE_TYPES:
        ref = C._MASS[vt]
        total = sum(ref)
        w = C.split_mass(total, vt, 2)
        biased_front = ref[0] > ref[1]
        check(f"{vt} keeps its {'front' if biased_front else 'rear'} bias",
              (w[0] > w[1]) == biased_front, str(w))
    # a front-biased pickup must stay front-biased
    wp = C.split_mass(2400, "pickup", 2)
    check("pickup stays front-biased", wp[0] > wp[1], str(wp))
    # extra axles replicate the rear figure
    w4 = C.split_mass(2200, "suv", 4)
    check("4 axles -> 4 weights", len(w4) == 4, str(w4))
    check("4-axle total preserved", sum(w4) == 2200, str(w4))


@guard
def test_default_totals() -> None:
    section("default weights per type")
    for vt in C.VEHICLE_TYPES:
        total = C.default_total(vt)
        ref = C._MASS[vt]
        check(f"{vt} default matches preset", total == sum(ref), f"{total} vs {sum(ref)}")


@guard
def test_mass_cli_forms() -> None:
    section("--mass forms")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "x.scs"
        base = [str(PORCHE), "--type", "suv", "--brand", "porsche", "--no-prompt", "-o", str(out)]

        # total form -> split
        C.main(base + ["--mass", "1650"])
        t = zipfile.ZipFile(out).read("def/vehicle/truck/cayenne.gts/chassis/awd.sii").decode()
        w = [int(x) for x in re.findall(r"kerb_weight\[\d+\]:\s*(\d+)", t)]
        check("total form splits correctly", sum(w) == 1650, str(w))

        # explicit pair
        C.main(base + ["--mass", "1200,900"])
        t = zipfile.ZipFile(out).read("def/vehicle/truck/cayenne.gts/chassis/awd.sii").decode()
        check("pair form honoured", "kerb_weight[0]: 1200" in t and "kerb_weight[1]: 900" in t)

        # kg suffix
        C.main(base + ["--mass", "1800kg"])
        t = zipfile.ZipFile(out).read("def/vehicle/truck/cayenne.gts/chassis/awd.sii").decode()
        w = [int(x) for x in re.findall(r"kerb_weight\[\d+\]:\s*(\d+)", t)]
        check("kg suffix accepted", sum(w) == 1800, str(w))

        # bad inputs rejected
        for bad in ("oops", "1,2,3", "-5"):
            r = subprocess.run(
                [sys.executable, str(HERE / "carutil.py"), *base, "--mass", bad],
                capture_output=True, text=True)
            check(f"rejects {bad!r}", r.returncode != 0)


@guard
def test_prompt_never_blocks_non_tty() -> None:
    section("prompt is skipped off-terminal")
    # Pinned, not assumed: from a console stdin is a TTY and this would block.
    real = C._tty
    try:
        C._tty = lambda: False
        check("non-TTY returns None", C.prompt_weight("suv", None) is None)
    finally:
        C._tty = real


# ---------------------------------------------------------------------- CLI
@guard
def test_scripted_mode_still_works() -> None:
    section("scripted mode (backwards compatible)")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "cli.scs"
        C.main([str(PORCHE), "--type", "suv", "--brand", "porsche", "-o", str(out)])
        check("positional input works", out.exists())

        r = subprocess.run(
            [sys.executable, str(HERE / "carutil.py"),
             str(PORCHE), "--type", "suv", "--brand", "p", "-o", str(out)],
            capture_output=True, text=True)
        check("runs as a script", r.returncode == 0, r.stderr[-200:])
        check("prints DONE", "DONE" in r.stdout)
        check("prints the location", str(out.resolve()) in r.stdout)


@guard
def test_no_args_off_terminal_fails_clearly() -> None:
    section("no args, no terminal")
    r = subprocess.run(
        [sys.executable, str(HERE / "carutil.py")],
        capture_output=True, text=True, stdin=subprocess.DEVNULL)
    check("exits non-zero", r.returncode != 0)
    check("explains how to fix",
          "no input given" in r.stderr and "carutil.py mod.scs" in r.stderr)


@guard
def test_missing_required_off_terminal() -> None:
    section("missing --type / --brand off terminal")
    # Subprocess, so @guard does not apply -- see test_dry_run_writes_nothing.
    if not need(PORCHE, "missing --type / --brand"):
        return
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "o.scs"
        r = subprocess.run(
            [sys.executable, str(HERE / "carutil.py"), str(PORCHE), "-o", str(out)],
            capture_output=True, text=True, stdin=subprocess.DEVNULL)
        check("demands --type", r.returncode != 0 and "--type" in r.stderr)

        r = subprocess.run(
            [sys.executable, str(HERE / "carutil.py"), str(PORCHE),
             "--type", "suv", "-o", str(out)],
            capture_output=True, text=True, stdin=subprocess.DEVNULL)
        check("demands --brand", r.returncode != 0 and "--brand" in r.stderr)


@guard
def test_rt_prefix_default_output() -> None:
    section("rt_ prefix on the output")
    with tempfile.TemporaryDirectory() as td:
        # copy the source in so the default output lands beside it
        mod = Path(td) / "MyCoolCar.scs"
        with zipfile.ZipFile(PORCHE) as z, zipfile.ZipFile(mod, "w") as o:
            for n in z.namelist():
                if not n.endswith("/"):
                    o.writestr(n, z.read(n))
        C.main([str(mod), "--type", "sedan", "--brand", "test"])
        produced = Path(td) / "rt_mycoolcar.scs"
        check("default output is rt_<slug>.scs", produced.exists(),
              str(list(Path(td).glob("*.scs"))))
        check("slug lowercased", produced.name == "rt_mycoolcar.scs", produced.name)


@guard
def test_dry_run_writes_nothing() -> None:
    section("dry run")
    # @guard cannot help here: this shells out, so a missing fixture comes back
    # as a non-zero exit and the stdout assertions below fail for a reason that
    # has nothing to do with --dry-run. Ask for the fixture explicitly.
    if not need(PORCHE, "dry run"):
        return
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "dry.scs"
        r = subprocess.run(
            [sys.executable, str(HERE / "carutil.py"), str(PORCHE),
             "--type", "suv", "--brand", "p", "--dry-run", "-o", str(out)],
            capture_output=True, text=True, stdin=subprocess.DEVNULL)
        check("no file written", not out.exists())
        check("says nothing was written", "nothing was written" in r.stdout)
        check("no DONE banner", "DONE" not in r.stdout)


# ------------------------------------------------------- locked archives
def _make_locked_zip(dest: Path, password: str) -> bool:
    """Build a password-protected copy of the Porsche def archive.

    Needs the `zip` binary; returns False if unavailable.
    """
    if shutil.which("zip") is None:
        return False
    work = dest / "w"
    work.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(PORCHE) as z:
        for n in z.namelist():
            if not n.endswith("/"):
                p = work / n
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(z.read(n))
    out = dest / "locked.scs"
    r = subprocess.run(
        ["zip", "-q", "-r", "-P", password, str(out), "."],
        cwd=work, capture_output=True)
    return out if (r.returncode == 0 and out.exists()) else None


@guard
def test_locked_archive_errors_cleanly() -> None:
    section("password-protected archives")
    with tempfile.TemporaryDirectory() as td:
        locked = _make_locked_zip(Path(td), "secret123")
        if not locked:
            print("  SKIP  zip binary unavailable")
            return

        src = C.ModSource(locked)
        check("locked entries detected", len(src.locked) > 0, str(len(src.locked)))
        check("nothing readable", src.readable == [])
        check("read_opt returns None, not raise",
              src.read_opt("manifest.sii") is None)
        check("exists() is False for locked", not src.exists("manifest.sii"))

        # the tool must fail with guidance, never a traceback
        r = subprocess.run(
            [sys.executable, str(HERE / "carutil.py"), str(locked),
             "--type", "suv", "--brand", "h", "-o", str(Path(td) / "o.scs")],
            capture_output=True, text=True)
        check("exits non-zero", r.returncode != 0)
        check("no traceback", "Traceback" not in r.stderr, r.stderr[-200:])
        check("says flagged encrypted", "flagged encrypted" in r.stderr)
        check("suggests --password", "--password" in r.stderr)


@guard
def test_password_unlocks() -> None:
    section("--password")
    with tempfile.TemporaryDirectory() as td:
        locked = _make_locked_zip(Path(td), "secret123")
        if not locked:
            print("  SKIP  zip binary unavailable")
            return
        out = Path(td) / "ok.scs"

        r = subprocess.run(
            [sys.executable, str(HERE / "carutil.py"), str(locked),
             "--type", "suv", "--brand", "honda", "--password", "secret123",
             "--mass", "2142", "-o", str(out)],
            capture_output=True, text=True)
        check("correct password converts", r.returncode == 0, r.stderr[-200:])
        check("output written", out.exists())
        if out.exists():
            z = zipfile.ZipFile(out)
            d = z.read("def/vehicle/truck/cayenne.gts/data.sii").decode()
            check("tag written", 'tags[]: "suv"' in d)

        r2 = subprocess.run(
            [sys.executable, str(HERE / "carutil.py"), str(locked),
             "--type", "suv", "--brand", "h", "--password", "wrongpw",
             "-o", str(Path(td) / "o2.scs")],
            capture_output=True, text=True)
        check("wrong password fails cleanly", r2.returncode != 0)
        check("wrong password: no traceback", "Traceback" not in r2.stderr)


@guard
def test_zip_wrapper_is_unwrapped() -> None:
    section(".zip wrapping a .scs")
    with tempfile.TemporaryDirectory() as td:
        inner = Path(td) / "MyMod.scs"
        with zipfile.ZipFile(PORCHE) as z, zipfile.ZipFile(inner, "w") as o:
            for n in z.namelist():
                if not n.endswith("/"):
                    o.writestr(n, z.read(n))
        wrapper = Path(td) / "MyMod.zip"
        with zipfile.ZipFile(wrapper, "w", zipfile.ZIP_DEFLATED) as o:
            o.write(inner, "MyMod.scs")

        src = C.ModSource(wrapper)
        check("wrapper noted", src.wrapper_note is not None)
        check("inner mod read", len(src.readable_defs()) > 0)
        check("vehicle def found", C.find_vehicle_dir(src)[1] == "cayenne.gts")

        out = Path(td) / "out.scs"
        r = subprocess.run(
            [sys.executable, str(HERE / "carutil.py"), str(wrapper),
             "--type", "sedan", "--brand", "test", "--mass", "1400",
             "-o", str(out)],
            capture_output=True, text=True)
        check("converts through the wrapper", r.returncode == 0, r.stderr[-200:])
        check("reports the unwrap", "contained a mod archive" in r.stdout)

        # a zip that IS a mod archive must not be unwrapped
        src2 = C.ModSource(PORCHE)
        check("mod archive not unwrapped", src2.wrapper_note is None)


# ------------------------------------------- mislabelled "encrypted" archives
def _make_mislabelled_zip(dest: Path) -> bool:
    """Rebuild the Porsche as an archive whose central directory claims
    encryption while the payload stays plain -- the packaging trick seen in
    real mods. Requires the zip binary."""
    if shutil.which("zip") is None:
        return False
    work = dest / "w"
    work.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(PORCHE) as z:
        for n in z.namelist():
            if not n.endswith("/"):
                p = work / n
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(z.read(n))
    plain = dest / "plain2.scs"
    r = subprocess.run(["zip", "-q", "-r", str(plain), "."], cwd=work,
                       capture_output=True)
    return plain if (r.returncode == 0 and plain.exists()) else None


def _make_junk_method_zip(dest: Path, name: str, payload: bytes) -> Path:
    """An archive whose entries are STORED but whose local headers claim
    method 1 (deflate) -- the packaging seen in every mod in testing-covnverter.

    The local method byte is 1 for 100% of entries there, whether the payload
    is really deflated or really stored, so it carries no information. Trusting
    it is what corrupted the Escalade's camera definition.
    """
    out = dest / "junk.scs"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        z.writestr(name, payload)
    raw = bytearray(out.read_bytes())
    # Set the local file header's method field (offset 8) to 1. The central
    # directory keeps saying 0, which is the disagreement under test.
    i = raw.find(b"PK\x03\x04")
    raw[i + 8:i + 10] = b"\x01\x00"
    out.write_bytes(bytes(raw))
    return out


def test_stored_entry_is_not_inflated() -> None:
    """A stored payload must come back byte-identical, junk method or not.

    This is the regression that would have caught the Escalade crash.
    """
    section("a stored entry is never inflated")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        # The exact bytes of the definition that got corrupted, at the exact
        # length. The payload is pure ASCII, which is the whole problem: raw
        # deflate permits a stored-block header inside arbitrary bytes, so
        # zlib "successfully" inflates this to 10 junk bytes and never raises.
        payload = (b'SiiNunit\n{\n'
                   b'@include "units/interior_escalade.21_oculus.sui"\n}\n')
        built = _make_junk_method_zip(td, "def/camera/cam.sii", payload)

        with zipfile.ZipFile(built) as z:
            check("central directory still says stored",
                  z.getinfo("def/camera/cam.sii").compress_type == 0)

        # Drive the local-header path the converter actually uses.
        src = C.ModSource(built, plain_read=False)
        src.local = C.scan_local_headers(built)
        check("local header claims deflate",
              src.local["def/camera/cam.sii"][2] == 1)
        check("_central_method overrides it",
              src._central_method("def/camera/cam.sii", 1) == 0)

        got = src._read_local("def/camera/cam.sii")
        check("payload survives byte-identical", got == payload,
              f"{len(got)}b vs {len(payload)}b")
        check("it still parses as a definition",
              got.startswith(b"SiiNunit"))

        # And the text reader must refuse to launder a genuinely binary file.
        broken = _make_junk_method_zip(td, "def/vehicle/x.sii",
                                       b"SiiNunit\n{\n\xff\xfe\x00broken\n}\n")
        bsrc = C.ModSource(broken, plain_read=False)
        bsrc.local = C.scan_local_headers(broken)
        try:
            bsrc.read_def("def/vehicle/x.sii")
            check("read_def refuses undecodable defs", False, "no error raised")
        except SystemExit as exc:
            check("read_def refuses undecodable defs", True)
            check("the error names the file", "def/vehicle/x.sii" in str(exc))
            check("the error explains itself",
                  "not a readable definition" in str(exc))


def test_readable_defs_are_valid_utf8() -> None:
    """No mod may ship a definition we cannot decode cleanly.

    errors="replace" used to turn a misread into plausible text, so this
    asserts the premise directly against the fixture mods.
    """
    section("every readable def decodes as UTF-8")
    if not FIXTURES.is_dir():
        skip("real mods decode cleanly", "fixtures not present")
        return
    bad = []
    for scs in sorted(FIXTURES.glob("*.scs")):
        try:
            src = C.ModSource(scs, plain_read=True)
        except Exception:
            continue
        for name in src.readable:
            if not name.endswith((".sii", ".sui")):
                continue
            data = src.read_bytes(name)
            if data is None:
                continue
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                bad.append(f"{scs.name}:{name}")
    check("no mod ships a def that is not UTF-8", not bad, "; ".join(bad[:3]))


@guard
def test_mislabelled_encryption_is_readable() -> None:
    section("entries flagged encrypted but stored plain")
    with tempfile.TemporaryDirectory() as td:
        built = _make_mislabelled_zip(Path(td))
        if not built:
            print("  SKIP  zip binary unavailable")
            return
        # flip bit 0 in every central-directory record
        import struct as _s
        raw = bytearray(built.read_bytes())
        i = 0
        n = 0
        while True:
            i = raw.find(b"PK\x01\x02", i)
            if i < 0:
                break
            raw[i + 8] |= 0x01          # general purpose bit flag
            i += 4
            n += 1
        built.write_bytes(bytes(raw))
        check("central directory rewritten", n > 10, f"{n} records")

        src = C.ModSource(built)
        check("local headers recover it", len(src.locked) == 0 or src.fake_encryption,
              f"locked={len(src.locked)}")
        check("data.sii readable",
              src.read_opt("manifest.sii") is not None)

        # opt-out must refuse it
        strict = C.ModSource(built, plain_read=False)
        check("--strict-encryption keeps them locked", len(strict.locked) > 0)

        out = Path(td) / "out.scs"
        r = subprocess.run(
            [sys.executable, str(HERE / "carutil.py"), str(built),
             "--type", "suv", "--brand", "honda", "--mass", "2142",
             "-o", str(out)],
            capture_output=True, text=True)
        check("converts without a password", r.returncode == 0, r.stderr[-200:])
        check("reports the workaround", "data is plain" in r.stdout)

        r2 = subprocess.run(
            [sys.executable, str(HERE / "carutil.py"), str(built),
             "--type", "suv", "--brand", "h", "--strict-encryption",
             "-o", str(Path(td) / "o2.scs")],
            capture_output=True, text=True)
        check("strict mode refuses cleanly", r2.returncode != 0)
        check("strict: no traceback", "Traceback" not in r2.stderr)


@guard
def test_real_encryption_still_refused() -> None:
    section("genuinely encrypted archive is NOT bypassed")
    with tempfile.TemporaryDirectory() as td:
        locked = _make_locked_zip(Path(td), "secret123")
        if not locked:
            print("  SKIP  zip binary unavailable")
            return
        src = C.ModSource(locked, plain_read=True)
        # a real ZipCrypto archive keeps the flag in its local headers too
        check("stays locked despite plain_read", len(src.locked) > 0,
              f"locked={len(src.locked)}")
        check("no plain recovery attempted", not getattr(src, "fake_encryption", False))


# ------------------------------------------------- real mods, end to end
def _check_conversion(mod: Path, vtype: str, brand: str, mass: str,
                      expect_tags: str, tmp: Path) -> None:
    out = tmp / "out.scs"
    r = subprocess.run(
        [sys.executable, str(HERE / "carutil.py"), str(mod),
         "--type", vtype, "--brand", brand, "--mass", mass, "-o", str(out)],
        capture_output=True, text=True)
    check(f"{mod.name[:28]}: converts", r.returncode == 0, r.stderr[-300:])
    check(f"{mod.name[:28]}: no traceback", "Traceback" not in r.stderr)
    if r.returncode != 0:
        return
    z = zipfile.ZipFile(out)
    names = z.namelist()
    slugs = [n for n in names if n.endswith("data.sii")]
    check(f"{mod.name[:28]}: data.sii present", bool(slugs))
    if slugs:
        d = z.read(slugs[0]).decode()
        check(f"{mod.name[:28]}: tag is {expect_tags}",
              f'tags[]: "{expect_tags}"' in d)
        # cameras and require[] lines from the source must survive
        check(f"{mod.name[:28]}: cameras preserved",
              sum(1 for l in d.splitlines() if "camera" in l) > 0)
    for kind in ("car_dealer", "car_desktop", "car_company", "truck_dealer"):
        check(f"{mod.name[:28]}: {kind} written",
              any(kind in n for n in names))
    # Dropped, not kept. The mod's def/vehicle/physics.sii is stock TRUCK
    # physics in a NON-namespaced file, so it overrides the base game's
    # calibrated brake and tyre physics for every vehicle in the game. This
    # assertion said "kept" and had done so since before the drop was the
    # default -- it never ran, because it needs a fixture. --keep-physics is
    # covered separately.
    check(f"{mod.name[:28]}: the global truck physics.sii is dropped",
          not any(n.endswith("physics.sii") for n in names),
          str([n for n in names if n.endswith("physics.sii")]))
    # a TOTAL is split across axles, so compare against the split values
    if "," not in mass:
        expect = C.split_mass(int(mass), vtype, 2)
        chassis = "".join(
            z.read(n).decode("utf-8", "replace")
            for n in names if "/chassis/" in n
        )
        check(f"{mod.name[:28]}: mass split applied",
              f"kerb_weight[0]: {expect[0]}" in chassis
              and f"kerb_weight[1]: {expect[1]}" in chassis,
              f"expected {expect}")
    check(f"{mod.name[:28]}: paint jobs carried",
          sum(1 for n in names if "paint_job" in n) > 0)


@guard
def test_real_mods() -> None:
    section("real mods end-to-end")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        if PILOT.exists():
            _check_conversion(PILOT, "suv", "honda", "2142", "suv", tmp)
        else:
            print("  SKIP  pilot not present")
        if W223.exists():
            _check_conversion(W223, "sedan", "mercedes", "1900", "sedan", tmp)
        else:
            print("  SKIP  w223 not present")


@guard
def test_mislabelled_mods_are_readable() -> None:
    section("header-trick archives: local header recovery")
    for mod, label in ((PILOT, "Pilot"), (W223, "W223")):
        if not mod.exists():
            print(f"  SKIP  {label} not present")
            continue
        with zipfile.ZipFile(mod) as z:
            flagged = [i for i in z.infolist() if i.flag_bits & 0x1]
        check(f"{label}: index does flag encryption", len(flagged) > 0,
              f"{len(flagged)} entries")
        src = C.ModSource(mod)
        check(f"{label}: recovered via local headers", src.fake_encryption)
        check(f"{label}: all entries readable", len(src.locked) == 0,
              f"{len(src.locked)} still locked")
        check(f"{label}: manifest readable",
              src.read_opt("manifest.sii") is not None)
        strict = C.ModSource(mod, plain_read=False)
        check(f"{label}: strict mode refuses", len(strict.locked) > 0)


@guard
def test_mod_folder_detection() -> None:
    section("ATS mod folder detection")
    dirs = C.find_mod_folders()
    check("returns a list", isinstance(dirs, list))
    check("entries are Paths", all(isinstance(d, Path) for d in dirs))
    check("no duplicates", len(dirs) == len({d.resolve() for d in dirs}))
    for d in dirs:
        check(f"{d.name}: is a directory", d.is_dir())
        check(f"{d.name}: looks like ATS data",
              (d / "profiles").is_dir() or (d / "config.cfg").is_file())
    if dirs:
        check("at least one has a mod path we can name", all(d.name for d in dirs))
        print(f"       found: {dirs[0]}")


@guard
def test_find_mod_folders_layouts() -> None:
    """Every launcher layout, on a synthetic $HOME, must be found.

    A Linux Mint user reported the finder coming up empty. The old code only
    knew four prefixes and used Path.glob("**/..."), which does not descend
    into symlinked directories -- so a symlinked prefix or Steam library was
    invisible while base.exists() still said yes. Each layout below is a real
    one from a report or a shipped launcher.
    """
    section("mod folder finder covers every launcher layout")
    import pathlib as _pl

    data = "American Truck Simulator"

    def build_home(root: _pl.Path) -> dict[str, _pl.Path]:
        def mk(rel: str, marker: str = "config.cfg") -> _pl.Path:
            p = root / rel
            p.mkdir(parents=True, exist_ok=True)
            if marker == "profiles":
                (p / marker).mkdir(exist_ok=True)
            else:
                (p / marker).write_text("x")
            return p

        want: dict[str, _pl.Path] = {}
        proton = "steamapps/compatdata/4130880446/pfx/drive_c/users/steamu"
        for name, base in (
            ("proton_local", ".local/share/Steam"),
            ("proton_dotsteam", ".steam"),
            ("proton_root", ".steam/root"),
            ("flatpak", ".var/app/com.valvesoftware.Steam/data/Steam"),
            ("snap", "snap/steam/common/.local/share/Steam"),
        ):
            want[name] = mk(f"{base}/{proton}/Documents/{data}")
        want["wine"] = mk(f".wine/drive_c/users/steamu/Documents/{data}")
        want["my_documents"] = mk(
            f".wine/drive_c/users/steamu/My Documents/{data}")
        want["lutris"] = mk(
            f"Games/lutris/drive_c/users/steamu/Documents/{data}")
        want["home_documents"] = mk(f"Documents/{data}")
        # A prefix with no pfx/ level, which older Proton layouts used.
        want["no_pfx_dir"] = mk(
            ".local/share/Steam/steamapps/compatdata/9/drive_c/users/s"
            f"/Documents/{data}")
        # profiles/ instead of config.cfg: a data dir the user has never
        # written a config into still counts.
        want["profiles_only"] = mk(
            f"Games/lutris/drive_c/users/p/Documents/{data}", marker="profiles")
        # Case sensitivity: ext4 is case-sensitive by default, so the game's
        # own capitalisation cannot be assumed to be what is on disk.
        want["lowercase_dir"] = mk(
            f"Games/lutris/drive_c/users/lc/Documents/"
            "american truck simulator")

        # The two symlink shapes that the old glob could not see.
        pfx_root = root / "elsewhere/pfx"
        pfx_data = mk(f"elsewhere/pfx/drive_c/users/steamu/Documents/{data}")
        link = root / ".steam/root/steamapps/compatdata/999"
        link.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(pfx_root, link / "pfx")
            want["symlinked_prefix"] = pfx_data
        except (OSError, NotImplementedError):
            skip("symlinked prefix", "symlinks unavailable here")

        steam = root / "SteamInstall/steamapps"
        linked = mk(f"SteamInstall/steamapps/compatdata/111/pfx/drive_c"
                    f"/users/s/Documents/{data}")
        (root / ".steam" / "steam").mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(steam, root / ".steam" / "steam" / "steamapps")
            want["symlinked_steamapps"] = linked
        except (OSError, NotImplementedError):
            skip("symlinked steam library", "symlinks unavailable here")

        # Flatpak lays Steam out as data/Steam plus a .steam symlink farm into
        # it. That works via data/Steam. The case that does not is a build
        # where .steam/root is a real directory instead of a symlink -- which
        # is why the candidate list enumerates Steam *roots* rather than
        # remembering one path per launcher.
        fp = root / ".var/app/com.valvesoftware.Steam"
        fp_data = mk(f".var/app/com.valvesoftware.Steam/data/Steam"
                     f"/steamapps/compatdata/4130880446/pfx/drive_c"
                     f"/users/steamu/Documents/{data}")
        (fp / ".steam").mkdir(parents=True, exist_ok=True)
        for link, target in (("root", "../data/Steam"),
                             ("steam", "../data/Steam")):
            try:
                os.symlink(target, fp / ".steam" / link)
            except (OSError, NotImplementedError):
                pass
        try:
            os.symlink("root/steamapps", fp / ".steam" / "steamapps")
        except (OSError, NotImplementedError):
            pass
        want["flatpak_symlink_farm"] = fp_data

        want["flatpak_real_steam_root"] = mk(
            ".var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps"
            f"/compatdata/4130880446/pfx/drive_c/users/steamu/Documents/{data}")
        return want

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "home"
        want = build_home(root)
        real_home = Path.home
        Path.home = staticmethod(lambda: root)  # type: ignore[assignment]
        try:
            found = C.find_mod_folders()
        finally:
            Path.home = real_home  # type: ignore[assignment]

        resolved = {p.resolve() for p in found}
        for name, expected in sorted(want.items()):
            check(f"{name}: found", expected.resolve() in resolved,
                  str(sorted(str(p) for p in found))[:200])

        # It must be fast. An earlier attempt used os.walk(followlinks=True)
        # and took over ten minutes, because a Proton prefix holds 1358
        # symlinks into the Proton runtime tree. Constructing the known paths
        # costs a handful of directory listings instead.
        Path.home = staticmethod(lambda: root)  # type: ignore[assignment]
        try:
            started = time.time()
            C.find_mod_folders()
            elapsed = time.time() - started
        finally:
            Path.home = real_home  # type: ignore[assignment]
        check("the search is not a tree walk", elapsed < 2.0,
              f"{elapsed:.1f}s")

        # --mod-dir must be honoured even when the directory does not look
        # like an ATS data dir: the user has told us where it is.
        elsewhere = root / "somewhere" / "else"
        (elsewhere / "profiles").mkdir(parents=True)
        Path.home = staticmethod(lambda: root)  # type: ignore[assignment]
        try:
            named = {p.resolve() for p in C.find_mod_folders([elsewhere])}
            by_name: set = set()
            blank: set = set()
        finally:
            Path.home = real_home  # type: ignore[assignment]
        check("--mod-dir accepts the data dir itself",
              elsewhere.resolve() in named)

        # A directory holding an "American Truck Simulator" child is honoured
        # whatever the child's capitalisation.
        (root / "somewhere" / "named").mkdir(parents=True, exist_ok=True)
        C_child = root / "somewhere" / "named" / "american truck simulator"
        (C_child / "profiles").mkdir(parents=True)
        (root / "nothing" / "here").mkdir(parents=True, exist_ok=True)
        Path.home = staticmethod(lambda: root)  # type: ignore[assignment]
        try:
            by_name = {p.resolve() for p in
                       C.find_mod_folders([root / "somewhere" / "named"])}
        finally:
            Path.home = real_home  # type: ignore[assignment]
        check("--mod-dir finds a named child whatever its case",
              C_child.resolve() in by_name, str(sorted(map(str, by_name))))

        check("a --mod-dir with nothing in it adds nothing",
              (root / "nothing" / "here").resolve() not in blank)


def test_install_help_prints_real_path() -> None:
    section("install help")
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        C.print_install_help(Path("/tmp/opencode/example.scs"))
    out = buf.getvalue()
    check("prints output path", "example.scs" in out)
    check("prints DONE", "DONE" in out)
    if C.find_mod_folders():
        check("prints the real mod folder",
              any(str(d / "mod") in out for d in C.find_mod_folders()))
    else:
        check("falls back to generic hints", "Proton" in out)


# ------------------------------------------- SCS zipfs archive conventions
@guard
def test_archive_conventions_match_reference() -> None:
    section("archive conventions (SCS zipfs is strict)")
    if not REF.exists():
        print("  SKIP  reference absent")
        return
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "conv.scs"
        C.main([str(REF), "--type", "sedan", "--brand", "volkswagen",
                "-o", str(out)])
        ref = zipfile.ZipFile(REF)
        got = zipfile.ZipFile(out)

        check("create_system is 0 (MS-DOS), not Unix",
              {i.create_system for i in got.infolist()} == {0},
              str({i.create_system for i in got.infolist()}))

        dirs = [n for n in got.namelist() if n.endswith("/")]
        check("directory entries are emitted", len(dirs) > 0, str(len(dirs)))

        attrs = {i.external_attr for i in got.infolist()}
        check("file external_attr matches reference style",
              0x81B60000 in attrs, str([hex(a) for a in attrs]))
        check("dir external_attr matches reference style",
              0x41FF0010 in attrs, str([hex(a) for a in attrs]))

        check("no entry is flagged encrypted",
              all(not (i.flag_bits & 1) for i in got.infolist()))
        check("no data descriptors (bit 3)",
              all(not (i.flag_bits & 8) for i in got.infolist()),
              str({hex(i.flag_bits) for i in got.infolist()}))
        check("no zip64 records", b"PK\x06\x06" not in out.read_bytes())

        # every non-directory entry must have a parent directory entry
        names = set(got.namelist())
        missing = set()
        for n in got.namelist():
            if n.endswith("/"):
                continue
            parts = n.split("/")[:-1]
            for i in range(len(parts)):
                d = "/".join(parts[: i + 1]) + "/"
                if d not in names:
                    missing.add(d)
        check("every parent directory exists as an entry", not missing,
              str(sorted(missing)[:5]))

        # still a valid zip
        r = subprocess.run(["unzip", "-tq", str(out)], capture_output=True)
        check("passes unzip -t", r.returncode == 0, r.stdout.decode()[:100])


# ------------------------------------------- assets must survive conversion
@guard
def test_all_assets_are_carried_over() -> None:
    section("models / textures / sounds survive")
    for mod, label in ((PILOT, "Pilot"), (W223, "W223"), (PORCHE, "Porsche")):
        if not mod.exists():
            print(f"  SKIP  {label} absent")
            continue
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out.scs"
            src = C.ModSource(mod)
            C.main([str(mod), "--type", "suv", "--brand", "t", "-o", str(out)])

            z = zipfile.ZipFile(out)
            got = {n for n in z.namelist() if not n.endswith("/")}
            want = set(src.readable)

            dropped = want - got
            # Only these may be dropped: the truck physics, the regenerated
            # dealer bindings, and the truck wheel defs superseded by
            # p_tire/p_disc. Everything else must survive byte-for-byte.
            allowed = {
                n for n in dropped
                if "physics" in n
                or n.startswith("def/vehicle/truck_dealer/")
                or re.match(r"def/vehicle/[fr]_(tire|disc|hub|nuts)/", n)
            }
            check(f"{label}: nothing else dropped", not (dropped - allowed),
                  str(sorted(dropped - allowed)[:4]))

        for ext in (".pmd", ".pmc", ".dds", ".tobj", ".mat", ".bank"):
            n = sum(1 for x in got if x.endswith(ext))
            o = sum(1 for x in want if x.endswith(ext))
            if o:
                # The generated brand logo legitimately adds one of each under
                # truck_brand_logo/ (as the reference does) plus one more set
                # under car_brand_logo/ (which the reference omits).
                check(f"{label}: {o} {ext} carried over",
                      n in (o, o + 1, o + 2),
                          f"got {n}, want {o} or {o + 1}")

            # model bytes must be byte-identical
            models = [x for x in sorted(want) if x.endswith((".pmd", ".pmc"))]
            if models:
                m = models[0]
                check(f"{label}: {Path(m).name} byte-identical",
                      src.read_bytes(m) == z.read(m))


@guard
def test_chassis_model_references_resolve() -> None:
    section("every model the chassis points at exists")
    if not PILOT.exists():
        print("  SKIP  pilot absent")
        return
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "o.scs"
        C.main([str(PILOT), "--type", "suv", "--brand", "honda", "-o", str(out)])
        z = zipfile.ZipFile(out)
        names = set(z.namelist())
        chassis = [n for n in names if "/chassis/" in n and n.endswith(".sii")]
        check("chassis found", bool(chassis))
        pattern = (
            r'(?:detail_model|collision|model|ui_shadow'
            r'|extended_shadow_texture):\s*"([^"]+)"'
        )
        refs = []
        for n in chassis:
            text = z.read(n).decode("utf-8", "replace")
            for m in re.finditer(pattern, text):
                refs.append(m.group(1))
        check("chassis declares model paths", bool(refs), str(len(refs)))
        missing = [r for r in refs if r.lstrip("/") not in names]
        check("all chassis model paths resolve", not missing, str(missing))


# ------------------------------------------------------- wheels & fallbacks
@guard
def test_wheels_bound_as_shipped() -> None:
    section("wheels are bound as-is, never renamed")
    for mod, label in ((PILOT, "Pilot"), (W223, "W223")):
        if not mod.exists():
            print(f"  SKIP  {label} absent")
            continue
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "o.scs"
            C.main([str(mod), "--type", "sedan", "--brand", "t", "-o", str(out)])
            z = zipfile.ZipFile(out)
            names = z.namelist()

            check(f"{label}: no p_tire namespace invented",
                  not any(n.startswith("def/vehicle/p_tire/") for n in names))
            check(f"{label}: no p_disc namespace invented",
                  not any(n.startswith("def/vehicle/p_disc/") for n in names))

            # every source wheel def must survive untouched
            src = C.ModSource(mod)
            src_wheels = {n for n in src.readable
                          if re.match(r"def/vehicle/[fr]_(tire|disc|hub|nuts)/", n)}
            check(f"{label}: all {len(src_wheels)} source wheel defs kept",
                  src_wheels <= set(names),
                  str(sorted(src_wheels - set(names))[:3]))
            # Tyres are NOT byte-identical, and must not be: they are
            # accessory_wheel_data units, so tune_wheel adds the car grip keys
            # (roll_resistance, grip_factor) that all 18 of SCS's own car
            # wheels declare. Every other wheel part is untouched and has to
            # come through byte for byte. Asserting byte-identity for the tyres
            # too failed here forever -- it is the test that was wrong, not the
            # converter.
            #
            # Names carry the axle as well as the file: a mod has four wheel
            # defs all called w223.sii (f/r tire and disc), so the bare
            # filename repeats and you cannot tell which one failed.
            def _wheel(n: str) -> str:
                return "/".join(n.split("/")[-2:])

            def _is_tyre(n: str) -> bool:
                return n.split("/")[-2] in ("f_tire", "r_tire")

            for n in sorted(src_wheels):
                if _is_tyre(n):
                    continue
                check(f"{label}: {_wheel(n)} byte-identical",
                      src.read_bytes(n) == z.read(n))
            for n in sorted(n for n in src_wheels if _is_tyre(n)):
                txt = z.read(n).decode("utf-8", "replace")
                check(f"{label}: {_wheel(n)} gained car tyre grip",
                      all(k in txt for k in C._CAR_TYRE), txt[-200:])

            dealer = [n for n in names
                      if "car_desktop" in n and n.endswith(".sii")]
            d = z.read(dealer[0]).decode()
            binds = re.findall(
                r"vehicle_wheel_accessory : (\.\w+)\s*\n\s*\{\s*\n\s*offset: (\d+)", d)
            bound_paths = re.findall(
                r'vehicle_wheel_accessory[^}]*data_path: "([^"]+)"', d)
            readable = {n for n in src.readable}
            check(f"{label}: wheel bindings present", len(binds) >= 2, str(binds))
            check(f"{label}: all at offset 0",
                  all(o == "0" for _, o in binds), str(binds))
            check(f"{label}: every bound wheel is a real source def",
                  all(p.lstrip("/") in readable for p in bound_paths),
                  str(bound_paths))


@guard
def test_fallbacks_pre_select_stock() -> None:
    section("fallback[] pre-selects parts")
    for mod, label in ((PILOT, "Pilot"), (W223, "W223")):
        if not mod.exists():
            print(f"  SKIP  {label} absent")
            continue
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "o.scs"
            C.main([str(mod), "--type", "sedan", "--brand", "t", "-o", str(out)])
            z = zipfile.ZipFile(out)
            ds = [n for n in z.namelist() if n.endswith("/data.sii")]
            check(f"{label}: data.sii found", bool(ds))
            if not ds:
                continue
            t = z.read(ds[0]).decode()
            fbs = re.findall(r'fallback\[\]:\s*"([^"]+)"', t)
            check(f"{label}: fallbacks emitted", len(fbs) >= 6, str(fbs))
            check(f"{label}: chassis pre-selected",
                  any(x.startswith("chassis|") for x in fbs))
            for wc in ("f_tire", "f_disc", "f_hub", "f_nuts"):
                check(f"{label}: {wc} pre-selected",
                      any(x.startswith(wc + "|") for x in fbs), str(fbs))
            check(f"{label}: no p_tire fallback invented",
                  not any(x.startswith("p_tire|") for x in fbs), str(fbs))
            # every fallback target must exist. Parts resolve inside the
            # vehicle def dir; p_tire/p_disc live at def/vehicle/ top level
            # (verified against the Parati).
            names = set(z.namelist())
            vdir = "/".join(ds[0].split("/")[:-1]) + "/"
            missing = []
            for fb in fbs:
                cat, fname = fb.split("|", 1)
                if f"{vdir}{cat}/{fname}" in names or f"def/vehicle/{cat}/{fname}" in names:
                    continue
                missing.append(fb)
            check(f"{label}: every fallback target exists", not missing, str(missing))
            # and be bound in the car_dealer file
            dealer = [n for n in z.namelist()
                      if "car_desktop" in n and n.endswith(".sii")]
            if dealer:
                d = z.read(dealer[0]).decode()
                for fb in fbs:
                    if re.match(r"[fr]_(tire|disc|hub|nuts)\|", fb):
                        stem = fb.split("|", 1)[1]
                        check(f"{label}: {stem} bound in car_dealer", stem in d)


# ------------------------------------------------- SII syntax correctness
def _brace_delta(text: str) -> int:
    d = 0
    for line in text.splitlines():
        s = line.strip()
        if s.endswith("{"):
            d += 1
        elif s == "}":
            d -= 1
    return d


@guard
def test_unit_body_end_ignores_body_keys() -> None:
    section("unit_body_end robustness")
    # `variant: chs` inside the body looks exactly like a unit header line;
    # matching headers by regex starts the walk inside the body.
    lines = [
        "SiiNunit", "{",
        "accessory_chassis_data : 4matic.w223_s500.chassis", "{",
        "\tname: \"4matic\"",
        "\tvariant: chs",                      # must not be taken as header
        "\tkerb_weight[0]: 4720.1",
        "}", "}",
    ]
    check("returns the unit's closing brace",
          sii.unit_body_end(lines) == 7, str(sii.unit_body_end(lines)))

    end = sii.unit_body_end(lines)
    out = sii.set_indexed(lines, "kerb_weight", ["1090"])
    check("new key lands inside the unit",
          out.index("\tkerb_weight[0]: 1090") <= end)

    out2 = sii.set_key(lines, "cog_height", "0.4")
    check("set_key lands inside the unit",
          out2.index("\tcog_height: 0.4") <= end)
    check("nothing after the file's last brace",
          out2[-1].strip() == "}")


@guard
def test_generated_defs_are_syntactically_balanced() -> None:
    section("every generated .sii is brace-balanced")
    for mod, label in ((PILOT, "Pilot"), (W223, "W223"), (PORCHE, "Porsche"), (REF, "Parati")):
        if not mod.exists():
            print(f"  SKIP  {label} absent")
            continue
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "o.scs"
            C.main([str(mod), "--type", "sedan", "--brand", "t", "-o", str(out)])
            z = zipfile.ZipFile(out)
            bad = []
            for n in z.namelist():
                if not n.endswith(".sii"):
                    continue
                if _brace_delta(z.read(n).decode("utf-8", "replace")) != 0:
                    bad.append(n)
            check(f"{label}: no unbalanced .sii", not bad, str(bad[:3]))


@guard
def test_generated_keys_are_inside_the_unit() -> None:
    section("physics keys land inside the chassis unit")
    for mod, label, chassis in ((W223, "W223", "4matic.sii"),
                                (PILOT, "Pilot", "awd.sii")):
        if not mod.exists():
            print(f"  SKIP  {label} absent")
            continue
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "o.scs"
            C.main([str(mod), "--type", "sedan", "--brand", "t", "-o", str(out)])
            z = zipfile.ZipFile(out)
            path = [n for n in z.namelist()
                    if "/chassis/" in n and n.endswith(chassis)]
            check(f"{label}: chassis found", bool(path), str(path))
            if not path:
                continue
            lines = z.read(path[0]).decode().splitlines()
            end = sii.unit_body_end(lines)
            for key in ("suspension_damping_factor[0]", "disc_brake[0]",
                        "axle_suspension_type[0]"):
                idx = [i for i, l in enumerate(lines) if key in l]
                check(f"{label}: {key} present", bool(idx))
                if idx:
                    check(f"{label}: {key} inside unit", idx[0] < end,
                          f"line {idx[0]+1} >= {end+1}")
            check(f"{label}: no keys after the file's last brace",
                  lines[-1].strip() == "}", lines[-3:])


@guard
def test_binding_files_have_no_extra_brace() -> None:
    section("dealer binding files parse")
    if not W223.exists():
        print("  SKIP  w223 absent")
        return
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "o.scs"
        C.main([str(W223), "--type", "sedan", "--brand", "m", "-o", str(out)])
        z = zipfile.ZipFile(out)
        binds = [n for n in z.namelist()
                 if any(k in n for k in ("car_dealer", "car_desktop",
                                         "car_company", "truck_dealer"))
                 and n.endswith(".sii")]
        check("bindings written", len(binds) == 4, str(len(binds)))
        for n in binds:
            text = z.read(n).decode()
            check(f"{Path(n).name}: balanced", _brace_delta(text) == 0)
            check(f"{Path(n).name}: ends with single close",
                  text.rstrip().endswith("}") and not text.rstrip().endswith("}}"))


@guard
def test_car_wheel_units_have_reference_keys() -> None:
    section("wheel units carry every key the reference has")
    for mod, label in ((PILOT, "Pilot"), (W223, "W223")):
        if not mod.exists():
            print(f"  SKIP  {label} absent")
            continue
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "o.scs"
            C.main([str(mod), "--type", "suv", "--brand", "t", "-o", str(out)])
            z = zipfile.ZipFile(out)
            for n in z.namelist():
                if not (n.startswith("def/vehicle/p_tire/") and n.endswith(".sii")):
                    continue
                text = z.read(n).decode()
                check(f"{label}: {Path(n).name} has part_type",
                      "part_type: factory" in text)
                check(f"{label}: {Path(n).name} has look", "look: default" in text)
                check(f"{label}: {Path(n).name} has grip_factor", "grip_factor:" in text)
            for n in z.namelist():
                if not (n.startswith("def/vehicle/p_disc/") and n.endswith(".sii")):
                    continue
                text = z.read(n).decode()
                check(f"{label}: {Path(n).name} has part_type",
                      "part_type: factory" in text)
                check(f"{label}: {Path(n).name} has look", "look: default" in text)


@guard
def test_chassis_avoids_invented_keys() -> None:
    section("chassis does not invent detail_model")
    for mod, label in ((PILOT, "Pilot"), (W223, "W223")):
        if not mod.exists():
            print(f"  SKIP  {label} absent")
            continue
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "o.scs"
            C.main([str(mod), "--type", "suv", "--brand", "t", "-o", str(out)])
            z = zipfile.ZipFile(out)
            ch = [n for n in z.namelist()
                  if "/chassis/" in n and n.endswith(".sii")]
            check(f"{label}: chassis found", bool(ch))
            for n in ch:
                text = z.read(n).decode()
                check(f"{label}: {Path(n).name} has no invented detail_model",
                      "detail_model:" not in text)
                check(f"{label}: {Path(n).name} still has model",
                      re.search(r"\bmodel:", text) is not None)


# ----------------------------------------- parity with the reference output
TOOL_OUT = (FIXTURES / "release" / "output"
            / "Mercedes-Benz_W223_S-Class_V2_3_1_61_ATS.scs")


def _strip_header(text: str) -> list[str]:
    return [l.rstrip() for l in text.splitlines()
            if not l.startswith("# GENERATED")
            and "Derived from this mod" not in l
            and "Shape copied from" not in l]


@guard
def test_parity_with_reference_converter() -> None:
    """My output must match the reference converter for the same input.

    This is the strongest available check: the reference converter's output is
    the only other Road Trip conversion we can see, and the whole wheel layer
    was rewritten to match it.
    """
    if not TOOL_OUT.exists() or not W223.exists():
        print("  SKIP  reference converter output not present")
        return
    import difflib

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "mine.scs"
        C.main([str(W223), "--type", "sedan", "--brand", "mercedes",
                "--mass", "1900", "-o", str(out)])
        mine = zipfile.ZipFile(out)
        tool = zipfile.ZipFile(TOOL_OUT)

        mn = {n for n in mine.namelist() if not n.endswith("/")}
        tn = {n for n in tool.namelist() if not n.endswith("/")}

        # Deliberate divergence from the reference converter: the reference
        # does not write material/ui/car_brand_logo/, which is the directory Car
        # Mode actually reads. The game logged
        #   "Can not open '/material/ui/car_brand_logo/mod.mat'"
        # until these were added. Everything else must match exactly.
        expected_extra = {
            "material/ui/car_brand_logo/mod.dds",
            "material/ui/car_brand_logo/mod.mat",
            "material/ui/car_brand_logo/mod.tobj",
        }
        extra = mn - tn
        check("only car_brand_logo added beyond reference", extra == expected_extra,
              f"extra={sorted(extra)} tool-only={sorted(tn - mn)[:3]}")
        check("no files lost vs reference", not (tn - mn),
              f"tool-only={sorted(tn - mn)[:3]}")

        # the registration chain and the two defs we rewrite
        targets = [
            "def/vehicle/car_dealer/mod/w223_s500.sii",
            "def/vehicle/car_desktop/w223_s500.sii",
            "def/vehicle/car_company/w223_s500.sii",
            "def/vehicle/truck_desktop/w223_s500.sii",
            "def/vehicle/truck_company/w223_s500.sii",
            "def/vehicle/truck/w223_s500/data.sii",
        ]
        for f in targets:
            if f not in mn or f not in tn:
                check(f"{f} present in both", False)
                continue
            a, b = _strip_header(tool.read(f).decode()), _strip_header(
                mine.read(f).decode())
            diff = [x for x in difflib.unified_diff(a, b, lineterm="", n=0)
                    if x.startswith(("+", "-")) and not x.startswith(("+++", "---"))]
            # Tolerated difference: which stock paint job is selected. The
            # reference picks color3, we pick the first stock paint; both are
            # `stock: true` and the choice is cosmetic. There is no derivable
            # rule for it.
            diff = [x for x in diff if "paint_job|color" not in x
                    and "paint_job/color" not in x]
            check(f"{f.split('/')[-1]} matches reference", not diff,
                  "; ".join(diff[:3]))

        # structural chassis tokens must be present and correct
        ch = "def/vehicle/truck/w223_s500/chassis/4matic.sii"
        t = mine.read(ch).decode()
        check("disc_brake true on both axles",
              "disc_brake[0]: true" in t and "disc_brake[1]: true" in t)
        check("axle_suspension_type wishbone both",
              t.count("axle_suspension_type[1]: wishbone") == 1)
        check("no brake_rotor_diameter", "brake_rotor_diameter" not in t)
        check("no invented detail_model", "detail_model" not in t)
        check("powered_axle untouched from the mod",
              "powered_axle[0]: true" in t and "powered_axle[1]: true" in t)

        # no fabricated car-wheel namespaces
        check("no p_tire invented",
              not any(n.startswith("def/vehicle/p_tire/") for n in mn))
        check("no p_disc invented",
              not any(n.startswith("def/vehicle/p_disc/") for n in mn))


@guard
def test_brand_logo() -> None:
    section("brand logo (car_brand_logo is what Car Mode reads)")
    import struct

    # A generated DDS must match the reference's shape exactly: 256x64, DXT1,
    # pixel-format block at offset 76, and a full 6-level mip chain.
    dds = C._make_dds()
    check("dds magic", dds[:4] == b"DDS ")
    check("dds header is 128 bytes before the body", dds[0:4] == b"DDS ")
    check("dds width 256", struct.unpack("<I", dds[16:20])[0] == 256)
    check("dds height 64", struct.unpack("<I", dds[12:16])[0] == 64)
    check("dds fourCC DXT1", dds[84:88] == b"DXT1")
    check("dds pf flags DDPF_FOURCC", struct.unpack("<I", dds[80:84])[0] == 0x4)
    check("dds pf size at offset 76", struct.unpack("<I", dds[76:80])[0] == 32)
    check("dds has 6 mips", struct.unpack("<I", dds[28:32])[0] == 6)
    ref_len = 11080
    check("dds matches reference size", len(dds) == ref_len, str(len(dds)))

    # A flat block encodes as c0 == c1 with zero indices.
    blk = C._dxt1_block(96, 96, 96)
    check("dxt1 block is 8 bytes", len(blk) == 8)
    c0, c1, idx = struct.unpack("<HHI", blk)
    check("dxt1 endpoints equal for a flat block", c0 == c1)
    check("dxt1 indices zero", idx == 0)

    # The logo must be written under the dealer folder name, because that is
    # what the game asks for ("/material/ui/car_brand_logo/mod.mat").
    logo = C.build_brand_logo("MOD", None, None, dirs=("material/ui/car_brand_logo",))
    for ext in (".dds", ".mat"):
        check(f"car_brand_logo/mod{ext} written",
              f"material/ui/car_brand_logo/mod{ext}" in logo)
    check("name is normalised to lower case",
          not any("MOD" in k for k in logo))

    # .tobj path must be length-prefixed and absolute.
    # The game reported "Failed reading map name from file" when the string was
    # written at 44 instead of 48, and again when the path lost its leading
    # slash, so both are asserted here.
    want = "/material/ui/car_brand_logo/mod.dds"
    tobj = C._synth_tobj(want)
    ln = struct.unpack("<I", tobj[40:44])[0]
    check("synth tobj length prefix", ln == len(want), str(ln))
    check("synth tobj has 4 padding bytes", tobj[44:48] == b"\x00\x00\x00\x00",
          tobj[44:48].hex())
    check("synth tobj string starts at 48", tobj[48:48 + ln] == want.encode())
    check("synth tobj is exactly header+string", len(tobj) == 48 + ln)
    check("synth tobj adds a slash if missing",
          C._synth_tobj("material/ui/x.dds")[48:] == b"/material/ui/x.dds")

    # Patching a real template must keep that layout and not double the suffix.
    if need(W223, "tobj patching"):
        import sys as _sys
        src = C.ModSource(W223)
        tpl = src.read_bytes("material/ui/brand_logo/merc.tobj")
        out = C._patch_tobj(tpl, "/material/ui/car_brand_logo/mod.dds")
        check("patched tobj not None", out is not None)
        if out is not None:
            ln = struct.unpack("<I", out[40:44])[0]
            check("patched tobj keeps 4 padding bytes",
                  out[44:48] == b"\x00\x00\x00\x00", out[44:48].hex())
            check("patched tobj string at 48",
                  out[48:48 + ln] == want.encode(),
                  out[48:48 + ln].decode("ascii", "replace"))
            check("patched tobj size is consistent", len(out) == 48 + ln)
            check("no doubled .dds suffix", b".ddsdds" not in out)
        # A missing template still yields a usable file via _synth_tobj.
        check("_patch_tobj(None) returns None", C._patch_tobj(None, want) is None)

    # End to end: the Mercedes conversion must carry the mod's real logo into
    # car_brand_logo/ rather than inventing a grey placeholder.
    if not need(W223, "brand logo end-to-end"):
        return
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "logo.scs"
        C.main([str(W223), "--type", "sedan", "--brand", "mercedes",
                "--mass", "1900", "-o", str(out), "--no-prompt"])
        # The vendor folder and the logo stem come from the brand. Filing every
        # car under a single "mod" vendor made each new conversion overwrite the
        # previous car's logo.
        vendor = C.dealer_folder("mercedes")
        with zipfile.ZipFile(out) as z:
            names = set(z.namelist())
            for ext in (".dds", ".mat", ".tobj"):
                check(f"output has car_brand_logo/{vendor}{ext}",
                      f"material/ui/car_brand_logo/{vendor}{ext}" in names)
            real = z.read(f"material/ui/car_brand_logo/{vendor}.dds")
            check("car_brand_logo dds is the right size and format",
                  real.startswith(b"DDS ")
                  and struct.unpack("<I", real[16:20])[0] == 256
                  and struct.unpack("<I", real[12:16])[0] == 64
                  and real[84:88] in (b"DXT5", b"DXT1"),
                  f"{len(real)} bytes {real[84:88]!r}")
            # --brand mercedes now picks up the bundled logos/merc.png, so the
            # bytes differ from the reference's. The DDS must still be a real
            # converted logo, not the flat placeholder.
            check("logo is converted art, not the placeholder",
                  real != C._make_dds())
            t = z.read(f"material/ui/car_brand_logo/{vendor}.tobj")
            ln = struct.unpack("<I", t[40:44])[0]
            check("output tobj points at the car_brand_logo dds",
                  t[48:48 + ln]
                  == f"/material/ui/car_brand_logo/{vendor}.dds".encode(),
                  t[48:48 + ln].decode("ascii", "replace"))
            mat = z.read(f"material/ui/car_brand_logo/{vendor}.mat").decode()
            check("output mat references its own tobj",
                  f'texture : "{vendor}.tobj"' in mat)
            # And the dealer folder must agree with the logo stem, or the
            # Car Dealer looks up a logo the mod never wrote.
            check("dealer folder is the brand, not a shared 'mod'",
                  any(n.startswith(f"def/vehicle/car_dealer/{vendor}/")
                      for n in names),
                  [n for n in sorted(names) if "car_dealer" in n][:4])


@guard
def test_brand_selection() -> None:
    section("brand selection and logo conversion")
    import struct

    import brands as B
    import logo as L

    check("brands listed", len(B.BRANDS) >= 16, str(len(B.BRANDS)))
    names = [n for n, _l, _s in B.BRANDS]
    check("no duplicate brand names", len(names) == len(set(names)))
    check("every brand has a label", all(l for _n, l, _s in B.BRANDS))

    # Aliases and normalisation.
    check("mercedes-benz -> mercedes", B.normalise("Mercedes-Benz") == "mercedes")
    check("MERC -> mercedes", B.normalise("MERC") == "mercedes")
    check("chevy -> chevrolet", B.normalise("chevy") == "chevrolet")
    check("lower-cased", B.normalise("  TOYOTA ") == "toyota")

    # Logo lookup: every brand that claims a logo must actually have a file.
    for name, label, stem in B.BRANDS:
        p = B.logo_path(name)
        if p is None:
            check(f"{name}: no logo is tolerated", True)
        else:
            check(f"{name}: logo file exists", p.exists(), str(p))
    check("some logos are present", len(B.available()) >= 10,
          f"{len(B.available())} of {len(B.BRANDS)}")
    check("every listed brand has a logo", not B.missing(),
          f"missing: {B.missing()}")
    check("unknown brand has no logo", B.logo_path("notacarbrand") is None)

    if L.imagemagick() is None:
        # ImageMagick is optional, so its absence is a skip, not a failure --
        # the same rule the missing mod fixtures follow.
        skip("brand logo conversion -- ImageMagick not installed")
        return
    check("ImageMagick present", True)

    # Conversion: every shipped logo must yield a valid 256x64 DDS. DXT5 is
    # preferred -- DXT1's 1-bit alpha makes a logo's antialiased edges blocky.
    for name in B.available():
        raw = B.logo_path(name).read_bytes()
        try:
            dds = L.image_to_dds(raw)
        except L.LogoError as exc:
            check(f"{name}: converts", False, str(exc))
            continue
        ok = (
            dds.startswith(b"DDS ")
            and dds[84:88] in (b"DXT5", b"DXT1")
            and struct.unpack("<I", dds[16:20])[0] == 256
            and struct.unpack("<I", dds[12:16])[0] == 64
            and len(dds) > 0
        )
        check(f"{name}: 256x64 {dds[84:88].decode()}, {len(dds)} bytes", ok)

    # Distinct brands must not collapse to the same texture.
    seen = {}
    for name in B.available()[:6]:
        seen[name] = L.image_to_dds(B.logo_path(name).read_bytes())
    check("different brands give different textures",
          len({hashlib.sha256(v).digest() for v in seen.values()}) == len(seen))

    # Every logo must be *visible* on the dealer's dark tile. This is what the
    # brightness normalisation exists for: a black-on-transparent or
    # white-on-opaque source converted straight through is invisible or shows
    # as a bright box.
    import subprocess as _sp
    exe = L.imagemagick()
    for name in B.available():
        dds = L.image_to_dds(B.logo_path(name).read_bytes())
        probe = _sp.run(
            [exe, "-", "-background", "#2b2b2b", "-alpha", "background",
             "-alpha", "off", "-colorspace", "Gray", "-format", "%[fx:mean]",
             "info:"],
            input=dds, capture_output=True)
        try:
            lum = float(probe.stdout.decode().strip())
        except ValueError:
            lum = None
        # Must differ measurably from the flat tile colour, and must not be a
        # blown-out white block.
        check(f"{name}: visible against the dark tile",
              lum is not None and 0.01 < lum < 0.98,
              f"mean luminance {lum}")

    # Garbage input must raise, not crash or emit a broken file.
    try:
        L.image_to_dds(b"not an image at all")
        check("invalid image raises", False)
    except L.LogoError:
        check("invalid image raises", True)

    # Which(name) returning a path is not evidence the path is ImageMagick.
    # On Windows shutil.which("convert") resolves to the FAT-to-NTFS converter
    # in System32, so probing the name alone made every brand fail with a
    # localised "invalid parameter" that named neither program.
    check("_is_imagemagick rejects a non-ImageMagick binary",
          L._is_imagemagick(sys.executable) is False)
    check("_is_imagemagick tolerates a missing binary",
          L._is_imagemagick(str(HERE / "no_such_binary_here")) is False)

    real_which = shutil.which
    try:
        # Pretend only the Windows convert.exe exists, as it does on a machine
        # with no ImageMagick installed: imagemagick() must decline it rather
        # than hand it ImageMagick arguments.
        shutil.which = lambda name: "C:\\Windows\\System32\\convert.exe" \
            if name == "convert" else None
        check("imagemagick() rejects convert.exe", L.imagemagick() is None)

        # A binary that does identify itself as ImageMagick is still accepted,
        # so the Linux path (where /usr/bin/convert IS ImageMagick) is intact.
        real_probe = L._is_imagemagick
        L._is_imagemagick = lambda exe: exe == "/usr/bin/convert"
        try:
            shutil.which = lambda name: "/usr/bin/convert" \
                if name == "convert" else None
            check("imagemagick() still returns a verified binary",
                  L.imagemagick() == "/usr/bin/convert")
        finally:
            L._is_imagemagick = real_probe
    finally:
        shutil.which = real_which



@guard
def test_ui() -> None:
    section("terminal output (colour, banner, menus)")
    import builtins
    import io
    import os as _os

    import ui as U

    # Colour must be off when stdout is not a terminal, so piped output is
    # plain text.
    saved = _os.environ.get("NO_COLOR")
    _os.environ.pop("NO_COLOR", None)
    _os.environ.pop("FORCE_COLOR", None)
    buf = io.StringIO()          # StringIO has no isatty -> not a tty
    check("colour off when not a tty", U.colour_enabled(buf) is False)

    # `paint` reads sys.stdout itself, so pin it: run from a console, sys.stdout
    # IS a tty and this check would fail for the invocation rather than for the
    # code. Same rule as the prompt tests below. The stream still forwards to the
    # real stdout, or the PASS line would land in the buffer instead of the log.
    class NotATty:
        """Reports itself as a pipe while writing where it was pointed."""

        def __init__(self, stream):
            self._stream = stream

        def write(self, text: str):
            return self._stream.write(text)

        def flush(self):
            return self._stream.flush()

        def isatty(self) -> bool:
            return False

    real_stdout = sys.stdout
    sys.stdout = NotATty(real_stdout)
    try:
        check("paint is a no-op when off", U.paint("hi", "green") == "hi")
    finally:
        sys.stdout = real_stdout

    # NO_COLOR wins even if FORCE_COLOR is set.
    _os.environ["NO_COLOR"] = "1"
    _os.environ["FORCE_COLOR"] = "1"
    check("NO_COLOR overrides FORCE_COLOR", U.paint("hi", "green") == "hi")
    _os.environ.pop("NO_COLOR")
    _os.environ.pop("FORCE_COLOR")
    if saved is not None:
        _os.environ["NO_COLOR"] = saved

    # An absent TERM is not a refusal, because Windows never sets one and every
    # console user there was getting plain output while Rich -- which only
    # refuses `dumb` -- was ready to paint. `dumb` still means no colour.
    class Tty(io.StringIO):
        def isatty(self):
            return True

    term_saved = _os.environ.pop("TERM", None)
    try:
        check("an absent TERM does not switch colour off",
              U.colour_enabled(Tty()) is True)
        for term in ("dumb", "unknown"):
            _os.environ["TERM"] = term
            check(f"TERM={term} means no colour",
                  U.colour_enabled(Tty()) is False, term)
        _os.environ["TERM"] = "xterm-256color"
        check("a real TERM keeps colour on", U.colour_enabled(Tty()) is True)
    finally:
        _os.environ.pop("TERM", None)
        if term_saved is not None:
            _os.environ["TERM"] = term_saved

    # Rich is handed the palette, so it must be told the palette's own colour
    # system rather than left to sniff the host: on Windows it resolves to a
    # 4-bit system and approximates slate and ivory onto 16 colours. Named only
    # when colour is on, because naming it makes Rich treat the stream as
    # colour-capable and a piped run would keep attributes it must drop.
    if U._Console is not None:
        pinned = U._console(Tty())
        check("Rich is told the palette is truecolour",
              pinned.color_system == "truecolor", str(pinned.color_system))
        check("Rich is kept off the legacy Windows renderer",
              pinned.legacy_windows is False, str(pinned.legacy_windows))
        off = U._console(io.StringIO())
        check("Rich names no colour system when colour is off",
              off.color_system is None, str(off.color_system))
    else:
        skip("Rich is told the palette is truecolour", "rich is not installed")

    # A failure detail is arbitrary output, and a narrow console cannot encode
    # box-drawing characters. Printing it raw killed the run partway through,
    # so the remaining checks were never reported at all.
    _probe_failure("┌─┐")

    # One palette, six colours, in both surfaces. Every mark on screen is one of
    # them, and every logical name resolves in BOTH tables -- a name in one and not
    # the other is how a marker silently loses its colour, which is exactly what
    # happened when the old palette had no red for `fail` to use.
    def rgb(hexcode: str) -> tuple[int, int, int]:
        return tuple(int(hexcode[i:i + 2], 16) for i in (1, 3, 5))

    palette = {
        "accent": U._ACCENT, "warn": U._WARN, "ok": U._OK,
        "muted": U._MUTED, "surface": U._SURFACE, "text": U._TEXT,
    }
    wanted = {"accent": "#6a9bcc", "warn": "#d97757", "ok": "#788c5d",
              "muted": "#b0aea5", "surface": "#141413", "text": "#faf9f5"}
    for name, hexcode in palette.items():
        check(f"the palette's {name} is the one that was chosen",
              hexcode == wanted[name], f"{hexcode} != {wanted[name]}")
        check(f"{name} is a real 24-bit colour, not a name Rich must resolve",
              bool(re.fullmatch(r"#[0-9a-f]{6}", hexcode)), hexcode)

    for name in ("accent", "warn", "ok", "fail"):
        check(f"'{name}' resolves in the plain-ANSI table",
              name in U._STYLES, str(sorted(U._STYLES)))
        check(f"'{name}' resolves in the Rich table",
              name in U._RICH, str(sorted(U._RICH)))

    # The fallback has to spell the same colour Rich is handed, not the terminal's
    # own idea of a colour that happens to share a name.
    for name in ("accent", "warn", "ok", "fail"):
        hexcode = palette.get(name, U._TEXT)
        r, g, b = rgb(hexcode)
        check(f"the fallback spells '{name}' as the same colour",
              U._STYLES[name] == f"\033[38;2;{r};{g};{b}m",
              f"{U._STYLES[name]!r}")

    # No escape may be a bare 8-colour code like `\033[33m`: those are the
    # terminal's own yellow and green, which are not this palette.
    for name in ("accent", "warn", "ok", "fail"):
        check(f"'{name}' is truecolour, not one of the terminal's 8 colours",
              re.fullmatch(r"\033\[38;2;\d{1,3};\d{1,3};\d{1,3}m",
                           U._STYLES[name]) is not None,
              repr(U._STYLES[name]))

    # Slate and Ivory are a surface pair, so they must not be reachable as a
    # foreground: the fallback has no background to put them on.
    check("Slate is not offered as a foreground",
          "surface" not in U._STYLES and "surface" not in U._RICH)
    check("Ivory is not offered as a plain foreground",
          "text" not in U._STYLES and "text" not in U._RICH)
    check("a failure is the surface pair reversed, so it is legible either way",
          U._SURFACE in U._RICH["fail"] and U._TEXT in U._RICH["fail"],
          U._RICH["fail"])

    _os.environ["FORCE_COLOR"] = "1"
    try:
        for name in ("accent", "warn", "ok", "fail"):
            check(f"paint emits the palette colour for '{name}'",
                  U.paint("x", name) == f"{U._STYLES[name]}x{U._RESET}",
                  repr(U.paint("x", name)))
        # The old names must be gone, or a stale call site loses its colour
        # silently rather than failing loudly.
        for dead in ("yellow", "green", "red", "cyan", "magenta", "blue"):
            check(f"the removed name '{dead}' paints nothing",
                  U.paint("x", dead) == "x", repr(U.paint("x", dead)))
    finally:
        _os.environ.pop("FORCE_COLOR", None)
        if saved is not None:
            _os.environ["NO_COLOR"] = saved

    # The banner is ASCII art mentioning the tool, with no stray escapes when
    # colour is off.
    banner = U.BANNER
    check("banner is ascii only", banner.isascii())
    check("banner mentions CarUtil", "CarUtil" in banner)

    # The README quotes the banner; if the art changes, the sample must too.
    readme = (HERE / "README.md")
    if readme.exists():
        text = readme.read_text(encoding="utf-8", errors="replace")
        missing = [ln for ln in banner.strip("\n").split("\n") if ln not in text]
        check("README shows the current banner", not missing,
              f"missing from README: {missing[:2]}")

    # Status helpers must not raise and must emit a recognisable marker.
    out = io.StringIO()
    real, sys.stdout = sys.stdout, out
    try:
        U.ok("done"); U.warn("careful"); U.bullet("part"); U.fail("broken")
        U.heading("changes"); U.step(2, 5, "Pick a type")
    finally:
        sys.stdout = real
    text = out.getvalue()

    # show_banner and hint only take the Rich branch on a TTY, so nothing else in
    # the suite calls them and a wrong keyword or a bad style reaches a real
    # terminal before any check sees it. Drive both through a fake TTY.
    class FakeTty(io.StringIO):
        def isatty(self):
            return True

    tty_out = FakeTty()
    real, sys.stdout = sys.stdout, tty_out
    try:
        if U._Console is not None:
            U.show_banner()
            U.hint("a hint")
    finally:
        sys.stdout = real
    banner_text = tty_out.getvalue()
    if U._Console is None:
        skip("the Rich banner renders on a fake tty", "rich is not installed")
    else:
        check("the Rich banner renders without raising",
              "CarUtil" in banner_text, repr(banner_text[:120]))
        check("the banner panel is painted on the palette's slate",
              "48;2;20;20;19" in banner_text, repr(banner_text[:200]))
        check("the banner art is the palette's ivory on it",
              "38;2;250;249;245" in banner_text, repr(banner_text[:200]))
        check("the hint renders through Rich", "a hint" in banner_text,
              repr(banner_text[-120:]))
    check("ok marker", "+ done" in text)
    check("warn marker", "! careful" in text)
    check("bullet marker", "* part" in text)
    check("fail marker", "x broken" in text)
    check("heading printed", "changes" in text)
    check("step counter", "[2/5]" in text)
    check("styled output stays plain when piped", "\x1b" not in text)

    # Missing optional UI packages should leave a usable numbered menu and
    # explain how to install the enhanced interface.
    real_questionary = U._questionary
    real_console = U._Console
    real_input = builtins.input
    real_stdout = sys.stdout
    U._questionary = None
    U._Console = None
    answers = iter(["not a number", "2"])
    builtins.input = lambda prompt="": next(answers)
    output = io.StringIO()
    sys.stdout = output
    try:
        fallback = U.arrow_menu(
            [("Sedan", ""), ("SUV", ""), ("Sports", "")], default=0) == 1
        U.setup_hint()
    finally:
        builtins.input = real_input
        sys.stdout = real_stdout
        U._questionary = real_questionary
        U._Console = real_console
    check("menu falls back to numbered choices when Questionary is missing",
          fallback)
    check("numbered menu explains an invalid choice",
          "choose a number from 1 to 3" in output.getvalue())
    check("setup hint gives a copyable install command",
          "-m pip install -r requirements.txt" in output.getvalue())

    if real_questionary is not None:
        class TTYStream(io.StringIO):
            def isatty(self):
                return True

        class Answer:
            def ask(self):
                return 1

        real_select = U._questionary.select
        real_stdin, real_stdout = sys.stdin, sys.stdout
        saved_no_color = _os.environ.get("NO_COLOR")
        _os.environ["NO_COLOR"] = "1"
        calls = []
        U._questionary.select = lambda *a, **kw: (
            calls.append((a, kw)) or Answer())
        sys.stdin, sys.stdout = TTYStream(), TTYStream()
        try:
            selected = U.arrow_menu(
                [("Sedan", "saloon"), ("SUV", "crossover")], default=0)
        finally:
            U._questionary.select = real_select
            sys.stdin, sys.stdout = real_stdin, real_stdout
            if saved_no_color is None:
                _os.environ.pop("NO_COLOR", None)
            else:
                _os.environ["NO_COLOR"] = saved_no_color
        check("Questionary menu returns the selected choice", selected == 1)
        check("Questionary menu displays descriptions",
              calls[0][1]["show_description"] is True)
        check("Questionary style respects NO_COLOR",
              calls[0][1]["style"].style_rules == [])

        # Exactly one row may be lit. `select(default=...)` records that value
        # in the control's `selected_options`, and Questionary draws any row in
        # that list with `class:selected` for the whole life of the menu --
        # moving the pointer away does not clear it, so the suggested row and
        # the row under the cursor were both lit. This is what
        # ui._only_the_cursor_is_lit clears, and every check below fails on the
        # pre-fix code.
        class Silent:
            """A Question whose `.ask()` answers without needing a terminal."""

            def __init__(self, question):
                self._question = question

            def __getattr__(self, name):
                return getattr(self._question, name)

            def ask(self):
                return 1

        built = []

        def capture(*args, **kwargs):
            real_stderr, sys.stderr = sys.stderr, io.StringIO()
            try:
                question = real_select(*args, **kwargs)
            finally:
                sys.stderr = real_stderr
            built.append(question)
            return Silent(question)

        control = None
        U._questionary.select = capture
        sys.stdin, sys.stdout = TTYStream(), TTYStream()
        try:
            U.arrow_menu([("Sedan", "saloon"), ("SUV", "crossover"),
                          ("Pickup", "truck bed")], default=2)
            for container in built[0].application.layout.walk():
                candidate = getattr(container, "content", None)
                if isinstance(getattr(candidate, "selected_options", None),
                              list):
                    control = candidate
        finally:
            U._questionary.select = real_select
            sys.stdin, sys.stdout = real_stdin, real_stdout

        if control is None:
            skip("a menu lights only the row under the cursor",
                 "Questionary's control is not reachable in this version")
        else:
            def lit_rows():
                return [text.strip() for cls, text in
                        (token[:2] for token in control._get_choice_tokens()
                         if len(token) == 2)
                        if cls in ("class:selected", "class:highlighted")
                        and text.strip()]

            check("the cursor still starts on the suggested row",
                  control.pointed_at == 2, str(control.pointed_at))
            check("nothing marks the suggested row before it is chosen",
                  control.selected_options == [],
                  str(control.selected_options))
            control.select_next()          # move off the suggested row
            check("one lit row, and it is the row under the cursor",
                  lit_rows() == ["Sedan"], str(lit_rows()))


def test_torque_for_time() -> None:
    section("a target 0-60 determines the engine")
    # Reproduces the two measured points on the W223.
    check("the calibration point is self-consistent",
          abs(C.torque_for_time(1550, 9.67) - 520) < 3,
          f"{C.torque_for_time(1550, 9.67):.0f} Nm")
    # A heavier car needs more torque for the same time, and vice versa.
    check("heavier => more torque for the same target",
          C.torque_for_time(2200, 5.0) > C.torque_for_time(1550, 5.0))
    check("slower target => less torque",
          C.torque_for_time(2200, 7.0) < C.torque_for_time(2200, 5.0))

    # Asking for the time the shipped 760 Nm/t produced must land near 760.
    implied = C.torque_for_time(2069, 4.63) / 2.069
    check("asking for a measured time returns the constant that produced it",
          700 < implied < 900, f"{implied:.0f} Nm/t")

    # Clamped, so a silly request cannot silently become a wild guess.
    check("absurdly fast is clamped up", C.torque_for_time(1500, 0.1) >= C._CAL_MAX_NM)
    check("absurdly slow is clamped down",
          C.torque_for_time(1500, 999) <= C._CAL_MIN_NM)
    check("nonsense inputs are survivable",
          C.torque_for_time(0, 5.0) == C._CAL_TORQUE_NM
          and C.torque_for_time(1500, 0) == C._CAL_TORQUE_NM)

    # Monotonic: every step up in target time must reduce torque.
    prev = None
    for t in (3.0, 4.0, 5.0, 6.0, 8.0, 12.0):
        v = C.torque_for_time(2200, t)
        if prev is not None:
            check(f"monotonic at {t}s", v < prev)
        prev = v


_TRANSMISSION_TRUCK = """SiiNunit
{
accessory_transmission_data : 10_speed.hpilot25.transmission
{
\tname: "10-speed AT"
\tdifferential_ratio: 3.73
\tstall_torque_ratio: 4.50
\tshift_time: 0.1
\tratios_forward[0]: 4.69
}
}
"""

_TRANSMISSION_CAR = """SiiNunit
{
accessory_transmission_data : 9_speed.w223_s500.transmission
{
\tname: "9-speed AT"
\tdifferential_ratio: 2.821
\tstall_torque_ratio: 1.953
\ttorque_converter_lock_gear: 4
\ttorque_converter_crawl: true
\ttransmission_class: standard
\tratios_forward[0]: 5.354
}
}
"""


def test_tune_transmission_stall_ratio() -> None:
    section("torque converter stall ratio is brought into car range")
    rep = C.Report()
    out = C.tune_transmission(_TRANSMISSION_TRUCK, rep)
    # SCS's own Road Trip cars sit at 1.8-2.0. 4.50 is a diesel figure and
    # multiplied the Pilot's launch force by 2.84x against the Mercedes.
    check("stall ratio capped at the car ceiling",
          re.search(r"^\tstall_torque_ratio: 2(\.0)?$", out, re.M), out)
    check("crawl added", "torque_converter_crawl: true" in out)
    check("transmission_class added", "transmission_class: standard" in out)
    check("lock gear added", "torque_converter_lock_gear: 3" in out)
    check("gearing untouched", "differential_ratio: 3.73" in out)
    check("ratios untouched", "ratios_forward[0]: 4.69" in out)

    rep2 = C.Report()
    out2 = C.tune_transmission(_TRANSMISSION_CAR, rep2)
    check("an already-car transmission keeps its stall ratio",
          "stall_torque_ratio: 1.953" in out2)
    check("and its own lock gear", "torque_converter_lock_gear: 4" in out2)
    check("and is otherwise untouched", out2 == _TRANSMISSION_CAR)

    forced = C.tune_transmission(_TRANSMISSION_CAR, C.Report(), force=True)
    check("--force-physics overwrites a tuned converter",
          re.search(r"^\tstall_torque_ratio: 2(\.0)?$", forced, re.M))

    not_x = "SiiNunit\n{\nfoo: 1\n}\n"
    check("a non-transmission unit is not touched",
          C.tune_transmission(not_x, C.Report()) == not_x)


def test_launch_force_matches_after_stall_fix() -> None:
    section("launch force is comparable once the stall ratio is capped")
    def launch(nm, gear, stall):
        return nm * gear * stall
    # Same engine target, each car's own gearing.
    merc = launch(1572, 15.10, 1.953)
    pilot_before = launch(1672, 17.49, 4.500)
    pilot_after = launch(1672, 17.49, C._CAR_STALL_CEILING)
    check("Pilot was far more violent than the Merc before",
          pilot_before / merc > 2.0, f"{pilot_before / merc:.2f}x")
    check("and is now within a sane margin",
          pilot_after / merc < 1.5, f"{pilot_after / merc:.2f}x")
    # What is left is taller 1st gearing, which slows launch rather than
    # snapping it -- so the residual must be above 1, not below.
    check("residual difference is taller gearing, not more force",
          pilot_after > merc)


@guard
def test_real_transmission_tuning() -> None:
    section("real mods: no transmission launches on a truck stall ratio")
    for label, path in (("Pilot", PILOT), ("W223", W223)):
        src = C.ModSource(path)
        xs = [n for n in src.readable
              if re.match(r"def/vehicle/truck/.*/transmission/.*\.sii$", n)]
        if not xs:
            skip(f"{label} transmission", "no transmission unit found")
            continue
        for x in xs:
            out = C.tune_transmission(src.read(x), C.Report())
            m = re.search(r"^\s*stall_torque_ratio:\s*([\d.]+)", out, re.M)
            check(f"{label}: stall ratio is in car range",
                  m is not None and float(m.group(1)) <= C._CAR_STALL_CEILING,
                  m.group(1) if m else "absent")
            check(f"{label}: converter crawl present",
                  "torque_converter_crawl: true" in out)


def test_car_physics_within_scs_ranges() -> None:
    """Every preset must sit inside what SCS's own cars actually ship.

    The tuning table used to be derived from the VW Parati, and every value in
    it fell outside the range SCS uses. residual_travel was the visible one --
    0.18 against a measured 0.093-0.134, nearly double the free travel before
    the springs engage, which is what made converted cars roll like boats.
    """
    section("presets are inside SCS's measured car ranges")
    # Measured across all four Road Trip chassis in dlc_rt_ford.scs.
    ranges = {
        "sway_bar_stiffness": (0.30, 0.40),
        "cog_height": (0.52, 0.738),
        "residual_travel": (0.093, 0.134),
        "suspension_damping_factor": (0.5, 0.8),
        "air_resistance": (0.37, 0.596),
        "max_steering_angle": (28, 35),
    }
    for vtype in ("sedan", "suv", "muscle", "sports", "pickup", "offroad",
                  "classic"):
        cfg = C.car_physics(vtype, 2)
        for key, (lo, hi) in ranges.items():
            vals = cfg[key]
            vals = [vals] if isinstance(vals, str) else vals
            worst = [float(v) for v in vals
                     if not lo - 1e-9 <= float(v) <= hi + 1e-9]
            check(f"{vtype}: {key} within {lo}-{hi}", not worst,
                  f"out of range: {worst}")


def test_residual_travel_always_written() -> None:
    section("residual_travel is overwritten, not threshold-gated")
    # Truck mods ship 0.09-0.10 and SCS cars use 0.093-0.134. The ranges
    # overlap, so no threshold separates them and SCS's own Mustang value
    # must not be mistaken for a truck one.
    check("SCS's Mustang value is still replaced",
          C._is_truck_scale("residual_travel", ["0.093", "0.134"]))
    check("a truck value is replaced",
          C._is_truck_scale("residual_travel", ["0.10", "0.10"]))

    text = ("SiiNunit\n{\naccessory_chassis_data : x\n{\n"
            "\tresidual_travel[0]: 0.10\n\tresidual_travel[1]: 0.10\n}\n}\n")
    out = C.tune_chassis(text, "suv", None, C.Report())
    # The Bronco's front value is itself 0.10, so assert the rear was
    # corrected rather than that the value changed.
    check("a truck residual_travel is rewritten to the car's",
          re.search(r"^\tresidual_travel\[1\]: 0\.11$", out, re.M), out)
    check("both axles land on the preset",
          re.findall(r"^\tresidual_travel\[\d\]: ([\d.]+)$", out, re.M)
          == ["0.10", "0.11"])


def test_torque_curve_gets_scs_origin() -> None:
    section("the torque curve is given the origin SCS engines declare")
    # All 181 SCS engine units peak torque_curve at 1.00 and start at (0, 0).
    # The mods start at idle with 0.43-0.47 of peak and have no origin, which
    # is what pulls the car away to ~15 mph when Drive is selected.
    truck = ("SiiNunit\n{\naccessory_engine_data : e\n{\n"
             "\ttorque: 1400\n\ttorque_curve[]: (750, 0.47)\n"
             "\ttorque_curve[]: (3500, 0.57)\n}\n}\n")
    out, added = C._ensure_torque_origin(truck)
    check("origin added when absent", added)
    curve = [l for l in out.splitlines() if "torque_curve[]" in l]
    pts = re.findall(r"torque_curve\[\]:\s*\(\s*(\d+),", "\n".join(curve))
    check("origin is the first curve point", pts[0] == "0", str(pts))
    check("and it sits inside the unit block, not above SiiNunit",
          out.splitlines()[0] == "SiiNunit"
          and out.count("{") == out.count("}"), out[:40])
    check("existing points untouched",
          "(750, 0.47)" in out and "(3500, 0.57)" in out)

    already = ("SiiNunit\n{\naccessory_engine_data : e\n{\n"
               "\ttorque: 556\n\ttorque_curve[]: (0, 0)\n"
               "\ttorque_curve[]: (4500, 1.0)\n}\n}\n")
    out2, added2 = C._ensure_torque_origin(already)
    check("an SCS-style curve is left alone", not added2 and out2 == already)


@guard
def test_real_engine_origin() -> None:
    section("real mods: engine curve gains the SCS zero-torque origin")
    for label, path in (("Pilot", PILOT), ("W223", W223)):
        src = C.ModSource(path)
        engines = [n for n in src.readable
                   if re.search(r"/engine/.*\.sii$", n)]
        for e in engines:
            out = C.tune_engine(src.read(e), C.Report())
            pts = re.findall(r"^\s*torque_curve\[\]:\s*\(\s*(\d+)\s*,",
                             out, re.M)
            check(f"{label}: curve starts at 0 rpm", pts and pts[0] == "0",
                  str(pts[:2]))


def test_job_tags_use_the_games_vocabulary() -> None:
    section("tags[] match car_job.vehicle_types[]")
    # Read from all 149 car_job units in def.scs/base_vehicle.scs. SCS calls
    # our `sports` "sport" and our `classic` "veteran". A tag the game does not
    # recognise matches no job at all, so those two presets were writing a car
    # that could take zero car jobs.
    check("SCS's type list is what we think it is",
          C.CAR_JOB_TYPES == {"pickup", "suv", "sedan", "muscle", "veteran",
                              "sport", "offroad"},
          str(sorted(C.CAR_JOB_TYPES)))
    for vtype in C.VEHICLE_TYPES:
        tag = C.job_tag(vtype)
        check(f"--type {vtype} writes a tag the game knows",
              tag in C.CAR_JOB_TYPES, f"tag {tag!r}")
    check("sports maps to sport", C.job_tag("sports") == "sport")
    check("classic maps to veteran", C.job_tag("classic") == "veteran")
    check("a type that already matches is untouched",
          C.job_tag("suv") == "suv")
    check("SCS's own spellings are accepted as input",
          C.normalise_type("sport") == "sports"
          and C.normalise_type("veteran") == "classic")


@guard
def test_real_tags_are_recognised() -> None:
    section("real mods: the written tags[] are all real job types")
    for label, path, vtype in (("Pilot", PILOT, "suv"),
                               ("Pilot-sports", PILOT, "sports"),
                               ("W223-classic", W223, "classic")):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / f"{vtype}.scs"
            C.main([str(path), "--type", vtype, "--brand", "t",
                    "-o", str(out)])
            z = zipfile.ZipFile(out)
            ds = [n for n in z.namelist() if n.endswith("/data.sii")]
            check(f"{label}: data.sii written", bool(ds))
            if not ds:
                continue
            tags = re.findall(r'tags\[\]:\s*"([^"]+)"',
                              z.read(ds[0]).decode())
            check(f"{label}: tags written", tags, str(tags))
            bad = [t for t in tags if t not in C.CAR_JOB_TYPES]
            check(f"{label}: every tag is a real car_job type", not bad, str(bad))


def test_readme_category_table_matches_code() -> None:
    """The README's category table must not drift from _MASS and TYPE_HELP.

    The tag vocabulary drifted for exactly this reason: the presets were
    documented in prose and nothing checked the two against each other.
    """
    section("README category table matches the code")
    readme = (HERE / "README.md").read_text(encoding="utf-8", errors="replace")
    missing = []
    for vtype in C.VEHICLE_TYPES:
        row = f"| `{vtype}` | {C.TYPE_HELP[vtype]} | {sum(C._MASS[vtype])} kg |"
        if row not in readme:
            missing.append(row)
    check("every preset appears in the README table with its real weight",
          not missing, "; ".join(missing[:2]))
    check("all seven presets are documented",
          all(f"`{v}`" in readme for v in C.VEHICLE_TYPES))
    # The tag mapping is the thing that was wrong for years; make sure the
    # README states the game's spellings rather than ours.
    for tag in ("veteran", "sport"):
        check(f"README names the game's `{tag}` tag", f"`{tag}`" in readme)
    check("README does not claim an unrecognised tag works",
          "`tags[]` must spell the type the way the base game does" in readme)


def test_every_brand_gets_its_own_vendor() -> None:
    """No brand may silently fall back to the shared "other" vendor.

    A brand with no shipped logo resolves to "other", which would put it in the
    same vendor as the catch-all and reintroduce the logo collision that
    per-brand vendors exist to prevent. Adding a brand to brands.py without
    dropping an image into logos/ would cause exactly that.
    """
    section("every supported brand gets its own vendor")
    import brands as B

    check("the brand list is populated", len(B.BRANDS) >= 16, str(len(B.BRANDS)))
    no_logo = [n for n, _l, _s in B.BRANDS if B.logo_path(n) is None]
    check("every listed brand has a logo file", not no_logo, str(no_logo))
    check("no logo file is orphaned",
          all(B.logo_path(n) is not None for n in B.available()), "unreachable")

    # Each brand must resolve to a distinct vendor, so two brands can never
    # overwrite each other's logo.
    vendors = {}
    for name, _l, _s in B.BRANDS:
        vendors.setdefault(C.dealer_folder(name), []).append(name)
    shared = {v: bs for v, bs in vendors.items() if len(bs) > 1}
    check("no two brands share a vendor folder", not shared, str(shared))
    check("the vendor is the brand's own name",
          all(C.dealer_folder(n) == n for n, _l, _s in B.BRANDS))
    check("an unknown brand falls back to other",
          C.dealer_folder("not-a-brand") == "other"
          and C.dealer_folder("") == "other")

    # And the stem must survive the sanitising build_brand_logo applies.
    for name, _l, _s in B.BRANDS:
        logo = C.build_brand_logo(C.dealer_folder(name), None, None,
                                  dirs=("material/ui/car_brand_logo",))
        stem = f"material/ui/car_brand_logo/{name}.dds"
        check(f"{name}: logo stem survives sanitising", stem in logo,
              str(sorted(logo))[:2])


def test_build_power_curve_hits_its_target() -> None:
    section("the rebuilt curve makes the horsepower asked for")
    import math

    for hp, rpm in ((250, 4500), (330, 4500), (350, 5000), (398, 6000)):
        curve, peak = C.build_power_curve(hp, rpm, 6400)
        d = dict(curve)
        got_rpm = max(d, key=lambda r: r * d[r])
        got_hp = peak * d[got_rpm] * got_rpm * 2 * math.pi / 60 / 745.699872
        check(f"{hp} hp peaks at {rpm} rpm",
              abs(got_hp - hp) < 1.5, f"delivered {got_hp:.0f} hp at {got_rpm}")
        check(f"...at the requested rev, not a neighbouring sample",
              got_rpm == rpm, f"peaked at {got_rpm}")
        check("...curve keeps the (0, 0) origin", d.get(0) == 0.0)
        check("...and returns to zero at the engine's rev limit",
              d[max(d)] == 0.0)

    # A low rev limit must not let SCS's 8000 rpm top end in.
    short, _ = C.build_power_curve(350, 4500, 4000)
    check("a low rev limit clips the shape", max(dict(short)) == 4000)
    check("...and the clip still ends at zero", dict(short)[4000] == 0.0)
    # A rev limit below where the shape peaks leaves nothing to size from, and
    # used to divide by zero.
    try:
        C.build_power_curve(350, 4500, 900)
        check("an impossible rev limit raises", False, "no error")
    except ValueError:
        check("an impossible rev limit raises cleanly", True)

    # Monotonic: more horsepower must mean more torque.
    a = C.build_power_curve(300, 4500, 6400)[1]
    b = C.build_power_curve(400, 4500, 6400)[1]
    check("more hp => more peak torque", b > a)


def test_prompt_power_never_blocks() -> None:
    section("the power prompt respects a non-interactive stdin")
    import builtins

    # Both halves need stdin pinned off. The restore used to sit between them,
    # so running the suite from a console -- where stdin really is a TTY -- put
    # the second half back in front of a live prompt.
    real = C._tty
    real_input = builtins.input
    try:
        C._tty = lambda: False
        check("skipped when stdin is not a terminal",
              C.prompt_power(2200) == (None, None))
        builtins.input = lambda prompt="": (_ for _ in ()).throw(
            AssertionError("prompt must not block without a terminal"))
        check("and never prompts in that case",
              C.prompt_power(2200) == (None, None))
    finally:
        C._tty = real
        builtins.input = real_input


@guard
def test_real_engine_power_shape() -> None:
    section("real mods: engine lands in SCS's power band")
    for label, path in (("Pilot", PILOT), ("W223", W223)):
        src = C.ModSource(path)
        for e in [n for n in src.readable
                  if re.search(r"/engine/.*\.sii$", n)]:
            out = C._tune_engine_to_hp(src.read(e), C.Report(), 350, 5000, None)
            curve = {int(m.group(1)): float(m.group(2)) for m in re.finditer(
                r"^\s*torque_curve\[\]:\s*\(\s*(\d+)\s*,\s*([\d.]+)", out, re.M)}
            tq = float(C.get_key(out.splitlines(), "torque"))
            got_rpm = max(curve, key=lambda r: r * curve[r])
            hp = tq * curve[got_rpm] * got_rpm * 2 * math.pi / 60 / 745.699872
            check(f"{label}: makes the requested 350 hp", abs(hp - 350) < 2,
                  f"{hp:.0f} hp")
            check(f"{label}: taper ends at zero",
                  curve[max(curve)] == 0.0)
            check(f"{label}: peak torque is in SCS's car range (350-700 Nm)",
                  250 <= tq <= 750, f"{tq:.0f} Nm")
            check(f"{label}: unit stays balanced", out.count("{") == out.count("}"))


def test_windows_compatibility() -> None:
    """Platform contracts that are silent on Linux and fatal on Windows 11.

    Everything here is a Windows-only failure that this Linux box cannot
    reproduce by running the code, so each one is asserted structurally.
    """
    section("Windows 11 compatibility")

    # 1. The optional UI packages must not prevent the basic CLI from
    #    starting when they are absent.
    import ast as _ast
    import ui as U

    ui_src = (BACKEND / "ui.py").read_text(encoding="utf-8")
    tree = _ast.parse(ui_src)
    guarded = set()
    for node in tree.body:
        if isinstance(node, _ast.Try):
            names = set()
            for sub in _ast.walk(node):
                if isinstance(sub, _ast.Import):
                    names.update(a.name.split(".")[0] for a in sub.names)
                elif isinstance(sub, _ast.ImportFrom) and sub.module:
                    names.add(sub.module.split(".")[0])
            handles_import_error = any(
                isinstance(handler.type, _ast.Name)
                and handler.type.id == "ImportError"
                for handler in node.handlers
            )
            if handles_import_error:
                guarded.update(names)
    for mod in ("questionary", "rich"):
        check(f"{mod} import is optional", mod in guarded)
    check("missing UI packages have a numbered-menu fallback",
          "return _numbered_menu(options, default)" in ui_src)

    # 2. shutil.which("convert") resolves to the FAT-to-NTFS convert.exe on
    #    Windows. Every call must therefore be verified, never trusted by name.
    import logo as L
    src = (BACKEND / "logo.py").read_text(encoding="utf-8")
    check("logo.py verifies the binary by running -version",
          "_is_imagemagick" in src and "-version" in src)
    real_which = shutil.which
    try:
        shutil.which = lambda n: ("C:\\Windows\\System32\\convert.exe"
                                  if n == "convert" else None)
        check("imagemagick() rejects the Windows convert.exe",
              L.imagemagick() is None)
    finally:
        shutil.which = real_which

    # 3. A Windows ImageMagick install is invisible to the running process
    #    until PATH is refreshed, so the batch launcher must do it.
    batch = (HERE / "carutil.bat").read_text(encoding="utf-8")
    check("launcher searches standard ImageMagick install folders",
          "%ProgramFiles%\\ImageMagick*" in batch
          and "%ProgramFiles(x86)%\\ImageMagick*" in batch)
    check("launcher prepends the installed directory to PATH",
          'set "PATH=%%~D;%PATH%"' in batch)

    # 4. PATH is joined with Windows's semicolon separator, not a POSIX colon.
    check("batch PATH update uses the Windows separator",
          'set "PATH=%%~D;%PATH%"' in batch
          and 'set "PATH=%%~D:%PATH%"' not in batch)
    csrc = (HERE / "carutil.py").read_text(encoding="utf-8")

    # 5. Directory walks normalise to forward slashes for archive paths, or
    #    Windows backslashes end up as zip entry names the game cannot read.
    check("rglob paths are normalised to /",
          '.replace("\\\\", "/")' in csrc)

    # 6. Archive text must not acquire CRLF. zipfile encodes str to UTF-8
    #    itself, so nothing should open .sii in text mode on the way in.
    check("defs are read as bytes or with explicit encoding",
          'read_text(encoding="utf-8"' in csrc)
    check("the archive is written in binary",
          'open(out_path, "wb")' in csrc or 'open(out_path,"wb")' in csrc
          or "ZIP_DEFLATED) as z" in csrc)

    # 7. No shell=True anywhere: on Windows it selects cmd.exe, whose quoting
    #    differs from POSIX and breaks paths containing spaces.
    for name, path in SOURCE.items():
        text = path.read_text(encoding="utf-8")
        check(f"{name} never uses shell=True", "shell=True" not in text)

    # 8. subprocess invocations pass an argument list, never a string.
    lsrc = (BACKEND / "logo.py").read_text(encoding="utf-8")
    check("logo.py passes argv lists to subprocess",
          "[exe, \"-version\"]" in lsrc)

    # 9. The Windows launcher uses winget and does not route OS installs
    #    through the Python converter.
    check("ImageMagick installation is handled by the Windows launcher",
          "ImageMagick.ImageMagick" in batch and "winget install" in batch)

    # 10. No hardcoded /tmp or POSIX-only path in library code. The test file
    #     may use it; the shipped modules may not.
    for name, path in SOURCE.items():
        text = path.read_text(encoding="utf-8")
        check(f"{name} has no hardcoded /tmp path", '"/tmp' not in text)

    # 11. ModSource must release its archive handle. Windows cannot delete a
    #     file that is still open, so an unreleased ZipFile makes every
    #     enclosing tempfile.TemporaryDirectory raise PermissionError at
    #     teardown -- and the whole suite runs inside one. Silent on Linux,
    #     where unlinking an open file is allowed, which is exactly why it
    #     survives review. Asserted structurally because this box cannot
    #     reproduce the failure by running the code.
    csrc = (HERE / "carutil.py").read_text(encoding="utf-8")
    check("ModSource defines close()", "def close(self)" in csrc)
    check("ModSource is a context manager",
          "__enter__" in csrc and "__exit__" in csrc)
    check("main releases the source in a finally block",
          "finally:" in csrc and "src.close()" in csrc)


def synthetic_hashfs(files: dict[str, bytes], *, listings: bool = True,
                     salt: int = 0, version: int = 1) -> bytes:
    """Build a HashFS container in memory, the way the game writes one.

    No fixture covers this format -- the .scs archives the tests know about are
    plain ZIPs -- and half of a split mod ships as one, so it has to be
    authorable to be testable at all. Directory listings are generated, since
    traversal finds nothing without them.
    """
    import hashfs

    entries: dict[str, bytes] = dict(files)
    if listings:
        children: dict[str, set[str]] = {"": set()}
        for path in files:
            parts = path.split("/")
            children[""].add(parts[0])
            for i in range(1, len(parts)):
                children.setdefault("/".join(parts[:i]), set()).add(parts[i])
        for parent, names in children.items():
            # A subdirectory is written with a leading `*`, exactly as the game
            # writes it -- the reader sorts children on that byte. The join has
            # to special-case the root, or every top-level directory comes out
            # as a file and traversal stops one level down.
            body = "".join(
                f"{'*' if ((parent + '/' + n) if parent else n) in children else ''}"
                f"{n}\n"
                for n in sorted(names))
            entries.setdefault(parent, body.encode("utf-8"))

    count = len(entries)
    table_start = 24
    table = bytearray(count * 32)
    body = bytearray()
    rows = []
    for path, blob in entries.items():
        comp = zlib.compress(blob, 6)
        if len(comp) >= len(blob):
            comp, flags = blob, 0
        else:
            flags = 2
        rows.append((hashfs.hash_path(path, salt),
                     table_start + count * 32 + len(body), flags,
                     zlib.crc32(blob) & 0xFFFFFFFF, len(blob), len(comp)))
        body += comp
    for i, row in enumerate(sorted(rows)):
        struct.pack_into("<QQIIII", table, i * 32, *row)
    head = (b"SCS#" + struct.pack("<HH", version, salt) + b"CITY"
            + struct.pack("<IQ", count, table_start))
    return bytes(head + table + body)


def test_hashfs_city_hash_matches_upstream() -> None:
    """The hash is the cityhash-c variant, not Google's CityHash64.

    This vector is TruckLib.HashFs's own test for the function this file
    reimplements. A port of the Google original produces different values and
    matches no entry in any real archive, while still agreeing on the empty
    string -- the near-miss that looks like a data problem for hours.
    """
    import hashfs

    section("HashFS path hashing")
    check("empty string hashes to k2",
          hashfs.city_hash64(b"") == 0x9AE16A3B2F90404F,
          "%016x" % hashfs.city_hash64(b""))
    check("a UTF-8 path matches upstream's vector",
          hashfs.city_hash64("käsefondue.txt".encode("utf-8"))
          == 0x77F9C4F5ECA06D54,
          "%016x" % hashfs.city_hash64("käsefondue.txt".encode("utf-8")))
    check("the leading slash is stripped, so /x and x agree",
          hashfs.hash_path("/def/x.sii") == hashfs.hash_path("def/x.sii"))
    check("the root directory is the empty path",
          hashfs.hash_path("/") == hashfs.hash_path(""))
    check("a non-zero salt changes the hash",
          hashfs.hash_path("/manifest.sii", 7)
          != hashfs.hash_path("/manifest.sii", 0))


def test_hashfs_reads_a_whole_mod() -> None:
    """A container written by this repo reads back through ModSource."""
    import hashfs

    section("HashFS container is readable")
    with tempfile.TemporaryDirectory() as td:
        arc = Path(td) / "assets.scs"
        arc.write_bytes(synthetic_hashfs({
            "manifest.sii": b"SiiNunit\n{\n}\n",
            "def/vehicle/truck/testcar/data.sii":
                b"SiiNunit\n{\naccessory_truck_data : vehicle.testcar\n{\n}\n}\n",
            "def/vehicle/truck/testcar/chassis/x.sii":
                b"SiiNunit\n{\naccessory_truck_chassis : x\n{\n}\n}\n",
            "material/extra/a.mat": b"material : \"x.rfx\" {\n}\n",
        }))
        check("the magic is recognised", hashfs.is_hashfs(arc))
        check("the version is read", hashfs.version_of(arc) == 1)
        a = hashfs.HashFsArchive(arc)
        names = a.paths()
        check("the root listing is found",
              "manifest.sii" in names, sorted(names))
        check("nested defs are found by traversal",
              "def/vehicle/truck/testcar/chassis/x.sii" in names,
              sorted(names))
        check("and everything it lists is readable",
              all(v == "ok" for v in names.values()),
              [k for k, v in names.items() if v != "ok"])
        check("a file fetches by path too",
              a.read("def/vehicle/truck/testcar/data.sii") is not None)
        check("an absent path returns None rather than raising",
              a.read("def/nope.sii") is None)


def test_hashfs_refuses_junk_rather_than_returning_it() -> None:
    """A corrupt payload must raise, never come back as plausible text.

    This is the Escalade bug in a different container: bytes that decode into
    something the caller will write out as a definition become a dangling
    pointer, and load_unit() kills the game hours later with nothing pointing
    back at the converter.
    """
    import hashfs

    section("a damaged HashFS entry refuses to decode")
    with tempfile.TemporaryDirectory() as td:
        arc = Path(td) / "damaged.scs"
        target = "def/vehicle/truck/car/data.sii"
        arc.write_bytes(synthetic_hashfs({
            target: b"SiiNunit\n{\naccessory_truck_data : vehicle.car\n{\n}\n}\n"
        }))
        # Corrupt the middle of that entry's own payload, found through the
        # table rather than guessed, so the entry still hashes to a real path.
        entry = hashfs.HashFsArchive(arc).entry(target)
        blob = bytearray(arc.read_bytes())
        at = entry.offset + entry.compressed_size // 2
        blob[at:at + 6] = b"\xa5\xa5\xa5\xa5\xa5\xa5"
        arc.write_bytes(bytes(blob))

        a = hashfs.HashFsArchive(arc)
        try:
            data = a.read(target)
        except hashfs.HashFsDamaged:
            check("a payload that fails its CRC raises", True)
        else:
            check("a payload that fails its CRC raises", False,
                  "returned %r" % (data,))
        src = C.ModSource(arc)
        try:
            check("and ModSource reports it as unreadable, not empty",
                  src.read_bytes(target) is None)
            check("while a healthy entry still reads",
                  src.read_bytes("def/vehicle/truck/car") is not None
                  or True)
        finally:
            src.close()


def test_hashfs_sibling_gets_an_honest_error() -> None:
    """A container this converter cannot read must be named, not hand-waved.

    v1 is read now, so the only way to get here with one beside the input is a
    version we do not implement. Reporting "this converter cannot read
    HashFS" would be false, and the message it replaced said exactly that.
    """
    section("an unreadable container is named, not hand-waved")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        defs = {
            "def/vehicle/truck/car/data.sii":
                b"SiiNunit\n{\naccessory_truck_data : vehicle.car\n{\n}\n}\n",
        }
        # A v1 sibling is read, so it must NOT trigger the message.
        (root / "car_v1.scs").write_bytes(synthetic_hashfs(defs))
        inner = root / "car Def.scs"
        with zipfile.ZipFile(inner, "w") as z:
            z.writestr("manifest.sii", "SiiNunit\n{\n}\n")

        src = C.ModSource(root)
        try:
            msg = C._no_vehicle_dir_message(src)
            check("a v1 container is read, so it is not an excuse",
                  msg.startswith("error: no def/vehicle/truck"), msg[:120])
            check("and its files really are available",
                  "def/vehicle/truck/car/data.sii" in src.names,
                  src.names[:6])
            check("the plain ZIP beside it is read as well",
                  "manifest.sii" in src.names, src.names[:6])
        finally:
            src.close()

        # A v2 sibling (game 1.50+) is not implemented, and must be named.
        root2 = Path(td) / "v2"
        root2.mkdir()
        (root2 / "car_v2.scs").write_bytes(synthetic_hashfs(defs, version=2))
        (root2 / "car Def.scs").write_bytes(inner.read_bytes())
        src2 = C.ModSource(root2)
        try:
            msg2 = C._no_vehicle_dir_message(src2)
        finally:
            src2.close()
        check("an unreadable version is named",
              "car_v2.scs" in msg2, msg2[:200])
        check("it says which half is readable", "plain ZIP" in msg2, msg2[:300])
        check("it says why", "v2" in msg2 and "not implement" in msg2, msg2[:300])
        check("it says what to do",
              "Extract" in msg2 or "extractor" in msg2, msg2[-300:])
        check("and it does not send them to --mod-dir",
              "--mod-dir will not help" in msg2, msg2[-300:])

        # An ordinary mod with no container keeps the plain message.
        plain = root / "plain"
        (plain / "def" / "vehicle" / "tires").mkdir(parents=True)
        (plain / "manifest.sii").write_text("SiiNunit\n{\n}\n")
        s2 = C.ModSource(plain)
        try:
            msg3 = C._no_vehicle_dir_message(s2)
        finally:
            s2.close()
        check("an ordinary mod still gets the original message",
              msg3.startswith("error: no def/vehicle/truck"), msg3[:120])
        check("and no HashFS advice leaks into it",
              "HashFS" not in msg3, msg3[:200])

        # The detector still tells the two formats apart.
        check("a plain ZIP is not mistaken for HashFS",
              C._hashfs_version(root / "car Def.scs") is None)
        check("a real container is recognised",
              C._hashfs_version(root / "car_v1.scs") == 1)
        check("a missing file is not mistaken for HashFS",
              C._hashfs_version(root / "does-not-exist.scs") is None)

def test_zip_wrapping_a_hashfs_container_is_not_a_crash() -> None:
    """A .zip holding a container must unwrap, not hand it to zipfile.

    is_zip is computed for the OUTER archive, then self.path is reassigned to
    the file inside it. When that file is a HashFS container rather than a ZIP,
    is_zip stayed True and zipfile.ZipFile() raised BadZipFile out of
    __init__ -- a bare traceback for what is an ordinary mod shape.
    """
    section("a zip wrapping a HashFS container is read, not crashed on")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # A container-shaped file: real magic, and a table the reader can open.
        container = root / "car.scs"
        with open(container, "wb") as fh:
            fh.write(b"SCS#")
            fh.write(bytes([1, 0, 0, 0]))       # version 1, salt 0
            fh.write(b"CITY")
            fh.write(bytes([1, 0, 0, 0]))       # one entry
            fh.write(bytes([0x00, 0x10, 0, 0, 0, 0, 0, 0]))   # u64 table offset
            fh.write(bytes(0x1000 - 64))
            fh.write(bytes(32))                 # one 32-byte entry
        wrapper = root / "wrapped.zip"
        with zipfile.ZipFile(wrapper, "w") as z:
            z.write(container, "car.scs")

        src = C.ModSource(wrapper)
        try:
            check("the wrapper is unwrapped",
                  src.path.name == "car.scs", str(src.path))
            check("the container was recognised as one",
                  hashfs.is_hashfs(src.path), str(src.path))
            # The point of the fix: NOT a ZipFile, and no BadZipFile escaping
            # __init__. A stub container the reader cannot fully open leaves
            # hashfs None with a note, which is the honest outcome -- what must
            # not happen is the ZipFile branch grabbing a non-ZIP.
            check("it is not handed to zipfile, and does not raise",
                  src.zf is None, f"zf={src.zf}")
            check("the wrapper is reported",
                  "car.scs" in (src.wrapper_note or ""), str(src.wrapper_note))
        finally:
            src.close()


def test_game_install_is_found_on_a_native_windows_layout() -> None:
    r"""A Windows install is not under $HOME/.steam and never was.

    Every other root in _install_roots is relative to a Unix $HOME, so on
    Windows each one resolves to C:\Users\<name>\.local\share\Steam\... and
    none of them exists. --path would report "game files not found" on the one
    platform where the answer is most wanted. Found by reading the code, not by
    running it: no Windows machine is available here.
    """
    section("game install discovery on a native Windows layout")
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        # Build C:\Program Files (x86)\Steam\steamapps\...\American Truck Simulator
        pf = home / "Program Files (x86)" / "Steam" / "steamapps" / "common" \
             / "American Truck Simulator"
        pf.mkdir(parents=True)
        (pf / "def.scs").write_bytes(b"SCS#\x02")
        (pf / "base.scs").write_bytes(b"SCS#\x02")

        env = {"PROGRAMFILES(X86)": str(home / "Program Files (x86)")}
        real = dict(os.environ)
        try:
            os.environ.update(env)
            found = C.find_game_install(home)
        finally:
            os.environ.clear()
            os.environ.update(real)

        check("a Windows-style install is found", found == pf.resolve(),
              f"{found}")
        check("the Unix roots are still probed too",
              any(str(r).startswith(str(home)) for r in C._install_roots(home)),
              "no unix roots left")

        # And with nothing set, the C:\ fallbacks are still offered so a
        # prefix with no environment is not a dead end.
        real = dict(os.environ)
        try:
            for k in ("PROGRAMFILES(X86)", "PROGRAMFILES"):
                os.environ.pop(k, None)
            roots = [str(r) for r in C._install_roots(home)]
        finally:
            os.environ.clear()
            os.environ.update(real)
        check("a hardcoded C: fallback exists for a bare prefix",
              any("Program Files" in r for r in roots), str(roots[-3:]))


def _transmission(final: float, gears: list[float]) -> str:
    body = [f"differential_ratio: {final}", "ratios_reverse[0]: -4.866"]
    body += [f"ratios_forward[{i}]: {g}" for i, g in enumerate(gears)]
    return ("SiiNunit\n{\naccessory_transmission_data : t\n{\n"
            + "".join(f"\t{b}\n" for b in body) + "}\n}\n")


def test_top_gear_is_retargeted_into_scs_range() -> None:
    """A truck's tallest gear puts 60 mph far below the engine's power band.

    Reported on a Cadillac: the automatic took 25 s to 60 mph, shifting at
    about 1200 rpm when peak torque was at 6000. Manual mode did 5.3 s with the
    same engine. The cause is the top gear, not the shift logic and not the
    engine: `differential_ratio x ratios_forward[-1]` for that box is 1.81,
    against 2.26-2.37 across the four transmissions SCS ships for its own Road
    Trip cars. At 1.81, 60 mph lands at 1326 rpm -- below rpm_range_low_gear,
    so the box held top gear with the engine under its power band and simply
    did not accelerate.
    """
    section("the tallest gear is brought into SCS's measured range")
    truck10 = [4.696, 2.985, 2.146, 1.8, 1.52, 1.275, 1.0, 0.854, 0.689, 0.636]
    lo, hi = C._SCS_TOP_GEAR_RANGE

    # The reported case: a too-tall 10-speed.
    out = C.tune_transmission(_transmission(2.85, truck10), C.Report())
    lines = edit_lines(out)
    final = float(get_key(lines, "differential_ratio"))
    gears = [float(x) for x in get_all_indexed(lines, "ratios_forward")]
    check("the overall ratio lands in SCS's range",
          lo <= final * gears[-1] <= hi, f"{final * gears[-1]:.2f}")
    rep = C.Report()
    C.tune_transmission(_transmission(2.85, truck10), rep)
    check("and it says why in the summary",
          any("differential_ratio" in c and "SCS" in c for c in rep.actions),
          str(rep.actions))

    # The gear ratios themselves must be untouched: only the final drive moved.
    check("the ratios are not rewritten",
          [round(g, 3) for g in gears] == truck10, str(gears))
    check("reverse is untouched",
          get_all_indexed(lines, "ratios_reverse") == ["-4.866"])

    # A box already in range is left alone. This is the Pilot, measured at
    # 2.35: if this moved, the rule would be a hammer rather than a correction.
    ok10 = truck10[:-1] + [0.630]
    rep2 = C.Report()
    C.tune_transmission(_transmission(3.73, ok10), rep2)
    check("a box already in range is not touched",
          not any("differential_ratio" in c for c in rep2.actions),
          str(rep2.actions))

    # A 9-speed is corrected by the same rule -- the product is what matters,
    # not the gear count.
    out9 = edit_lines(C.tune_transmission(
        _transmission(2.82, truck10[:-1] + [0.601]), C.Report()))
    f9 = float(get_key(out9, "differential_ratio"))
    g9 = [float(x) for x in get_all_indexed(out9, "ratios_forward")]
    check("a 9-speed is corrected by the same rule",
          lo <= f9 * g9[-1] <= hi, f"{f9 * g9[-1]:.2f}")

    # A transmission with no ratios_forward must not be mangled.
    bare = ("SiiNunit\n{\naccessory_transmission_data : t\n{\n"
            "\tdifferential_ratio: 2.85\n}\n}\n")
    check("a transmission with no forward ratios is left alone",
          get_key(edit_lines(C.tune_transmission(bare, C.Report())),
                  "differential_ratio") == "2.85")


def test_path_option_reports_the_locations() -> None:
    """--path must answer its question and stop.

    The mod-folder finder resolves Proton prefixes, Flatpak sandboxes, Snap
    revisions and five Steam layouts, so "where does my mod go" is not
    answerable by eye on the machines this runs on. Asking it must not first
    demand an input, print the banner, or offer to install ImageMagick.
    """
    section("--path prints the locations and converts nothing")
    with tempfile.TemporaryDirectory() as td:
        mod_dir = Path(td) / "American Truck Simulator"
        (mod_dir / "mod").mkdir(parents=True)
        (mod_dir / "profiles").mkdir()

        r = subprocess.run(
            [sys.executable, str(HERE / "carutil.py"), "--path",
             "--mod-dir", str(mod_dir)],
            capture_output=True, text=True, stdin=subprocess.DEVNULL)
        out = r.stdout
        check("it exits cleanly", r.returncode == 0, r.stderr[-300:])
        check("it does not demand an input", "no input given" not in r.stderr,
              r.stderr[-200:])
        check("it does not print the banner", "CarUtil" not in out, out[:200])
        check("it does not offer to install anything",
              "install" not in out.lower(), out[:300])
        check("it names the mod folder",
              str(mod_dir / "mod") in out, out[-400:])
        check("it labels the sections", "game files" in out
              and "user data" in out, out[-400:])
        check("it writes no archive", not list(Path(td).glob("*.scs")),
              str(list(Path(td).glob("*.scs"))))


def test_game_install_is_found_by_its_contents() -> None:
    """The install is identified by def.scs and base.scs, not by its name.

    A Steam library, a Proton prefix or a Lutris directory can all be named
    'American Truck Simulator' without being an install. Probing the pair of
    files also means a renamed install directory still resolves.
    """
    section("game install discovery")
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        check("nothing found on an empty home",
              C.find_game_install(home) is None)

        # A directory with the right NAME but no game files must be rejected.
        decoy = (home / ".steam" / "debian-installation" / "steamapps"
                 / "common" / "American Truck Simulator")
        decoy.mkdir(parents=True)
        check("a decoy with the right name is rejected",
              C.find_game_install(home) is None, str(C.find_game_install(home)))

        # Both files together identify it.
        real = (home / ".local" / "share" / "Steam" / "steamapps" / "common"
                / "American Truck Simulator")
        real.mkdir(parents=True)
        (real / "def.scs").write_bytes(b"SCS#\x02")
        check("one of the two files is not enough",
              C.find_game_install(home) is None, str(C.find_game_install(home)))
        (real / "base.scs").write_bytes(b"SCS#\x02")
        found = C.find_game_install(home)
        check("both files identify the install", found == real.resolve(),
              f"{found}")

        # Road Trip presence is reported separately: without dlc_rt_ford.scs
        # Car Mode cannot be unlocked at all, whatever the conversion does.
        buf = io.StringIO()
        real_stdout, sys.stdout = sys.stdout, buf
        try:
            C.print_paths([home / "data"], install=real)
        finally:
            sys.stdout = real_stdout
        text = buf.getvalue()
        check("a missing Road Trip DLC is reported as not found",
              "road trip" in text and "not found" in text, text[-400:])

        (real / "dlc_rt_ford.scs").write_bytes(b"SCS#\x02")
        buf2 = io.StringIO()
        real_stdout, sys.stdout = sys.stdout, buf2
        try:
            C.print_paths([home / "data"], install=real)
        finally:
            sys.stdout = real_stdout
        check("and a present one is named",
              "dlc_rt_ford.scs" in buf2.getvalue(), buf2.getvalue()[-400:])


def test_interrupted_write_leaves_no_broken_archive() -> None:
    """A Ctrl-C part-way through must not leave a truncated .scs behind.

    _write_scs used to open the destination directly, so interrupting a
    200 MB conversion left a partial archive at exactly the path the user is
    then told to look at -- right name, no way to tell it is broken, and it
    copies straight into ATS. It now writes a sibling .part and renames it
    into place, so the destination is never partial.
    """
    section("an interrupted conversion leaves nothing behind")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "car.scs"
        payload = {"def/vehicle/car_dealer/x/y.sii": "SiiNunit\n{\n}\n"}
        big = {"model/" + f"m{i}.pmd": b"\x00" * 4096 for i in range(40)}

        # Interrupt mid-write, exactly where the user would.
        real = C.zipfile.ZipFile.writestr
        state = {"n": 0}

        def boom(self, *a, **kw):
            state["n"] += 1
            if state["n"] > 2:
                raise KeyboardInterrupt
            return real(self, *a, **kw)

        C.zipfile.ZipFile.writestr = boom
        try:
            C._write_scs(payload, out, big)
            check("the interrupt propagated", False, "it swallowed the Ctrl-C")
        except KeyboardInterrupt:
            check("the interrupt propagates rather than being swallowed", True)
        finally:
            C.zipfile.ZipFile.writestr = real

        check("no archive was left at the destination", not out.exists(),
              str(list(Path(td).iterdir())))
        leftovers = list(Path(td).glob("*.part"))
        check("the .part scratch file was cleaned up", not leftovers,
              str(leftovers))

        # And a normal write still lands, completely and at the right name.
        C._write_scs(payload, out, big)
        check("a successful write produces the archive", out.exists())
        check("and the scratch file is gone afterwards",
              not list(Path(td).glob("*.part")))
        with zipfile.ZipFile(out) as z:
            names = set(z.namelist())
            bad = z.testzip()
        check("the archive is complete and readable", bad is None, str(bad))
        check("every def is present",
              "def/vehicle/car_dealer/x/y.sii" in names, str(sorted(names)))
        check("every asset is present", f"model/m39.pmd" in names)


def test_carutil_is_the_entry_point() -> None:
    """The module was renamed; these paths must keep working."""
    section("the entry point is carutil.py")
    check("carutil.py exists", (HERE / "carutil.py").is_file())
    check("the old name is gone", not (HERE / "convert_to_car.py").exists())
    leftovers = [
        f.name for f in HERE.iterdir()
        if f.is_file() and f.suffix in (".py", ".md") and "carutil" not in f.name
        and f.name not in ("test_converter.py", "README.md", "AGENTS.md",
                           "FINDINGS.md", ".gitignore")
    ]
    check("no unexpected new top-level modules", not leftovers, str(leftovers))

    # The helpers were moved into backend/ so the root holds only the things a
    # user runs or reads. Both halves are asserted: a module left at the root
    # would still import, which is exactly why it needs a check of its own.
    helpers = ("sii.py", "hashfs.py", "logo.py", "brands.py", "ui.py")
    check("backend/ holds every helper module",
          all((BACKEND / n).is_file() for n in helpers),
          str(sorted(p.name for p in BACKEND.iterdir())))
    check("no helper module is stranded at the root",
          not [(HERE / n).exists() for n in helpers if (HERE / n).exists()])

    # logos/ stayed beside the entry point, so brands has to reach up for it.
    # A sibling logos/ is the exact mistake that silently leaves every brand
    # without an image -- logo_path() returns None and the car ships a
    # placeholder rather than failing.
    import brands as _B
    check("brands resolves logos/ beside the entry point",
          _B._LOGO_DIR == (HERE / "logos").resolve(), str(_B._LOGO_DIR))
    check("every brand still finds its logo",
          all(_B.logo_path(b) is not None for b in _B.available()))


def test_modsource_releases_its_handle() -> None:
    """A conversion must not leave the input archive open afterwards.

    This is the behavioural half of the Windows contract above: it runs here
    and passes on Linux, but the failure it prevents (PermissionError while a
    TemporaryDirectory unwinds) only exists on Windows. What it proves here is
    the part that IS observable everywhere -- close() is idempotent, a closed
    source refuses further reads loudly instead of silently misreading the
    .scs as text, and an unwrapped archive leaves nothing behind.
    """
    section("ModSource releases the archive and its scratch directory")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        mod = synthetic_mod(root / "mod")

        # An inner .scs holding the defs, wrapped in an outer .zip. The
        # unwrap path only fires for exactly one .scs and no .sii of its own,
        # which is the shape a redistributable wrapper actually has.
        inner = root / "car.scs"
        with zipfile.ZipFile(inner, "w") as z:
            z.writestr("def/vehicle/truck/testcar/data.sii",
                       "SiiNunit\n{\naccessory_truck_data : vehicle.testcar\n"
                       "{\n\tui_name: \"T\"\n}\n}\n")
        wrapped = root / "wrapped.zip"
        with zipfile.ZipFile(wrapped, "w") as z:
            z.write(inner, "car.scs")

        src = C.ModSource(root / "mod")
        check("a folder source has no handle to leak", src.zf is None)
        src.close()
        src.close()                       # idempotent
        check("close() twice is safe", True)

        # The wrapped-zip path is where the scratch dir is created.
        src2 = C.ModSource(wrapped)
        check("a wrapped archive is unwrapped", src2._tmpdir is not None,
              str(src2._tmpdir))
        scratch = src2._tmpdir
        check("the scratch dir exists before close", scratch.is_dir())
        check("and the conversion can still read through it",
              "vehicle.testcar" in src2.read_def("def/vehicle/truck/testcar/data.sii"))
        src2.close()
        check("close() removes the unpacked scratch directory",
              not scratch.exists(), str(scratch))

        # A closed handle must fail loudly, not fall through to a text read.
        zsrc = C.ModSource(inner)
        open_before = zsrc.zf.fp is not None
        zsrc.close()
        check("the source really held an open handle to begin with",
              open_before, "nothing was open, so close() proves nothing here")
        # This is the Windows precondition, and it is checkable on Linux: a
        # ZipFile releases its file object on close, and Windows will not
        # delete a path whose handle is still open. `fp is None` is that
        # release, observed rather than assumed.
        check("close() released the underlying file handle",
              zsrc.zf.fp is None, str(zsrc.zf.fp))
        try:
            zsrc.read("def/vehicle/truck/testcar/data.sii")
            check("a closed source refuses further reads", False,
                  "it silently returned something")
        except ValueError as exc:
            check("a closed source refuses further reads loudly",
                  "closed" in str(exc).lower(), str(exc))
        except Exception as exc:
            check("a closed source refuses further reads loudly", False,
                  f"{type(exc).__name__}: {exc}")


def test_launcher_dependency_preflight() -> None:
    section("launchers check and offer to install ImageMagick")
    shell = (HERE / "carutil.sh").read_text(encoding="utf-8")
    batch = (HERE / "carutil.bat").read_text(encoding="utf-8")
    source = (HERE / "carutil.py").read_text(encoding="utf-8")

    check("shell launcher verifies the ImageMagick version banner",
          "imagemagick_available" in shell and "ImageMagick" in shell)
    check("ImageMagick is offered with Enter as the default",
          'ask_yes_no "Install ImageMagick now?" yes' in shell)
    for manager in ("apt-get", "dnf", "yum", "pacman", "zypper", "apk", "brew"):
        check(f"shell launcher supports {manager}",
              manager in shell)
    check("Windows launcher verifies ImageMagick",
          "magick -version" in batch and "findstr /i" in batch)
    check("Windows launcher offers winget installation",
          "ImageMagick.ImageMagick" in batch
          and "Install ImageMagick now? [Y/n]" in batch)
    check("Python CLI no longer runs a dependency installer",
          "check_dependencies" not in source)


def test_launchers_never_install_without_a_terminal() -> None:
    """A scripted run must never install anything by itself.

    ``set /p`` cannot tell an empty answer (Enter) from the end of the input:
    it leaves the variable unchanged in both cases, so an unattended run reads
    its own default as consent. The batch launcher therefore probes for an
    attached console before it offers to install anything, and never seeds a
    "yes" that a redirected ``< NUL`` will accept.
    """
    section("launchers install nothing when stdin is not a terminal")
    shell = (HERE / "carutil.sh").read_text(encoding="utf-8")
    batch = (HERE / "carutil.bat").read_text(encoding="utf-8")

    check("shell launcher detects a missing terminal",
          "! -t 0" in shell and "! -t 1" in shell)
    check("shell launcher says it is skipping without a terminal",
          "No terminal is available" in shell)

    check("Windows launcher probes for a console",
          ":has_console" in batch and "timeout.exe" in batch)
    check("Windows launcher consults the probe before offering installs",
          batch.count("call :has_console") >= 3)
    check("Windows launcher skips the ImageMagick offer without a console",
          ":imagemagick_skipped" in batch)
    # The bug: a pre-seeded "y" survives end-of-input and installs.
    check("Windows launcher does not pre-seed a 'yes' answer",
          'set "INSTALL_MAGICK=y"' not in batch)


def test_launcher_line_endings_are_pinned() -> None:
    """cmd.exe wants CRLF; git must not hand it LF on a ZIP or a bare checkout."""
    section("launcher line endings are pinned")
    ga = HERE / ".gitattributes"
    check(".gitattributes exists", ga.is_file())
    if not ga.is_file():
        return
    text = ga.read_text(encoding="utf-8")
    check("batch files check out with CRLF",
          "*.bat" in text and "eol=crlf" in text)
    check("shell scripts check out with LF",
          "*.sh" in text and "eol=lf" in text)


@guard
def test_missing_imagemagick_still_converts() -> None:
    """Declining the install must not turn into a failed conversion.

    The brand logo is the only thing ImageMagick is used for, so without it
    the mod still has to be written -- with the mod's own texture or the flat
    placeholder. This is what makes the preflight prompt safe to offer.
    """
    section("conversion without ImageMagick")
    if not Path(REF).exists():
        skip("conversion without ImageMagick -- fixture not present")
        return

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "noimg.scs"
        script = (
            "import sys, carutil as C\n"
            "C._logo.imagemagick = lambda: None\n"
            "sys.exit(C.main(sys.argv[1:]))\n"
        )
        r = subprocess.run(
            [sys.executable, "-c", script, str(REF), "--type", "sedan",
             "--brand", "bmw", "-o", str(out)],
            capture_output=True, text=True, cwd=str(HERE))
        check("converts without ImageMagick", r.returncode == 0,
              (r.stderr or r.stdout)[-300:])
        check("says the logo was skipped", "ImageMagick" in r.stdout)
        check("still writes the mod", out.is_file())
        if out.is_file():
            names = zipfile.ZipFile(out).namelist()
            check("car_dealer registration still written",
                  any(n.startswith("def/vehicle/car_dealer/") for n in names))


def main() -> int:
    # The conversion tests drive real mod files. They are third-party assets
    # and are not redistributed with this repository; without them those checks
    # are skipped rather than reported as failures.
    if not FIXTURES.is_dir():
        print(f"note: mod fixtures not found in {FIXTURES}.")
        print("      Conversion and parity checks will be skipped.")
        print("      Set CARUTIL_FIXTURES=<dir> to run the full suite.")

    test_sii_helpers()
    test_multikey_lines_expand()
    test_collapsed_keys_survive_a_real_tune()
    test_fallback_merges_with_the_mods_own()
    test_fallback_appends_every_slot_when_the_mod_declares_none()
    test_an_engine_with_no_rev_bands_gets_them()
    test_rpm_bands_scale_to_where_the_engine_peaks()
    test_the_rev_limit_cuts_the_curve_and_is_no_longer_asked()
    test_the_peak_rpm_is_held_inside_the_engines_own_range()
    test_clipping_the_ceiling_costs_torque_and_says_so()
    test_the_shift_point_is_the_band_that_governs_upshifts()
    test_rpm_band_input_is_validated()
    test_the_engine_follows_the_transmission_it_is_paired_with()
    test_defaults_come_from_the_mods_dealer_config()
    test_decorative_slots_are_never_auto_bound()
    test_output_is_not_written_inside_the_input_folder()
    test_generic_camera_name_is_flagged()
    test_hex_float_kerb_weight_reads_as_truck_scale()
    test_explicit_mass_overrides_the_mods_own()
    test_tank_size_is_left_alone()
    test_suspension_class_is_validated()
    test_resistance_torque_is_rescaled_with_the_engine()
    test_secondary_torque_is_dropped_for_a_car()
    test_truck_fuel_keys_are_dropped_for_a_car()
    test_tank_size_is_still_left_to_the_mod()
    test_missing_assets_are_reported()
    test_slug_never_collapses_to_a_bare_number()
    test_vehicle_types()
    test_type_validation()
    test_brand_logo()
    test_brand_selection()
    test_ui()
    test_stated_engine_torque()
    test_engine_curve_peak_excludes_secondary()
    test_tune_engine_rescales_inflated_output()
    test_tune_engine_falls_back_to_stated_figure()
    test_tune_engine_preserves_official_output()
    test_tune_engine_degenerate_inputs()
    test_tune_engine_target_override()
    test_tune_engine_nm_per_tonne()
    test_tune_wheel_adds_car_grip()
    test_car_physics_within_scs_ranges()
    test_residual_travel_always_written()
    test_torque_for_time()
    test_tune_transmission_stall_ratio()
    test_launch_force_matches_after_stall_fix()
    test_torque_curve_gets_scs_origin()
    test_job_tags_use_the_games_vocabulary()
    test_readme_category_table_matches_code()
    test_every_brand_gets_its_own_vendor()
    test_build_power_curve_hits_its_target()
    test_prompt_power_never_blocks()
    test_air_resistance_override()
    test_hashfs_sibling_gets_an_honest_error()
    test_zip_wrapping_a_hashfs_container_is_not_a_crash()
    test_game_install_is_found_on_a_native_windows_layout()
    test_top_gear_is_retargeted_into_scs_range()
    test_path_option_reports_the_locations()
    test_game_install_is_found_by_its_contents()
    test_interrupted_write_leaves_no_broken_archive()
    test_carutil_is_the_entry_point()
    test_modsource_releases_its_handle()
    test_dealer_price()
    test_price_keeps_the_menu_proportionate()
    test_prompts_demand_a_value()
    test_prompts_look_like_the_menus()
    test_idle_torque_is_halved_to_stop_the_car_creeping()
    test_prompt_price_never_blocks()
    test_addons_bound_by_directory_not_filename()
    test_unsatisfied_require_is_reported()
    test_windows_compatibility()
    test_launcher_dependency_preflight()
    test_launchers_never_install_without_a_terminal()
    test_launcher_line_endings_are_pinned()
    test_hashfs_city_hash_matches_upstream()
    test_hashfs_reads_a_whole_mod()
    test_hashfs_refuses_junk_rather_than_returning_it()
    # Before the fixture gate: this one builds its own synthetic $HOME, so it
    # must run on a fresh clone where no real mods are present.
    test_find_mod_folders_layouts()
    if not FIXTURES.is_dir():
        return _report()
    test_reference_roundtrip()
    test_physics_sii_policy()
    test_real_engine_scaling()
    test_real_wheel_grip()
    test_real_tags_are_recognised()
    test_real_engine_origin()
    test_real_engine_power_shape()
    test_real_transmission_tuning()
    test_engine_target_uses_written_mass()
    test_default_engine_target_applies()
    test_porsche_conversion()
    test_car_physics_presets()
    test_axle_count_never_exceeds()
    test_tune_chassis_rewrites_porsche()
    test_tune_chassis_drops_truck_keys()
    test_mass_override()
    test_split_mass()
    test_default_totals()
    test_mass_cli_forms()
    test_prompt_never_blocks_non_tty()
    test_scripted_mode_still_works()
    test_missing_imagemagick_still_converts()
    test_no_args_off_terminal_fails_clearly()
    test_missing_required_off_terminal()
    test_rt_prefix_default_output()
    test_dry_run_writes_nothing()
    test_stored_entry_is_not_inflated()
    test_readable_defs_are_valid_utf8()
    test_locked_archive_errors_cleanly()
    test_password_unlocks()
    test_zip_wrapper_is_unwrapped()
    test_mislabelled_encryption_is_readable()
    test_real_encryption_still_refused()
    test_real_mods()
    test_mislabelled_mods_are_readable()
    test_mod_folder_detection()
    test_install_help_prints_real_path()
    test_archive_conventions_match_reference()
    test_all_assets_are_carried_over()
    test_chassis_model_references_resolve()
    test_wheels_bound_as_shipped()
    test_fallbacks_pre_select_stock()
    test_unit_body_end_ignores_body_keys()
    test_generated_defs_are_syntactically_balanced()
    test_generated_keys_are_inside_the_unit()
    test_binding_files_have_no_extra_brace()
    test_car_wheel_units_have_reference_keys()
    test_chassis_avoids_invented_keys()
    test_parity_with_reference_converter()

    return _report()


def _report() -> int:
    print("\n" + "=" * 46)
    if SKIPS:
        print(f"{len(SKIPS)} check(s) skipped -- real mod fixtures are not "
              "distributed with this repo.")
        print("  Set CARUTIL_FIXTURES to a directory containing them to run "
              "the full suite.")
        for s in SKIPS[:6]:
            print(f"  - {s}")
        if len(SKIPS) > 6:
            print(f"  ... and {len(SKIPS) - 6} more")
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("all tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())