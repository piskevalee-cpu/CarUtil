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
| `carutil.py` | The converter and CLI. Everything of substance lives here (~4000 lines). |
| `sii.py` | Parsing/editing helpers for SCS's `.sii` data format. |
| `hashfs.py` | Reader for SCS's own `SCS#` container (HashFS v1). See below. |
| `brands.py` | 23 brands (22 named plus the `other` catch-all) and the mapping to each logo file. |
| `logo.py` | Image → 256×64 DDS conversion. Uses ImageMagick. |
| `ui.py` | Banner, colour, arrow-key menus. Pure presentation. |
| `test_converter.py` | The suite (~4600 lines). Run it before every commit. |
| `FINDINGS.md` | Research log, §1–§33. **Early sections were wrong; see Corrections.** |
| `logos/` | 23 brand images. **10 of them are not PNGs** — 6 WebP and 4 SVG wear a `.png` extension, which is why `brands.logo_path()` probes several extensions. |

`ALIASES` in `brands.py` can name a brand `BRANDS` does not contain: `vw` maps
to `volkswagen`, which has no row, so `--brand vw` resolves to the `other`
vendor with no logo. Check both lists when adding a brand.

## Commands

```bash
python3 test_converter.py                                        # the whole suite
CARUTIL_FIXTURES=/path/to/mods python3 test_converter.py         # incl. fixture checks
python3 carutil.py                                               # guided walkthrough
python3 carutil.py --path                                        # where are my files?
python3 carutil.py mod.scs --type suv --brand cadillac -o out.scs
```

**There is no way to run a single test.** No pytest, no unittest — `main()`
calls each `def test_*()` in sequence and there is no selector flag. Each test
function is standalone, so drive one directly:

```bash
python3 -c "import test_converter as t; t.test_stored_entry_is_not_inflated()"
```

**The suite must exit 0 before you commit.** Do not trust a hardcoded check
count: most checks are emitted from loops over the 23 brands, so the number
moves with the brand list and with whether `CARUTIL_FIXTURES` is set. The
README deliberately does not quote a number either — read the exit code. A
fresh clone reports skips, not failures, when the fixtures are absent — that
is expected, not a regression.

**The suite must also be run somewhere with a TTY on stdin.** Three prompt
tests used to assume the harness's stdin was not a terminal, so from a console
the first one failed and `test_prompt_price_never_blocks` would have *hung*
waiting for an answer. They now pin `C._tty` off themselves; keep it that way
rather than relying on how the suite was invoked.

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
`test_dependency_check` monkeypatches `C._logo.imagemagick`, `shutil.which`,
`C._tty` and `C._confirm`.

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
  `build_brand_logo`'s sanitising. Note the places a count is written out by
  hand: the `>= 16` floor (in **two** tests, `test_brand_selection` and
  `test_every_brand_gets_its_own_vendor`), the `logos/` row above, and the
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

**Flags do not remove the guided prompts.** On a terminal the prompts are
mandatory: step 4 always asks for the weight and its answer replaces whatever
`--mass` was parsed to, and step 5 runs unless `--acceleration` was given
(`--power` alone switches the step-5 prompt from power to acceleration).
`--price` and `--acceleration` are the only two that suppress their own prompt.
The "skips steps 4–6" claim in `FINDINGS.md` §31, in `README.md` and in the
comment above `if guided:` is stale: `d0b8f28` changed `elif guided:` to
`if guided:` and nothing updated the prose.

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

## Reading a HashFS container

`hashfs.py` is the reader; the traps below are why it took a day to get right.
`FINDINGS.md` §33 has the measured layout.

**The entry table's offset in the header is a u64, not a u32.** Reading it as a
u32 lands 8 bytes early and shifts every record by half a field. The payload
offsets then still look plausible, still decompress, and still tile the payload
exactly — so every check passes while the hash column is actually the *next*
entry's hash. That single field width produced every wrong conclusion about the
container, including a false positive on `hash_path("")`.

**The hash is not Google's CityHash64.** HashFS uses the **cityhash-c** variant,
which differs in the short and medium paths and adds a fourth constant
(`k3 = 0xc949d7c7509e6557`). A faithful port of the Google original agrees on
the empty string — which is what the root directory hashes to, so it looks like
a hit — and matches nothing else anywhere. The test pins upstream's own vector,
`hash_path("/käsefondue.txt") == 0x77f9c4f5eca06d54`, including the UTF-8 path.
Also note `hash_path` strips a leading slash and prepends a non-zero salt as
*decimal text*, not as a mixed-in seed.

