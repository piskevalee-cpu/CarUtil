# fix.md — review findings, 2026-10-20

A review of the converter for bugs and regressions. **Nothing in this file has
been fixed**; it is a report, written so the fixes can be triaged and each one
can be re-checked.

## Method

- The whole suite was run twice before looking at any code:
  `python3 test_converter.py < /dev/null` and
  `script -qec "python3 test_converter.py" /dev/null`. **Both exit 0**, with the
  usual 5 fixture skips and no failures.
- Every file was read end to end: `carutil.py` (5232 lines), `backend/sii.py`,
  `backend/hashfs.py`, `backend/logo.py`, `backend/ui.py`, `backend/brands.py`,
  `test_converter.py` (6313 lines).
- Every candidate defect was then **reproduced against the real code** before it
  was written down here. A candidate that would not reproduce is not in this
  report. The reproduction script is at the end, and it prints `DEFECT PRESENT`
  for each one — all 14 checks confirm.
- Severity is about consequence for the converted car, not about effort.

**Every finding below is a defect the test suite does not catch.** The suite
passes with all eleven present.

## Summary

| id | severity | where | symptom | regression? |
|---|---|---|---|---|
| F1 | high | `carutil.py:2426` | `dealer_price()` raises an uncaught `ValueError` and aborts the conversion | no — since 2026-10-13 |
| F2 | high | `carutil.py:2547` | requested kerb weight not honoured on a 3-axle mod (2200 kg → 3250 kg written) | no — since 2026-10-11 |
| F3 | high | `carutil.py:1491` | `rpm_range_power` written with low > high | **yes** — 2026-10-17, `0247a7c` |
| F4 | high | `carutil.py:2552` | a truck-scale value with a trailing comment reads as car-sane | no — since 2026-10-11; the hex-float half was fixed 2026-10-14 |
| F5 | medium | `carutil.py:3334` | `--shift-up-rpm` below the band floor inverts the gear bands | **yes** — 2026-10-18, `ab7017c` |
| F6 | medium | `carutil.py:1146` | `tank_size` configured in every preset, never written | **yes** — 2026-10-14, `4403c5a` |
| F7 | medium | `carutil.py:5108` | the step-5 0-60 answer is collected and then discarded | no — since 2026-10-13 |
| F8 | medium | `carutil.py:1796` | only the last dangling `gps_path` in a file is repointed | no — since 2026-10-19, `109cb0a` |
| F9 | low | `carutil.py:5017` | `--extra-tags` with an empty entry names the wrong flag | no — since 2026-10-11 |
| F10 | low | `backend/sii.py:38` | an unquoted value containing a colon is split, corrupting the def | no — since 2026-10-11 |
| F11 | low | `carutil.py:1013` | an unreadable paint-job def aborts the whole conversion | no — since 2026-10-11 |

Dates are the commit that introduced the code, found with `git log -S`.

---

## F1 — `dealer_price()` aborts the conversion on a `price:` that is not a bare number

**Severity: high.** An uncaught exception, mid-conversion, on a mod that is
otherwise fine.

`carutil.py:2425-2426`:

```python
cur = get_key((files.get(name) or "").splitlines(), "price")
rows.append((name, int(float(cur)) if cur else 0))
```

`get_key()` returns the value **as written, including any trailing comment**,
and `float()` is called on it with no guard. Three ordinary ways a mod's price
line looks all raise:

```
price: 1000 # base price          -> ValueError: could not convert string to float: '1000 # base price'
price: 1000   icon: "x"           -> ValueError: could not convert string to float: '1000   icon: "x"'
price: &459380cd                  -> ValueError: could not convert string to float: '&459380cd'
```

The three cases have three separate causes:

1. **The trailing comment.** The repo already knows this trap: `AGENTS.md` records
   that this mod's limiter is `6100 # max of 6.2 L V8 is 6100 RPM`, and
   `engine_rev_bounds()` deliberately reads the limiter with a regex rather than
   `get_key()` for exactly this reason. `dealer_price()` reads it with
   `get_key()`.
2. **The collapsed line.** `dealer_price()` uses a bare `.splitlines()` instead of
   `edit_lines()`, so several keys on one line read as one key whose value
   happens to contain the others — the trap the whole `sii.py` module exists to
   handle.
