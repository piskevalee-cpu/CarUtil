# ATS Road Trip / Car Mode — findings

Research notes for converting a truck-classified mod into a Road Trip car.
Everything below was verified by reading the files in this directory.
Items marked **UNVERIFIED** could not be confirmed offline and need an in-game test.

## 1. `.scs` container format

`.scs` is a **container, not a fixed format**. Two variants appear in this install:

| Variant | Magic | Read with |
|---|---|---|
| Plain ZIP | `PK\x03\x04` (`50 4b 03 04`) | `zipfile`, `unzip` — no deps |
| 7-Zip / SCS | `SCS#` (`53 43 53 23`) | needs `7z` / `p7zip` |

- `Volkswagen_Parati_1994.scs` — ZIP. Readable immediately.
- `2021 Porsche Cayenne Coupe Def.scs` — ZIP. Readable.
- `2021 Porsche Cayenne Coupe Gts_1.61x.scs` — `SCS#`. Opaque on this machine.
- `dlc_rt_ford.scs`, `BMW_M3_CS_v1.0.scs` — `SCS#`. Opaque on this machine.

### Correction: these files are NOT encrypted

An earlier pass in this project reported the `SCS#` archives as "obfuscated" /
"encrypted" on the basis of their TOC bytes and ~7.9 bits/byte entropy. **That was
wrong.** Near-maximum entropy is the normal signature of an LZMA-compressed
7-Zip payload, not encryption. Those files were always readable; this machine
simply has no `7z`/`p7zip` installed (`which 7z 7za 7zr p7zip` → nothing,
`py7zr` not importable).

To read `SCS#` archives on this machine: `sudo pacman -S p7zip`, then
`7z x <file.scs>`.

## 2. What actually makes a mod a Road Trip car

Reference: `Volkswagen_Parati_1994.scs` (Cristhian Cardoso, works in Car Mode).
Counter-example: `2021 Porsche Cayenne Coupe Def.scs` (Gaza; drives as a car mod,
but is truck-classified and does not appear as a Road Trip car).

Six differences, in order of importance:

| # | Change | Parati (works) | Porsche (truck-only) |
|---|---|---|---|
| 1 | Vehicle type tag | `tags[]: "sedan"` in `data.sii` | absent |
| 2 | Dealer bindings | `car_dealer/`, `car_desktop/`, `car_company/`, `truck_dealer/` | absent |
| 3 | Asset root | `/vehicle/car/<slug>/` | `/porshe_cayenne/` |
| 4 | Wheel namespaces | `def/vehicle/p_tire/` + `p_disc/`, bound via `vehicle_wheel_accessory` with `offset:` | none |
| 5 | Manifest category | `category[]: "truck"` | `category[]: "Car"` |
| 6 | Root unit | `data.sii` with `accessory_truck_data` + cameras + `fallback[]`/`require[]` | absent |

### 2a. `tags[]` is the vehicle type

In `def/vehicle/truck/<slug>/data.sii`:

```
accessory_truck_data : vehicle.volkswagen.parati1994
{
	info[]: "Volkswagen"
	tags[]: "sedan"          <-- the type
	behind_camera: camera.behind.vw_parati94
	interior_camera: camera.interior.vw.parati94
	...
	fallback[]: "chassis|4x2a.sii"
	require[]: mirror
}
```

`tags[]` is what the wiki calls the seven vehicle types — `pickup`, `SUV`,
`sedan`, `muscle`, `classic`, `sports`, `off-road`. The type decides which
courier cargoes the vehicle can haul in Car Mode (per the wiki, only `muscle`,
`classic` and `sports` are restricted). **This is a single line.**

Note the root unit is still `accessory_truck_data`, and the manifest still says
`"truck"`. Cars are *not* a separate unit type — the `def/vehicle/truck/` path
and the `accessory_truck_data` root are both correct for cars.

### 2b. The four dealer bindings

A `vehicle: .car` unit listing `vehicle_accessory` / `vehicle_wheel_accessory` /
`vehicle_addon_accessory` / `vehicle_paint_job_accessory` pointers, one per part:

```
vehicle_accessory : .chassis {
 data_path: "/def/vehicle/truck/volkswagen.parati1994/chassis/4x2a.sii"
}
vehicle_wheel_accessory : .ptire {
	offset: 0
	data_path: "/def/vehicle/p_tire/vwp1994_175_70.sii"
}
vehicle_wheel_accessory : .ptire2 {      # rear axle
	offset: 2
	data_path: "/def/vehicle/p_tire/vwp1994_175_70.sii"
}
```

**Verified:** four of these files are byte-identical (md5 `5bc651345f5e1003`):

- `def/vehicle/truck_dealer/volkswagen/volks_parati_0.sii`
- `def/vehicle/car_dealer/volkswagen/volks_parati_0.sii`
- `def/vehicle/car_desktop/volkswagen_parati_1994.sii`
- `def/vehicle/car_company/volkswagen_parati_gl.sii`

Only `car_company/volkswagen_fox_wagon_gls.sii` differs, and it differs *solely*
by swapping which cabin / engine / transmission / wheel / accessory it points at.
A "GLS trim" is just a different part combination in the same binding file.

So: **one generator emits all of them**; trims vary only the `data_path` values.

### 2c. Wheels use separate namespaces

Truck mods use `accessory_wheel_data` inline. Cars use two flat namespaces:

- `def/vehicle/p_tire/<x>.sii` → `accessory_wheel_data : <id>.ptire`
- `def/vehicle/p_disc/<x>.sii` → `accessory_rim_data : <id>.pdisc`

cross-linked by `suitable_for[]` in both directions, and bound per-axle via
`offset:` (0 = front, 2 = rear). Unit-name suffixes change `.fwheel`→`.ptire`
and `.fdisc`→`.pdisc`.

### 2d. Asset root convention

Parati models live at `/vehicle/car/volkswagen_parati_1994/*.pmd`.
The Porsche's models are at `/porshe_cayenne/car.pmd` — note the leading
slash-less, non-standard root. The converter rewrites this prefix.

## 3. Physics — the interesting part

**The Parati has no `physics.sii` at all.** Verified: zero files matching
`physics` anywhere in the archive.

The Porsche has `def/vehicle/physics.sii`, and it is **verbatim stock truck
physics**: `cabin_mass: 700.0`, `fifth_wheel_pitch/roll`,
`trailer_joint_pitch/roll`, `spreader_joint_pitch/roll`, tandem-axle tuning.
That is what makes it feel like a truck.

The Parati instead carries car physics **inline in the chassis unit**:

```
air_resistance: 0.1                  # Porsche truck default is 3.0
cog_height: 0.4                     # low CG
sway_bar_stiffness: 0.38
max_steering_angle: 28
kerb_weight[]: 620 / kerb_weight[]: 415     # Porsche: 5250 / 5151
disc_brake[]: true / disc_brake[]: false
brake_rotor_diameter[]: 0.200 / 0.170
suspension_damping_factor[]: 0.9 / 0.7
axle_suspension_type[]: wishbone / solidaxle
nominal_voltage: 12.0               # Porsche: 24
air_tank_pressure: 0.0
tank_size: 47                       # Porsche: 960
```

**Hypothesis (UNVERIFIED):** dropping the global truck `physics.sii` and writing
these car keys inline in the chassis is the actual mechanism by which a vehicle
gets car physics. Supported by the working reference having no `physics.sii` and
the failing one having stock truck physics.

This needs an in-game test. The converter drops `physics.sii` by default and
prints a warning; `--keep-physics` restores the old behaviour.

## 4. Scope limits — what this project does NOT do

The Porsche is **much thinner than the Parati**: 5 def files vs 169.

| | Parati | Porsche |
|---|---|---|
| def files | 169 | 5 |
| `data.sii` | yes | **no** |
| cabin / interior | yes | **no** |
| wheels (`p_tire`/`p_disc`) | 6 | **no** |
| paint jobs | 24 | **no** |
| cameras | 6 | **no** |
| sound banks | 10 | referenced only |
| dashboard UI | yes | **no** |