**A directory listing can name a file that is not there.** A DRM packer leaves
the name in the listing with no entry behind it. Treating a listing as proof of
existence makes `exists()` lie, and the converter then tries to inject `tags[]`
into a `data.sii` with no bytes — which is a traceback, not a diagnosis. Only
`ok` paths may be merged into `names`.

**A listing entry whose payload is corrupt must raise, not return the garbage.**
`read()` verifies the recorded size *and* CRC and raises `HashFsDamaged`; it
never hands back bytes that merely decoded. That is §32's bug in a second
container, and the same rule applies: whatever comes back is about to be written
out as a definition.

## Editing defs: every helper is line-oriented

This is the failure mode that produced a 0-gallon tank, and it will produce
another one. **Some mods put several keys on one line**, and the regexes here
cannot tell a value from the key after it:

```
residual_travel[0]: 0.08   nominal_voltage: 12   tank_size: 1200
```

Read as *one* key whose value happens to contain the others. `set_key` then
replaces the whole line and takes `nominal_voltage` and `tank_size` with it.

**Always reach lines through `sii.edit_lines(text)`, never `text.splitlines()`
or `text.split("\n")`.** `edit_lines` is `expand_multi_key_lines` and it is the
single entry point every editing and reading helper is wired to. It returns a
line carrying a single pair — or an `@include`, a comment, or anything the
scanner cannot read confidently — untouched, so a def you never edit keeps the
mod's own bytes. A new editing function that splits lines itself reintroduces
the bug silently.

Two more consequences of the same line-orientation:

- **`KEY_RE`'s name group is `[A-Za-z_][\w.]*`, which has no brackets.** So
  `set_key(lines, "fallback[]", ...)` can never match — it appends every call.
  Repeated keys take the bracket separately (`set_key(..., indexed="[]")`) or
  `append_key`, which always adds. Choosing `set_key` for a list silently
  collapses it to its last entry.
- **A matched line's trailing comment matters.** `_rescale_in_place` and
  `_set_scalar_in_place` exist so `torque: 1400 # per the tuner` survives;
  `set_key` discards the comment with the line. Numeric scalars an author
  annotated should go through the in-place setters.

SII also writes some scalars in **hex-float form** — `kerb_weight[0]:
&459380cd` is 4720.1 kg. Use `sii.to_float()`, which decodes it; plain
`float()` raises, and any caller wrapping that in `except ValueError` will
judge the value harmless and preserve a truck's worth of mass.

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
no price at all. `--price` therefore scales the mod's **whole** price table by
one factor, not just the listed components — scaling only those left the
upgrade menu at truck-scale prices, so a `$1,000` car had a `$40,000` AWD
chassis available. The author's *relative* pricing is what survives, which is
the part worth keeping; the absolute figure was a guess for a truck. The
hazard is scaling **up**, not down, because every remaining purchase then gets
more expensive at once — so warn when the target exceeds the mod's own total.
See `FINDINGS.md` §30.

**Addons bind by directory, never by filename.** First `.sii` per
`accessory/<dir>` wins. An earlier version matched `shape<N>.sii` and
special-cased `steering_w`, which silently dropped any addon named otherwise —
the GLS's `accessory/f_light_chs/led.sii` went unbound and the headlights
vanished entirely rather than dimming. `require[]` is validated against the
bound accessories and unsatisfied entries are reported as gaps at conversion
time, precisely so this class of fault is visible before the game is launched.

**`ModSource` owns two resources and must release both.** The `ZipFile` handle
and, when a `.zip` was unwrapped, the temp dir it was unpacked into. Windows
cannot delete an open file, so an unreleased handle makes every enclosing
`TemporaryDirectory` raise at teardown — and this suite runs inside 48 of them.
`/tmp` is tmpfs, so an unreleased scratch dir is RAM. Use `close()` or `with`;
`__del__` is a backstop, not the mechanism. **Do not null `self.zf` on close** —
a closed `ZipFile` raises "archive was already closed", whereas `None` would
send reads down the folder branch and silently misread the `.scs` as text.

