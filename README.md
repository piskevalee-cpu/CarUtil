# CarUtil — ATS Road Trip car converter

Converts a truck-classified American Truck Simulator mod into a **Road Trip**
vehicle, so it registers in the Car Dealer and drives with car physics.

## TL;DR

1. **Get the files** — download the zip and extract it, or clone the repo:

   ```bash
   git clone https://github.com/piskevalee-cpu/CarUtil.git
   ```

2. **Run it:**

   ```bash
   python3 convert_to_car.py
   ```

3. **Have fun!**

That's the whole thing. It asks you where your mod is, what kind of car it is,
and which brand, then writes a `.scs` and tells you where to copy it. Nothing
to install and nothing to configure — the brand logos come included.

---

Python 3.9+, standard library only — no dependencies. (ImageMagick is optional
and only used to convert brand logo images. If it is missing, the script says so
up front and offers to install it for your platform; decline and the car still
converts, just with the mod's own logo or a flat placeholder.)

## Choosing the car category

`--type` picks the car's category. It decides three things at once: the car's
handling preset, its default kerb weight, and which **jobs it can take** — so
it is worth getting right.

| `--type` | Means | Default weight |
|---|---|---|
| `pickup` | pickup truck | 2400 kg |
| `offroad` | off-road / 4x4 | 2200 kg |
| `suv` | crossover / SUV | 2200 kg |
| `muscle` | muscle car | 1700 kg |
| `classic` | classic car | 1550 kg |
| `sedan` | saloon / hatchback | 1220 kg |
| `sports` | sports car | 1280 kg |

Leave it off and the guided run asks. Alternative spellings are normalised
(`off-road`→`offroad`, `crossover`→`suv`, `wagon`→`sedan`, `coupe`→`sports`),
and the game's own names `sport` and `veteran` are accepted too.

### Picking a category that fits the real vehicle

The category's default weight is a *starting point*, not a fact about your
mod. Most truck mods sit at 9–10 t in their own chassis file, so the preset is
always wrong for them. For anything real, set the weight yourself:

```bash
python3 convert_to_car.py mod.scs --type suv --mass 2069 --brand mercedes
```

Mass is the single most consequential value in the conversion — it drives
acceleration, braking and suspension response together. A 2069 kg S-Class
classified as `classic` inherits a 1550 kg preset and will drive wrong no
matter how good the handling numbers are.

### How the category affects jobs

The tag the game matches on is not always spelled the way you asked for. SCS's
own vocabulary is `pickup`, `suv`, `sedan`, `muscle`, `veteran`, `sport`,
`offroad`, so `--type sports` is stored as `sport` and `--type classic` as
`veteran`. A tag the game does not recognise matches **no jobs at all**, so
this is worth knowing rather than assuming.

Of the base game's 149 car jobs, 95 accept only `offroad`, `pickup`, `sedan`
and `suv` — the heavy-cargo work. `muscle`, `sport` and `veteran` are excluded
from those and appear only in the two premium jobs. So a muscle or classic car
takes a narrower set of work, not none. `--extra-tags pickup` widens it:

```bash
python3 convert_to_car.py mod.scs --type classic --extra-tags pickup --brand mercedes
```

See [Vehicle types and cargo](#vehicle-types-and-cargo) for the full picture.

---

## Usage

Run the script with `python3`. There is nothing to install, no `chmod +x`, and
no `carutil` command on your PATH — it is a script you execute directly.

**Interactive** — start it with no arguments and it asks you everything:

```bash
python3 convert_to_car.py
```

```
  _____           _    _ _   _ _
 / ____|         | |  | | | (_) |
| |     __ _ _ __| |  | | |_ _| |
| |    / _` | '__| |  | | __| | |
| |___| (_| | |  | |__| | |_| | |
 \_____\__,_|_|   \____/ \__|_|_|

        CarUtil -- ATS Road Trip car converter

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
| `--mod-dir` | Where this machine keeps its ATS user data, if the automatic search cannot find it. Repeatable. Only affects the folder printed at the end. |
| `--slug` | Vehicle identifier. Defaults to the vehicle's own unit name — see below. |
| `--asset-root` | Explicit model root. Default: `/vehicle/car/<slug>`. |
| `--mass` | Kerb weight: `TOTAL` kg (`--mass 1650`) or `FRONT,REAR` (`--mass 1150,1050`). Skips the prompt. |
| `--no-prompt` | Never prompt, even on a terminal. |
| `--password` | Password for a protected mod archive. |
| `--no-car-tune` | Leave chassis physics alone (still writes `tags[]` + dealer bindings). |
| `--power` | Target peak power in horsepower (SCS's own Road Trip cars make 202-398 hp). **Preferred.** Rebuilds the torque curve on SCS's shape. |
| `--peak-power-rpm` | Rev at which peak power occurs, e.g. `4500` (SCS's cars: 4500-6000). A higher value also lowers idle torque, so the car creeps less. |
| `--air-resistance` | Chassis drag coefficient. Use this to settle top speed and the 60-120 run — see below. |
| `--price` | What the Car Dealer charges for the finished car, in dollars — see below. |
| `--acceleration` | Target 0-60 mph in seconds. The engine is tuned to hit it at the given `--mass`. Preferred over `--engine-nm-per-tonne`. |
| `--force-physics` | Overwrite chassis physics even when the mod already has car-sane values. |
| `--keep-physics` | Keep the global truck `physics.sii`. **Off by default** — see below. |

### Top speed, and why the engine curve can't fix it

Drag rises with the square of speed; engine torque depends on revs. In a truck
10-speed box those two do not line up: top gear is so short that the engine is
still in the same 1500-4000 rpm band at 120 mph that it uses for 0-60. So there
is no torque taper that slows 60-120 while leaving the launch alone — the two
phases occupy the same revs.

`--air-resistance` is therefore the only lever that separates them, and because
drag share climbs with v³, raising it barely touches 0-60:

| `--air-resistance` | push left @60 | @80 | @120 | 0-60 cost |
|---|---|---|---|---|
| `0.55` (default for an SUV) | 98% | 96% | 86% | — |
| `2.4` | 92% | 82% | 39% | −1.4% |

SCS's own cars ship `0.37-0.596`. A converted car with a realistic engine will
run away well past 150 mph at those values, which is why the override exists.

### Price

ATS has **no single car-price key**. The Car Dealer totals the `price:` of every
accessory named by a `data_path` in the dealer unit — SCS's own `f150_23`,
`bronco_24`, `mustang_67` and `victoria_06` dealer files declare no price at all.
The `accessories[]` list *is* the price breakdown.

`--price 45000` rescales the mod's **entire price table** by a single factor,
so the dealer totals the figure you asked for and every other part stays
proportionate to the car.

That last part matters more than it sounds. Scaling only the base car's
components leaves the upgrade menu at the mod's original prices, and those are
**truck-scale**. On one real mod, `--price 1000` took the car from $113,304 to
$1,000 while `chassis/awd.sii` — an all-wheel-drive chassis, sold as an
optional extra — stayed at $40,000. The car ended up 40× cheaper than one of
its own parts.

Scaling everything by one factor preserves the author's **relative** pricing,
which is the part worth keeping: an AWD chassis that cost 35% of the truck's
price still costs 35% of the car's, and `$40,000` on a $113,304 truck becomes
`$353` on a $1,000 car. The exact figure was the author's guess for a truck;
the proportion is the information.

Scaling **up** is the direction that costs the player: every remaining
purchase gets more expensive at once. The converter says so when the target is
above the mod's own total.

Without the flag, the mod's own prices are kept untouched and the resulting
total is only reported.

### Vehicle types and cargo

Road Trip has two separate job markets, and confusing them is the most common
reason a converted car looks "missing":

- **Quick Job** — the company *lends you* a vehicle. Your own car is never
  listed there, by design.
- **Dispatch Market** — you drive a vehicle from your own garage. Your
  converted car appears here, once you own it.

So a car that is absent from Quick Jobs but works for jobs is working
correctly. `tags[]` does not decide whether a car shows up at all; it decides
which **cargo** the car qualifies to haul.

`tags[]` must spell the type the way the base game does. All 149 of its
`car_job` units match on seven values: `pickup`, `suv`, `sedan`, `muscle`,
`veteran`, `sport`, `offroad`. The converter writes those, so `--type sports`
is stored as `sport` and `--type classic` as `veteran`; an unrecognised tag
matches no job at all, and is now warned about at conversion time.

Of the 149 jobs, 95 accept only `offroad`, `pickup`, `sedan` and `suv` — the
heavy-cargo work. `muscle`, `sport` and `veteran` are excluded from those and
appear only in the two premium jobs.

The seven presets are `pickup`, `suv`, `sedan`, `muscle`, `classic`, `sports`,
`offroad`. `--type` is not restricted to them: alternative spellings are
normalised (`off-road`→`offroad`, `crossover`→`suv`, `wagon`→`sedan`,
`coupe`→`sports`, and the game's own `sport`/`veteran` are accepted too).

`--extra-tags` adds further types so a car qualifies for more cargo:

```
python3 convert_to_car.py mod.scs --type suv --extra-tags pickup,sedan --brand honda
```

### Where the mod goes

At the end of a conversion the tool prints your ATS mod folder. It finds that by
constructing the paths a Windows ATS install can occupy rather than searching for
one: `~/Documents`, a Proton prefix (`<appid>/pfx/drive_c/users/<user>/Documents`),
a Wine prefix, and the Flatpak, Snap and Lutris layouts.

For Steam it lists every **Steam root** it might sit under — `~/.local/share/Steam`,
`~/.steam`, `~/.steam/debian-installation`, the Flatpak sandbox
(`~/.var/app/com.valvesoftware.Steam/…`) and Snap (`~/snap/steam/…`) — and probes
each for `steamapps/compatdata`. That matters because Steam installs vary: `~/.steam`
is normally a symlink farm whose `root` and `steam` entries point back into the real
tree, but on some builds they are real directories instead. Listing roots rather than
one finished path per launcher means both resolve.

Symlinks are handled by the filesystem, so a linked prefix or Steam library is found
without anything being traversed.

Two things it deliberately does **not** do. It does not walk the prefix tree —
a Proton prefix holds over a thousand symlinks into the runtime, so following them
costs minutes. And it does not assume letter case: ext4 is case-sensitive by
default, so a probe that matches on NTFS can miss on a stock Linux Mint install.

If your layout is still not found, name it:

```bash
python3 convert_to_car.py mod.scs --mod-dir "$HOME/Documents/American Truck Simulator"
```

Repeatable, and it takes the data directory itself, a directory containing one,
or a prefix root.

### Brand logos

`--brand` selects the vendor the Car Dealer files the car under, and the logo it
shows. Omit it and the guided prompt lists the brands in `brands.py`.

The vendor is the **manufacturer**, which is what the game expects: SCS's own
Road Trip DLC files `mustang_67`, `f150_23`, `bronco_24` and `victoria_06` under
a single Ford folder. So every Ford you convert shares one vendor and one logo,
and two different brands never overwrite each other's. A brand with no shipped
logo falls back to `other`.

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

All 23 brands currently have artwork. Note the files are not all actually PNGs
despite the extension — some are WebP and some SVG — which is why conversion
goes through ImageMagick rather than a PNG decoder.

| `--dry-run` | Report what would change; write nothing. |

The tool prints three groups at the end: **changes**, **parts bound**, and
**gaps** — parts the source mod does not contain and that you must add by hand.

It can also print **warnings**, which are things that converted but are worth
knowing before you launch the game: a unit name the base game already owns, a
decorative slot left unbound, or assets your input is missing.

## Tests

```bash
python3 test_converter.py
```

Hundreds of checks — the exact number moves with the brand list and with whether
the fixtures are set, so read the exit code rather than a remembered total. The
important one is the **reference fidelity check**: it converts the
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

**Two engine keys are removed or corrected rather than scaled:**

- `secondary_torque` / `secondary_torque_gear_ratio` are a **truck** mechanism
  — extra torque in the lower gears. No production car has one and all 181 of
  SCS's engine units leave it alone. Truck mods declaring it were getting 1.5x
  to 1.7x torque in every gear at or below the ratio. It is dropped.
- `resistance_torque` is engine drag, and it is a **fraction of `torque`**, not
  a fixed number — so it is rescaled by whatever factor `torque` is. Left
  alone it becomes the dominant force: a mod declaring `torque: 4900` with
  `resistance_torque: 320` kept 320 against a 476 Nm car engine, which is 67%
  drag and why lifting off at 50 mph dropped it to about 30. `-1` means "no
  drag" and is left exactly as written.

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
| `residual_travel` | always — see below |
| `suspension_class` | anything but `standard` (`bus` is a coach rating) |
| `sway_bar_stiffness` | > 2.0 |
| `max_steering_angle` | > 45 |

`residual_travel` is the body-roll key and gets **no threshold**. Truck mods
ship 0.09–0.10 and SCS's own cars use 0.093–0.134 — the ranges overlap, so no
cut-off separates them and an old "below 0.12 is a truck" rule flagged SCS's
own Mustang as one. A key whose truck and car values overlap has to be written
outright.

Numbers are read through a decoder that also handles SII's hex-float form
(`kerb_weight[0]: &459380cd` is 4720.1 kg). Without it a hex-float value raised,
was caught, and was read as car-sane — leaving a 9.4 t truck's chassis intact.

Converting the Parati leaves its chassis **byte-identical**. Use
`--force-physics` to overwrite everything regardless. An explicit `--mass`
always wins over whatever the mod shipped.

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

**Only single-archive mods are supported.** The input must be **one** `.scs`
file, or a folder holding the mod's files. Mods shipped as **two** archives —
a `... Def.scs` with the definitions plus a separate `....scs` with the assets —
are **not supported**, and cannot be made supported by pointing the tool at
either file or at the folder:

```
error: this mod's files are split across two archives, and only one of them
can be read.

    2023 BMW 135i M_1.61x.scs  (58 MB, HashFS v1)  <- unreadable
    2023 BMW 135i M Def.scs  (27 KB, plain ZIP)  <- readable
```

The assets half uses SCS's own **HashFS** container (`SCS#`, the format the game
itself uses). The converter reads plain ZIP archives and folders; it does not
read HashFS. So the definitions are reachable but the geometry, textures,
sounds and usually the root `data.sii` are not, and converting one half alone
cannot produce a working car. This used to report `no def/vehicle/truck/<slug>/
found`, which was false — the mod does have one.

To use a split mod, extract the HashFS archive with a tool that understands SCS
containers and point the converter at the extracted folder, or find a
single-archive or pre-extracted release of the same mod.

**Incomplete extractions.** Some mods ship without directory listings for part
of their tree, so a plain extraction comes up hundreds of files short — the
engine's sound bank, most of the textures and models. Every definition still
parses, so nothing downstream notices and you get a 12 MB archive out of a
124 MB mod that spawns mute and untextured. The converter compares the model,
texture and sound paths the mod's own definitions point at against what is
actually present, and says so:

```
  ! 774 of 874 assets this mod's defs point at are not in the input (89%):
    /model/.../engine.bank, ... That is what an incomplete extraction looks
    like -- re-extract with a deep mode (7-Zip's --deep, or 'extract all' in a
    tool that reads directory listings), otherwise the car will spawn without
    its textures and sounds
```

Only paths under a root the mod itself declares are counted. A definition
pointing at `/vehicle/truck/...` or `/material/...` is pointing at the base
game, which is not in your input and not counted against you.

## Which parts get bound, and which the car arrives with

Two separate questions, and the answers used to be the same thing.

**Which file is this part?** The mod's own `truck_dealer` unit decides. Each
`vehicle_accessory : .slot { data_path: ... }` entry names the file the author
intended for that slot, so that is what gets bound. Only slots the dealer unit
says nothing about fall back to directory discovery.

**What does the car arrive wearing?** `fallback[]` in `data.sii`, using the
syntax the Parati uses — `<part>|<file relative to it>`:

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

**A slot the mod already defaults is left alone.** If the author's `data.sii`
already carries a `fallback[]` for a part, that entry is kept and nothing is
added for it — it is the only statement of what the car should ship with. Only
slots the mod left open are filled in, and the summary says how many of each.

Neither decision is made by sorting a directory. That used to be the whole
method, and on a mod shipping several chassis files it bound `xdl.sii` — the
-30 mm sport spring — where the author's dealer config named `xdrive.sii`.

### Decorative slots are left out on purpose

Some mods carry ETS2's interior toys in `accessory/toybig/`, `toyseat/`,
`toystand/` and `set_dashbrd/`. These are optional shop items, not equipment.
Binding one makes it part of the spawned car, and ATS ships no models for that
namespace, so the game dies on load with:

```
Can not open '/vehicle/truck/upgrade/interior_decors/...pmd'
```

They stay in the archive — nothing is deleted — and are reported as skipped. If
you want one fitted, add it to the mod's own `truck_dealer` unit and the
converter will bind it.

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

When the last segment is a bare number it is not naming the vehicle —
`vehicle.gle.450` -> `450`, `vehicle.prado.20` -> `20` — so the whole path is
used instead (`gle_450`, `prado_20`). Those would otherwise be registration
files two different mods both wanted. Pass `--slug` to choose it yourself.

The mod's own `truck_dealer` entry is **left exactly as shipped**.

## Physics tokens written

`disc_brake` is `true` on both axles, and `axle_suspension_type` is
`wishbone` on both. Deliberately **not** written:

| token | why |
|---|---|
| `brake_rotor_diameter` | tyre diameter comes from the `.pmd`, not any `.sii`; inventing a rotor size risks a brake/wheel mismatch |
| `powered_axle` | the mod's own drivetrain declaration is authoritative |
| `detail_model` | not synthesised from `model` |
| `tank_size` | fuel is economy, not driving physics — see below |
| axle/rim model paths | would import foreign assets |

### Fuel is deliberately left alone

`tank_size` is **not** rewritten, even when the mod carries a truck's 1200 L
tank, and `--force-physics` does not reach it either.

This was briefly wrong in the other direction: `tank_size` *was* written, with
car-sized presets per vehicle type. That made range worse, not better. A real
mod's 291 L tank became 79 L while its fuel consumption — which nothing in this
tool touches — stayed at the truck's rate, and a smaller tank at a truck's
consumption is a strictly shorter range than the tank the mod shipped with.

Half-fixing fuel economy is worse than not fixing it. Calibrating capacity
*and* consumption together against SCS's own Road Trip cars needs
`dlc_rt_ford.scs` from an ATS install; until that is measured, the mod's own
figures are left alone rather than guessed at.

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