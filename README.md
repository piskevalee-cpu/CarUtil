# CarUtil — ATS Road Trip car converter

Converts a truck-classified American Truck Simulator mod into a **Road Trip**
vehicle, so it registers in the Car Dealer and drives with car physics.

Python 3.9+, standard library only — no dependencies. (ImageMagick is optional
and only used to convert brand logo images.)

## Usage

Run the script with `python3`. There is nothing to install, no `chmod +x`, and
no `carutil` command on your PATH — it is a script you execute directly.

**Interactive** — start it with no arguments and it asks you everything:

```bash
python3 convert_to_car.py
```

```
   ___         _   _   _   _   ___
  / __\__ _  | | | | | |_(_) / __| __ _ _ __ _ __
 | (__/ _` | | | | |  _| | \__ \/ _` | '__| '__|
  \___|(_| | |_| | |_| | |___/ (_| | |  | |
  |_/   \__, |\__,_|\__|_|    |_|___|_|  |_|
         __/ |
        |___/   CarUtil -- ATS Road Trip car converter

  [1/5] Find your mod
  [2/5] Choose the body type
      > Sedan    saloon / hatchback
        SUV      crossover
        Pickup   truck bed
        ...
    arrow keys to move, Enter to choose
```

**Scripted** — pass everything as flags, nothing is prompted:

```bash
python3 convert_to_car.py "../mods/2021 Porsche Cayenne Coupe Def.scs" \
    --type suv --brand porsche --slug porsche_cayenne \
    -o out_porsche_roadtrip.scs
```

Both modes produce the same `.scs`; the interactive one just asks first.

### Terminal output

Colour and the ASCII banner are automatic and never get in the way:

- **Colour switches itself off** when stdout is not a terminal, so piping to a
  file or a log gives clean text.
- **`NO_COLOR=1`** disables it anywhere; **`FORCE_COLOR=1`** enables it anywhere.
- **Arrow-key menus** need a real terminal. Without one — piped input, CI, or a
  dumb `$TERM` — they fall back to typing a number, so scripted runs are never
  blocked.
- **`TERM=dumb`** or an unknown terminal also falls back to the numbered prompt.

You can always type a name instead of using the arrows (e.g. `sedan`,
`porsche`), so the menus are never a dead end.

### Options

| Flag | Meaning |
|---|---|
| `--type` | Vehicle type written as `tags[]` in `data.sii`. Required. |
| `--extra-tags` | Comma-separated extra types, e.g. `pickup,sedan`. Adds further `tags[]` entries so the car can haul more cargo. |
| `--brand-logo` | Logo image (PNG/WebP/SVG) for this car. Overrides the bundled `logos/` and the mod's own. |
| `--brand-logo-dir` | Directory to look for `<brand>.png` in, instead of the bundled `logos/`. |
| `--brand` | Brand name, used for the dealer folder and `info[]`. |
| `--slug` | Vehicle identifier. Defaults to the input filename stem. |
| `--asset-root` | Explicit model root. Default: `/vehicle/car/<slug>`. |
| `--mass` | Kerb weight: `TOTAL` kg (`--mass 1650`) or `FRONT,REAR` (`--mass 1150,1050`). Skips the prompt. |
| `--no-prompt` | Never prompt, even on a terminal. |
| `--password` | Password for a protected mod archive. |
| `--no-car-tune` | Leave chassis physics alone (still writes `tags[]` + dealer bindings). |
| `--force-physics` | Overwrite chassis physics even when the mod already has car-sane values. |
| `--keep-physics` | Keep the global truck `physics.sii`. **Off by default** — see below. |

### Vehicle types and cargo

Road Trip has two separate job markets, and confusing them is the most common
reason a converted car looks "missing":

- **Quick Job** — the company *lends you* a vehicle. Your own car is never
  listed there, by design.
- **Dispatch Market** — you drive a vehicle from your own garage. Your
  converted car appears here, once you own it.