**A mod may be split across two archives, and both halves are now read.** The
definitions live in a plain ZIP, everything else in an SCS HashFS container
(`SCS#`, the format the game uses), and `hashfs.py` reads the container. Point
the converter at the *folder* holding both: `ModSource` opens the plain ZIP and
the container beside it and treats them as one mod. `paths()` in `hashfs.py`
returns `ok` / `damaged` / `missing` per file, and only `ok` paths are merged
into the mod's name list — see the two traps under "Reading a HashFS
container", which is where the packer gets clever.

**A container can be packed so that it cannot be converted.** Some packs leave
893 of their 1408 entries (159.7 MB of a 160 MB archive) referenced by no
directory listing, which is the packer's "strip from directory structures"
option. The files are in the archive but their paths are unrecoverable, because
an entry is addressed by hash and cannot be listed. If a mod converts and the
body or most textures are missing, that is the reason and no tool can fix it;
say so rather than shipping a car with no body.

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

`test_find_mod_folders_layouts` builds a synthetic `$HOME` with 16 real layouts —
Proton/Wine/Flatpak/Snap/Lutris, `My Documents`, a prefix with no `pfx/`,
`profiles/` instead of `config.cfg`, a lowercase directory, both symlink shapes,
and both Flatpak arrangements — and asserts each is found. It also asserts the
search completes in under two seconds, which is what stops anyone reintroducing a
walk. It runs **before** the fixture gate, since it needs no real mods.

**Two different directories, and they are not related.** `find_mod_folders()`
returns ATS **user data** — inside a Proton prefix that lives under a fake
`C:` drive, never at the Steam install. `find_game_install()` returns the game
itself, under `steamapps/common`. They have separate root lists
(`_prefix_candidates` vs `_install_roots`) because conflating them prints a path
that looks like an install and has no `def.scs` in it. The install is
identified by `def.scs` **and** `base.scs` together, so a Steam library or
prefix named `American Truck Simulator` is rejected and a renamed install still
resolves. `--path` prints both; it short-circuits before the banner, the
ImageMagick preflight and the "no input given" refusal, because asking where
your mod folder is must not first demand an input or offer to install a program
you did not ask about.

**Enumerate Steam *roots*, not finished `compatdata` paths.** Steam's layout is
not one thing: `~/.steam` is normally a symlink farm whose `root` and `steam`
entries point back into `~/.local/share/Steam`, but on some builds they are real
directories, and Flatpak adds a sandbox root on top. Hand-listing
`.steam/steamapps/compatdata` covered the symlink case and silently missed the
real-directory one. `_prefix_candidates()` now lists roots
(`~/.steam`, `~/.steam/root`, `~/.steam/debian-installation`,
`~/.var/app/com.valvesoftware.Steam/…`, `~/snap/steam/…`) and appends
`steamapps/compatdata` itself, so both arrangements resolve and adding a launcher
means adding one root rather than remembering every permutation beneath it.

**The Flatpak and Snap support is reasoned from their documented layouts, not
verified against a live install** — this machine has neither. The synthetic
tests cover the shapes; `--mod-dir` covers anything they miss.

**The test fixtures are third-party assets and must never be committed.** Real
car mods live in `../testing-covnverter/` — the typo is in the real directory
name — and are not in the repo. `*.scs` is gitignored, so a mod dropped in the
working directory is safe, but do not widen that. Everything the fixtures are
needed for should *also* be expressible against `synthetic_mod()` in
`test_converter.py`, which builds a minimal but complete mod folder on disk —
prefer that, so the behaviour is covered without the assets.

**When you pick a file out of a generated archive, filter on `.sii`.** An
archive carries a `def/vehicle/car_desktop/` **directory entry** alongside the
`.sii` file, and `[n for n in namelist() if "car_desktop" in n][0]` returns the
directory. Reading that back gives `b""`, so every "is this bound?" assertion
becomes a silent **false pass**. `_desktop()` in the suite does the filtering
for you.

**`/tmp` is tmpfs on the Linux box — it is RAM, not disk.** Each conversion
writes a 100–200 MB archive. A dozen test runs filled `/tmp` to 4 GB and took
the machine down to 1.9 GB free RAM. Delete scratch output as you go. Check
with `df -h /tmp`.

**Do not trust the reference converter's self-reported passes.** Its
`job-tag ... matches car_job vehicle_types[]` line checks a hard-coded list, not
the base game. Its own summary still carries `UNVERIFIED 1`. Matching it
byte-for-byte proves nothing about in-game behaviour.