3. **The hex-float form.** `sii.to_float()` decodes `&459380cd`; plain `float()`
   does not, and `AGENTS.md` warns that "any caller wrapping that in
   `except ValueError` will judge the value harmless".

The irony is that `apply_dealer_price()` **does** guard the same read, 35 lines
later (`carutil.py:2462-2468`, in the `every` loop). The guard exists in one
place and not the other, and `apply_dealer_price()` calls `dealer_price()` first
(`carutil.py:2449`), so the unguarded one is always the one that fires.

**Impact.** `--price` dies on any mod whose price line carries a comment, and so
does the default path — `apply_dealer_price(files, ..., price, rep)` runs
unconditionally at `carutil.py:4103`, so the price is reported even when
`--price` was not asked for.

**Fix direction.** Read the price through `edit_lines()` and `sii.to_float()`,
strip a trailing comment, and wrap the conversion in a `try/except ValueError`
that treats an unreadable price as 0 and reports it — the same treatment
`apply_dealer_price()`'s own loop already gives it.

---

## F2 — the requested kerb weight is not honoured on a 3-axle mod

**Severity: high.** The car comes out substantially heavier than the user asked
for, and the engine is tuned for the weight that was asked for rather than the
one written.

`carutil.py:2544-2548`, in `car_physics()`:

```python
kg = [int(x) for x in cfg["kerb_weight"]]
while len(kg) < axles:
    kg.append(kg[-1] if kg else 0)
cfg["kerb_weight"] = [str(v) for v in kg[:axles]]
```

The extra axles are given a **copy of the rear figure**, so the total grows.
`--mass 2200` on a 3-axle mod:

```
requested mass: [1150, 1050] total 2200
written: ['kerb_weight[0]: 1150', 'kerb_weight[1]: 1050', 'kerb_weight[2]: 1050']
written total: 3250 kg   -> over by 1050 kg
```

`split_mass()` already knows how to do this correctly — `split_mass(2200,
'suv', 3)` gives `[1150, 525, 525]`, whose sum is exactly 2200 — but it is never
called with the real axle count. `main()` always parses `--mass` as two axles
(`carutil.py:5078-5080`), and `prompt_weight()` always returns two
(`carutil.py:1418`).

**Impact.** The engine target is computed from `kerb_kg = sum(mass)`
(`carutil.py:3911`) — the *requested* 2200 kg — so the engine is tuned for a car
48 % lighter than the one written. A 3-axle truck converted at `--mass 2200`
drives like a 3.25 t car with an engine built for 2.2 t.

**Fix direction.** Pass the real axle count into the split: `car_physics()`
should call `split_mass()` for the mass it is given rather than padding it, and
`main()` should not be the only place that knows the split is two axles.

---

## F3 — `rpm_range_power` can be written with low > high

**Severity: high.** A malformed band goes into the engine unit.

`carutil.py:1489-1492`, in `_rpm_range_bands()`:

```python
power = (round(3500 * p / _RPM_RANGE_REFERENCE_RPM),
         round(6000 * p / _RPM_RANGE_REFERENCE_RPM))
if rpm_limit and power[1] > rpm_limit:
    power = (power[0], int(rpm_limit))
```

Only the band's **top** is clamped to the rev limit. When the limit falls below
the band's floor the result is inverted:

```
peak=4500 limit=3000 -> rpm_range_power: '(3150, 3000)'
peak=5000 limit=3000 -> rpm_range_power: '(3500, 3000)'
peak=6000 limit=3500 -> rpm_range_power: '(4200, 3500)'
```

The floor is `0.70 × peak`, so any rev limit below that inverts the band.

**Reachability.** On the horsepower path the peak is clamped to the limit first
(`_requested_power_peak()` takes `rev_limit` and caps at it, `carutil.py:3320`),
so the band comes out as `(2100, 3000)` — ordered. The defect is on the
**rescale path** (`--engine-torque` / `--engine-nm-per-tonne`, no `--power`),
where `_ensure_rpm_ranges()` is called with `power_peak_rpm=None` and the peak
is read from the mod's own curve (`_power_peak_rpm()`), which is **not** clamped
to the ceiling about to be written. A mod whose curve peaks at 4500, converted
with `--rev-limit 3000 --engine-torque 500`, gets `rpm_range_power: (3150,
3000)`.

