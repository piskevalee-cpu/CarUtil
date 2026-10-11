"""The brands the Car Dealer offers, and the logo file each one uses.

`name` is what the user types or picks; `logo` is the stem of the image in
logos/. They differ for a few brands because the logo files were collected
under shorter names (mercedes -> merc, chevrolet -> chevy, and so on).

A brand whose logo is missing is not an error: build_brand_logo falls back to a
flat placeholder, and lookups that match nothing fall back to "other".
"""

from __future__ import annotations

import re
from pathlib import Path

# (name, label shown in the menu, logo stem)
BRANDS: list[tuple[str, str, str]] = [
    ("audi",        "Audi",          "audi"),
    ("bmw",         "BMW",           "bmw"),
    ("cadillac",    "Cadillac",      "cadillac"),
    ("chevrolet",   "Chevrolet",     "chevy"),
    ("corvette",    "Corvette",      "corvette"),
    ("dodge",       "Dodge",         "dodge"),
    ("ferrari",     "Ferrari",       "ferrari"),
    ("ford",        "Ford",          "ford"),
    ("honda",       "Honda",         "honda"),
    ("jeep",        "Jeep",          "jeep"),
    ("lamborghini", "Lamborghini",   "lambo"),
    ("landrover",   "Land Rover",    "landrover"),
    ("mazda",       "Mazda",         "mazda"),
    ("mercedes",    "Mercedes-Benz", "merc"),
    ("nissan",      "Nissan",        "nissan"),
    ("other",       "Other",         "other"),
    ("porsche",     "Porsche",       "porsche"),
    ("ram",         "Ram",           "ram"),
    ("renault",     "Renault",       "renault"),
    ("subaru",      "Subaru",        "subaru"),
    ("tesla",       "Tesla",         "tesla"),
    ("toyota",      "Toyota",        "toyota"),
]

# Friendly spellings mapped onto a canonical brand name.
ALIASES = {
    "mercedes-benz": "mercedes",
    "mercedesbenz": "mercedes",
    "merc": "mercedes",
    "vw": "volkswagen",
    "chevy": "chevrolet",
    "lambo": "lamborghini",
    "land-rover": "landrover",
    "rover": "landrover",
    "range-rover": "landrover",
    "generic": "other",
    "none": "other",
}

_LOGO_DIR = Path(__file__).resolve().parent / "logos"


def normalise(raw: str) -> str:
    """Map a user-supplied brand onto a canonical name, lower-cased."""
    key = re.sub(r"[^a-z0-9]+", "", (raw or "").strip().lower())
    return ALIASES.get(key, key)


def logo_path(brand: str) -> Path | None:
    """Path to the logo image for a brand, or None if it ships none."""
    stem = _logo_stem(brand)
    if stem is None:
        return None
    for ext in (".png", ".webp", ".svg", ".jpg", ".jpeg", ".dds"):
        candidate = _LOGO_DIR / f"{stem}{ext}"
        if candidate.exists():
            return candidate
    return None


def _logo_stem(brand: str) -> str | None:
    key = normalise(brand)
    for name, _label, stem in BRANDS:
        if name == key:
            return stem
    return None


def available() -> list[str]:
    """Brand names that actually have a logo image on disk."""
    return [n for n, _l, _s in BRANDS if logo_path(n) is not None]


def missing() -> list[str]:
    """Brand names with no logo image."""
    return [n for n, _l, _s in BRANDS if logo_path(n) is None]