**The base game's `def.scs` / `base.scs` are readable — stop guessing.** They are
HashFS **v2**, not encryption, and `v1` is in `hashfs.py`. `dlc_rt_ford.scs` in
a local ATS install holds SCS's own Road Trip car physics and is the reference
for any drivetrain question — read it rather than inferring. FINDINGS §18 has
the v2 layout; §33 has v1, measured.

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
- **Torque targets must be derived, never assumed.** `--engine-torque 1125`
  was fitted on a `--type classic` car whose preset mass was 1550 kg when the
  real vehicle was 2069 kg, so it was 34% out before it was ever driven — and
  a single Nm/t constant then made the W223 correct and the Pilot
  uncontrollable. Ask for the target 0-60 (`--acceleration`, or step 5 of the
  walkthrough) and derive torque from the mass *read back from the written
  chassis* instead. A hand-tuned absolute figure is a guess wearing a decimal
  point.
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
  See `FINDINGS.md` §18, §19 and §23.
- **…except the top gear, which does need correcting.** This file used to say
  *"do not touch gearing — the mods' 10-speed already matches SCS's own 10R80."*
  **That was measured false** on 2 of the 3 fixtures, and it is very likely why
  the bug below survived: `differential_ratio × ratios_forward[-1]` is **1.81**
  on the Escalade and **1.70** on the W223, against **2.26–2.37** across the
  four transmissions SCS ships for its own cars. A truck's top gear is geared
  for 40 tonnes. `_retarget_final_drive` now moves the final drive so the
  product lands on the nearer bound. It is a **ratio**, so no wheel radius is
  involved, and it scales every gear by one factor. **Guard on ≥3 forward
  gears** — `ratios_forward[-1]` is only the top gear if there *is* a gearbox,
  and a one-ratio stub would otherwise be rewritten to nonsense.
- **A too-tall top gear is not "shifts early", it is "does not accelerate".**
  Reported on a Cadillac: 25 s to 60 mph on the automatic, shifting at ~1200 rpm
  with peak torque at 6000; **5.3 s in manual** with the same engine. The box
  held top gear while the engine sat at 1326 rpm — below `rpm_range_low_gear`,
  so it could not shift *out* of the gear it should have shifted *into*. The
  engine was fine and the shift logic was fine. Note that `rpm_range_*` in the
  truck mods is **already** inside SCS's car range; it was never the cause, and
  "fixing" it would have been fitting the symptom. Only some mods are affected:
  the Pilot's box measures 2.35 and converts fine, which is the control.
  **Read that "never the cause" narrowly — see the next entry.** It held on the
  three fixtures that declare the keys; a fourth declares none at all.
- **A truck dealer names the manual, and a car has no Drive without one.** The
  CTS-V Sport Wagon's `truck_dealer` names `6_manual.sii`; the mod also ships
  `6_auto.sii` with `auto_mode_name: D`. `_declared()` believed the author, so
  the converted car was bound to a box with no Drive mode at all, and neither
  engine declared `rpm_range_*` — so there was not even a band to shift on.
  `_pick_core` prefers a candidate carrying `auto_mode_name`. It is the mod's
  own signal, not a filename or a gear count (all four SCS cars are converter
  boxes and one is a 3-speed). Fires on the CTS and nowhere else among the four
  fixtures. **A correct diagnosis about one car is not a correct diagnosis in
  general** — the 25 s had *both* causes and was verified with neither fix in.
- **`rpm_range_*` is left alone even when absent, and that is deliberate.** No
  engine in the CTS declares it, so there is no author anchor to copy and any
  triple would be invented. Do not "fix" it by copying SCS's values: those are
  tied to curves belonging to SCS's own engines. Closing it needs a
  `game.log.txt` and a tacho reading, not a plausible-looking number. See
  `FINDINGS.md` §35.
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
- **The mod's own dealer unit is the source of truth for what a part is.**
  `discover_parts()` used to bind `sorted(candidates)[0]` per slot, which is a
  guess: on a mod with several chassis files it bound the -30 mm sport spring
  where the author's `truck_dealer` unit named the standard one. The dealer
  config is read first now; sorted discovery only fills slots it does not
  mention. **Never auto-bind a decorative slot** (`toy*`, `toystand`,
  `set_dashbrd`, `interior_decor*`): binding one makes it part of the spawned
  car, and ATS has no models for that namespace, so the game dies with
  `Can not open '/vehicle/truck/upgrade/interior_decors/...pmd'`. They stay in
  the archive; a mod that wants one lists it in its own dealer unit.
