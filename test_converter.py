#!/usr/bin/env python3
"""Tests for convert_to_car.py.

Run: python3 test_converter.py

Two levels:
  1. Unit tests on the SII helpers and converter internals.
  2. A fidelity round-trip against the Volkswagen Parati reference, which is
     the mod we know already works in Car Mode. If the converter can reproduce
     that file's structure, its understanding of the schema is sound.
"""

from __future__ import annotations

import functools
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import convert_to_car as C  # noqa: E402
import sii  # noqa: E402
from sii import get_all_indexed, get_key, set_key, set_tag  # noqa: E402

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


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}" + (f" -- {detail}" if detail else ""))
        FAILURES.append(name)


def skip(name: str, detail: str = "") -> None:
    """Record a check that could not run because a fixture is missing."""
    print(f"  SKIP  {name}" + (f" -- {detail}" if detail else ""))
    SKIPS.append(name)


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

    return run



def section(title: str) -> None:
    print(f"\n=== {title} ===")


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
            [sys.executable, str(HERE / "convert_to_car.py"), str(REF),
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
            [sys.executable, str(HERE / "convert_to_car.py"), str(REF),
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
    check("secondary_torque scaled by the same factor",
          abs(float(re.search(r"secondary_torque:\s*(\d+)",
                              out).group(1))
              / float(re.search(r"secondary_torque:\s*(\d+)",
                                _ENGINE_INFLATED).group(1)) - tq / 1400) < 0.01)
    check("author's comment survives the rewrite",
          "# torque x 1.26 (final drive relation)" in out)
    check("torque_curve left untouched",
          out.count("torque_curve[]") == _ENGINE_INFLATED.count("torque_curve[]"))
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
        peak = C._primary_curve_peak(et)
        tq = float(C.get_key(et.splitlines(), "torque"))
        # This regressed once: convert() passed the CLI's None straight through,
        # which overrode tune_engine's own default and silently fell back to
        # the mod's stated torque (833 Nm instead of 1672).
        check("default target is applied without any flag",
              abs(tq * peak - 760 * mass / 1000) < 2,
              f"peak {tq * peak:.0f} Nm, expected "
              f"{760 * mass / 1000:.0f} Nm for {mass:.0f} kg")
        check("...and it is far above the mod's stated output",
              tq * peak > 1000, f"{tq * peak:.0f} Nm")


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
        check(f"{t_}: air_resistance is car-scale",
              float(cfg["air_resistance"]) < 0.30, cfg["air_resistance"])
        check(f"{t_}: cog_height is car-scale",
              0.25 <= float(cfg["cog_height"]) <= 0.85, cfg["cog_height"])
        check(f"{t_}: steering lock is car-scale",
              24 <= int(cfg["max_steering_angle"]) <= 36, cfg["max_steering_angle"])
        mass = sum(int(x) for x in cfg["kerb_weight"])
        check(f"{t_}: kerb mass plausible", 900 <= mass <= 2600, str(mass))
        check(f"{t_}: voltage 12V", cfg["nominal_voltage"] == "12.0")
        check(f"{t_}: no air brakes", cfg["air_tank_pressure"] == "0.0")

    # sports must be the grippiest, pickup the least aerodynamic
    check("sports has least drag",
          float(C.car_physics("sports", 2)["air_resistance"])
          < float(C.car_physics("pickup", 2)["air_resistance"]))
    check("sports has stiffest sway bar",
          float(C.car_physics("sports", 2)["sway_bar_stiffness"])
          > float(C.car_physics("sedan", 2)["sway_bar_stiffness"]))
    check("offroad has most travel",
          float(C.car_physics("offroad", 2)["residual_travel"][0])
          > float(C.car_physics("sports", 2)["residual_travel"][0]))


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
            [sys.executable, str(HERE / "convert_to_car.py"), str(PORCHE),
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
                [sys.executable, str(HERE / "convert_to_car.py"), *base, "--mass", bad],
                capture_output=True, text=True)
            check(f"rejects {bad!r}", r.returncode != 0)


@guard
def test_prompt_never_blocks_non_tty() -> None:
    section("prompt is skipped off-terminal")
    # stdin in the test harness is not a TTY, so this must return immediately
    check("non-TTY returns None", C.prompt_weight("suv", None) is None)


# ---------------------------------------------------------------------- CLI
@guard
def test_scripted_mode_still_works() -> None:
    section("scripted mode (backwards compatible)")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "cli.scs"
        C.main([str(PORCHE), "--type", "suv", "--brand", "porsche", "-o", str(out)])
        check("positional input works", out.exists())

        r = subprocess.run(
            [sys.executable, str(HERE / "convert_to_car.py"),
             str(PORCHE), "--type", "suv", "--brand", "p", "-o", str(out)],
            capture_output=True, text=True)
        check("runs as a script", r.returncode == 0, r.stderr[-200:])
        check("prints DONE", "DONE" in r.stdout)
        check("prints the location", str(out.resolve()) in r.stdout)


@guard
def test_no_args_off_terminal_fails_clearly() -> None:
    section("no args, no terminal")
    r = subprocess.run(
        [sys.executable, str(HERE / "convert_to_car.py")],
        capture_output=True, text=True, stdin=subprocess.DEVNULL)
    check("exits non-zero", r.returncode != 0)
    check("explains how to fix",
          "no input given" in r.stderr and "convert_to_car.py mod.scs" in r.stderr)


@guard
def test_missing_required_off_terminal() -> None:
    section("missing --type / --brand off terminal")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "o.scs"
        r = subprocess.run(
            [sys.executable, str(HERE / "convert_to_car.py"), str(PORCHE), "-o", str(out)],
            capture_output=True, text=True, stdin=subprocess.DEVNULL)
        check("demands --type", r.returncode != 0 and "--type" in r.stderr)

        r = subprocess.run(
            [sys.executable, str(HERE / "convert_to_car.py"), str(PORCHE),
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
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "dry.scs"
        r = subprocess.run(
            [sys.executable, str(HERE / "convert_to_car.py"), str(PORCHE),
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
            [sys.executable, str(HERE / "convert_to_car.py"), str(locked),
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
            [sys.executable, str(HERE / "convert_to_car.py"), str(locked),
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
            [sys.executable, str(HERE / "convert_to_car.py"), str(locked),
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
            [sys.executable, str(HERE / "convert_to_car.py"), str(wrapper),
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
            [sys.executable, str(HERE / "convert_to_car.py"), str(built),
             "--type", "suv", "--brand", "honda", "--mass", "2142",
             "-o", str(out)],
            capture_output=True, text=True)
        check("converts without a password", r.returncode == 0, r.stderr[-200:])
        check("reports the workaround", "data is plain" in r.stdout)

        r2 = subprocess.run(
            [sys.executable, str(HERE / "convert_to_car.py"), str(built),
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
        [sys.executable, str(HERE / "convert_to_car.py"), str(mod),
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
    check(f"{mod.name[:28]}: physics.sii kept",
          any(n.endswith("physics.sii") for n in names))
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
            for n in src_wheels:
                check(f"{label}: {n.split('/')[-1]} byte-identical",
                      src.read_bytes(n) == z.read(n))

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
        with zipfile.ZipFile(out) as z:
            names = set(z.namelist())
            for ext in (".dds", ".mat", ".tobj"):
                check(f"output has car_brand_logo/mod{ext}",
                      f"material/ui/car_brand_logo/mod{ext}" in names)
            real = z.read("material/ui/car_brand_logo/mod.dds")
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
            t = z.read("material/ui/car_brand_logo/mod.tobj")
            ln = struct.unpack("<I", t[40:44])[0]
            check("output tobj points at the car_brand_logo dds",
                  t[48:48 + ln] == b"/material/ui/car_brand_logo/mod.dds",
                  t[48:48 + ln].decode("ascii", "replace"))
            mat = z.read("material/ui/car_brand_logo/mod.mat").decode()
            check("output mat references mod.tobj", 'texture : "mod.tobj"' in mat)


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
    check("paint is a no-op when off", U.paint("hi", "green") == "hi")

    # NO_COLOR wins even if FORCE_COLOR is set.
    _os.environ["NO_COLOR"] = "1"
    _os.environ["FORCE_COLOR"] = "1"
    check("NO_COLOR overrides FORCE_COLOR", U.paint("hi", "green") == "hi")
    _os.environ.pop("NO_COLOR")
    _os.environ.pop("FORCE_COLOR")
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
    check("ok marker", "+ done" in text)
    check("warn marker", "! careful" in text)
    check("bullet marker", "* part" in text)
    check("fail marker", "x broken" in text)
    check("heading printed", "changes" in text)
    check("step counter", "[2/5]" in text)

    # The numbered fallback is what non-interactive environments get.
    check("menu options are a list of (label, hint) pairs",
          isinstance([("a", "b")], list))
    check("_menu_label selects the marker",
          ">" in U._menu_label("Sedan", "saloon", True)
          and ">" not in U._menu_label("Sedan", "saloon", False))

    # termios/tty are POSIX-only. An unguarded import here raised
    # ModuleNotFoundError on Windows, which killed the guided walkthrough one
    # prompt after the user pasted their mod path -- the numbered-menu
    # fallback below never got the chance to engage. The probe must degrade to
    # False instead of propagating.
    try:
        raw_ok = U._raw_keys_available()
        check("_raw_keys_available returns a bool without raising", isinstance(raw_ok, bool))
    except BaseException as exc:                     # noqa: BLE001
        check("_raw_keys_available returns a bool without raising", False,
              f"{type(exc).__name__}: {exc}")

    # Simulate Windows by making the termios import fail, whichever way the
    # module tries to reach it.
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) \
        else __builtins__.__import__

    def _no_termios(name, *a, **kw):
        if name in ("termios", "tty"):
            raise ModuleNotFoundError(f"No module named {name!r}")
        return real_import(name, *a, **kw)

    if isinstance(__builtins__, dict):
        __builtins__["__import__"] = _no_termios
    else:
        __builtins__.__import__ = _no_termios
    try:
        check("_raw_keys_available is False when termios is absent",
              U._raw_keys_available() is False)
    finally:
        if isinstance(__builtins__, dict):
            __builtins__["__import__"] = real_import
        else:
            __builtins__.__import__ = real_import

    # And the menu must still be answerable when raw keys are unavailable.
    real_raw = U._raw_keys_available
    U._raw_keys_available = lambda: False
    real_input = builtins.input
    builtins.input = lambda prompt="": "2"
    try:
        check("arrow_menu falls back to the numbered menu", U.arrow_menu(
            [("Sedan", ""), ("SUV", ""), ("Sports", "")], default=0) == 1)
    finally:
        U._raw_keys_available = real_raw
        builtins.input = real_input


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


def test_windows_compatibility() -> None:
    """Platform contracts that are silent on Linux and fatal on Windows 11.

    Everything here is a Windows-only failure that this Linux box cannot
    reproduce by running the code, so each one is asserted structurally.
    """
    section("Windows 11 compatibility")

    # 1. POSIX-only imports must stay inside a try/except. An import above the
    #    guard raises ModuleNotFoundError on Windows and takes down the guided
    #    walkthrough at step 1 of 5, before the numbered-menu fallback.
    import ast as _ast
    import ui as U

    tree = _ast.parse((HERE / "ui.py").read_text(encoding="utf-8"))
    guarded = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Try):
            for sub in _ast.walk(node):
                if isinstance(sub, _ast.Import):
                    for a in sub.names:
                        guarded.add(a.name.split(".")[0])
    for mod in ("termios", "tty"):
        imported_at_module_level = False
        for node in tree.body:
            if isinstance(node, _ast.Import) and \
                    any(a.name.split(".")[0] == mod for a in node.names):
                imported_at_module_level = True
        check(f"{mod} is not imported at module level in ui.py",
              not imported_at_module_level)
        check(f"{mod} import is guarded", mod in guarded)

    # 2. shutil.which("convert") resolves to the FAT-to-NTFS convert.exe on
    #    Windows. Every call must therefore be verified, never trusted by name.
    import logo as L
    src = (HERE / "logo.py").read_text(encoding="utf-8")
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
    #    until PATH is refreshed, so that recovery must exist and be nt-only.
    check("_refresh_magick_path exists", hasattr(C, "_refresh_magick_path"))
    csrc = (HERE / "convert_to_car.py").read_text(encoding="utf-8")
    check("_refresh_magick_path is gated on nt", 'os.name != "nt"' in csrc)
    check("it looks for magick.exe",
          "magick.exe" in csrc and "ImageMagick" in csrc)

    # 4. PATH is joined with os.pathsep, never a bare ":". The latter
    #    silently truncates PATH on Windows.
    check("PATH is joined with os.pathsep", "os.pathsep" in csrc)
    check('no hardcoded ":" PATH join', '":" + os.environ' not in csrc)

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
    for name in ("convert_to_car.py", "sii.py", "logo.py", "ui.py",
                 "brands.py"):
        text = (HERE / name).read_text(encoding="utf-8")
        check(f"{name} never uses shell=True", "shell=True" not in text)

    # 8. subprocess invocations pass an argument list, never a string.
    lsrc = (HERE / "logo.py").read_text(encoding="utf-8")
    check("logo.py passes argv lists to subprocess",
          "[exe, \"-version\"]" in lsrc)

    # 9. The installer must be winget-first on Windows: apt/pacman/brew do not
    #    exist there, and offering them would produce a useless suggestion.
    import os as _os
    if _os.name == "nt":
        check("winget is the first installer offered",
              C._INSTALLERS[0][0] == "winget")
    else:
        check("winget is present in the installer list",
              any(m == "winget" for m, _ in C._INSTALLERS))

    # 10. No hardcoded /tmp or POSIX-only path in library code. The test file
    #     may use it; the shipped modules may not.
    for name in ("convert_to_car.py", "sii.py", "logo.py", "ui.py",
                 "brands.py"):
        text = (HERE / name).read_text(encoding="utf-8")
        check(f"{name} has no hardcoded /tmp path", '"/tmp' not in text)


def test_dependency_check() -> None:
    section("dependency preflight")
    import io

    real_magick = C._logo.imagemagick
    real_which = shutil.which
    real_tty = C._tty
    real_confirm = C._confirm

    try:
        # Nothing missing -> the check is silent and fast.
        C._logo.imagemagick = lambda: "/usr/bin/magick"
        check("no missing dependencies when ImageMagick is present",
              C.missing_dependencies() == [])
        out = io.StringIO()
        real, sys.stdout = sys.stdout, out
        try:
            C.check_dependencies()
        finally:
            sys.stdout = real
        check("preflight is silent when nothing is missing", out.getvalue() == "")

        # Missing -> reported, with an install command, and no prompt when
        # stdin is not a terminal (scripted runs and CI must never block).
        C._logo.imagemagick = lambda: None
        check("reports a missing ImageMagick",
              C.missing_dependencies() == ["ImageMagick"])

        C._tty = lambda: False
        C._confirm = lambda prompt: (_ for _ in ()).throw(
            AssertionError("preflight must not prompt without a terminal"))
        out = io.StringIO()
        real, sys.stdout = sys.stdout, out
        try:
            C.check_dependencies()
        finally:
            sys.stdout = real
        text = out.getvalue()
        check("explains what ImageMagick is for",
              C.IMAGEMAGICK_WHY in text)
        check("offers an install command without prompting",
              any(m in text for m in ("winget", "brew", "apt-get", "dnf",
                                      "yum", "pacman", "apk")),
              text)

        # With a terminal it must ask, and declining must not raise.
        C._tty = lambda: True
        asked = []
        C._confirm = lambda prompt: (asked.append(prompt), False)[1]
        out = io.StringIO()
        real, sys.stdout = sys.stdout, out
        try:
            C.check_dependencies()
        finally:
            sys.stdout = real
        check("offers to install on a terminal", len(asked) == 1, str(asked))
        check("declining carries on without an error",
              "Skipped" in out.getvalue())

        # No package manager at all must still be non-fatal.
        shutil.which = lambda name: None
        check("no package manager -> no suggestion", C._suggest_install() is None)
        out = io.StringIO()
        real, sys.stdout = sys.stdout, out
        try:
            C.check_dependencies()
        finally:
            sys.stdout = real
        check("no package manager still exits cleanly",
              "nothing to offer" in out.getvalue())
    finally:
        C._logo.imagemagick = real_magick
        shutil.which = real_which
        C._tty = real_tty
        C._confirm = real_confirm


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
            "import sys, convert_to_car as C\n"
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
    test_torque_for_time()
    test_tune_transmission_stall_ratio()
    test_launch_force_matches_after_stall_fix()
    test_windows_compatibility()
    test_dependency_check()
    if not FIXTURES.is_dir():
        return _report()
    test_reference_roundtrip()
    test_physics_sii_policy()
    test_real_engine_scaling()
    test_real_wheel_grip()
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