So a car that is absent from Quick Jobs but works for jobs is working
correctly. `tags[]` does not decide whether a car shows up at all; it decides
which **cargo** the car qualifies to haul, and only `muscle`, `classic` and
`sports` are restricted from certain cargoes.

The seven presets are `pickup`, `suv`, `sedan`, `muscle`, `classic`, `sports`,
`offroad`. `--type` is not restricted to them: alternative spellings are
normalised (`off-road`→`offroad`, `crossover`→`suv`, `wagon`→`sedan`,
`coupe`→`sports`) and anything unrecognised is written through as given.

`--extra-tags` adds further types so a car qualifies for more cargo:

```
python3 convert_to_car.py mod.scs --type suv --extra-tags pickup,sedan --brand honda
```

### Brand logos

`--brand` selects the name the Car Dealer files the car under, and the logo it
shows. Omit it and the guided prompt lists the brands in `brands.py`.

Logos live in `logos/` and may be PNG, WebP or SVG — the game never reads those
directly. Each is letterboxed onto the 256×64 canvas and converted to the DDS
the game wants (`logo.py`, which uses ImageMagick). White-on-transparent
artwork is composited onto a dark plate so it stays visible.

Textures are written as **DXT5**, not DXT1. DXT1 keeps a single alpha bit per
4×4 block, which turns a logo's antialiased edges into visible blocky steps;
DXT5 keeps 8-bit alpha and renders far smoother for 22 KB instead of 11 KB.

Sources vary in how they're drawn, so each is normalised for the dealer's dark
tile:

- **opaque with a painted (usually white) background** — knocked out to alpha,
  so no bright rectangle appears in the tile;
- **black artwork on transparency** — colour-inverted so the mark reads as
  light artwork instead of vanishing into the background;
- everything else is letterboxed onto the 256×64 canvas as-is.

The test suite asserts every shipped logo is measurably visible against the
tile colour, so a source that would come out invisible fails the build.

The logo is written to both `material/ui/car_brand_logo/<dealer>.{dds,mat,tobj}`
— the directory Car Mode reads — and `material/ui/truck_brand_logo/<brand>.*`.
Missing a logo is not an error: a flat placeholder is used instead.

Adding a brand is two steps: drop its image in `logos/`, and add a row to
`BRANDS` in `brands.py` mapping the name the user picks to that file. A brand
with no image falls back to the flat placeholder rather than failing.
`--brand-logo` uses a specific file, `--brand-logo-dir` a directory of your own.

All 22 brands currently have artwork. Note the files are not all actually PNGs
despite the extension — some are WebP and some SVG — which is why conversion
goes through ImageMagick rather than a PNG decoder.

| `--dry-run` | Report what would change; write nothing. |

The tool prints three groups at the end: **changes**, **parts bound**, and
**gaps** — parts the source mod does not contain and that you must add by hand.

## Tests

```bash
python3 test_converter.py
```

515 checks. The important one is the **reference fidelity check**: it converts the
Volkswagen Parati (a mod already known to work in Car Mode) and asserts the
generated `car_dealer` binding covers every accessory the original binds, and
that every `data_path` it emits resolves to a real file.

Those checks drive real mod files. They are third-party assets and are not
redistributed with this repository, so on a fresh clone the fixture-backed
checks are reported as **skipped** and the rest still run. Point the suite at a
directory containing them to run everything:

```bash
CARUTIL_FIXTURES=/path/to/mods python3 test_converter.py
```

It expects `Volkswagen_Parati_1994.scs`,
`Mercedes-Benz W223 S-Class V2.3 1.61.scs`,
`Honda Pilot YG2 2025 V1.6 1.61.scs`, `porsche-cayenne/`, `release/output/`
and a password-protected archive.

## Physics — making it drive like a car

This is two separate things, and you need **both**.

**1. Drop the global truck physics** (default). `def/vehicle/physics.sii` is
stock *truck* physics — `cabin_mass: 700`, `fifth_wheel_*`, `trailer_joint_*`,
`spreader_joint_*`. The Parati ships with **no `physics.sii` at all**. Use
`--keep-physics` to keep it.

