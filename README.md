# CarUtil

Convert a truck-classified American Truck Simulator (ATS) mod into a Road Trip
car mod. CarUtil updates the vehicle definitions and dealer bindings, then
writes a new `.scs` archive.

## Quick start

1. Download the repository as a ZIP and extract it.
2. Open the extracted `CarUtil` folder.
3. Start the launcher for your computer:
   - **Windows:** double-click `carutil.bat`.
   - **macOS:** open Terminal in the folder and run `bash carutil.sh`.
   - **Linux:** open Terminal in the folder and run `bash carutil.sh`.

The launcher checks for Python and the required menu packages, sets up
CarUtil's private environment the first time, then starts the guided
walkthrough. If the menu packages are missing, it asks before installing them;
press Enter to accept the default. Declining still starts CarUtil with its
built-in numbered menus. Later runs skip setup when everything is already
installed. If Python is missing, the launcher offers to install it where a
supported installer is available. On Linux, it can also offer to install
Pipx; if you decline or Pipx is unavailable, it uses Python's built-in virtual
environment instead.

Before starting CarUtil, both launchers check for ImageMagick and offer to
install it (press Enter to accept). It is optional: declining or an
unavailable installer does not stop conversion; CarUtil keeps the mod's logo
or uses a placeholder. On Linux, automatic package installation supports
Debian/Ubuntu (`apt`), Fedora (`dnf`/`yum`), Arch (`pacman`), openSUSE
(`zypper`), and Alpine (`apk`). macOS uses Homebrew, and Windows uses `winget`.

Installing Python, Pipx, ImageMagick, or the menu packages needs an internet
connection. Install prompts show what will be installed; system-package
installs may ask for your computer's administrator password. If a required
tool is not available for automatic installation, the launcher prints the
next step to take.

For manual setup, install the packages from `requirements.txt` in a Python
virtual environment and run `carutil.py` with that environment's Python.
The launchers also check and offer to install ImageMagick; when running
`carutil.py` directly, ImageMagick remains optional and is not installed by
CarUtil.

It asks for the mod, vehicle type, brand, weight, engine target, rev limit and
price, then writes the converted archive next to the input. In guided mode,
`--mass` does not skip the weight prompt; `--power` changes the engine question
to a 0–60 target. `--acceleration`, `--rev-limit` and `--price` skip their
respective prompts. To avoid all prompts, pass `--no-prompt` and provide the
input, `--type` and `--brand`:

```bash
python3 carutil.py "mods/my-car.scs" \
    --no-prompt --type suv --brand porsche --mass 2069 \
    --power 300 --peak-power-rpm 5000 --rev-limit 6500 \
    --price 45000 -o my-car-roadtrip.scs
```

## Vehicle type

`--type` sets the handling preset, default weight, and the cargo categories the
car can use. Choose the closest fit; the weight is only a starting point, so
enter the vehicle's real weight with `--mass` when you know it.

| Type | Description | Default weight |
|---|---|---|
| `pickup` | pickup truck | 2400 kg |
| `offroad` | off-road / 4x4 | 2200 kg |
| `suv` | crossover / SUV | 2200 kg |
| `muscle` | muscle car | 1700 kg |
| `classic` | classic car | 1550 kg |
| `sedan` | saloon / hatchback | 1220 kg |
| `sports` | sports car | 1280 kg |

The game matches cargo using its own tags: `pickup`, `suv`, `sedan`, `muscle`,
`veteran`, `sport`, and `offroad`. CarUtil writes `sports` as `sport` and
`classic` as `veteran`. `tags[]` must spell the type the way the base game does.
Use `--extra-tags pickup,sedan` to add more cargo categories.

Your own car does not appear in Quick Jobs; that mode lends you a vehicle.
Converted cars are used in the Dispatch Market once you own them. The tags
control which cargo jobs your car can take.

## Common options

| Option | Purpose |
|---|---|
| `--type TYPE` | Vehicle category. Required when using `--no-prompt`. |
| `--brand BRAND` | Manufacturer shown in the Car Dealer. Required when using `--no-prompt`. |
| `--mass KG` | Set total kerb weight, for example `2069`, or axle weights, for example `1150,1050`. |
| `--power HP` | Set target peak engine power. |
| `--acceleration SECONDS` | Set the target 0–60 mph time. |
| `--peak-power-rpm RPM` | Set the engine's peak-power RPM. |
| `--rev-limit RPM` | Set the maximum engine RPM. |
| `--shift-up-rpm RPM` | Set the automatic transmission's upshift RPM. |
| `--air-resistance COEFF` | Adjust drag to affect top speed and high-speed acceleration. |
| `--price DOLLARS` | Scale the mod's full accessory price table to the requested dealer price. |
| `--extra-tags TYPES` | Add comma-separated cargo categories. |
| `--no-prompt` | Disable the guided prompts. Supply the input, type and brand. |
| `-o, --output FILE` | Choose the output `.scs` path. By default it is written next to the input. |
| `--dry-run` | Report planned changes without writing an archive. |
| `--path` | Show the detected ATS game and user-data folders, then exit. |

Other options are available for special cases:

- `--slug` and `--asset-root` override the vehicle identifier and model path.
- `--no-car-tune`, `--force-physics`, and `--keep-physics` control physics edits.
- `--engine-torque` and `--engine-nm-per-tonne` provide alternative engine
  targets; `--power` or `--acceleration` is usually easier to specify.
- `--password` opens a genuinely password-protected input archive.
  `--strict-encryption` refuses archives whose index claims encryption even
  when their file data is readable.
- `--brand-logo` supplies a specific logo image; `--brand-logo-dir` looks for
  the selected brand's image in another directory.
- Repeat `--mod-dir DIR` to specify ATS user-data locations if automatic
  detection does not find yours.

Run `python3 carutil.py --help` for the complete command-line reference.

## Input and output

CarUtil accepts a mod folder or a plain-ZIP `.scs` archive. It can also read a
`.zip` containing one mod archive, or a folder containing a split mod's
definition archive and SCS HashFS archive.

The output is an `.scs` file. Copy it into the ATS `mod` folder; use
`python3 carutil.py --path` to see the detected location. If automatic
detection misses your folders, use `--mod-dir`.

CarUtil can bind parts that the source mod contains, but it cannot create
missing models, textures, wheels, or other assets. Review the conversion
summary's **warnings** and **gaps** before launching the game. A complete car
also requires access to Car Mode, which is unlocked by owning a Road Trip
vehicle DLC.

The bundled brand logos are stored in `logos/`. ImageMagick is needed to
convert logos into the game's format; if it is unavailable, CarUtil skips logo
replacement and uses the mod's logo or a placeholder. An explicitly provided
`--brand-logo` requires ImageMagick. There are 23 listed brands, including
`other`.

## Tests

Run the test suite with:

```bash
python3 test_converter.py
```

The suite runs without third-party mod files. Tests that need fixtures are
skipped unless `CARUTIL_FIXTURES` points to a directory containing them:

```bash
CARUTIL_FIXTURES=/path/to/mods python3 test_converter.py
```

Fixture assets are not included in this repository.

## CLI banner

The guided run displays:

```
  _____           _    _ _   _ _
 / ____|         | |  | | | (_) |
| |     __ _ _ __| |  | | |_ _| |
| |    / _` | '__| |  | | __| | |
| |___| (_| | |  | |__| | |_| | |
 \_____\__,_|_|   \____/ \__|_|_|

        CarUtil -- ATS Road Trip car converter
```