**Impact.** The automatic gearbox is handed a band whose lower bound is above
its upper bound. The game is being told to hold full power from 3150 rpm in a
band that ends at 3000 rpm.

**Fix direction.** Clamp the whole band, not just its top: if `rpm_limit` falls
below the floor, pull the floor down with it — or refuse to write a band whose
two bounds are not ordered, and say why.

---

## F4 — a trailing comment on a value defeats every numeric read

**Severity: high.** A 9.4 t truck chassis is preserved as though it were a car.

`get_key()` and `get_all_indexed()` return the value **as written, including
its trailing comment**. Every numeric reader then hands that to `to_float()`,
which raises, and the `except (TypeError, ValueError)` in `_is_truck_scale()`
turns the raise into "this value is fine, keep it":

```
_is_truck_scale("air_resistance", "1.5")       -> True   (truck-scale, correctly)
_is_truck_scale("air_resistance", "1.5 # x")   -> False  (truck-scale, missed)
_is_truck_scale("kerb_weight", ["4720.1"])     -> True   (truck-scale, correctly)
_is_truck_scale("kerb_weight", ["4720.1 # x"]) -> False  (truck-scale, missed)
_is_truck_scale("kerb_weight", ["&459380cd"])  -> True   (truck-scale, correctly)
_is_truck_scale("kerb_weight", ["&459380cd # x"]) -> False (truck-scale, missed)
```

A whole chassis that would be replaced without the comment:

```
kerb_weight[0]: 4720.1 # front
kerb_weight[1]: 4720.1 # rear
air_resistance: 1.5 # truck drag
```

comes out of `tune_chassis()` **unchanged**.

**This is the same failure the function's own docstring says was fixed.**
`carutil.py:2559-2562`:

> Every number goes through sii.to_float, not float(): SII's hex-float form
> (`kerb_weight[0]: &459380cd` = 4720.1 kg) used to raise ValueError here, be
> swallowed by the except, and return False -- so a 9.4 t truck chassis read as
> car-sane and was preserved whole.

The hex-float half was fixed on 2026-10-14 (`0bd3cce`). The comment half is the
same bug on the same line, and it is not fixed.

**Impact.** A mod that annotates its chassis — which the repo's own history says
these mods do — keeps its truck kerb weight, its truck drag and its truck
steering angle. `tune_chassis()` reports them as "preserved existing tuned
values", so the report says the opposite of what happened.

**Fix direction.** Strip the trailing comment before any numeric read, in one
place, and use that everywhere. `sii.to_float()` is the natural home: it is
already the function whose job is "a SII numeric value as a float".

---

## F5 — `--shift-up-rpm` below the band floor inverts the gear bands

**Severity: medium.** Unvalidated user input produces a malformed band.

`carutil.py:3324-3335`, in `_set_shift_up()`:

```python
for key in ("rpm_range_low_gear", "rpm_range_high_gear"):
    lo = int(re.findall(r"\d+", out[key])[0])
    out[key] = f"({lo}, {shift_up_rpm})"
```

There is no check that `shift_up_rpm` is above the floor it is being written
over:

```
--shift-up-rpm 1000 -> rpm_range_low_gear: (1350, 1000)   low > high
--shift-up-rpm 5000 -> rpm_range_low_gear: (1350, 5000)   (in range, fine)
```

The same write happens on the path where the mod declares its own bands
(`carutil.py:3376-3380`), where the floor is the mod's own value and can be
anywhere.

**Fix direction.** Validate the shift point against the band it is written into,
and refuse or clamp rather than write an inverted band. `_parse_band()`
(`carutil.py:1862`) already enforces `0 < lo < hi` for the interactive path —
the flag path should use the same rule.

---

## F6 — `tank_size` is configured in every preset and never written

**Severity: medium.** Dead configuration that reads like a working rule.

`_PARATI` carries `"tank_size": "47"` (`carutil.py:1146`) and every entry of
`_TUNING` carries its own (`carutil.py:2368-2388`: 79, 72, 55, 80, 75, 72).
`car_physics()` copies them into the config. **No loop in `tune_chassis()` writes
them** — the indexed loop (`carutil.py:2802`) handles `residual_travel`,
`suspension_damping_factor`, `disc_brake`, `axle_suspension_type`, `kerb_weight`
and `brake_rotor_diameter`; the scalar loop (`carutil.py:2824`) handles
`air_resistance`, `cog_height`, `sway_bar_stiffness`, `max_steering_angle`,
`suspension_class`, `nominal_voltage` and `air_tank_pressure`. `tank_size` is
in neither.

