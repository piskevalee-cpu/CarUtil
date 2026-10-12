# AGENTS.md

Guidance for AI agents working in this repository. Read this before editing
anything; most of it is knowledge that cost real debugging time to acquire.

## What this is

A converter that turns a truck-classified American Truck Simulator mod into a
Road Trip car so it appears in the Car Dealer and drives with car physics. One
Python 3.9+ entry point, standard library only. ImageMagick is optional and is
used only to convert brand logo images.

| File | Lines | Role |
|---|---|---|
| `convert_to_car.py` | ~1960 | The converter and CLI. Everything of substance lives here. |
| `sii.py` | ~237 | Parsing/editing helpers for SCS's `.sii` data format. |
| `brands.py` | ~92 | The brand list and the mapping to each logo file. |
| `logo.py` | ~195 | Image → 256×64 DDS conversion. Uses ImageMagick. |
| `ui.py` | ~192 | Banner, colour, arrow-key menus. Pure presentation. |
| `test_converter.py` | ~1780 | 565 checks. Run it before every commit. |
| `FINDINGS.md` | — | Research log. **Several early sections were wrong; see Corrections.** |
| `logos/` | — | 22 brand images. Not all are actually PNGs. |

## Commands

```bash
python3 test_converter.py                          # the whole suite
CARUTIL_FIXTURES=/path/to/mods python3 test_converter.py   # incl. fixture-backed checks
python3 convert_to_car.py                         # guided walkthrough
python3 convert_to_car.py mod.scs --type suv --brand porsche -o out.scs
```

Tests must be at **565 passing, 0 failing** before you commit. A fresh clone
reports skips, not failures, unless `CARUTIL_FIXTURES` is set — that is
expected, not a regression.

## Non-obvious constraints

**The test fixtures are third-party assets and must never be committed.** Real
car mods live in `../testing-covnverter/` and are not in the repo. `git add` is
already scoped to specific files, but do not widen it.

**`/tmp` is tmpfs on this machine — it is RAM, not disk.** Each conversion
writes a 100–200 MB archive. A dozen test runs filled `/tmp` to 4 GB and took
the machine down to 1.9 GB free RAM. Delete scratch output as you go. Check
with `df -h /tmp`.

**Do not trust the reference converter's self-reported passes.** Its
`job-tag ... matches car_job vehicle_types[]` line checks a hard-coded list, not
the base game. Its own summary still carries `UNVERIFIED 1`. Matching it
byte-for-byte proves nothing about in-game behaviour.

**The base game's `def.scs` / `base.scs` are SCS# v2 and unreadable here.**
Magic is `53 43 53 23`. There is no offline way to confirm what the game
accepts — that needs an in-game test or an SCS-aware extractor.

## Corrections to prior conclusions

This project has been wrong repeatedly in ways that cost hours. Don't repeat
them, and don't treat `FINDINGS.md` §1–§15 as authoritative.

- **`car_job` / `tags[]` is not about job visibility.** Quick Jobs *lend you a
  vehicle* by design. Your own car belongs in the **Dispatch Market**, once
  owned. `tags[]` decides which *cargo* a car may haul, and only `muscle`,
  `classic` and `sports` are restricted. A converted car missing from Quick
  Jobs is working correctly. See `FINDINGS.md` §17.
- **Game file layout is not textbook.** The logo DDS reads its pixel-format
  block at **offset 76**, not 32; `dwCaps` is `0x00401008`. The `.tobj` stores
  its length as u32 at offset 40, has **four padding bytes at 44–47**, and the
  string starts at **48**, beginning with a **leading slash**. Getting any of
  these wrong yields `Failed reading map name from file ...`.
- **Use DXT5 for logo textures, not DXT1.** DXT1 keeps one alpha bit per 4×4
  block, which makes a logo's antialiased edges visibly blocky. The game's own
  logos are DXT1 only because they are opaque; ours are transparent cut-outs.
- **Logo sources need normalising before conversion.** Three cases, handled in
  `logo.py`: opaque artwork (knock the painted background out to alpha),
  black-on-transparent (invert the colour channels, then plate), and
  white-on-transparent (plate only). **Order matters** — inverting after
  compositing inverts the plate and yields a light box. The darkness test must
  run on the *knocked-out* image; measuring the original sees the white page
  and concludes a black logo is bright.
- **`reference/tool_output/` and `*.scs` are gitignored.** Do not re-add them.

## Conventions

- **Docstrings explain *why*, not *what*.** Comments that restate the code get
  deleted. The comments that survive explain an SCS quirk or a rejected
  alternative.
- **New behaviour gets a test in the same commit.** `test_converter.py` covers
  byte-level formats, so regressions in DDS headers, `.tobj` offsets or ZIP
  conventions are caught immediately.
- **Prefer measuring to assuming.** Several bugs here were found by dumping
  the game's own files and comparing, not by reasoning about what "should" be
  right. When format details matter, read a working file.
- **The README quotes the banner;** a test asserts they match. If you change the
  art, update both.
- `ui.py` output must stay clean when piped: no ANSI when stdout is not a tty,
  and `NO_COLOR` must beat `FORCE_COLOR`.

## Before committing

```bash
python3 test_converter.py          # 565 pass, 0 fail
git status --short                 # only intended files
git diff --cached --stat           # sanity-check the size
```

Push to `main`. Pushes occasionally fail once with a bare "remote rejected" and
succeed on an unchanged retry; that is transient, not a repo problem.