- **A configured key is not a written key.** `tank_size` had a value in every
  preset *and* a branch in `_is_truck_scale`, and no loop ever wrote it — a
  converted car kept its 240–1200 L truck tank for the life of the project.
  When you add a preset key, add it to one of the two loops in `tune_chassis`
  and add a test that reads it back out of a converted chassis.
- **…and a half-fix is worse than no fix.** `tank_size` was then wired in, and
  that made fuel range *worse*: a mod's 291 L tank became 79 L while its fuel
  consumption — which nothing here touched — stayed at the truck's rate. A
  smaller tank at a truck's consumption is a strictly shorter range. Capacity
  and consumption have to move together, or not at all. A threshold branch with
  no writer is dead code that reads like a working rule, so remove the branch
  rather than leave it dormant.
- **Fuel: drop the truck's consumption model, leave the tank.** Recovering the
  **657 definition units** SCS ships for its own four Road Trip cars from
  `dlc_rt_ford.scs` shows `consumption_coef` appears **zero times**; the only
  fuel key present is `adblue_consumption: 0.0`. Every truck mod carries one
  anyway — `0.42` (Pilot, W223), `0.52`/`0.70` (Escalade), `1` (BMW 135i) —
  and those are truck burn rates. So `consumption_coef`, `adblue_consumption`
  and `no_adblue_power_limit` are dropped on car conversion, **warned not
  merely reported**, because they change how the car drives in the world.
  Removing beats retuning: there is no measured car value to substitute, and
  removing puts the car on the same footing as the Mustang. **It still needs an
  in-game range check** — moving toward the reference is not the same as
  calibrated. `tank_size` stays the mod's own; SCS's four cars measure 61–98 L
  (Mustang 61, Victoria 72, Bronco 79, F-150 98), so the presets at 47–80 are
  themselves extrapolations and should be corrected before anyone rewrites the
  tank.
- **HashFS content extraction works, and is how the 657 units were read.** No
  reader, no CityHash: scan the archive for zlib streams, decompress, and match
  on content (`SiiNunit`, or any distinctive key). Enough to answer questions
  about SCS's own cars. It does *not* recover file paths — those are stored as
  64-bit hashes — so it cannot build an archive.

- **`resistance_torque` is a fraction of `torque`, not a fixed number.** It has
  to be rescaled by the same factor whenever torque is, or a 476 Nm engine
  inherits a 9.4 t truck's 320 Nm of drag — 67% of peak, which is why lifting
  off at 50 mph dropped the car to 30. `-1` means "no drag" and must survive
  as written.
- **`secondary_torque*` is a truck mechanism and is dropped on conversion.**
  All 181 SCS engine units leave it alone and no production car has one;
  keeping it proportional handed a converted Pilot 1.7x torque in every gear
  at or below `secondary_torque_gear_ratio`. Do not "preserve the author's
  ratio" — that was the bug.
- **The last dot-separated token is not always a vehicle name.** The
  registration slug was `unit.split(".")[-1]`, which turned `vehicle.gle.450`
  into `450` and `vehicle.prado.20` into `20` — files two mods would both
  want. A purely numeric token now falls back to the whole path. Named
  vehicles keep the slugs they always had (`w223_s500`, `parati1994`), and
  other files reference those names, so do not "simplify" that rule.
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

## Talking to GitHub

**Never comment on, close, reopen, label or edit an issue without the user's
explicit consent for that specific action.** Draft the reply, show it, and wait.
This is not a "be careful" rule — a posted comment is public, permanent and
attributed to the maintainer's account, and a wrong technical claim in it is
the maintainer's reputation to defend. Reading issues with `gh issue view` is
fine and expected; writing to them is not, without being asked.

Same applies to opening a PR: prepare the title and body, then ask.

## Before committing

```bash
python3 test_converter.py          # must exit 0
git status --short                 # only intended files
git diff --cached --stat           # sanity-check the size
```

Push to `main`. Pushes occasionally fail once with a bare "remote rejected" and
succeed on an unchanged retry; that is transient, not a repo problem.