**2. Write car physics into the chassis.** This is the part that actually
creates the car feel, and it is what the third-party tool's README never
mentions. The Parati carries its physics inline in the chassis unit:

```
air_resistance: 0.1        cog_height: 0.4
sway_bar_stiffness: 0.38   max_steering_angle: 28
kerb_weight[]: 620 / 415   disc_brake[]: true / false
brake_rotor_diameter[]:    suspension_damping_factor[]
residual_travel[]:         axle_suspension_type[]
suspension_class: standard nominal_voltage: 12.0  air_tank_pressure: 0.0
```

A mod without these falls back to engine defaults and still drives like a truck.
The Porsche had **none** of them.

### Setting the weight

Run the tool on a terminal and it asks:

```
  How much do you want this car to weigh?
    Enter the total in kg (a typical suv is about 2200 kg),
    or 'f,r' to set front and rear axles separately,
    or press Enter to accept the default.

  Weight [2200 kg]: 1650
```

- Enter a **total** and it is split across the axles using that vehicle type's
  natural weight distribution — so a pickup stays front-biased and a mid-engined
  sports car keeps its balance.
- Enter **`f,r`** to set each axle yourself.
- **Enter** alone accepts the type default.
- Trailing `kg` / `k` / `t` is ignored, so `2100kg` and `2.1t` both work.
- Rejected with a re-prompt: non-numbers, out-of-range totals (400–6000 kg),
  and non-positive axle weights. `q` cancels.

The prompt is **skipped automatically when stdin is not a terminal**, so piped
and scripted runs never block. `--no-prompt` forces it off; `--mass` skips it.

Whatever you pick is echoed back in the summary:

```
  - kerb weight set by you: 1150 kg front + 1050 kg rear = 2200 kg total
```

### Preserved, not overwritten

Existing values are **preserved** unless they are provably truck-scale:

| Key | Replaced when |
|---|---|
| `air_resistance` | >= 1.0 (truck default is 3.0, cars ~0.1) |
| `kerb_weight` | total > 4000 kg |
| `nominal_voltage` | >= 24 (trucks are 24V, cars 12V) |
| `cog_height` | > 0.85 |
| `residual_travel` | any element < 0.12 (stiff truck value) |
| `tank_size` | > 200 |
| `sway_bar_stiffness` | > 2.0 |
| `max_steering_angle` | > 45 |

Converting the Parati leaves its chassis **byte-identical**. Use
`--force-physics` to overwrite everything regardless.

Truck-only trailer/cab keys (`fifth_wheel_*`, `trailer_joint_*`,
`spreader_joint_*`, `cabin_*`) are removed — a car has no way to attach them.

### The phantom-axle trap

In SII, `key[N]: v` means "element N equals v". There is **no array-size
line**. Writing `kerb_weight[2]: 2` invents a third axle weighing 2 kg, and
feeds the number `2` into `disc_brake[]` (a bool) and `axle_suspension_type[]`
(a name) — which can crash the game. The third-party tool shipped exactly this
bug and needed a separate hotfix to remove five lines.

This converter always emits **exactly** `axles` elements, where `axles` is
counted from the source. There is a test for it.

## Making it appear in the Car Dealer

Registration is three things together — `tags[]` alone is not enough:

1. **`data.sii`** — the `accessory_truck_data` root unit, with the vehicle type.
   Cars still use `accessory_truck_data`; there is no separate car unit type.
2. **`tags[]: "<type>"`** — the vehicle type. Decides which courier cargoes the
   car can haul. `pickup`, `suv`, `sedan`, `muscle`, `classic`, `sports`, `offroad`.
3. **Four binding files** — `car_dealer/`, `car_desktop/`, `car_company/`, and
   `truck_dealer/`. Each is a `vehicle: .car` unit wiring `vehicle_accessory`,
   `vehicle_wheel_accessory` (with `offset:` per axle),
   `vehicle_addon_accessory` and `vehicle_paint_job_accessory` to the mod's own
   parts. In the reference these four files are **byte-identical**; only trim
   variants differ, and only by swapping `data_path` values.

