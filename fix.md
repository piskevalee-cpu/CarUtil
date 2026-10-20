# fix.md — review findings, and what was done about them, 2026-10-20

A review of the converter for bugs and regressions, and the fixes that came out
of it. **All eleven findings are fixed**, each with a regression test that fails
without the fix. Two further observations are recorded at the end and are
**not** fixed.

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

**Every finding below was a defect the test suite did not catch.** The suite
passed with all eleven present.

## Summary

| id | severity | where | symptom | regression? | status |
|---|---|---|---|---|---|
| F1 | high | `carutil.py:2426` | `dealer_price()` raises an uncaught `ValueError` and aborts the conversion | no — since 2026-10-13 | **fixed** |
| F2 | high | `carutil.py:2547` | requested kerb weight not honoured on a 3-axle mod (2200 kg → 3250 kg written) | no — since 2026-10-11 | **fixed** |
| F3 | high | `carutil.py:1491` | `rpm_range_power` written with low > high | **yes** — 2026-10-17, `0247a7c` | **fixed** |
| F4 | high | `carutil.py:2552` | a truck-scale value with a trailing comment reads as car-sane | no — since 2026-10-11; the hex-float half was fixed 2026-10-14 | **fixed** |
| F5 | medium | `carutil.py:3334` | `--shift-up-rpm` below the band floor inverts the gear bands | **yes** — 2026-10-18, `ab7017c` | **fixed** |
| F6 | medium | `carutil.py:1146` | `tank_size` configured in every preset, never written | **yes** — 2026-10-14, `4403c5a` | **fixed** |
| F7 | medium | `carutil.py:5108` | the step-5 0-60 answer is collected and then discarded | no — since 2026-10-13 | **fixed** |
| F8 | medium | `carutil.py:1796` | only the last dangling `gps_path` in a file is repointed | no — since 2026-10-19, `109cb0a` | **fixed** |
| F9 | low | `carutil.py:5017` | `--extra-tags` with an empty entry names the wrong flag | no — since 2026-10-11 | **fixed** |
| F10 | low | `backend/sii.py:38` | an unquoted value containing a colon is split, corrupting the def | no — since 2026-10-11 | **fixed** |
| F11 | low | `carutil.py:1013` | an unreadable paint-job def aborts the whole conversion | no — since 2026-10-11 | **fixed** |
| F12 | medium | `carutil.py:3911` | with no `--mass`, the engine is tuned for the type default while the chassis may keep the mod's own weight | no — since 2026-10-11 | **not fixed** — see below |
| F13 | medium | `carutil.py:3856` | `stall_torque_ratio` has a ceiling but no floor, so a mod declaring a low one keeps it (the G63's 1.00 against SCS's 1.8-2.0) | no — since 2026-10-11 | **not fixed** — see below |

Dates are the commit that introduced the code, found with `git log -S`.

## What was done

Each fix is the smallest change that removes the defect, and each has a
regression test in `test_converter.py` that fails without it. The test names
carry the finding's number.

| id | the change |
|---|---|
| F1 | `dealer_price()` reads the price through `edit_lines()` and `sii.to_float()`, so a comment, a collapsed line and the hex-float form are all read as the number they are. An unreadable price is **reported** and counted as zero rather than ending the run. `apply_dealer_price()`'s own loop uses the same reader. |
| F2 | `car_physics()` spreads the mass over the real axle count with `_spread_over_axles()`: the extra axles share the rear group instead of copying the rear figure, so the total is preserved exactly and the front figure the author asked for survives. |
| F3 | `_rpm_range_bands()` clamps the **whole** power band when the ceiling is below its floor, scaling the band down to fit under the ceiling instead of leaving `(3150, 3000)`. The bands that were already ordered are untouched. |
| F4 | `sii.to_float()` cuts the trailing comment off a value before reading it, so one function fixes every numeric read. The hex-float and the quoted forms are handled there too. |
| F5 | `_set_shift_up()` raises a change-up point that is at or below the band floor to just above it, and `_report_shift()` **warns** that the user's point was raised and says what it became. The declared-bands path shares the same helper. |
| F6 | The dead `tank_size` is removed from `_PARATI` and from every entry of `_TUNING`. The tank was never written, so no output changes; what changes is that nothing advertises a figure the converter does not apply. |
| F7 | The step-5 question is split: `prompt_peak_rpm()` asks where the power peaks, and it is asked on its own when `--power` already settled the horsepower. The 0-60 answer is no longer collected, so it cannot be discarded. |
| F8 | `_fix_dangling_ui_paths()` rewrites each occurrence **where it stands** instead of going through `set_key()`, so every path in a file is fixed and a path that was already fine is left alone. |
| F9 | `--extra-tags` skips an empty entry instead of handing `""` to `normalise_type()`. |
| F10 | `sii.expand_multi_key_lines()` leaves a line alone when the split would give the first pair an empty value — which is the signature of a mis-read boundary, and of a value that contains a colon. |
| F11 | The two `read_def()` calls in `discover_parts()` that were unguarded are guarded, like every other one in the file. The second one was found by the new test, not by the review. |