So "preserve the Porsche's accessories" is not possible — there are none to
preserve. The converter **synthesises bindings only for parts that actually
exist** in the source mod, and prints a gap report for the rest.

It will **not invent `.pmd` model paths**. A binding pointing at a nonexistent
model renders as an invisible/broken accessory, which is worse than an honest
omission. Every synthetic or skipped part is reported.

The Porsche also has a single `car.pmd` for the whole vehicle — no separate
body/collision split beyond `car.pmc`. The converter cannot author missing
geometry; it reclassifies what exists.

## 5. Licensing notes

- **Volkswagen Parati 1994** — no `license.txt` in the archive. Used here purely
  as a structural reference.
- **BMW M3 CS** — ships a restrictive `license.txt` (carbon901 & Deppelover) that
  forbids copying "the definition files" and vehicle physics into other mods
  without permission. Also an **ETS2** mod, not ATS. Not used as a reference.
- **Road Trip: Ford DLC** — commercial, SCS Software, ~$7.99. `dlc_rt_ford.scs`
  is proprietary content. This project reads def-format conventions only; it
  does not redistribute DLC assets.

Car Mode is gated on **owning a Road Trip vehicle DLC** — it appears as a career
option in-game once owned. That is account-side, not file-side: no mod can grant
it. What a mod *can* do is register its vehicle so it shows up in the Car Dealer
and My Cars once the module is unlocked, which is what this converter targets.

## 6. Vehicle types

The seven types accepted by `--type`, from the Truck Simulator Wiki's Road Trip
page (matching the DLC's official vehicle table):

`pickup`, `suv`, `sedan`, `muscle`, `classic`, `sports`, `offroad`

Official assignments: Bronco = off-road, F-150 = pickup, Mustang '67 =
classic muscle, Crown Victoria = sedan, Charger '70 = classic muscle,
Challenger '23 = muscle, Viper '02 = sports, Ram 1500 '25 = pickup.
## 7. Third-party tool: "ATS Mod Converter" (`release/`)

An unpacked release was dropped in `testing-covnverter/release/` for analysis.
**It was not executed.** Static analysis only.

### What it is

| Property | Value |
|---|---|
| Format | PE32+ Windows GUI, x86-64, 65,871,488 bytes |
| SHA-256 (converter) | `96ea4cc787aff96a2b67a4f7da0174abfc9a8dd8bc4d0196bfabbab2881eee73` |
| SHA-256 (hotfix) | `671f7c710233cb03d1428d07ef7d4888d54faaa47f6f741b4728e546f7e155c7` |
| Runtime | .NET 8 self-contained single-file publish |
| PE timestamp | 2026-06-24 10:14:02 UTC |
| File mtime | 2026-10-03 15:51 |
| Authenticode | none — unsigned |
| Manifest identity | `MyApplication.app` v1.0.0.0 (generic template, no vendor) |

Evidence for .NET: `mscoree.dll` / `_CorExeMain` imports, `Microsoft.NETCore.App`
string, embedded `deps.json` tail at EOF, `assemblyVersion 8.0.0.0` entries.
The README's "no .NET to install" is consistent with a self-contained publish.

### Verification limit — important

**No ATS/SII vocabulary exists anywhere in the binary**, in ASCII or UTF-16LE:
`tags[]`, `car_dealer`, `car_desktop`, `car_company`, `truck_dealer`,
`accessory_truck_data`, `vehicle: .car`, `vehicle_wheel_accessory`,
`accessories[]`, `data.sii`, `physics.sii`, `kerb_weight`, `disc_brake`,
`cog_height`, `air_resistance`, `residual_travel`, `p_tire`, `p_disc`,
`category[]`, and all seven type names — **zero hits for every one.**

The managed assemblies are compressed inside the single-file bundle; repeated
attempts to extract them (zlib/deflate scan, .NET bundle manifest parse at
signature `0x795630`) failed to yield the payload.

**Consequence: what this tool actually writes into a .scs is unverifiable
without running it on Windows.** Everything below is assessment of its claims,
not of its code.

### Claims that check out against ground truth

Verified independently against the Parati and the ATS game archives:

- *"Works with vehicle mods distributed as a ZIP .scs"* — **true.** Both the
  Parati and the Porsche `Def.scs` are `PK\x03\x04` ZIPs.
- *"Does not work with native SCS# containers"* — **true.** `dlc_rt_ford.scs`
  and the Porsche asset archive are `SCS#`.
- *"No conversion can promise 100% compatibility"* — honest, and matches the
  scope limits in §4.

### Its hotfix documents a real and important SII trap

> `kerb_weight[2]: 2` — "In SCS mod format there is no 'array size' line. So
> `kerb_weight[2]: 2` does not mean '2 elements', it means 'element 2 has the
> value 2'."

This is correct, and it is a genuinely subtle failure mode: a two-axle car
becomes a three-axle car whose third axle weighs 2 kg, while the number `2` is
also fed into `disc_brake[]` (a bool) and `axle_suspension_type[]` (a name),
which can crash the game.

This converter cannot hit that bug: it never writes chassis array keys at all —
it copies the source `chassis/*.sii` verbatim and only rewrites model paths.

### Notable gap: the README never mentions Car Mode registration

Its documentation is entirely about mass, suspension and physics:
*"The vehicle mass is estimated from the mod's category"*, *"Suspension values
are interpolated from the official Road Trip cars"*, *"Independent rear
suspension, multiple axles and trailers may still need manual tuning"*.

It **never mentions**, anywhere:

- the `tags[]` vehicle-type line
- `data.sii` / `accessory_truck_data`
- the `car_dealer` / `car_desktop` / `car_company` binding chain
- the `p_tire` / `p_disc` wheel namespaces
- manifest `category[]`

Per the independent analysis in §2, those are precisely what makes a vehicle
*register* with Car Mode. Mass and suspension tuning alone change how a car
drives; they do not make it appear in the Car Dealer.

**Risk to check:** the tool may produce a vehicle that drives like a car but
never shows up in Car Mode. That is the first thing to verify if anyone runs it.

### Date discrepancy worth flagging

The README says builds "older than 3 October 2026" produce broken output, and a
HOTFIX is shipped for them. But the shipped converter's PE timestamp is
**2026-06-24**, three months before that boundary. The file was placed on
2026-10-03.

A PE timestamp can be faked or fixed by a reproducible build, so this is not
proof. But if it is meaningful, the shipped converter predates its own fix.

Cheap test if it is ever run: unzip any output and confirm the chassis contains
no `kerb_weight[2]: 2` lines.

### Safety

Unsigned binary of unknown provenance, ~66 MB, opaque internals. Nothing
suspicious was visible — the only URL in the file is `http://www.microsoft.com`,
which is boilerplate — but the compressed payload means the absence of evidence
is not evidence of absence. Treat as untrusted. It cannot run natively on Linux;
evaluating it needs Windows, ideally a throwaway VM.

### Comparison to this project's converter

| | ATS Mod Converter | `converter/` (this project) |
|---|---|---|
| Inspectable | no (opaque binary) | yes (Python source, 46 checks) |
| Car Mode registration (`tags[]`, dealer chain) | not documented | implemented, tested |
| Vehicle type selection | not documented | 7 types, `--type` |
| Physics approach | interpolates mass/suspension | drops truck `physics.sii`, chassis keys |
| Wheels / paint | not documented | binds only parts that exist, reports gaps |
| Runtime | Windows only, 66 MB | Python 3 stdlib, cross-platform |

## 8. Making a car drive like a car (added after analysis of §7)

The §7 tool's README covers only mass and suspension and never mentions
chassis physics. Comparing the Parati against the Porsche shows the mechanism
is **two separate things**, and the second is the one that actually does the work.

### 8a. Drop the global truck physics

`def/vehicle/physics.sii` in the Porsche is verbatim stock truck physics:
`cabin_mass: 700.0`, `fifth_wheel_pitch/roll`, `trailer_joint_pitch/roll`,
`spreader_joint_pitch/roll`, `air_resistance: 3.0`, tandem-axle tuning.

The Parati has **no `physics.sii` at all** (verified: zero files match
`physics` anywhere in the archive).

### 8b. Write car physics into the chassis — the real mechanism

This is the part that matters, and dropping `physics.sii` alone is **not
enough**: the Porsche's chassis carries none of these keys, so the engine would
fall back to its own (truck-oriented) defaults.

Keys read from `def/vehicle/truck/volkswagen.parati1994/chassis/4x2a.sii`:

| Key | Parati (car) | Porsche (truck) |
|---|---|---|
| `air_resistance` | 0.1 | 3.0 (in physics.sii) |
| `cog_height` | 0.4 | `cabin_cog_height_offset: 0.5` |
| `sway_bar_stiffness` | 0.38 | absent |
| `max_steering_angle` | 28 | absent |
| `kerb_weight[]` | 620 / 415 kg | 5250 / 5151 kg |
| `nominal_voltage` | 12.0 | 24 |
| `tank_size` | 47 | 960 |
| `air_tank_pressure` | 0.0 | absent |
| `disc_brake[]` | true / false | absent |
| `brake_rotor_diameter[]` | 0.200 / 0.170 | absent |
| `suspension_damping_factor[]` | 0.9 / 0.7 | absent |
| `axle_suspension_type[]` | wishbone / solidaxle | absent |
| `residual_travel[]` | 0.18 / 0.16 | 0.08 / 0.08 |
| `suspension_class` | standard | absent |

`air_resistance` is the headline number: 0.1 vs 3.0 is a 30x reduction in drag,
which is most of the "drives like a car" sensation on its own.

Truck-only keys that must be removed from a converted car, because a car has no
way to attach them: `fifth_wheel_*`, `trailer_joint_*`, `spreader_joint_*`,
`cabin_suspension_*`, `cabin_sway_bar_factor`, `cabin_cog_height_offset`,
`cabin_mass`, `cabin_max_*`, `damping_scaling_trailer_factor`.

### 8c. Per-type presets

The Parati is a small FWD wagon, so it is the **sedan baseline**. Every other
type scales from it using published manufacturer kerb masses and drag:

| type | kerb F/R (kg) | air_resistance | cog_height | sway_bar | steer |
|---|---|---|---|---|---|
| sedan | 700 / 520 | 0.10 | 0.40 | 0.38 | 30 |
| suv | 1150 / 1050 | 0.16 | 0.55 | 0.50 | 28 |
| muscle | 900 / 800 | 0.13 | 0.42 | 0.55 | 32 |
| sports | 720 / 560 | 0.07 | 0.32 | 0.80 | 35 |
| pickup | 1350 / 1050 | 0.22 | 0.75 | 0.45 | 26 |
| offroad | 1200 / 1000 | 0.25 | 0.80 | 0.40 | 32 |
| classic | 850 / 700 | 0.15 | 0.45 | 0.35 | 30 |

**Token-valued keys are never invented.** `suspension_class` and
`axle_suspension_type` reuse the Parati's exact tokens (`standard`, `wishbone`,
`solidaxle`) for every type, because an invalid token is a load-time error
whereas a numeric value is only a tuning difference.

