# AGENTS.md

Guidance for AI agents working in this repository. Read this before editing
anything; most of it is knowledge that cost real debugging time to acquire.

## What this is

A converter that turns a truck-classified American Truck Simulator mod into a
Road Trip car so it appears in the Car Dealer and drives with car physics. One
Python 3.9+ entry point, standard library only. ImageMagick is optional and is
used only to convert brand logo images.

| File | Role |
|---|---|
| `convert_to_car.py` | The converter and CLI. Everything of substance lives here (~3200 lines). |
| `sii.py` | Parsing/editing helpers for SCS's `.sii` data format. |
| `brands.py` | The 22-brand list and the mapping to each logo file. |
| `logo.py` | Image → 256×64 DDS conversion. Uses ImageMagick. |
| `ui.py` | Banner, colour, arrow-key menus. Pure presentation. |
| `test_converter.py` | The suite (~3100 lines). Run it before every commit. |
| `FINDINGS.md` | Research log, §1–§32. **Early sections were wrong; see Corrections.** |
| `logos/` | 23 brand images. **11 of them are not PNGs** — 7 WebP and 4 SVG wear a `.png` extension, which is why `brands.logo_path()` probes several extensions. |

## Commands

```bash
python3 test_converter.py                                        # the whole suite
CARUTIL_FIXTURES=/path/to/mods python3 test_converter.py         # incl. fixture checks
python3 convert_to_car.py                                       # guided walkthrough
python3 convert_to_car.py mod.scs --type suv --brand cadillac -o out.scs
```

**There is no way to run a single test.** No pytest, no unittest — `main()`
calls each `def test_*()` in sequence and there is no selector flag. Each test
function is standalone, so drive one directly:

```bash
python3 -c "import test_converter as t; t.test_stored_entry_is_not_inflated()"
```

**The suite must exit 0 before you commit.** Do not trust a hardcoded check
count: most checks are emitted from loops over the 23 brands, so the number
moves with the brand list and with whether `CARUTIL_FIXTURES` is set. `README.md`
claims 515; the real number is whatever the run prints. Read the exit code.
A fresh clone reports skips, not failures, when the fixtures are absent — that
is expected, not a regression.

**`python3` and `python` may be different interpreters** (on the Windows dev
box, 3.14 and 3.11). Pick one and use it for both the tests and the CLI.

**A half-populated `CARUTIL_FIXTURES` now skips instead of aborting.** A
fixture directory missing one mod (e.g. no `porsche-cayenne/`) previously killed
the whole run partway through, because `main()` signals a missing input with
`SystemExit`, which `@guard` did not catch. It now skips, so an incomplete
fixture directory is safe but silently reduces coverage — check the skip list.

## The suite asserts on source text, not only behaviour

`test_windows_compatibility` is the trap. It greps the *source text* of the
shipped modules for literal substrings (`'os.pathsep' in csrc`,
`'read_text(encoding="utf-8"' in csrc`, `"shell=True" not in text`) and
`ast`-parses `ui.py` to prove `termios`/`tty` are not imported at module level.
`test_dependency_check` monkeypatches `shutil.which` and `C._tty`.

A refactor that is behaviour-preserving can still fail these. Reformatting a
call, renaming a variable, hoisting an import, or re-quoting a string literal
is enough. If a check fails on wording rather than on behaviour, that is
usually the check being brittle — but confirm the behaviour is genuinely intact
before you touch it.

## Adding a brand or a preset is a multi-file change

- A brand in `brands.py` **without** an image in `logos/` fails
  `test_every_brand_gets_its_own_vendor`. No logo → `dealer_folder()` returns
  `other` → the shared vendor → the exact logo collision that per-brand vendors
  exist to prevent. The same test also requires the vendor stem to survive
  `build_brand_logo`'s sanitising. Note the three places a count is written out
  by hand: this test's `>= 16` floor, the `logos/` row in `AGENTS.md`, and the
  "All N brands" line in `README.md`. They are deliberately not asserted
  against each other, so adding a brand means updating them all.
- Editing `_MASS` or `TYPE_HELP` fails `test_readme_category_table_matches_code`,
  which requires the README row `` | `type` | help | N kg | `` to match exactly.
- The banner is asserted against `README.md`, as are the spellings `veteran`
  and `sport`. Update the README in the same commit.

## Dependencies