### Verification

- The suite passes **both** ways: `python3 test_converter.py < /dev/null` and
  `script -qec "python3 test_converter.py" /dev/null`. The second is the one
  that catches a test reaching a prompt: a new test of mine reached the
  rev-band menu and hung the console run, which is exactly the failure the
  repository's own notes describe.
- A **regression matrix**: the same synthetic mod was converted under eleven
  option sets — default, `--power`, `--acceleration`, `--mass`, `--rev-limit`,
  a rev limit below the power peak, `--shift-up-rpm`, a shift point below the
  band floor, `--price`, `--force-physics` and a 3-axle chassis — with the
  committed code and with the fixed code, and every generated `.sii` was
  compared. **Eight of the eleven are byte-identical.** The three that differ
  are exactly the three defects that were supposed to change:

  | case | before | after |
  |---|---|---|
  | 3-axle chassis | `kerb_weight[1]: 1050`, `[2]: 1050` (3250 kg) | `[1]: 525`, `[2]: 525` (2200 kg) |
  | rev limit below the power peak | `rpm_range_power: (3500, 3000)` | `(1750, 3000)` |
  | shift point below the band floor | `(1215, 900)`, `(1305, 900)` | `(1215, 1216)`, `(1305, 1306)` |

  Nothing else moved.
- The reproduction script at the end of this file was re-run against the fixed
  code: every defect it could demonstrate is gone.

---

The eleven sections below describe each defect **as it was found**. What was
changed is in the table above; the "What was done" paragraph at the end of
each section is the same change, with the reasoning.

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

**What was done.** Read the price through `edit_lines()` and `sii.to_float()`,
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

**What was done.** Pass the real axle count into the split: `car_physics()`
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

**What was done.** Clamp the whole band, not just its top: if `rpm_limit` falls
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

**What was done.** Strip the trailing comment before any numeric read, in one
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

**What was done.** Validate the shift point against the band it is written into,
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

**What was done.** Either remove `tank_size` from `_PARATI` and `_TUNING`, or
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

**What was done.** Either do not ask when the answer cannot be used, or use it.
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

**What was done.** Rewrite the line in place at the match position rather than
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

**What was done.** Skip empty entries, and let `normalise_type()`'s message
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

**What was done.** A value that is not quoted is a value to the end of the line;
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

**What was done.** The same `try/except SystemExit` the other call sites use.

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

## Found during the fixes, and left alone

These are real, and they are **not fixed**. They are recorded here rather than
fixed because each one needs a product decision or a change with a much larger
blast radius than the eleven above.

### F12 — with no `--mass`, the engine is tuned for the type default while the chassis may keep the mod's own weight

`convert()` computes the engine target from `kerb_kg = sum(mass) if mass else
sum(_MASS.get(vtype, _MASS["sedan"]))` — the *type's* default when no mass was
asked for. `tune_chassis()` keeps the mod's own `kerb_weight` when it is not
truck-scale, so the two can disagree:

```
no --mass, mod total 3000 kg -> kerb_weight[0]: 1500, kerb_weight[1]: 1500
engine target would be: 1220 kg (the sedan type default)
```

A 3000 kg car with an engine built for 1220 kg. It does not bite on a truck
chassis, because a truck's weight is above the 4000 kg threshold and is
replaced by the type default — it bites on a heavy car that is not a truck.

**Not fixed**, because the fix is not local: the engine is tuned before the
chassis is read, and `src.readable` is in archive order, so the chassis may not
have been seen yet. The honest fix is to read the chassis's `kerb_weight` first
and derive the engine target from the mass that is actually going to be
written, which is a change to the order of the conversion pass and to what
"no mass asked for" means.

### F13 — the stall ratio has a ceiling but no floor, so a mod that declares a low one keeps it

`stall_torque_ratio` is the launch multiplier: launch force is
`torque x first_gear_total x stall_torque_ratio`. The converter guards the
**ceiling** — anything above 2.0 is replaced, because a diesel figure of 4.50
multiplied the launch force 2.3x — but there is no floor, so a value below
SCS's own cars' range is preserved.

The G63 declares **1.00**. SCS's own four Road Trip cars use **1.8-2.0**.
Launch force is therefore about 45 % of what the game's own cars get from the
same torque: the car is not violent, but it is slow off the line for what it
has. The other four mods measured are all fine, because all four declare a
value in the range or above it:

| mod | declares | converted to |
|---|---|---|
| G63 | 1.00 | **1.00 (preserved)** |
| W223 | 1.953 | 1.953 |
| BMW M5 | 2.322 | 2.0 |
| Escalade | 2.412 | 2.0 |
| Pilot | 4.50 | 2.0 |

**Not fixed**, because it is a product decision and not a bug in the code: the
value is the mod author's own, the converter's rule is explicitly "preserve
what is already in range", and a floor would change how every converted car
launches. The question is whether `stall_torque_ratio` should be required to
land in SCS's own range on **both** sides, the way `residual_travel` is
written outright because its truck and car values overlap. A floor of 1.8
would be the measured answer, but it is a decision about how every car feels,
and it is the maintainer's to make.

### A rev limit below the engine's power peak is not reported

`--rev-limit 3000` on an engine whose curve peaks at 4500 writes the ceiling
and leaves the curve alone on the rescale path, so the engine cannot reach its
own power peak. `_warn_airy_limit()` reports the opposite case — a ceiling
*above* the rev the curve runs out at — and says nothing about this one. The
band now scales to fit under the ceiling (F3), so the gearbox is not told
something impossible, but the user is not told that the engine lost its top
end.

### The conversion summary repeats itself

The chassis is tuned once per chassis file, and a mod with two chassis files
(say `xdl.sii` and `xdrive.sii`) reports the whole set of chassis changes
twice. Harmless, but noisy, and it makes the report harder to read than it
needs to be.

## Tested on real cars

Three real mods were converted and the results checked the way the game reads
them, not the way the converter reports them. The cars came from
`~/Downloads`: the G63, the BMW M5 and the X167 GLS, the last one inside a
`.zip` wrapper so the unwrap path was exercised too. The base game's own Road
Trip cars in `dlc_rt_ford.scs` were used as the reference.

Each converted archive was checked for: a valid zip with every entry
decompressing; every def valid UTF-8 with balanced braces and a recognised
header; a complete registration chain; the vehicle unit's tags, its three
`info[]` lines and its fallbacks, every one of which resolves; **nothing
pointing at a file the archive does not contain** — `data_path`, `gps_path`,
`dashboard_path`, `texture` and `look_template`; the brand logo as a complete
dds/mat/tobj triple; every source asset carried over; and the engine, chassis,
gearbox and tyre values.

| car | input | result |
|---|---|---|
| Mercedes-Benz G63 | `.scs` | 12 checks, 0 problems |
| BMW M5 G90 | `.scs`, index-flagged encryption | 12 checks, 0 problems |
| Mercedes-Benz X167 GLS | `.zip` wrapping a `.scs` | 12 checks, 0 problems |

Against **SCS's own four Road Trip cars**, every value the converter writes
sits inside the range they use — `air_resistance` 0.55 against their
0.37-0.596, `cog_height` 0.72 against 0.52-0.738, `brake_rotor_diameter` 0.302
against 0.279-0.356, the top gear 2.26 against their 2.258-2.790, the torque
curve peaking at exactly 1.00 with a `(0, 0)` origin, and `roll_resistance`
11 and `grip_factor` 1.1 against the base game's own 11-13 and 1.1-1.2. The
one value outside is the G63's `stall_torque_ratio` — see F13 above.

The G63 also confirms the two defects FINDINGS §41 measured are handled: the
dashboard's two dangling DAF template units were written into
`ui/template/dashboard_text.w463_g63_gps.sii`, and the `gps_path` that pointed
at a script nobody ships was repointed at the dashboard the vehicle's own
other parts use.

## The verification script
Every finding above was reproduced against the code before it was fixed, and
the same script re-run against the fixed code now shows each one gone. It is
kept here so the claim can be re-checked rather than taken on trust. Run it
from the repository root:

```bash
python3 - <<'PY'
import sys, tempfile, zipfile, re
from pathlib import Path
sys.path.insert(0, "backend"); sys.path.insert(0, ".")
import carutil as C, sii

rows = []
def check(name, good, detail=""):
    rows.append((name, good, detail))

dealer = "def/vehicle/car_dealer/mercedes/x.sii"
dealer_text = ('SiiNunit\n{\nvehicle : .car\n{\n}\n}\n'
  'vehicle_accessory : .chassis\n{\n\tdata_path: "/def/vehicle/truck/x/chassis/a.sii"\n}\n')
def mk(line):
    return {dealer: dealer_text,
            "def/vehicle/truck/x/chassis/a.sii":
              "SiiNunit\n{\naccessory_chassis : a\n{\n\t%s\n}\n}\n" % line}

# F1 -- a price that is not a bare number must not raise.
for label, line, want in (("a trailing comment", "price: 1000 # base", 1000),
                          ("a collapsed line", 'price: 1000   icon: "x"', 1000),
                          ("the hex-float form", "price: &459380cd", 4720)):
    try:
        total, rows_ = C.dealer_price(mk(line), dealer)
        check(f"F1  a price with {label} is read", total == want, str(total))
    except Exception as exc:
        check(f"F1  a price with {label} is read", False, f"{type(exc).__name__}: {exc}")

# F2 -- the requested mass is the mass that is written.
cfg = C.car_physics("suv", 3, [1150, 1050])["kerb_weight"]
check("F2  the requested mass survives the axle count",
      sum(int(v) for v in cfg) == 2200, str(cfg))

# F3 -- a rev band is ordered.
for peak, limit in ((4500, 3000), (5000, 3000), (6000, 3500)):
    band = C._rpm_range_bands(peak, limit)["rpm_range_power"]
    lo, hi = (int(v) for v in re.findall(r"\d+", band))
    check(f"F3  the band is ordered (peak {peak}, ceiling {limit})", lo < hi, band)

# F4 -- a number with a comment is still a number.
check("F4  a truck-scale value with a comment is detected",
      C._is_truck_scale("kerb_weight", ["4720.1 # front"]))
check("F4  a hex-float value with a comment is detected",
      C._is_truck_scale("kerb_weight", ["&459380cd # front"]))

# F5 -- a shift point cannot invert a band.
band = C._set_shift_up(C._rpm_range_bands(5000, None), 1000)["rpm_range_low_gear"]
lo, hi = (int(v) for v in re.findall(r"\d+", band))
check("F5  the shift point cannot invert a band", lo < hi, band)

# F6 -- the presets carry no tank size.
check("F6  the presets carry no dead tank size",
      "tank_size" not in C.car_physics("suv", 2))

# F7 -- the walkthrough asks only what is unknown. Checked directly: the
#        0-60 prompt is no longer on the guided path.
import inspect
src = inspect.getsource(C.main)
check("F7  the walkthrough no longer asks the 0-60 question",
      "prompt_acceleration" not in src.split("# ---- 7. output")[0])

# F8 -- every dangling path is repointed.
files = {
 "def/vehicle/truck/v/interior/a.sii":
   'SiiNunit\n{\naccessory_interior : a\n{\n\tgps_path: "/ui/gps.sii"\n}\n}\n'
   'accessory_interior : b\n{\n\tgps_path: "/ui/gps.sii"\n}\n}\n',
 "def/vehicle/truck/v/interior/c.sii":
   'SiiNunit\n{\naccessory_interior : c\n{\n\tgps_path: "/ui/dashboard/v_gps.sii"\n}\n}\n',
 "ui/dashboard/v_gps.sii": "SiiNunit\n{\n}\n",
}
C._fix_dangling_ui_paths(files, C.Report())
text = files["def/vehicle/truck/v/interior/a.sii"]
check("F8  every dangling path is repointed", "/ui/gps.sii" not in text, text)

# F9 -- an empty --extra-tags entry is skipped.
try:
    for extra in "suv,".split(","):
        if extra.strip():
            C.job_tag(C.normalise_type(extra))
    check("F9  an empty --extra-tags entry is skipped", True)
except SystemExit as exc:
    check("F9  an empty --extra-tags entry is skipped", False, str(exc))

# F10 -- an unquoted value with a colon is not split.
check("F10 an unquoted value with a colon is not split",
      sii.expand_multi_key_lines(['\tname: foo: bar']) == ['\tname: foo: bar'])

# F11 -- an unreadable paint job does not end the run.
with tempfile.TemporaryDirectory() as td:
    p = Path(td)/"m.scs"
    vdir = "def/vehicle/truck/testcar/"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr(vdir+"data.sii",
                   "SiiNunit\n{\naccessory_truck_data : vehicle.testcar\n{\n}\n}\n")
        z.writestr(vdir+"paint_job/bad.sii", b"\xff\xfe\x00bad bytes")
    try:
        C.discover_parts(C.ModSource(p), vdir, "testcar", "")
        check("F11 an unreadable paint job does not end the run", True)
    except SystemExit as exc:
        check("F11 an unreadable paint job does not end the run", False, str(exc)[:60])

print()
w = max(len(n) for n, _, _ in rows)
for n, good, d in rows:
    print(f"  {'FIXED' if good else 'NOT FIXED':>9}  {n:<{w}}  {d}")
bad = sum(1 for _, g, _ in rows if not g)
print(f"\n  {len(rows) - bad} of {len(rows)} checks pass.")
raise SystemExit(1 if bad else 0)
PY
```

Expected output: every check reads `FIXED`, and the script exits 0.