### 8c-2. Weight is asked for interactively

On a terminal the converter asks *"How much do you want this car to weigh?"*
before converting. It accepts a **total** (auto-split by the type's natural
front/rear ratio so the balance survives), an explicit `front,rear` pair, or an
empty line for the type default. Suffixes `kg`/`k`/`t` are stripped.

The prompt is skipped when stdin is not a terminal, so scripts and CI never
block; `--no-prompt` disables it explicitly and `--mass` bypasses it.

Totals outside 400-6000 kg are rejected with a re-prompt rather than silently
producing a physics-breaking vehicle.

### 8d. Preserve, don't stomp

Existing chassis values are kept unless they are provably truck-scale:
`air_resistance` >= 1.0, `kerb_weight` total > 4000 kg, `nominal_voltage` >= 24,
`cog_height` > 0.85, any `residual_travel` < 0.12, `tank_size` > 200,
`sway_bar_stiffness` > 2.0, `max_steering_angle` > 45.

Verified: converting the Parati leaves its chassis **byte-identical**. Without
this rule the converter replaced its working 620/415 kg mass with the generic
sedan preset's 700/520 kg — a regression on a mod that already drives well.
`--force-physics` overrides.

### 8e. Axle-count safety

Every per-axle array is emitted with **exactly** the source's axle count, which
is read from `kerb_weight`/`powered_axle`. This is the failure mode the §7
tool shipped: writing one element too many invents a phantom axle. Covered by
the `axle_count_never_exceeds` and `tune_chassis_rewrites_porsche` tests.

Note the Porsche is nominally AWD (`awd.sii`) but declares
`powered_axle[0]: true / powered_axle[1]: false` — the rear axle is
unpowered. That is preserved as-is; making it AWD is a manual edit.

### 8f. Still unverified

Everything above is derived from the one Road-Trip-compatible mod we can read.
Whether the resulting handling matches SCS's own cars cannot be confirmed
offline — that needs an in-game test.


## 9. Locked mod archives (Honda Pilot YG2 2025, 1.61x)

Adding this mod surfaced a failure mode the converter did not handle: every
ZIP entry carries the encryption flag, so `zipfile.read()` raises
`RuntimeError: File ... is encrypted` partway through a conversion.

**All 326 of 326 entries are encrypted.** Nothing is readable without the
password, so the mod cannot be converted at all. Filenames remain visible in
the central directory, which reveals the structure but not the contents:

```
def/vehicle/truck/hpilot25/data.sii          <- has data.sii already
def/vehicle/truck/hpilot25/chassis/awd.sii
def/vehicle/truck/hpilot25/cabin/yg2_2025.sii
def/vehicle/truck/hpilot25/engine/35l.sii
def/vehicle/truck/hpilot25/transmission/10_speed.sii
def/vehicle/truck/hpilot25/head_light/xenon.sii
def/vehicle/truck/hpilot25/interior/{black,brown}.sii
def/vehicle/truck/hpilot25/paint_job/        <- 60+ colours incl. dlc_metallics
```

This mod is **much richer than the Porsche** — it has `data.sii`, interior
variants, headlights and a full paint range, so it would convert well if it
could be read. It also ships a `def/vehicle/truck_dealer/` entry already.

### Wheel namespaces differ (unresolved)

Its wheels use the **truck** namespaces -- `f_disc`, `f_hub`, `f_nuts`,
`f_tire`, `r_disc`, `r_hub`, `r_nuts`, `r_tire` -- **not** the Parati's
`p_tire` / `p_disc`. The converter does not remap these, so a converted Pilot
would have no selectable rims. Remapping means splitting a combined
`.fwheel` unit into separate `.ptire` and `.pdisc` units, and the correct
key set cannot be verified without a readable example. Not attempted rather
than guessed.

### Handling added

- `ModSource` tracks locked entries and scans only readable ones
- A fully-locked mod fails fast with actionable guidance, not a traceback
- `--password` unlocks an archive; a wrong password reports cleanly
- A `.zip` wrapping a single `.scs` is unpacked automatically
- Tests cover all of the above with a real encrypted fixture


## 10. The Pilot's "encryption" is a header trick, not encryption

Re-examining `Honda Pilot YG2 2025 V1.6 1.61.scs` (FINDINGS.md §9 recorded it
as "locked"), the two disagree:

| Source | Flags | Method | Actual payload |
|---|---|---|---|
| Central directory | `0x0001` (encrypted) | 8 (deflate) | — |
| Local file header | `0x0000` (**not encrypted**) | 1 (stored) | inflates as raw deflate |

The bytes on disk are ordinary deflate. `zlib.decompress(data, -15)` on the
raw entry recovers `SiiNunit { accessory_truck_data : vehicle.hpilot25 ...`
directly. Python's `zipfile` and 7-Zip both trust the central directory and
therefore refuse to open it, which is the entire effect.

So there is **no password and no encryption** — that is why you never saw one.
The flag exists to make generic extraction tools fail. The game reads these
mods regardless.