**ImageMagick is the only external program**, used for exactly one thing:
turning a shipped brand logo image into the 256×64 DDS the game reads.
Everything else is the standard library. `main()` runs `check_dependencies()`
before the walkthrough, which reports a missing ImageMagick and offers the
package-manager command for the platform. Two rules hold it together:

- **Declining the install must never abort the conversion.** A missing
  ImageMagick downgrades the *logo* only — the mod falls back to its own
  texture or the flat placeholder and the car is still written.
- **The preflight must never block when stdin is not a terminal**, or scripted
  runs and CI hang. It prints the command and returns instead.

An explicitly passed `--brand-logo` is the exception: that image was asked for
by name, so failing loudly is correct. A missing ImageMagick is a **skip**, not
a failure, so a green run without it is still a valid green run.

## Reading a mod archive: trust the central directory, not the local header

This is the single most destructive bug class in the repo, and it produced a
game crash. Read this before touching `ModSource`.

**The local file header's method byte is meaningless in these archives.** It is
`1` for *every* entry — measured 509/509 in the Escalade, 463/463 in the W223,
326/326 in the Pilot — whether the payload is genuinely deflated or genuinely
stored. `ModSource._central_method()` therefore takes the method from the
central directory's `compress_type`; that is the only field that matches the
bytes.

**Never "try to inflate and fall back on exception" for a stored payload.**
Raw deflate permits a stored-block header inside arbitrary bytes, so
`zlib.decompress` on ASCII *succeeds*, returns a short garbage slice and raises
nothing. The fallback therefore cannot fire. `ModSource._read_local()` returns
stored bytes outright when the central directory says stored.

**Never decode a def with `errors="replace"`.** That turns each bad byte into
U+FFFD, so a misread comes back looking like text and gets written out as a
definition. Use `ModSource.read_def()`, which refuses and names the file.

That combination is what killed the Escalade. A 61-byte camera definition was
"inflated" to 10 junk bytes, laundered into valid UTF-8, written back as a def,
and the game rejected it with *does not contain serialised units - missing magic
mark*. Every unit it defined became a dangling pointer in `data.sii`, and
`load_unit()` crashed the game the moment a save spawned the vehicle — hours
later, with nothing pointing back at the converter.

**When reading the game's own or a mod's files, prefer measuring to assuming.**
Two facts that were once guesses and are now measured: the local method byte is
constant, and the central directory's `compress_type` is correct for 100% of
entries across all three reference mods.

## Cross-platform traps

These cost real debugging time, and they are Windows-only failures that are
silent on Linux — which is exactly why they survive review.

**`shutil.which("convert")` is not ImageMagick on Windows.** It returns
`C:\Windows\System32\convert.exe`, the FAT→NTFS filesystem converter. Handing
that binary ImageMagick arguments exits 4 with a *localised* "invalid
parameter" message, so every logo fails and the error names neither program nor
cause. `logo.imagemagick()` therefore **verifies** the candidate by running
`<exe> -version` and looking for `ImageMagick` in the banner. Do not revert
that to a bare `which()`. It prefers `magick` (ImageMagick 7) over `convert`
(ImageMagick 6), which is what keeps Linux correct.

**`termios`/`tty` are POSIX-only.** They must stay *inside* the `try` in
`ui._raw_keys_available()`. An import above the guard raises
`ModuleNotFoundError` on Windows and kills the guided walkthrough at its very
first prompt, before the numbered-menu fallback can engage.

**A Windows ImageMagick install is invisible to the running process.** The
installer updates the machine PATH, but the process keeps its old copy, so
`magick` stays "not found" for the rest of the run. `_refresh_magick_path()`
prepends the install directory to `os.environ["PATH"]` to avoid needing a
re-login. If you add a second dependency, it needs the same treatment.

**CRLF is a non-issue, and that is worth knowing.** Windows translates `\n`
only in *text mode*. Def units are handed to `zipfile` as `str` and encoded to
UTF-8 internally, so generated files stay LF-only on every platform. Defs that
*do* contain CRLF are the mod author's own, carried through verbatim. Do not
"fix" this by writing defs in binary.

## Prompts

The guided walkthrough is **7 steps** (`total = 7` in `main()`): source, type,
brand, weight, power *or* acceleration, price, convert.

**Every prompt demands a real value.** Empty input is an omission, not a
request for the default: it re-asks with the same message used for a bad value.
There are deliberately **no bracketed suggestions** in the prompt labels and
no "Press Enter for N" lines — the user asked for bare fields, and a visible
default invites the answer the walkthrough is trying to avoid. The explanatory
paragraph above each prompt still states the sensible range, so guidance is
preserved without being a pre-filled answer.