```
preset carries tank_size=79, chassis keeps tank_size: 1200
```

**Provenance.** It was wired in on 2026-10-14 (`0bd3cce`) and reverted the same
day (`4403c5a`, "Revert the tank_size injection: it made fuel range worse") —
correctly, because a smaller tank at the truck's untouched consumption rate is a
shorter range. The revert removed the writer but left the configured value
behind, so the presets now advertise a tank size the converter does not apply.

**This is the trap the repository's own documentation names**, in
`AGENTS.md`:

> **A configured key is not a written key.** `tank_size` had a value in every
> preset *and* a branch in `_is_truck_scale`, and no loop ever wrote it.

and again:

> A threshold branch with no writer is dead code that reads like a working rule,
> so remove the branch rather than leave it dormant.

The branch is gone; the configured value is not. `AGENTS.md` also says the
presets at 47-80 "are themselves extrapolations and should be corrected before
anyone rewrites the tank", which reads as though they are in use.

**Fix direction.** Either remove `tank_size` from `_PARATI` and `_TUNING`, or
keep it and say in the report that the tank is deliberately the mod's own. The
second is closer to what the code decided on 2026-10-14; the first is closer to
the "no dead config" rule.

---

## F7 — the step-5 0-60 answer is collected and then discarded

**Severity: medium.** A mandatory prompt whose answer has no effect at all.

`main()` (`carutil.py:5105-5110`):

```python
if power is None and wanted is None:
    _ui.step(5, total, "Set the power")
    power, peak_rpm = prompt_power(kerb, engine_rev_bounds(src))
elif wanted is None:
    _ui.step(5, total, "Set the acceleration")
    wanted = prompt_acceleration(kerb)
```

With `--power` on the command line, the first branch is false and the second is
true, so the walkthrough **asks for a 0-60 target**. The answer becomes
`engine_nm_per_tonne` (`carutil.py:5128-5130`) and is passed to `convert()`.
But `tune_engine()` returns from the horsepower branch before it is used
(`carutil.py:3472-3475`):

```python
if target_hp is not None:
    return _tune_engine_to_hp(text, rep, target_hp, peak_power_rpm, ...)
```

Two completely different 0-60 answers produce byte-identical engines:

```
with a 0-60 answer of 6.0 s -> torque: 516
with a 0-60 answer of 3.0 s -> torque: 516
```

**The comment above the branch contradicts the code:**

> 0-60 is the older proxy, kept for scripts, and only offered when neither a
> --power nor a --acceleration was given on the command line.

It *is* offered when `--power` was given. `README.md` compounds it: "In guided
mode, `--mass` does not skip the weight prompt; `--power` changes the engine
question to a 0–60 target" — which reads as though the 0-60 answer is what
drives the engine. It does not.

**Fix direction.** Either do not ask when the answer cannot be used, or use it.
Asking a question and throwing the answer away is the one thing a walkthrough
must not do.

---

## F8 — only the last dangling `gps_path` in a file is repointed

**Severity: medium.** The dangling pointer the fix exists to remove survives.

`_fix_dangling_ui_paths()` (`carutil.py:1788-1802`) iterates over the matches in
the original text but writes with `set_key()`, which replaces the **last**
matching line:

```python
lines = set_key(lines, key, f'"/{new}"')
```

A def file with two `gps_path` lines — two units in one file, which is legal
SII — comes out as:

```
gps_path: "/ui/dashboard/v_gps.sii"   <- repointed
gps_path: "/ui/gps.sii"               <- still dangling
```

`set_key()` replaces the last match, so the second iteration of the loop
rewrites the line the first iteration already fixed, and the first one is never
touched.

**Impact.** The function exists because a `gps_path` pointing at a script nobody
shipped is the same death as a missing template unit — the game dies loading a
script that is not there. On a file with two such lines it removes half the
cause.

**Fix direction.** Rewrite the line in place at the match position rather than
going through `set_key()`, so each match is handled where it is.

---

## F9 — `--extra-tags` with an empty entry names the wrong flag