`ModSource` now falls back to parsing local file headers when, and only when,
zipfile refuses an entry *and* that entry's local header does not claim
encryption. A real ZipCrypto archive sets the flag in both places, so the
fallback cannot open it — covered by `test_real_encryption_still_refused`.

`--strict-encryption` disables the fallback for anyone who prefers not to
defeat the packaging.

### What the Pilot actually contains

Now readable, and considerably richer than the Porsche:

```
data.sii    11 camera bindings, require[]: mirror, require[]: steering_w
chassis/awd.sii    powered_axle[0]: true, powered_axle[1]: true   (AWD, correct)
                   nominal_voltage: 12     (already car-correct, preserved)
                   kerb_weight 4750 / 4660 kg  (9.4 t truck mass -> replaced)
                   tank_size 280           (-> 70)
                   residual_travel 0.10 x2 (truck-stiff -> replaced)
cabin, engine/35l, transmission/10_speed, head_light/xenon
interior/{black,brown}, 45 paint_job entries incl. dlc_metallics
accessory/mirror, accessory/steering_w
```

Converted output keeps all 11 camera bindings and both `require[]` lines,
carries the 45 paint jobs through, and binds 10 parts. Only gap remains the
wheels: the Pilot uses truck namespaces (`f_disc`, `f_hub`, `f_nuts`, `f_tire`,
`r_disc`, ...) rather than the Parati's `p_tire` / `p_disc`, so a converted
Pilot would have no selectable rims. Remapping a combined `.fwheel` into
separate tire and disc units is not attempted without a verified example.


## 11. Mercedes-Benz W223 S-Class V2.3 — second real mod

Same packaging trick as the Pilot: all 463 entries flagged encrypted in the
central directory, payload plain in the local headers. Recovered the same way.

```
def/vehicle/* : f_disc, f_hub, f_nuts, f_tire,
                r_disc, r_hub, r_nuts, r_tire,
                truck, truck_dealer
vehicle def dir: w223_s500
```

### Source values

```
data.sii      9 camera bindings, require[]: mirror / smirror / steering_w
chassis       powered_axle[0]: true, powered_axle[1]: true   (AWD, preserved)
              nominal_voltage: 12                              (preserved)
              kerb_weight 4720 / 4680 kg   (9.4 t -> replaced)
              tank_size 265 -> 47
              residual_travel 0.09 x2      (truck-stiff -> replaced)
```

Converted with `--type sedan --mass 1900`: 84 def files out, 45 paint jobs
carried, 9 cameras and all 3 `require[]` lines preserved, AWD untouched, four
dealer bindings written, truck `physics.sii` dropped.

### Two real mods agree on the truck-mass pattern

| | Pilot | W223 | Porsche |
|---|---|---|---|
| `kerb_weight` | 4750 / 4660 kg | 4720 / 4680 kg | 5250 / 5151 kg |
| `nominal_voltage` | 12 | 12 | 24 |
| `residual_travel` | 0.10 | 0.09 | 0.08 |
| `data.sii` | present | present | **absent** |
| paint jobs | 45 | 45 | 0 |

All three carry a ~9.5 t placeholder mass — mod authors copy a truck chassis
template and never set the real figure. That is why `kerb_weight` is the single
most important value for the conversion to feel right, and why the tool asks
for it explicitly.

### Wheel remapping: now testable, still not attempted

Both real mods use truck wheel namespaces (`f_disc`, `f_hub`, `f_nuts`,
`f_tire`, `r_disc`, `r_hub`, `r_nuts`, `r_tire`). The Parati's car equivalents
are `p_tire` / `p_disc`. Two real examples now show exactly what a truck
`.fwheel` unit contains, but neither is a *car* mod that also carries car wheel
names, so the unit-splitting rule (one combined `.fwheel` -> separate `.ptire`
and `.pdisc`, plus the `suitable_for[]` cross-links and `offset:` axle binding)
still has no verified target. Left as a reported gap.

Both converted cars therefore bind 11 parts but have no selectable rims. This is
the one outstanding piece of work.


## 12. First in-game test: the mod mounted but loaded nothing

Converted Pilot dropped into the mod folder, listed as an active mod, absent
from the Car Dealer. The game log was decisive:

```
[mods] Active local mod rt_honda_pilot_yg2_2025_v1.6_1.61
<ERROR> [zipfs] Failed to process incompatible zip entry: manifest.sii
<ERROR> [zipfs] Failed to process incompatible zip entry: def/vehicle/truck/hpilot25/data.sii
... 92 such errors, one per entry ...
[ufs] mod pool: 1 packs, 0 files, 1 dirs
```

**Every single entry was rejected.** `0 files` means the mod contributed
nothing, which is exactly why nothing appeared anywhere.

### The container was valid

Ruled out first:

- `unzip -t` — no errors
- all 90 local headers scanned: flags `0x0`, no encryption bit, no data
  descriptor, correct sizes, `create_version`/`extract_version` 20
- EOCD structurally identical to the working Parati
- local header of `data.sii` byte-for-byte comparable to the Parati's

So the payload was fine. The archive was fine by ZIP standards.

### What actually differed

Compared against `Volkswagen_Parati_1994.scs`, a mod the game does accept:

| Property | Parati (accepted) | My output (rejected) |
|---|---|---|
| `create_system` | `0` (MS-DOS) | `3` (Unix) — Python default |
| directory entries | 32 | **0** |
| `external_attr` files | `0x81b60000` | `0x01800000` |
| `external_attr` dirs | `0x41ff0010` | n/a |

SCS's zipfs expects the conventions a Windows tool produces. Python's
`zipfile` emits Unix-flavoured ZIPs by default, which are still valid ZIP but
which SCS declines to read.

### Fix

`_write_scs` now mirrors the accepted mod exactly: `create_system = 0`, an
explicit directory entry for every parent folder, `external_attr` of
`0x81b60000` for files and `0x41ff0010` for directories. 263 tests, including
one that asserts every convention against the reference mod.

This is the kind of failure that looks like a content bug and is actually a
container bug — and it is invisible to `unzip`.


## 13. Second in-game test: crash on selecting rims

The Mercedes loaded, appeared in the Car Dealer and could be configured in the
Car Explorer. Selecting rims crashed the game. The log's repeating error:

```
<ERROR> Suspicious wheel width on wheel no. 0, falling back to default value.
<ERROR> Suspicious wheel width on wheel no. 1, falling back to default value.
```

repeated until the log simply stops — a hard crash with no clean exit.

### Cause: the mod was never converted to car wheels

Both real mods ship truck wheel namespaces. They were already partly
Road-Trip-aware — every wheel def includes

```
@include "/def/vehicle/car_wheels_conflicts.sui"
```

which is SCS's **car** wheel conflict list, at an absolute `/def/` path. So the
author knew about car wheels, but the units themselves kept truck names.

| mod | tires | rims |
|---|---|---|
| Mercedes | `f_tire` (2), `r_tire` (2) | `f_disc` (5), `r_disc` (5) |
| Pilot | `f_tire` (1), `r_tire` (1) | `f_disc` (1), `r_disc` (1) |
| Parati (reference) | `p_tire` (3) | `p_disc` (3) |

Front and rear sets share file stems (`f_tire/w223.sii` and `r_tire/w223.sii`),
so a naive copy collided. Cars fit one wheel to both axles via `offset:`, so
the front set is the correct single source and the rear duplicates are dropped.

### Mapping applied

```
def/vehicle/f_tire/<stem>.sii  accessory_wheel_data : <id>.ftire
   -> def/vehicle/p_tire/<stem>.sii   accessory_wheel_data : <id>.ptire
def/vehicle/f_disc/<stem>.sii  accessory_rim_data : <id>.fdisc
   -> def/vehicle/p_disc/<stem>.sii    accessory_rim_data : <id>.pdisc
```

Model paths are untouched so the `.pmd` files resolve; the existing
`car_wheels_conflicts.sui` include is kept. Cross-links follow the Parati:
tires get `suitable_for[]` per rim, rims get `defaults[]` plus
`suitable_for[]: "*.<slug>.chassis"`.

`f_hub` / `f_nuts` are dropped — all of them point at `none.pmd`, placeholders
for truck hubs that cars carry inside the rim model.

