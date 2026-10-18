"""Turn a logo image into the DDS texture Car Mode expects.

The game does not read PNGs. Every brand logo in an ATS package is a triple of

    <dir>/<name>.dds    256x64 DXT1, full mip chain
    <dir>/<name>.mat    material : "ui" { texture : "<name>.tobj" ... }
    <dir>/<name>.tobj    binary; u32 length at offset 40, ASCII path at 44

The .dds is produced by ImageMagick, which is used here because it reads every
format these logos arrive in (PNG, WebP, SVG -- the files in logos/ are not all
actually PNGs despite the extension). When ImageMagick is unavailable the
caller falls back to a flat placeholder from carutil.
"""

from __future__ import annotations

import shutil
import subprocess

LOGO_W = 256
LOGO_H = 64

# Letterbox the logo onto the game's canvas rather than scaling to fill:
# logos carry wordmarks, and filling 4:1 would cut the ends off most of them.
_FIT = [
    "-background", "none",
    "-resize", f"{LOGO_W}x{LOGO_H}",
    "-gravity", "center",
    "-extent", f"{LOGO_W}x{LOGO_H}",
]

# Some logos are pure white on a fully transparent background, which renders as
# a blank tile. Composite those over a dark plate so they stay visible; images
# with their own opaque background are left alone.
_CHECK_BLANK = [
    "-alpha", "off",
    "-colorspace", "Gray",
    "-format", "%[fx:mean]",
    "info:",
]
# Mean alpha of the source. A fully opaque logo has its own painted background
# (often white), which must be dealt with rather than shown as a bright box.
_OPAQUE = [
    "-alpha", "extract",
    "-format", "%[fx:mean]",
    "info:",
]

# The dealer UI sits on a dark background, so artwork that is near-black once
# transparency is treated as black will not be visible. Composite those onto a
# dark plate so the light-coloured artwork reads against it.
_PLATE = [
    "-background", "#1a1a1a",
    "-alpha", "background",
    "-alpha", "off",
]

# For logos painted on an opaque white background, drop the white to
# transparency first and then plate, so only the mark survives.
_DROP_WHITE = [
    "-fuzz", "12%",
    "-transparent", "white",
]

# Artwork with almost no luminance of its own (pure black on transparent) would
# be invisible on the dealer's dark tile even once plated, because it stays
# dark on dark. Invert the colour channels only -- alpha must be preserved or
# the cut-out turns into a solid block -- so the mark reads as light artwork.
_INVERT = ["-channel", "RGB", "-negate", "+channel"]

# -define dds:compression=dxt1 selects DXT1. The trailing "-" makes ImageMagick
# write to stdout so nothing has to touch the filesystem.
_CONVERT = [
    "-colorspace", "sRGB",
    "dds:-",
]

# DXT5 is preferred over DXT1. DXT1 stores alpha as a single bit per 4x4 block,
# which snaps the antialiased edges of a logo logo-shaped cut-out to hard,
# blocky steps -- that is the "choppy" look. DXT5 keeps 8-bit alpha and a
# separate colour block, which renders far smoother for a 22 KB file. The game
# reads both (its own opaque logos are DXT1).
_COMPRESSION = ["-define", "dds:compression=dxt5"]
_FALLBACK_COMPRESSION = ["-define", "dds:compression=dxt1"]


class LogoError(Exception):
    """A logo image could not be converted."""


def imagemagick() -> str | None:
    """Path to a usable ImageMagick binary, or None if there isn't one.

    The binary must be verified, not just found. On Windows
    `shutil.which("convert")` happily returns C:\\Windows\\System32\\
    convert.exe, the FAT-to-NTFS filesystem converter, because it answers to
    the same name. Handing that to ImageMagick arguments exits 4 with a
    localised "invalid parameter" message, so every logo conversion fails
    with an error that names neither the real program nor the real cause.
    """
    for name in ("magick", "convert"):
        found = shutil.which(name)
        if found and _is_imagemagick(found):
            return found
    return None