**Severity: low.** A confusing message, no crash of the user's own making.

`carutil.py:5017`:

```python
for extra in args.extra_tags.split(","):
    extra = job_tag(normalise_type(extra))
```

`normalise_type("")` raises `SystemExit("error: --type must not be empty")`
(`carutil.py:1179`). A trailing or leading comma in `--extra-tags` therefore
produces:

```
error: --type must not be empty
```

when the user never passed `--type`. Both `--extra-tags "suv,"` and
`--extra-tags ",suv"` fail this way.

**Fix direction.** Skip empty entries, and let `normalise_type()`'s message
mention the flag it was actually given.

---

## F10 — an unquoted value containing a colon is split, corrupting the def

**Severity: low.** Needs an unquoted value with a colon and a space after it.

`backend/sii.py:38-70`, `_key_token_offsets()`, scans a line for key tokens and
skips quoted text. A **quoted** value with a colon is handled correctly — the
docstring gives `info[]: "V: 8"` as the example. An **unquoted** one is not:

```
sii.expand_multi_key_lines(['\tname: foo: bar'])
  -> ['\tname:', '\tfoo: bar']
```

The def is now two lines, the first with an empty value. SII values are usually
quoted, which is why this has not been seen in the wild — but the scanner's own
docstring claims it knows "only a token outside quotes and before the comment is
a real key", and an unquoted value is a token outside quotes.

**Fix direction.** A value that is not quoted is a value to the end of the line;
the scanner should not look for keys after the first `:` on a line whose value
is unquoted.

---

## F11 — an unreadable paint-job def aborts the whole conversion

**Severity: low.** Inconsistent with every other read in the file.

`carutil.py:1007-1015`:

```python
stock = [
    n for n in pj
    if re.search(r"\bstock:\s*true\b", src.read_def(n))
]
```

`read_def()` raises `SystemExit` when a file will not decode — that is its
whole purpose, and every other call site in the file wraps it:

```python
try:
    text = src.read_def(n)
except SystemExit:
    continue
```

This list comprehension does not, so a single paint-job def the converter
cannot read ends the run:

```
SystemExit: error: def/vehicle/truck/testcar/paint_job/bad.sii is not a readable
definition file.
```

**Fix direction.** The same `try/except SystemExit` the other call sites use.

---

## Consistency notes (not defects)

- `stale_binding` is defined twice, identically, at `carutil.py:3903` and
  `carutil.py:4066`. The second shadows the first; both are used, so it works,
  but one of them is dead weight.
- In `main()`, the two branches that choose the output path
  (`carutil.py:5143-5151`) now produce the same result. The comment above them
  explains a difference ("Writing inside it used to mean…") that no longer
  exists in the code.
- `discover_parts()`'s first parameter is named `vdir`, but it receives the
  full vehicle directory (`def/vehicle/truck/<slug>/`), not the shared parent.
  `convert()` passes `vehicle_dir`. The name reads as though it were the parent.

## Checked and found clean

So the ground is known to be covered, these were looked at specifically and
nothing was found:

- The ZipFS local-header / central-directory method handling
  (`ModSource._central_method`, `_read_local`) — the documented trap is handled
  as described, and the stored-payload path returns bytes outright rather than
  attempting an inflate.
- `sii.edit_lines()` as the single entry point: every editing helper in
  `carutil.py` goes through it. The two exceptions found are F1 (a bare
  `.splitlines()` in `dealer_price`) and F10 (a scanner limitation inside
  `edit_lines` itself).
- `sii.set_key` / `set_indexed` bracket handling — `indexed="[]"` is used
  correctly everywhere; no call site passes `"fallback[]"` as the key.
- The HashFS reader — the u64 table offset, the cityhash-c variant, the
  CRC-and-size check in `read()`, and the `ok`/`damaged`/`missing` split in
  `paths()` all match their documentation.
- The DDS and `.tobj` writers — the pixel-format block at offset 76, `dwCaps`
  `0x00401008`, the `.tobj` length at 40, the four padding bytes at 44-47 and
  the path at 48 all match the documented layout.
- `ModSource.close()` — the `ZipFile` handle and the temp directory are both
  released, and `self.zf` is not nulled, as `AGENTS.md` requires.