Plus: manifest `category[]` must be `"truck"` (cars keep the truck category —
`"Car"` is a mod-site convention the game does not read), and models live under
`/vehicle/car/<slug>/`.

Car Mode itself is unlocked by **owning a Road Trip vehicle DLC** — that part is
account-side and no mod can grant it.

## What it does

1. Injects `tags[]: "<type>"` into `def/vehicle/truck/<slug>/data.sii`
   (creating the unit if the mod has none).
2. Generates four binding files — `car_dealer/`, `car_desktop/`, `car_company/`,
   `truck_dealer/` — each a `vehicle: .car` unit wiring `vehicle_accessory`,
   `vehicle_wheel_accessory` (with `offset:` per axle), `vehicle_addon_accessory`
   and `vehicle_paint_job_accessory` to the mod's own parts.
3. Rewrites model asset roots to the `/vehicle/car/<slug>/` convention,
   leaving `/def/...`, `/ui/`, `/sound/`, `/material/` paths untouched.
4. Sets manifest `category[]` to `"truck"` — cars keep the truck category;
   `"Car"` is a mod-site convention the game does not read.
5. Repacks to a plain-ZIP `.scs`.

## Archive conventions matter — SCS rejects sloppy ZIPs

The game will mount a `.scs` and list it as a mod while silently loading
**zero** files from it. In `game.log.txt` that looks like:

```
<ERROR> [zipfs] Failed to process incompatible zip entry: manifest.sii
...
[ufs] mod pool: 1 packs, 0 files, 1 dirs
```

SCS's zipfs is stricter than a general ZIP reader, and Python's `zipfile`
defaults break it. The output must use:

| Property | Required | Python default |
|---|---|---|
| `create_system` | `0` (MS-DOS) | `3` (Unix) |
| directory entries | one per parent folder | none |
| `external_attr` (files) | `0x81b60000` | `0x01800000` |
| `external_attr` (dirs) | `0x41ff0010` | — |

The archive is still a valid ZIP either way — `unzip -t` passes on a broken
one — so this only shows up in the game. Tests assert every one of these.

## Locked and wrapped archives

