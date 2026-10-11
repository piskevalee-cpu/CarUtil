"""Turn a logo image into the DDS texture Car Mode expects.

The game does not read PNGs. Every brand logo in an ATS package is a triple of

    <dir>/<name>.dds    256x64 DXT1, full mip chain
    <dir>/<name>.mat    material : "ui" { texture : "<name>.tobj" ... }
    <dir>/<name>.tobj    binary; u32 length at offset 40, ASCII path at 44

The .dds is produced by ImageMagick, which is used here because it reads every
format these logos arrive in (PNG, WebP, SVG -- the files in logos/ are not all
actually PNGs despite the extension). When ImageMagick is unavailable the
caller falls back to a flat placeholder from convert_to_car.
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
# Composite white-on-transparent artwork onto a dark plate so it stays visible.
_PLATE = [
    "-background", "#1a1a1a",
    "-alpha", "background",
    "-alpha", "off",
]

# -define dds:compression=dxt1 selects DXT1. The trailing "-" makes ImageMagick
# write to stdout so nothing has to touch the filesystem.
_CONVERT = [
    "-define", "dds:compression=dxt1",
    "-colorspace", "sRGB",
    "dds:-",
]


class LogoError(Exception):
    """A logo image could not be converted."""


def imagemagick() -> str | None:
    """Path to a usable ImageMagick binary, or None if there isn't one."""
    for name in ("magick", "convert"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _run(exe: str, args: list[str], image: bytes) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            [exe, "-", *args], input=image, capture_output=True, check=False
        )
    except OSError as exc:              # pragma: no cover - platform dependent
        raise LogoError(f"could not run ImageMagick: {exc}") from exc


def _is_blank(image: bytes, exe: str) -> bool:
    """True if the logo would be invisible on a transparent background.

    Measures the image's own luminance with alpha ignored. Several logos are
    pure white artwork on a fully transparent background: their luminance is
    0 once transparent pixels are treated as black, yet they are perfectly
    visible on the dark UI. Anything in that bucket gets composited onto a
    plate instead of being letterboxed into invisibility.
    """
    gray = _run(exe, _CHECK_BLANK, image)
    try:
        return float(gray.stdout.decode().strip() or 0) < 0.01
    except ValueError:
        return False


def image_to_dds(image: bytes) -> bytes:
    """Convert image data (PNG/WebP/SVG/...) to a 256x64 DXT1 DDS.

    Raises LogoError if ImageMagick is missing or the image cannot be read.
    """
    exe = imagemagick()
    if exe is None:
        raise LogoError(
            "ImageMagick is needed to convert logo images but was not found. "
            "Install it, or drop a pre-converted .dds into logos/."
        )

    if _is_blank(image, exe):
        proc = _run(exe, [*_PLATE, *_FIT, *_CONVERT], image)
    else:
        proc = _run(exe, [*_FIT, *_CONVERT], image)

    if proc.returncode != 0 or not proc.stdout.startswith(b"DDS "):
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise LogoError(
            "could not convert logo image: " + (detail[-1] if detail else "unknown error")
        )
    return proc.stdout