### Stock pre-selection

The Parati's `data.sii` lists `fallback[]` for every part. Mods that lack one
get the shop's first choice instead, which is how the car spawned unconfigured.
The converter now emits `fallback[]` for every bound part:

```
fallback[]: "chassis|4matic.sii"        fallback[]: "p_tire|w223.sii"
fallback[]: "cabin|w223_s500.sii"       fallback[]: "p_disc|w223.sii"
fallback[]: "interior|stock.sii"        fallback[]: "paint_job|color0.sii"
fallback[]: "engine|30l.sii"            fallback[]: "head_light|digital.sii"
fallback[]: "transmission|9_speed.sii"
```

Resolution was verified against the Parati: most parts resolve inside
`def/vehicle/truck/<slug>/<part>/`, while `p_tire` and `p_disc` live at
`def/vehicle/<part>/`. Tests assert every fallback target exists and that
wheels are bound in the `car_dealer` file.

**Unverified:** whether the remapped wheels stop the crash. That needs another
in-game run.


## 14. Third in-game test: rear axle had no wheel

After the brace fixes, `mod pool: 1 packs, 472 files` and **no syntax errors** --
both earlier bugs were genuinely fixed. But the car still crashed on rims and
still wore truck wheels. New, much clearer errors:

```
<ERROR> Redundant p-rim accessory removed!
<ERROR> Incorrect f-wheel definition for /vehicle/truck/w223/car.pmd
<ERROR> Incorrect r-wheel definition for /vehicle/truck/w223/car.pmd
```

### Cause: only the front axle was bound

The reference binds **four** wheel accessories:

```
vehicle_wheel_accessory : .ptire  { offset: 0, data_path: "/def/vehicle/p_tire/..." }
vehicle_wheel_accessory : .pdisc  { offset: 0, data_path: "/def/vehicle/p_disc/..." }
vehicle_wheel_accessory : .ptire2 { offset: 2, data_path: "/def/vehicle/p_tire/..." }
vehicle_wheel_accessory : .pdisc2 { offset: 2, data_path: "/def/vehicle/p_disc/..." }
```

The converter emitted only the two `offset: 0` entries. A car needs wheels on
both axles; the second pair is the rear axle, and the offset is what places
the wheel on it. Without them the rear axle was bare, which is exactly what
"Incorrect r-wheel definition" reports. The "Redundant p-rim" line is the
game discarding a rim it had already resolved for a slot with nothing on the
other side.

### Two earlier fixes confirmed by the log

- `Expecting end of file, found '}'` -- **gone**. The extra brace in the
  generated binding files was real and is fixed.
- `Expecting end of file, found 's'` on `chassis/4matic.sii` -- **gone**. The
  cause was `	variant: chs` in the chassis body matching the unit-header
  regex, so the brace walk began inside the body and skipped the real closing
  brace, appending physics keys after the file's last `}`. `unit_body_end` now
  counts brace depth instead of matching headers.

### Truck wheel defs were still shipped

Even after remapping to `p_tire`/`p_disc`, the original `f_tire`/`f_disc` defs
remained in the archive, so the game found both and kept fitting truck wheels.
They are now deleted after remapping -- 18 removed, 0 remaining, matching the
reference, which has no truck wheel defs at all.

### Cosmetic error left alone

```
<ERROR> Can not open '/material/ui/accessory/<slug>.mat'
```

The Car Dealer looks for a per-vehicle UI material named after the vehicle.
The mod ships none, and the reference carries its UI materials under
`material/ui/car_brand_logo/`. Purely a missing icon; it does not affect
driving and no equivalent file exists in these mods to copy.


## 15. Fourth in-game test: no crash, but wheels still wrong

**No crash** with the Honda Pilot -- the rear-axle fix (section 14) held.
Remaining, on car spawn:

```
<ERROR> Redundant p-rim accessory removed!
<ERROR> Incorrect f-wheel definition for /vehicle/truck/pilot_2024/car.pmd
<ERROR> Incorrect r-wheel definition for /vehicle/truck/pilot_2024/car.pmd
```

Also reported by the user: stock parts not visible in the Car Explorer, and the
car absent from Quick Jobs.

### What was checked and ruled out

- Wheel bindings: four present, offsets 0/0/2/2 -- correct
- `fallback[]`: eight entries, every target exists
- Truck wheel defs: 0 remaining
- Generated `.sii` files: all brace-balanced
- The wheel `.pmd` models themselves **do** exist
  (`vehicle/truck/pilot_2024/acc/wheel/tire_01.pmd`, `rim_01.pmd`)
- Asset paths left as the author wrote them, so they resolve

### Two differences from the reference, both fixed

Key-by-key comparison of a generated wheel unit against every `p_tire` in the
Parati:

| key | Parati | generated (was) |
|---|---|---|
| `part_type` | `factory` | **missing** |
| `look` | `default` | **missing** |
| `grip_factor` | `1.1` | **missing** |

And the chassis:

| key | Parati | Pilot (was) |
|---|---|---|
| `detail_model` | present | **missing** (only `model`) |

Both are now added. 411 tests.

### Honest assessment of where this stands

This is the fourth in-game round, and each one has surfaced a new layer:
archive conventions, dropped assets, broken braces, a bare rear axle. That is
expected -- the Parati is the *only* working Road Trip reference available, and
every other decision is inference from one example.

The remaining wheel error may not be a definition problem at all. The very
first symptom in section 12 was `Suspicious wheel width` -- the game derives
tyre width from the `.pmd` geometry. These mods carry **truck-sized wheel
models**. A definition can be renamed `ftire` -> `ptire` perfectly and the
geometry is still a truck wheel, which no amount of def rewriting can change.
If that is the cause, the fix is a width override or new wheel models, not a
converter change.

## 17. Quick Jobs vs Dispatch Market (correction)

A long thread of this investigation assumed a converted car failing to appear in
**Quick Jobs** was a converter defect. It is not.

SCS's Road Trip blog states it directly: "you won't need to have your own
vehicle to take on a Quick Job. A suitable vehicle will be provided for you by
the company offering the job." Quick Jobs always use a borrowed vehicle.

The truck-mode equivalent of a freight market, where the player supplies their
own car, is the **Dispatch Market**. That is where a converted car appears, once
the player owns it through Car Mode.

Consequences:

- `tags[]` does **not** decide whether a car is visible in a job list at all. It
  decides which cargo the car qualifies to haul, and per the wiki only `muscle`,
  `classic` and `sports` are restricted from certain cargoes -- so `sedan`,
  `suv`, `pickup` and `off-road` are unrestricted in practice.
- The reference converter's `job-tag ... matches car_job vehicle_types[]` line is
  a check against a **built-in list**, not against the base game's `car_job`
  unit. Its own summary carries `UNVERIFIED 1` for runtime jobs. Matching that
  tool byte-for-byte therefore proved nothing about job availability.
- Confirmed in-game: a converted Mercedes (`tags[]: "sedan"`) and a converted
  Honda both take jobs normally. Neither was ever a converter bug.

The two ERROR lines that *were* real, and are now fixed, were about UI materials
the game could not find:

```
<ERROR> [resource_task] Can not open '/material/ui/car_brand_logo/mod.mat'
<ERROR> [resource_task] Can not open '/material/ui/accessory/hpilot25.mat'
```

The first is fixed: the logo is now written to `car_brand_logo/` under the dealer
folder name, which is what the game asks for. The reference converter does not
write that directory at all.


## 16. The reference converter: what it actually does (correction)

The third-party ATS Mod Converter was run on the Mercedes. Its `logs/` file is a
full specification of its behaviour, and its `output/` is a working conversion
to diff against. Several of my conclusions in sections 12-15 were wrong.

### It removes nothing

463 source members in, 471 out. Zero deletions. It only adds eight files: five
registration units and a three-file brand logo.

### It never creates `p_tire` / `p_disc`

The biggest correction. It binds all eight truck wheel defs in place:

```
vehicle_wheel_accessory : .ftire { offset: 0  paint_color: (1.000000, 1.000000, 1.000000)
                                     data_path: "/def/vehicle/f_tire/w223.sii" }
... .fdisc .fhub .fnuts .rtire .rdisc .rhub .rnuts   (all offset 0)
```

and `fallback[]` uses `f_tire|w223.sii`, `f_disc|...` -- the truck namespaces.
My p_tire/p_disc remapping (sections 13-15) was the cause of the wheel errors,
not a fix for them.

### What I got wrong, and the evidence

| my belief | reality |
|---|---|
| wheels must be renamed to car namespaces | bind them as shipped; renaming is rejected |
| truck wheel defs should be deleted | delete nothing |
| `brake_rotor_diameter` should be written | deliberately omitted: "Tyre diameter comes from the .pmd model and is not recorded in any .sii file... inventing a rotor size risks a brake/wheel mismatch" |
| `axle_suspension_type: wishbone/solidaxle` | `wishbone/wishbone`; a solid axle needs models and animations the mod lacks, so copying it creates a dangling reference |
| `disc_brake: true/false` | `true/true`; the reference's `[true,false]` would declare a rear drum |
| `detail_model` should be added | not written |
| `powered_axle` should be authored | never written; the mod's declaration is authoritative |
| the mod's `physics.sii` should be dropped | kept; it is non-namespaced and overrides the base game for every vehicle |
| the wheel models were the blocker | the definitions were the blocker |

### Parity test added

`test_parity_with_reference_converter` converts the Mercedes and diffs it
against the reference output: identical file set, and byte-identical
registration files and `data.sii` apart from the generator's own header comment
and the choice of stock paint. 411 tests.

### Remaining difference

Paint job: the reference picks `color3` ("Alaska White"), we pick the first
stock paint (`color1`, "Alpine White"). Both are `stock: true`; no rule was
derivable. Cosmetic only.

## 18. The cars drove like trucks: the drivetrain and the global physics (correction)

Section 8 concluded that rewriting the chassis was "the actual mechanism by
which a vehicle gets car physics", and section 3 called the base game
unreadable. Both are now wrong. A converted Honda Pilot reached 60 mph in about
three seconds and stopped in one, at a correct 2200 kg.

### The base game was never unreadable

`def.scs` and the DLCs are HashFS **v2**, not an encrypted blob. The container
is a 49-byte packed header -- `SCS#`, version, salt, the hash method `CITY`,
two table counts and lengths, then the two table offsets -- followed by two
**zlib** tables. The entry table is 16 bytes per file (`u64` CityHash of the
path, `u32` metadata index, `u16` metadata count, `u16` flags). The metadata
table is a typed stream; type `128`/`129` are plain file and directory records
carrying a 3-byte little-endian compressed size, a size, and an **offset block
of 16 bytes**. File data is raw deflate behind a **2-byte prefix**.

With that, `def.scs` and `dlc_rt_ford.scs` decode in about thirty lines of
Python. Every "UNVERIFIED, needs an in-game test" note below section 3 was
avoidable. Measure the file, don't assume it is opaque.

### The engine was the whole acceleration problem

Ground truth, from SCS's own Road Trip Ford DLC:

| | SCS Road Trip engines | the truck mods |
|---|---|---|
| `torque` | 389 / 382 / 556 / 563 Nm | Pilot **1400**, W223 **4000** |
| own `info[]` says | 389 / 414 / 556 / 542 Nm | 475 Nm, 520 Nm |
| `secondary_torque` | absent | 2250 / 4800 |

SCS's `torque` **is** the engine's real peak in Nm -- the F-150 V8 declares
`torque: 556` against its own `info[]: "410 @@lb_ft@@ (556@@nm@@)"`. The mods
inflate the same field by 2.9x and 7.7x while stating the true figure in
`info[]`. So the mod is its own calibration: `tune_engine` divides the declared
value by the primary `torque_curve` peak and rescales to the stated number.
Pilot 1400 -> 833 Nm, W223 4000 -> 945 Nm.

The gearbox is **not** at fault and must not be touched: the mods' 10-speed
ratios (4.69 / 2.98 / 2.14 / 1.76 / 1.52 / 1.27 / 1.00 / 0.85 / 0.68 / 0.63)
are the same 10R80 pattern SCS ships on the F-150
(4.696 / 2.985 / 2.146 / 1.769 / 1.520 / 1.275 / 1.000 / 0.854 / 0.689 / 0.636).

### Section 16's physics.sii ruling was backwards

Section 16 reversed section 3 and kept the mod's `physics.sii`, reasoning that
dropping it is a global change. The block *is* global -- but it is stale, and
it is what brakes like a trap. Against `def.scs`:

| key | base game | mod |
|---|---|---|
| `brake_balance` | 1.1 | 1.5 |
| `braking_curve_peak_rel` | 1.5 | 1.15 |
| `bristle_stiffness_lateral/longitudinal` | 1.0 / 1.0 | 2.05 / 1.95 |
| `bristle_damping_lateral/longitudinal` | 1.0 / 1.0 | 2.25 / 2.15 |
| `slip_lateral/longitudinal_factor` | 1.0 / 1.0 | 2.10 / 2.05 |
| `sway_bar_stiffness_factor` | 1.0 | 3.0 |
| `steering_sensitivity_multiplier_0kmh` | 1.25 | 1.0 |

A ~2019 truck tyre model forced onto the whole game. Dropping it is now the
default and `--keep-physics` opts back in -- which is also what `README.md`
already documented, so the flag drift between docs and code is closed.

### Section 8's chassis tuning was right all along

Converting the Pilot with `--type pickup` reproduces DN Modding's Road Trip
version of the same vehicle key for key (`air_resistance 0.22`, `cog_height
0.75`, `kerb_weight 1238/962`, `residual_travel 0.20/0.22`). Of the 326
definition files the two mods share, only `chassis/awd.sii` and `data.sii`
differ at all. Two lessons: the reference converter's *physics* was not the
problem, and `tune_chassis` never needed changing.

Note the reference versions are DN Modding conversions, not SCS-authored cars.
They are a valid structural reference and an invalid physics reference.

### Also wrong

- `secondary_torque_curve[]` contains the substring `torque_curve[]`. An
  unanchored curve regex merges both curves and reports the secondary peak as
  the primary one, silently mis-sizing every rescale.
- `get_key` returns a value with its trailing comment, so
  `secondary_torque: 2250 # ...` does not parse as a float. `_rescale_in_place`
  rewrites just the number instead, keeping the comment that documents it.

## 19. In-game result: over-corrected, and why (correction)

Section 18's engine rescale made the Mercedes *slower than intended* — 12.70 s
to 60 mph, against the roughly 4.8 s a real W223 does. Two things came out of
measuring that.

### Acceleration is linear in the declared `torque`, and nothing else

Original 3.00 s at `torque: 4000`; after rescaling to 945, 12.70 s. That is
`3.00 x 4000/945 = 12.70` exactly. So drive force scales with the declared
scalar, and `torque_curve` does not independently scale the peak.

### Every SCS engine declares `torque` as its true peak, and peaks its curve at 1.00

Surveying all 181 engine units in `def.scs`, `base_vehicle.scs` and every DLC
truck or Road Trip archive: **`torque_curve` maxes out at exactly 1.00 in all
181**, and `torque` equals the figure the engine's own `info[]` states — for
trucks just as much as cars (International LT 2305 Nm, Volvo VNR 5492 Nm,
Cascadia 2779 Nm, F-150 556 Nm). Not one uses `secondary_torque`.

So SCS's convention is: `torque` is peak newton-metres and the curve is a
normalised shape. The mods break that convention — `torque: 4000` against a
stated 520, with a curve peaking at 0.55. The inflation is not a constant
factor either (Pilot 2.95x, Mercedes 7.69x), which is why normalising to the
stated figure moved the two cars by very different amounts.

### The tyres were the real limiter, and they are worse than expected

Surveying all 18 official SCS **car** wheel units: every one declares exactly
`roll_resistance` (11 or 13) and `grip_factor` (1.1 or 1.2), and **nothing
else**. No SCS car wheel has ever used `wet_grip`. The six truck wheel units
declare neither key and inherit the global default.