**Cancelling is still an escape.** `Ctrl-C`/`Ctrl-D` return `None` and fall
back to the default. Without that, a mandatory field would be unescapable.
Scripted mode, `--no-prompt` and CI are unaffected: the prompts only run on a
TTY.

## Non-obvious constraints

**A converted car cannot have a realistic engine *and* SCS's drag.** SCS ships
`air_resistance` 0.37-0.596; a 2200 kg car with a sane engine runs past 150 mph
at those values. `--air-resistance` exists because of this.

> **The code does not ship `2.4`.** `FINDINGS.md` §29 says it does, and that
> section is history, not current state. `_TUNING` uses SCS's measured
> 0.37-0.596 and `--air-resistance` defaults to `None` (no override at all);
> `test_car_physics_within_scs_ranges` enforces the range. Do not "correct"
> either value without a measurement.

**The engine curve cannot control the mid-range.** Top gear is `0.63 x 3.73 =
2.35:1`, so the engine is in the same 1500-4000 rpm band at 120 mph that it
uses for 0-60. Any attempt to slow 60-120 with `--peak-power-rpm` will fail and
will look like a bug in the curve shape. Only drag separates the two phases.

**Car price is not a key in ATS.** The dealer sums the `price:` of every
accessory its `data_path` lists; SCS's own four Road Trip dealer units declare
no price at all. Anything that re-prices a car must touch the listed
components, and must leave the mod's wider upgrade menu alone or it will inflate
everything the player can still buy afterwards. See `FINDINGS.md` §30.

**Addons bind by directory, never by filename.** First `.sii` per
`accessory/<dir>` wins. An earlier version matched `shape<N>.sii` and
special-cased `steering_w`, which silently dropped any addon named otherwise —
the GLS's `accessory/f_light_chs/led.sii` went unbound and the headlights
vanished entirely rather than dimming. `require[]` is validated against the
bound accessories and unsatisfied entries are reported as gaps at conversion
time, precisely so this class of fault is visible before the game is launched.

**Never make `find_mod_folders()` a tree walk.** It resolves the mod folder by
*constructing* the known paths (`<appid>/pfx/drive_c/users/<user>/Documents/…`)
rather than searching. An `os.walk(followlinks=True)` attempt took **over ten
minutes**: a Proton prefix holds ~1358 symlinks, mostly into the Proton runtime
tree. And `Path.glob("**/…")` — the original — is the opposite failure: it does
not descend into symlinked directories at all, so a linked prefix or Steam
library was invisible while `base.exists()` still reported `True`. The finder
silently found nothing while reporting no error. `pathlib` resolves symlinks on
its own, so constructing paths finds them without traversing anything.

Every path component is matched case-insensitively. ext4 is case-sensitive by
default, so an exact-case probe finds the directory on NTFS and on a
case-insensitive btrfs but misses it on a stock Linux Mint install.

`test_find_mod_folders_layouts` builds a synthetic `$HOME` with 14 real layouts —
Proton/Wine/Flatpak/Snap/Lutris, `My Documents`, a prefix with no `pfx/`,
`profiles/` instead of `config.cfg`, a lowercase directory, and both symlink
shapes — and asserts each is found. It also asserts the search completes in under
two seconds, which is what stops anyone reintroducing a walk. It runs **before**
the fixture gate, since it needs no real mods.

**The test fixtures are third-party assets and must never be committed.** Real
car mods live in `../testing-covnverter/` — the typo is in the real directory
name — and are not in the repo. `*.scs` is gitignored, so a mod dropped in the
working directory is safe, but do not widen that.

**`/tmp` is tmpfs on the Linux box — it is RAM, not disk.** Each conversion
writes a 100–200 MB archive. A dozen test runs filled `/tmp` to 4 GB and took
the machine down to 1.9 GB free RAM. Delete scratch output as you go. Check
with `df -h /tmp`.

**Do not trust the reference converter's self-reported passes.** Its
`job-tag ... matches car_job vehicle_types[]` line checks a hard-coded list, not
the base game. Its own summary still carries `UNVERIFIED 1`. Matching it
byte-for-byte proves nothing about in-game behaviour.