- `find_mod_folders()` — it constructs known paths and does not walk the tree;
  `test_find_mod_folders_layouts` covers 16 layouts and asserts the two-second
  bound.
- The six-colour palette in `backend/ui.py` — every logical name resolves in
  both `_RICH` and `_STYLES`, `colour_enabled()` refuses `dumb` but not an
  absent `TERM`, and `_console()` pins `truecolor` only when colour is on.
- The `--extra-tags` vocabulary, `TAG_FOR_TYPE` and `TYPE_ALIASES` — the
  outward mapping (`sports`→`sport`, `classic`→`veteran`) is correct in both
  directions.
- `_retarget_final_drive()` — the ≥3-forward-gear guard is in place, the ratio
  is computed independently of wheel radius, and the rounding direction keeps
  the product inside the band.

## Not verifiable on this machine

- The real mod fixtures live in `../testing-covnverter/` and are **not
  present**. Every fixture-dependent check in the suite is skipped (5 of them).
  Behaviour that depends on the real mods — the Parati round trip, the real
  engine scaling, the real transmission tuning, the Porsche conversion — was
  reviewed by reading the code, not by running it. Findings F1, F2 and F4 in
  particular are *more* likely to occur on the real mods than on the synthetic
  one, because the real mods are the ones that carry trailing comments.
- Nothing was tested in game. The in-game numbers quoted throughout the code
  and in `FINDINGS.md` are the author's, and this review did not re-measure any
  of them.
- The Windows-only paths (`carutil.bat`, `convert.exe` vs ImageMagick, the
  Windows console colour path) were read, not run. This is Linux.

## The reproduction script

Every check prints `DEFECT PRESENT` when the defect is there. Run it from the
repository root:

```bash
python3 - <<'PY'
import sys, tempfile, zipfile
from pathlib import Path
sys.path.insert(0, "backend"); sys.path.insert(0, ".")
import carutil as C, sii

rows = []
def defect(n, present, detail):
    rows.append((n, present, detail))

dealer = "def/vehicle/car_dealer/mercedes/x.sii"
dealer_text = ('SiiNunit\n{\nvehicle : .car\n{\n}\n}\n'
  'vehicle_accessory : .chassis\n{\n\tdata_path: "/def/vehicle/truck/x/chassis/a.sii"\n}\n')
def mk(line):
    return {dealer: dealer_text,
            "def/vehicle/truck/x/chassis/a.sii":
              "SiiNunit\n{\naccessory_chassis : a\n{\n\t%s\n}\n}\n" % line}

# F1
for label, line in [("price with a trailing comment","price: 1000 # base"),
                    ("price on a collapsed line",'price: 1000   icon: "x"'),
                    ("price in hex-float form","price: &459380cd")]:
    try:
        C.dealer_price(mk(line), dealer)
        defect(f"F1  dealer_price raises on a {label}", False, "no exception")
    except ValueError as e:
        defect(f"F1  dealer_price raises on a {label}", True, f"ValueError: {e}")

# F2
chassis3 = ("SiiNunit\n{\naccessory_chassis : c\n{\n"
  "\tkerb_weight[0]: 3000\n\tkerb_weight[1]: 3000\n\tkerb_weight[2]: 3000\n}\n}\n")
mass = C.split_mass(2200, "suv", 2)
out = C.tune_chassis(chassis3, "suv", mass, C.Report())
tot = sum(float(l.split(":")[1]) for l in out.splitlines() if "kerb_weight" in l)
defect("F2  requested kerb weight honoured on a 3-axle mod", tot != 2200,
       f"requested 2200 kg, chassis written {tot:.0f} kg "
       f"(split_mass(2200,'suv',3)={C.split_mass(2200,'suv',3)} is never used)")

# F3
b = C._rpm_range_bands(4500, 3000)
lo, hi = b["rpm_range_power"].strip("()").split(",")
defect("F3  rpm_range_power written low > high", int(lo) > int(hi),
       f"rpm_range_power = {b['rpm_range_power']} (peak 4500, rev limit 3000)")

# F4
defect("F4  truck-scale kerb_weight with a trailing comment is detected",
       not C._is_truck_scale("kerb_weight", ["4720.1 # front"]),
       "4720.1 kg axle reads as car-sane")
defect("F4  truck-scale air_resistance with a trailing comment is detected",
       not C._is_truck_scale("air_resistance", "1.5 # truck drag"),
       "1.5 drag reads as car-sane")

# F5
b5 = C._set_shift_up(C._rpm_range_bands(5000, None), 1000)
lo, hi = b5["rpm_range_low_gear"].strip("()").split(",")
defect("F5  --shift-up-rpm below the band floor inverts the band", int(lo) > int(hi),
       f"rpm_range_low_gear = {b5['rpm_range_low_gear']} (--shift-up-rpm 1000)")

# F6
cfg = C.car_physics("suv", 2, None, None)
out = C.tune_chassis("SiiNunit\n{\naccessory_chassis : c\n{\n\ttank_size: 1200\n}\n}\n",
                     "suv", None, C.Report())
defect("F6  preset tank_size reaches the chassis", "tank_size: 79" not in out,
       f"preset carries tank_size={cfg['tank_size']}, chassis keeps 1200 L")

# F7
eng = ("SiiNunit\n{\naccessory_engine_data : e\n{\n\ttorque: 1400\n"
       "\ttorque_curve[]: (1500, 0.43)\n\ttorque_curve[]: (3750, 1.0)\n\trpm_limit: 5000\n}\n}\n")
def build(nm_per_tonne):
    rep = C.Report()
    o = C.tune_engine(eng, rep, target_hp=300.0, nm_per_tonne=nm_per_tonne, tonnes=2.2)
    return [l.strip() for l in o.splitlines() if l.strip().startswith("torque:")][0]
a, b = build(622.1), build(1200.0)
defect("F7  the 0-60 answer reaches the engine when --power was given", a == b,
       f"6.0 s -> {a}; 3.0 s -> {b}; identical, so the answer changed nothing")

# F8
files = {
 "def/vehicle/truck/v/interior/a.sii":
   'SiiNunit\n{\naccessory_interior : a\n{\n\tgps_path: "/ui/gps.sii"\n}\n}\n'
   'accessory_interior : b\n{\n\tgps_path: "/ui/gps.sii"\n}\n}\n',
 "def/vehicle/truck/v/interior/c.sii":
   'SiiNunit\n{\naccessory_interior : c\n{\n\tgps_path: "/ui/dashboard/v_gps.sii"\n}\n}\n',
 "ui/dashboard/v_gps.sii": "SiiNunit\n{\n}\n",
}
C._fix_dangling_ui_paths(files, C.Report())
dang = [l.strip() for l in files["def/vehicle/truck/v/interior/a.sii"].splitlines()
        if "gps_path" in l and "/ui/gps.sii" in l]
defect("F8  every dangling gps_path is repointed", bool(dang),
       f"still points at /ui/gps.sii: {dang}")

# F9
try:
    for extra in "suv,".split(","):
        C.job_tag(C.normalise_type(extra))
    defect("F9  --extra-tags with an empty entry names the wrong flag", False, "no error")
except SystemExit as e:
    defect("F9  --extra-tags with an empty entry names the wrong flag", "--type" in str(e),
           f"SystemExit: {e}")

# F10
out10 = sii.expand_multi_key_lines(['\tname: foo: bar'])
defect("F10 an unquoted value containing a colon is not split", out10 != ['\tname: foo: bar'],
       f"became {out10}")

# F11
with tempfile.TemporaryDirectory() as td:
    p = Path(td)/"m.scs"
    vdir = "def/vehicle/truck/testcar/"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr(vdir+"data.sii", "SiiNunit\n{\naccessory_truck_data : vehicle.testcar\n{\n}\n}\n")
        z.writestr(vdir+"paint_job/bad.sii", b"\xff\xfe\x00bad bytes")
    try:
        C.discover_parts(C.ModSource(p), vdir, "testcar", "")
        defect("F11 an unreadable paint_job def aborts the run", False, "no error")
    except SystemExit as e:
        defect("F11 an unreadable paint_job def aborts the run", True, f"SystemExit: {str(e)[:70]}")

print()
w = max(len(n) for n,_,_ in rows)
for n, present, d in rows:
    print(f"  {'DEFECT PRESENT' if present else 'not present  '}  {n:<{w}}  {d}")
print(f"\n  {sum(1 for _,p,_ in rows if p)} of {len(rows)} checks confirm a defect.")
PY
```

Expected output: 14 of 14 checks confirm a defect.