The mods sit on the truck side of that split. The Mercedes `p_tire` declares
**no grip key at all**. The Pilot uses `wet_grip: 4.02` and
`roll_resistance: 6.0`, both truck-era. So after the drivetrain was corrected,
both cars were still running on the truck's tyre calibration — which is why the
Mercedes stayed slow despite having *more* torque and *less* mass than the
Pilot. `tune_wheel` writes the two keys SCS actually reads.

Note `wet_grip` and `noise_volume` are left in place rather than deleted. They
are not keys any SCS car wheel carries, but nothing establishes that the engine
ignores them, and this project has been wrong often enough by inventing.

### Lesson

Two of the three original symptoms were the drivetrain and the global physics,
which was right. The third — the car still feeling slow once those were fixed
— was never on the list because section 8 assumed the chassis was the whole
story. Measuring the base game's car population, rather than reasoning about
what should be right, is what found it.

## 20. Brake rotors are a car-only key, and we were inheriting the truck's

FINDINGS section 16 declined to write `brake_rotor_diameter`, quoting the
reference converter: "Tyre diameter comes from the .pmd model and is not
recorded in any .sii file... inventing a rotor size risks a brake/wheel
mismatch." That reasoning does not survive measurement.

Across every SCS chassis unit:

| | chassis units | declaring `brake_rotor_diameter` | values |
|---|---|---|---|
| Road Trip cars | 4 | **4** | 0.279, 0.285, 0.302, 0.308, 0.350, 0.356 |
| trucks | 39 | **0** | — |

So SCS states the rotor diameter explicitly on *every* car and omits it from
*every* truck, which leaves trucks on the global default. A converted car that
omits it too is therefore running the truck's rotor — the original "insane
brake sensitivity", and the reason `--force-physics` never fixed it. `tune_chassis`
now writes the F-150's own pair (0.302 / 0.285, mid-range for the fleet).

The section 16 reasoning assumed the value was unknowable. It is knowable, and
the way to know it was to go and read what SCS ships.

Note this reverses a conclusion recorded as settled. Section 16 was careful and
well-reasoned; it was reasoning from an assumption about the .pmd instead of
from the files.

## 21. Mass was the bug: a torque figure fitted on the wrong car (correction)

The W223 was converted as `--type classic`, which is a 1550 kg Crown Victoria
preset. The real car is 2069 kg. Both acceleration measurements (9.67 s at
520 Nm, 3.70 s at 1600 Nm) were therefore taken on a vehicle 519 kg too light,
and section 20's extrapolation of them to a 5 s target was wrong before it was
ever tested.

Fitting `t ∝ T^-k` across the two points gives `k = 0.855` -- close to the
inverse-torque 1.0 of pure torque limiting, so torque is very nearly the whole
lever and only mild grip limiting is in play. But a time is only meaningful
against the mass that produced it, and the mass was wrong.

### Real-world torque is the wrong target, twice over

| | kerb | stated Nm | Nm/t | what it should do |
|---|---|---|---|---|
| real W223 S-Class | 2069 kg | 520 | 251 | ~5.0 s |
| SCS's own F-150 | 1873 kg | 556 | 297 | — |
| SCS's own Bronco | 2144 kg | 563 | 263 | — |
| SCS's own Mustang 67 | 1323 kg | 382 | 289 | — |

SCS's cars all sit near **250-300 Nm/t**. That is the reference. A converted
car at its real-world torque lands at 251 Nm/t and, in the simulation,
0-60 takes about **12.9 s** -- roughly 2.5x the real car. The simulation's
torque-to-traction relationship is simply not the real one, so matching real
engineering figures produces a car that feels gutless no matter how correct the
numbers look on paper.

The honest calibration is the measured one: **~760 Nm/t**, which puts both the
Merc (2069 kg -> 1572 Nm) and the Pilot (2200 kg -> 1672 Nm) at a predicted
5.0 s. That is roughly **2.6x the stated torque** -- which is also why the
original 7.69x inflation felt merely "insane" rather than unplayable, and why
section 18's correction to real-world torque over-corrected.

### The interface bug that caused it

`--engine-torque NM` is absolute, so it silently assumes the mass you measured
was the mass you will ship. The flag is now `--engine-nm-per-tonne NM/T`,
computed from the kerb weight actually written to the chassis, not from the
`--type` preset. A test asserts the two can never disagree: it converts with
`--mass 2069` and checks the engine's peak torque equals
`760 * (mass_read_back_from_the_chassis) / 1000`.

Any future tuning number in this area must be expressed per unit mass or per
unit of something measured from the output file. An absolute number tuned by
hand is a guess wearing a decimal point.

## 22. Windows 11 audit

Requested explicitly. Most of the risk was already covered by AGENTS.md and
verified sound; the one real defect found was not platform-related at all.

### Verified already correct

- `ui.py` imports `termios`/`tty` only inside `try:` in
  `_raw_keys_available()`. Simulating their absence (import blocked) returns
  False and the numbered menu still answers, so the guided walkthrough
  survives.
- `logo.py` resolves ImageMagick through `shutil.which` and then **verifies**
  the candidate by running `-version`. `imagemagick()` correctly rejects
  `C:\Windows\System32\convert.exe` — the FAT→NTFS converter — when it is the
  only candidate on PATH.
- `_refresh_magick_path()` is gated on `os.name != "nt"`, looks for
  `magick.exe` under `C:\Program Files\ImageMagick*`, and prepends using
  `os.pathsep`. No hardcoded `":"` join anywhere.
- `winget` is the first entry in `_INSTALLERS`, so a Windows box is offered a
  command that exists. The others are inert there.
- Directory walks normalise with `.replace("\\", "/")`, so archive entry names
  never carry backslashes.
- No `shell=True` in any shipped module, and every `subprocess` call passes an
  argument list. On Windows a shell would select `cmd.exe`, whose quoting
  differs from POSIX and breaks paths containing spaces.
- No hardcoded `/tmp` in shipped code; `tempfile.mkdtemp()` is used instead.

### CRLF: checked, and it is a non-issue

Windows translates `\n` to `\r\n` in **text mode** only. Def units are written
into the ZIP as Python `str`, and `zipfile` encodes them to UTF-8 internally,
so no translation happens on any platform. Verified: generated defs contain no
`\r`. The 31 defs that *do* contain CRLF are the mod author's own files,
carried through verbatim byte-for-byte, which is the required behaviour.

### The real defect: the 760 Nm/t default was silently inert

Not a Windows bug, found by the simulated-Windows end-to-end run. `convert()`
passed the CLI's `engine_nm_per_tonne=None` straight through, which overrode
`tune_engine`'s own parameter default. Every conversion without an explicit
flag fell back to the mod's stated torque (833 Nm) instead of 760 Nm/t
(1672 Nm). The flag looked like it worked because passing it explicitly took a
different path.

Fixed by coalescing `None` to `_DEFAULT_NM_PER_TONNE` at the call site, with
`test_default_engine_target_applies` asserting the no-flag case. A default set
on a parameter is not a default if a caller can pass `None` over it — which is
exactly why the fix belongs at the boundary, not in the signature.

## 23. A single Nm/t constant was wrong, and the Pilot found it

Section 21 shipped 760 Nm/t, fitted from one car. It made the W223 correct
and the **Honda Pilot uncontrollable** -- not merely quick, but violent. Two
causes, and the second was invisible in the engine.

### Cause 1: stall_torque_ratio (the real culprit)

Launch force is `engine_torque x first_gear_total x stall_torque_ratio`,
because the torque converter multiplies engine torque at the stall speed:

| | stall ratio | first gear | launch force (arb.) |
|---|---|---|---|
| Mercedes (feels right) | 1.953 | 15.10 | 132454 |
| Pilot, as shipped | **4.50** | 17.49 | **375985** (2.84x) |
| Pilot, stall capped to 2.0 | 2.0 | 17.49 | 167104 (1.26x) |