**The base game's `def.scs` / `base.scs` are readable — stop guessing.** They are
HashFS **v2**, not encryption. It used to be recorded here as opaque, and that
falsehood cost several "UNVERIFIED, needs an in-game test" conclusions. A 49-byte
packed header (`SCS#`, u16 version, u16 salt, `CITY`, two table counts and
lengths, two u64 table offsets), then two **zlib** tables: entries are 16 bytes
(u64 CityHash of the path, u32 metadata index, u16 metadata count, u16 flags);
the metadata table is a typed stream where `128`/`129` are plain file/directory
records holding a 3-byte LE compressed size, a size, and an **offset block of
16**. Data is raw deflate behind a **2-byte prefix**. Roughly thirty lines of
Python reads an entire DLC. `dlc_rt_ford.scs` in a local ATS install holds SCS's
own Road Trip car physics and is the reference for any drivetrain question.
See `FINDINGS.md` §18.

## Corrections to prior conclusions

This project has been wrong repeatedly in ways that cost hours. Don't repeat
them, and don't treat `FINDINGS.md` §1–§15 as authoritative.

- **`car_job` / `tags[]` is not about job visibility.** Quick Jobs *lend you a
  vehicle* by design — the company supplies the car. Your own car belongs in the
  **Dispatch Market**, once owned, where the 149 `car_job` units apply and do
  match on `tags[]`. Not appearing in Quick Jobs is correct, not a bug.
  See `FINDINGS.md` §17.
- **`tags[]` must use the game's own vocabulary.** All 149 `car_job` units match
  on exactly seven values: `pickup, suv, sedan, muscle, veteran, sport,
  offroad`. Our presets are named `sports` and `classic`, which SCS has never
  used — a tag the game does not recognise matches **no job at all**, so those
  two types could take zero car jobs. Two tables, in opposite directions:
  `TYPE_ALIASES` normalises user input (`sport`→`sports`), and `TAG_FOR_TYPE`
  maps outward (`sports`→`sport`, `classic`→`veteran`). See `FINDINGS.md` §26.
- **Only `muscle`, `sport` and `veteran` are restricted.** 95 of the 149 jobs
  list only `offroad, pickup, sedan, suv`, so those three are excluded from the
  heavy-cargo work.
- **Never express a physics target as an absolute number.** `--engine-torque
  1125` was fitted on a `--type classic` car whose preset mass was 1550 kg when
  the real vehicle was 2069 kg, so it was 34% out before it was ever driven.
  Target per tonne of the mass *read back from the written chassis* instead
  (`--engine-nm-per-tonne`). A hand-tuned absolute figure is a guess wearing a
  decimal point.
- **A single Nm/t constant cannot fit two cars.** 760 Nm/t made the W223 correct
  and the Pilot uncontrollable. Ask for the target 0-60 (`--acceleration`, or
  step 5 of the walkthrough) and derive torque from mass *and* time.
- **An engine needs its horsepower, not its torque.** Scaling the `torque`
  scalar cannot bound top speed: the mods' curves are flat and never taper, so
  power keeps climbing to the rev limiter. `tune_engine` rebuilds the curve on
  SCS's shape against `--power` and `--peak-power-rpm`. SCS's cars are 202-398 hp
  at 4500-6000 rpm. See `FINDINGS.md` §28.
- **`stall_torque_ratio` is the most violent number in the drivetrain.** Launch
  force is `torque x first_gear_total x stall_torque_ratio`. SCS's cars use
  1.8-2.0; the Pilot mod declares 4.50, a diesel figure that multiplied its
  launch force 2.30x. No torque target can compensate. See `FINDINGS.md` §23.
- **Every SCS engine curve starts at `(0, 0)`; the mods' do not.** All 181
  base-game engines do. The truck mods start theirs *at* idle with 0.43-0.47 of
  peak, so after the torque rescale they carry ~800 Nm at idle and walk off the
  line when Drive is selected. `tune_engine` supplies the missing origin; the
  peak and shift points are untouched. See `FINDINGS.md` §25.
- **When a value can be read off the reference, do not infer it.**
  `torque_converter_crawl` was removed on the sound-looking inference that a key
  called "crawl" causes crawling — when all four SCS transmissions declare it. A
  name is not a mechanism. See `FINDINGS.md` §25.
- **Real-world torque is the wrong target for SCS's simulation.** SCS's own Road
  Trip cars all sit at 250-300 Nm/t and still take ~2.5x their real-world 0-60.
  Matching the mod's stated output therefore produces a gutless car.
  See `FINDINGS.md` §21.