**Mods flagged as encrypted.** Some mods carry an encryption flag in the
archive's *index* while the actual file data is plain. Standard readers
(7-Zip, Python's `zipfile`) consult the index first, see "encrypted", and
refuse. The converter notices the mismatch, reads the real data from the local
file headers, and says so:

```
  - 326 entries were flagged encrypted in the archive index but their data is
    plain; read directly
```

Use `--strict-encryption` to refuse this and only accept a real password.

**Genuinely password-protected mods** still fail cleanly:

```
error: cannot read locked.scs (8 of 8 entries flagged encrypted).

  Options:
    - If you have the mod author's password:
        --password 'thepassword'
    ...
```

A wrong password is reported the same way, never as a traceback. Real
encryption is never bypassed: recovery only applies when the local header does
*not* claim encryption.

**Note on the packaged-with-a-flag case.** Setting that flag is a packaging
trick, not encryption — the bytes are readable by anyone who parses the format
correctly. If you would rather not have your tool defeat it on someone's mod,
pass `--strict-encryption`.

**Mods wrapped in a .zip.** If a `.zip` contains exactly one `.scs` and no
definitions, it is unpacked automatically and the inner mod is converted:

```
  - MyMod.zip contained a mod archive; read MyMod.scs inside it
```

## Pre-selecting parts (fallback[])

`fallback[]` in `data.sii` is what tells the game which part to fit when the
car is spawned. Without it the shop shows its first choice and the car arrives
half-configured.

The converter emits one per bound part, using the syntax the Parati uses —
`<part>|<file relative to it>`:

```
fallback[]: "chassis|4matic.sii"
fallback[]: "cabin|w223_s500.sii"
fallback[]: "interior|stock.sii"
fallback[]: "engine|30l.sii"
fallback[]: "transmission|9_speed.sii"
fallback[]: "head_light|digital.sii"
fallback[]: "p_tire|w223.sii"
fallback[]: "p_disc|w223.sii"
```

Most resolve inside the vehicle def folder
(`def/vehicle/truck/<slug>/<part>/<file>`); `p_tire` and `p_disc` live at
`def/vehicle/<part>/<file>`. Tests assert every fallback target exists.

## Wheels

**Wheels are bound exactly as the mod ships them, and are never renamed.**

An earlier version of this converter rewrote `f_tire`/`f_disc` into car
`p_tire`/`p_disc` namespaces and deleted the originals. The game rejected that
with `Redundant p-rim accessory removed!` followed by `Incorrect f-/r-wheel
definition`, then crashed. Renaming is not what Road Trip wants.

What the converter actually does:

- binds every wheel def the mod ships, each as a `vehicle_wheel_accessory`
  at `offset: 0` with `paint_color: (1.000000, 1.000000, 1.000000)`
- a truck mod contributes `.ftire .fdisc .fhub .fnuts .rtire .rdisc .rhub .rnuts`
- a mod that already ships car wheels keeps its own `p_tire` / `p_disc`
- adds a `fallback[]` entry for each, using the mod's real namespaces
- leaves every source wheel def byte-identical

## Registration chain

Five files, matching the reference converter:

```
def/vehicle/car_dealer/mod/<slug>.sii     vehicle : .car
def/vehicle/car_desktop/<slug>.sii        vehicle : .car
def/vehicle/car_company/<slug>.sii        vehicle : .car
def/vehicle/truck_desktop/<slug>.sii      vehicle : .desktop.truck
def/vehicle/truck_company/<slug>.sii      vehicle : .company.truck
```

The three `car_*` files are byte-identical. `<slug>` comes from the mod's own
vehicle unit name (`vehicle.w223_s500` -> `w223_s500`), not the filename.

The mod's own `truck_dealer` entry is **left exactly as shipped**.

## Physics tokens written

`disc_brake` is `true` on both axles, and `axle_suspension_type` is
`wishbone` on both. Deliberately **not** written:

| token | why |
|---|---|
| `brake_rotor_diameter` | tyre diameter comes from the `.pmd`, not any `.sii`; inventing a rotor size risks a brake/wheel mismatch |
| `powered_axle` | the mod's own drivetrain declaration is authoritative |
| `detail_model` | not synthesised from `model` |
| `tank_size` | fuel capacity is economy, not driving physics |
| axle/rim model paths | would import foreign assets |

## What it does not do

- **Does not invent model paths.** If a part is missing, it is reported as a gap,
  not fabricated. A binding pointing at a nonexistent `.pmd` renders as an
  invisible accessory.
- **Does not author geometry.** The Porsche has one `car.pmd` for the whole
  vehicle; the converter reclassifies what exists, it cannot build a missing
  body/collision split.
- **Does not copy addons from other mods.** The Parati's ~25 accessories are not
  transplanted into your car — those are its author's work.

## Prerequisites for a car to work

A mod needs these for a complete Road Trip car. The Porsche is missing most:

| Part | Porsche |
|---|---|
| `data.sii` with `tags[]` | created by this tool |
| chassis | present |
| engine, transmission | present |
| cabin, interior, head_light | **missing** |
| `p_tire` / `p_disc` wheels | **missing** |
| `paint_job` colours | **missing** |

Car Mode itself is gated on **owning a Road Trip vehicle DLC** — it appears as a
career option in-game once unlocked. That is account-side and no mod can grant
it. This tool makes your vehicle *register* once you have it.

## Files

- `convert_to_car.py` — the converter
- `sii.py` — minimal `.sii`/`.sui` reader/writer (line-oriented, comment-preserving)
- `test_converter.py` — test suite
- `FINDINGS.md` — schema research and notes