def _is_imagemagick(exe: str) -> bool:
    """True if `exe` really is ImageMagick, identified by its version banner."""
    try:
        proc = subprocess.run(
            [exe, "-version"], capture_output=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    banner = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    return "ImageMagick" in banner


def _run(exe: str, args: list[str], image: bytes) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            [exe, "-", *args], input=image, capture_output=True, check=False
        )
    except OSError as exc:              # pragma: no cover - platform dependent
        raise LogoError(f"could not run ImageMagick: {exc}") from exc


def _mean(image: bytes, exe: str, args: list[str]) -> float | None:
    """Mean of an ImageMagick expression over the image, or None if unusable."""
    proc = _run(exe, [*args, "info:"], image)
    try:
        return float(proc.stdout.decode().strip())
    except ValueError:
        return None


def _has_painted_background(image: bytes, exe: str) -> bool:
    """True if the logo is fully opaque, i.e. carries its own background.

    A logo that is opaque everywhere has a painted backdrop (usually white)
    that would otherwise show as a bright rectangle in the dealer tile.
    """
    alpha = _mean(image, exe, ["-alpha", "extract", "-format", "%[fx:mean]"])
    return alpha is not None and alpha > 0.99


def _is_too_dark(image: bytes, exe: str) -> bool:
    """True if the artwork has essentially no luminance of its own.

    Measures luminance over an opaque black backdrop, so transparent pixels do
    not count. A logo that scores near zero is dark-on-transparent and would
    disappear against the dealer's dark tile even after being plated.
    """
    lum = _mean(image, exe, ["-background", "black", "-alpha", "background",
                             "-alpha", "off", "-colorspace", "Gray",
                             "-format", "%[fx:mean]"])
    return lum is not None and lum < 0.02


def _knock_out_white(image: bytes, exe: str) -> bytes:
    """Render the image to PNG with any white backdrop made transparent.

    Re-encoding is needed so the darkness test sees the *artwork* rather than
    the white page it was drawn on: a black logo printed on white measures as
    bright, and would be left black-on-transparent and invisible.
    """
    proc = _run(exe, [*_DROP_WHITE, "png:-"], image)
    if proc.returncode == 0 and proc.stdout.startswith(b"\x89PNG"):
        return proc.stdout
    return image


def image_to_dds(image: bytes) -> bytes:
    """Convert image data (PNG/WebP/SVG/...) to a 256x64 DDS.

    Prefers DXT5 for smooth edges; falls back to DXT1 if this build of
    ImageMagick cannot produce DXT5.

    Raises LogoError if ImageMagick is missing or the image cannot be read.
    """
    exe = imagemagick()
    if exe is None:
        raise LogoError(
            "ImageMagick is needed to convert logo images but was not found. "
            "Install it, or drop a pre-converted .dds into logos/."
        )

    # Three failure modes, all invisible or ugly against the dealer's dark tile:
    #   * fully opaque artwork -- knock the painted background out to alpha;
    #   * artwork with almost no luminance of its own (black-on-transparent) --
    #     invert the colour so the mark reads as light artwork;
    #   * anything still sitting on nothing -- composite onto a dark plate.
    #
    # Order matters: the invert must happen before the plate, or it would turn
    # the dark plate into a light box.
    prep: list[str] = []
    source = image
    if _has_painted_background(image, exe):
        source = _knock_out_white(image, exe)

    dark = _is_too_dark(source, exe)
    if dark:
        prep += _INVERT
    if dark or _has_painted_background(image, exe):
        prep += _PLATE

    for compression in (_COMPRESSION, _FALLBACK_COMPRESSION):
        proc = _run(exe, [*prep, *_FIT, *compression, *_CONVERT], source)
        if proc.returncode == 0 and proc.stdout.startswith(b"DDS "):
            return proc.stdout

    detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
    raise LogoError(
        "could not convert logo image: " + (detail[-1] if detail else "unknown error")
    )