`stall_torque_ratio: 4.50` is a diesel torque-converter figure. SCS's four
Road Trip transmissions use **1.8, 1.8, 1.82 and 2.0** -- never above 2.0. So
the converter was multiplying an already-corrected 1694 Nm by 2.30x. That is
why the Pilot felt unlike a fast car and unlike the Merc: no engine target can
compensate for a multiplier that large, which is also why tuning torque alone
had made it worse.

Both mods also omit three keys that all four SCS car transmissions declare:
`torque_converter_lock_gear`, `torque_converter_crawl` and
`transmission_class`. `tune_transmission` now caps the stall ratio at 2.0,
preserving anything already at or below it, and supplies the missing keys.

### Cause 2: one constant cannot fit two cars

760 Nm/t was right for a 2069 kg S-Class and wrong for a 2200 kg Pilot only
because the two cars have different launch multipliers. But the deeper lesson
is that a single constant fitted from one measurement cannot generalise at
all. The fix is to stop exporting a constant and start asking.

### Ask for the two numbers that actually determine it

The walkthrough now prompts for the target 0-60 after the kerb weight, and
`--acceleration SECONDS` does the same non-interactively. Torque follows from
the calibration in `torque_for_time()`:

    Nm = 520 x ((9.67 x mass/1550) / target)^(1/0.855)

Self-consistency check: asking for the 4.63 s that 760 Nm/t actually produced
returns 834 Nm/t, against the 760 that produced it -- within the error of the
exponent. Real-world torque is never the target; the user's own target time
is, and mass scales it explicitly.

The prompt is skipped entirely when stdin is not a terminal, so scripted runs
and CI cannot block.

## 24. Body roll: the tuning table was never calibrated (correction)

The converted cars rolled like boats. `_TUNING` and the `_PARATI` baseline had
been carried since section 8 and derived by *extrapolating a 1994 wagon*, not
by reading anything SCS ships. Measuring all four Road Trip chassis shows
**every preset was outside SCS's own range on at least one key**:

| key | our presets were | SCS cars actually use |
|---|---|---|
| `residual_travel` | 0.12 – **0.28** | 0.093 – **0.134** |
| `air_resistance` | 0.07 – 0.25 | 0.37 – 0.596 |
| `cog_height` | 0.32 – **0.80** | 0.52 – 0.738 |
| `suspension_damping_factor` | 0.8 – **1.2** | 0.5 – 0.8 |
| `sway_bar_stiffness` | 0.3 – **0.8** | 0.3 – 0.4 |

`residual_travel` is the roll bug. It is the suspension's free travel before
the springs engage, so 0.22 against a measured 0.10–0.11 is **more than double
the compliance** — the body free-flies before anything resists it. The Pilot's
chassis arrived carrying `0.10`, but the old threshold only replaced values
below 0.12 and the SUV preset wrote 0.22 over it anyway.

A second contribution: dropping the mod's `physics.sii` removed its
`sway_bar_stiffness_factor: 3.0`, so we now inherit SCS's 1.0. That was
correct on brakes and tyres but it halved the anti-roll the mod had been
granting, which made the roll change feel sudden rather than gradual.

### truck and car values overlap, so the threshold had to go

Truck mods ship `residual_travel` of 0.09–0.10. SCS's cars use 0.093–0.134.
**The ranges overlap**, so no threshold can separate them -- and the old
"below 0.12 means truck" rule was starting to flag SCS's *own* Mustang value
as a truck value. `residual_travel` is now written unconditionally.

### The table is now copied, not invented

Each preset is anchored to the SCS car closest to it in mass and character:
Mustang 67 (1323 kg) for sedan and sports, Victoria 06 (1873 kg) for classic
and muscle, Bronco 24 (2144 kg) for suv and offroad, F-150 23 for pickup. No
value can be out of range by construction, and
`test_car_physics_within_scs_ranges` asserts all 42 of them stay inside.

The type ordering is preserved where SCS supports it -- tall heavy cars drag
and roll most -- but two Parati-era assumptions are gone, because SCS's own
cars contradict them: the Mustang is *more* draggy than the Victoria, and the
sway bar spread across real cars is only 0.3–0.4.

## 25. Creep on selecting Drive, and using SCS's values rather than judgement

The cars pulled away to about 15 mph the instant Drive was selected from
neutral. The tempting explanation was the `torque_converter_crawl: true` this
work had just added, and that key was duly made opt-out -- on the reasoning
that "crawl" sounds like creeping. That was a guess, and the wrong one to act
on: **SCS's own four Road Trip transmissions all declare `torque_converter_crawl: true`**. A key that every one of the reference cars carries is a car value, and dropping it was tuning by vibes.

Restored. The real gap was elsewhere, and it is measurable.

### Every SCS engine curve starts at (0, 0). The mods' do not.

| engine | first curve point | fraction of peak at idle |
|---|---|---|
| SCS F-150 5.0 | **(0, 0)** then 1000 rpm @ 0.60 | 0.39 interpolated |
| SCS Mustang 289 | **(0, 0)** then 1000 rpm @ 0.39 | 0.31 |
| SCS Bronco 2.7 | **(0, 0)** then 1000 rpm @ 0.35 | 0.21 |
| SCS Victoria 4.6 | **(0, 0)** then 1000 rpm @ 0.47 | 0.38 |
| Honda Pilot mod | **750 rpm @ 0.47**, no origin | 0.47 |
| W223 mod | **500 rpm @ 0.43**, no origin | 0.43 |

Two separate faults. No `(0, 0)` origin at all, and the first point sitting
*at* idle rather than above it. After the torque rescale the Pilot carries
about 800 Nm at idle -- 3.6x the absolute idle torque of an SCS car of the same
mass. Through a 17.5:1 first gear that is exactly enough to walk the car off
the line.

`tune_engine` now adds the `(0, 0)` origin, matching all 181 SCS engine units.
The existing points are untouched, so the peak, its rev, the power band and the
shift points are all preserved -- only the missing origin is supplied.

### The lesson, which is the one that matters

`torque_converter_crawl` was removed on a plausible-sounding inference and
would have been re-added anyway. The evidence was already sitting in the
decoded DLC: four units, all declaring it. **When a value can be read off the
reference, do not infer it.** Section 18's drumroll about the Parati is the
opposite mistake and cost a day; this was the same mistake in miniature.

It is also a reminder that a name is not a mechanism. `crawl` describes the
SCS unit's intent; the torque comes from the engine curve, and that is where
the fault was.

## 26. Job availability: our tag names are not the game's names

"I can do jobs with pickups but not with some other kinds of car." The cause is
a vocabulary mismatch, and it is in the base game's `car_job` units, which are
readable like everything else.

All **149** car_job units across `def.scs` and `base_vehicle.scs` declare
`vehicle_types[]` drawn from exactly seven values:

    pickup, suv, sedan, muscle, veteran, sport, offroad

Our preset names are not that set. `VEHICLE_TYPES` contained `sports` and
`classic`, and SCS has never heard of either. A tag the game does not recognise
matches **no job at all**, so every `--type sports` and `--type classic` car was
built with a tag that silently did nothing. Pickups worked, which is why it
looked like a classification issue rather than a spelling one.

### It is not a uniform list either

Three distinct sets, and the split matters for which cargo a car can haul:

| `vehicle_types[]` | jobs |
|---|---|
| all seven | 52 |
| `offroad, pickup, sedan, suv` | 95 |
| `muscle, sport, veteran` | 2 (`car_job.prem_*`) |

The 95-job set is the heavy-cargo work -- `car_cargo.mach_parts`, `ore_smpl`,
`drillcores`, `pack_proto`. **`muscle`, `sport` and `veteran` are excluded from
it**, which is the one restriction section 17 was reaching for and could not
confirm offline. Those three types are in the premium jobs instead.

So a converted muscle or classic car does not take no jobs -- it takes a
narrower set, and only once its tag is one the game recognises at all.

### Fix

`job_tag()` maps preset name to the tag the game matches on (`sports` ->
`sport`, `classic` -> `veteran`); the rest already share their names. The
preset names stay, because they drive the physics tables and the menu.
`--type sport` and `--type veteran` are accepted as input aliases, and an
unrecognised tag now warns at conversion time instead of failing silently in
game.