- **Chassis tuning is not what makes a car drive like a car.** The drivetrain
  and the tyres are. Truck engines declare `torque: 1400`/`4000` against their
  own `info[]` of `475 @@nm@@`/`520 @@nm@@`. A car wheel with no `grip_factor`
  runs on truck tyre defaults and will not pull even once the engine is fixed.
  **Do not touch gearing** — the mods' 10-speed already matches SCS's own 10R80.
  See `FINDINGS.md` §18, §19 and §23.
- **`residual_travel` is the body-roll key.** Truck mods ship 0.09–0.10 and
  SCS's cars use 0.093–0.134 — the ranges **overlap**, so no threshold separates
  them and the key must be written unconditionally. See `FINDINGS.md` §24.
- **Never extrapolate a tuning table from one reference vehicle.** The whole of
  `_TUNING` was scaled from the VW Parati for its entire life and every value in
  it sat outside what SCS ships. Anchor each preset to the nearest real SCS car,
  and keep the measured ranges in a test.
- **SCS's conventions, measured across the base game.** All 181 engine units peak
  `torque_curve` at exactly 1.00, set `torque` equal to the figure their own
  `info[]` states, and never use `secondary_torque`. All 18 car wheel units
  declare exactly `roll_resistance` (11-13) and `grip_factor` (1.1-1.2). When a
  value looks wrong, check what SCS itself ships before inventing a replacement.
- **The Car Dealer vendor is the manufacturer, not the car.** SCS's own Road Trip
  DLC files `mustang_67`, `f150_23`, `bronco_24` and `victoria_06` under one
  `car_dealer/<folder>/`. Hardcoding a single `"mod"` vendor made every converted
  car share one logo file, so the last mod loaded owned it.
  `dealer_folder(brand)` names it per brand. See `FINDINGS.md` §27.
- **Game file layout is not textbook.** The logo DDS reads its pixel-format block
  at **offset 76**, not 32; `dwCaps` is `0x00401008`. The `.tobj` stores its
  length as u32 at offset 40, has **four padding bytes at 44–47**, and the string
  starts at **48**, beginning with a **leading slash**. Getting any of these
  wrong yields `Failed reading map name from file ...`.
- **Use DXT5 for logo textures, not DXT1.** DXT1 keeps one alpha bit per 4×4
  block, which makes a logo's antialiased edges visibly blocky. The game's own
  logos are DXT1 only because they are opaque; ours are transparent cut-outs.
- **`brake_rotor_diameter` is a car-only key.** All 4 SCS Road Trip chassis
  declare it; none of the 39 truck chassis do. Omitting it leaves the car on the
  global truck default, which is what over-brakes. When a key is declared by
  every car and no truck, it belongs in the car preset. See `FINDINGS.md` §20.
- **Logo sources need normalising before conversion.** Three cases, handled in
  `logo.py`: opaque artwork (knock the painted background out to alpha),
  black-on-transparent (invert the colour channels, then plate), and
  white-on-transparent (plate only). **Order matters** — inverting after
  compositing inverts the plate and yields a light box. The darkness test must
  run on the *knocked-out* image; measuring the original sees the white page and
  concludes a black logo is bright.
- **`reference/tool_output/` and `*.scs` are gitignored.** Do not re-add them.

## Conventions

- **Docstrings explain *why*, not *what*.** Comments that restate the code get
  deleted. The comments that survive explain an SCS quirk or a rejected
  alternative — including the alternatives that were *tried and were wrong*.
- **New behaviour gets a test in the same commit.** `test_converter.py` covers
  byte-level formats, so regressions in DDS headers, `.tobj` offsets or ZIP
  conventions are caught immediately. A fix for a crash needs a test that would
  have failed before it.
- **Prefer measuring to assuming.** Several bugs here were found by dumping the
  game's own files and comparing, not by reasoning about what "should" be right.
  When format details matter, read a working file.
- **The README quotes the banner;** a test asserts they match. If you change the
  art, update both.
- `ui.py` output must stay clean when piped: no ANSI when stdout is not a tty,
  and `NO_COLOR` must beat `FORCE_COLOR`.

## Before committing

```bash
python3 test_converter.py          # must exit 0
git status --short                 # only intended files
git diff --cached --stat           # sanity-check the size
```

Push to `main`. Pushes occasionally fail once with a bare "remote rejected" and
succeed on an unchanged retry; that is transient, not a repo problem.