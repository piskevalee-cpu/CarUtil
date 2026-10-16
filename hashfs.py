"""Reader for SCS's own HashFS container (the `SCS#` format).

Some mods ship their files as two archives: the definitions in a plain ZIP this
project has always read, and everything else -- data.sii, the accessory
definitions, the geometry, textures and sounds -- in a HashFS container, which
is the format the game itself uses. The container is not encrypted (FINDINGS
§1 called it opaque and said so, which was wrong), it is just not a ZIP.

What is worth knowing before reading this file:

**A HashFS entry is content-addressed by CityHash64 of its full path, so a
file can be fetched by path but the archive cannot be *listed* from the entry
table.** Traversal works only because archives also carry directory listings:
entries whose own path is a directory, containing one name per line with `*`
marking a subdirectory. Mods may omit the top-level listing precisely to stop
extractors, so `paths()` returns what can be reached and the caller must not
assume that is everything.

**The hash is not Google's CityHash64.** It is the variant in cityhash-c
(Alexander Nusov's port), which differs in the short and medium length paths
and introduces a fourth constant. Both implementations agree that the empty
string hashes to 0x9ae16a3b2f90404f, which is exactly why a port of the
Google original produces values that match nothing at all. Verified against
upstream's own vector: hash_path("/kasefondue.txt") == 0x77f9c4f5eca06d54.

**`StartOffset` is a u64.** Reading the entry table's first field as a u32
lands 8 bytes early, which shifts every record by half a field: the payload
offsets still look plausible and still decompress, and the hash column comes
out as the *next* entry's hash. Every wrong conclusion about this format
traced back to that one field width.

Some entries are deliberately corrupted by a DRM packer -- the payload is a
truncated deflate stream, not a valid one. They are reported by `damaged()`
rather than silently skipped, because a mod that lost five definitions is a
different problem from a mod that never had them.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

MAGIC = b"SCS#"
_ENTRY_SIZE = 32
_DIR_MARKER = "*"

_K0 = 0xC3A5C85C97CB3127
_K1 = 0xB492B66FBE98F273
_K2 = 0x9AE16A3B2F90404F
_K3 = 0xC949D7C7509E6557
_KMUL = 0x9DDFEA08EB382D69
_M64 = (1 << 64) - 1

# Flags, as written into the entry table.
_FLAG_DIRECTORY = 1
_FLAG_COMPRESSED = 2


class HashFsError(Exception):
    """The file is not a HashFS container, or its table is unreadable."""


class HashFsDamaged(Exception):
    """An entry exists but its payload will not decompress or fails its CRC.

    This is what a DRM packer's mangled entries look like: the entry is present
    and its path hashes correctly, but the bytes are a truncated deflate stream
    with no valid end. There is no offset or alignment that recovers them.
    """


def _rot(value: int, shift: int) -> int:
    if shift == 0:
        return value & _M64
    return ((value >> shift) | (value << (64 - shift))) & _M64


def _rot_nz(value: int, shift: int) -> int:
    """`_rot` for a shift that is never zero, so the C# `RotateByAtleast1`
    guard does not change the result."""
    return ((value >> shift) | (value << (64 - shift))) & _M64


def _shift_mix(value: int) -> int:
    return (value ^ (value >> 47)) & _M64


def _f32(data: bytes, pos: int) -> int:
    return int.from_bytes(data[pos:pos + 4], "little")


def _f64(data: bytes, pos: int) -> int:
    return int.from_bytes(data[pos:pos + 8], "little")


def _hash128_to64(low: int, high: int) -> int:
    a = ((low ^ high) * _KMUL) & _M64
    a ^= a >> 47
    b = ((high ^ a) * _KMUL) & _M64
    b ^= b >> 47
    return (b * _KMUL) & _M64


def _hash_len16(u: int, v: int) -> int:
    return _hash128_to64(u, v)


def _hash_len0to16(data: bytes, length: int) -> int:
    if length > 8:
        a = _f64(data, 0)
        b = _f64(data, length - 8)
        return _hash_len16(a, _rot_nz((b + length) & _M64, length)) ^ b
    if length >= 4:
        a = _f32(data, 0)
        return _hash_len16((length + (a << 3)) & _M64, _f32(data, length - 4))
    if length > 0:
        first = data[0]
        middle = data[length >> 1]
        last = data[length - 1]
        y = (first + (middle << 8)) & _M64
        z = (length + (last << 2)) & _M64
        return (_shift_mix(((y * _K2) & _M64) ^ ((z * _K3) & _M64)) * _K2) & _M64
    return _K2


def _hash_len17to32(data: bytes, length: int) -> int:
    a = (_f64(data, 0) * _K1) & _M64
    b = _f64(data, 8)
    c = (_f64(data, length - 8) * _K2) & _M64
    d = (_f64(data, length - 16) * _K0) & _M64
    return _hash_len16((_rot((a - b) & _M64, 43) + _rot(c, 30) + d) & _M64,
                       (a + _rot((b ^ _K3) & _M64, 20) - c + length) & _M64)


def _hash_len33to64(data: bytes, length: int) -> int:
    z = _f64(data, 24)
    a = (_f64(data, 0) + (((length + _f64(data, length - 16)) & _M64) * _K0)) & _M64
    b = _rot((a + z) & _M64, 52)
    c = _rot(a, 37)
    a = (a + _f64(data, 8)) & _M64
    c = (c + _rot(a, 7)) & _M64
    a = (a + _f64(data, 16)) & _M64
    vf = (a + z) & _M64
    vs = (b + _rot(a, 31) + c) & _M64
    a = (_f64(data, 16) + _f64(data, length - 32)) & _M64
    z = _f64(data, length - 8)
    b = _rot((a + z) & _M64, 52)
    c = _rot(a, 37)
    a = (a + _f64(data, length - 24)) & _M64
    c = (c + _rot(a, 7)) & _M64
    a = (a + _f64(data, length - 16)) & _M64
    wf = (a + z) & _M64
    ws = (b + _rot(a, 31) + c) & _M64
    r = _shift_mix((((vf + ws) & _M64) * _K2) & _M64
                   + ((((wf + vs) & _M64) * _K0) & _M64))
    return (_shift_mix((((r * _K0) & _M64) + vs) & _M64) * _K2) & _M64


def _weak_hash32(w: int, x: int, y: int, z: int, a: int, b: int) -> tuple[int, int]:
    a = (a + w) & _M64
    b = _rot((b + a + z) & _M64, 21)
    c = a
    a = (a + x) & _M64
    a = (a + y) & _M64
    b = (b + _rot(a, 44)) & _M64
    return (a + z) & _M64, (b + c) & _M64


def _weak_hash_at(data: bytes, pos: int, a: int, b: int) -> tuple[int, int]:
    return _weak_hash32(_f64(data, pos), _f64(data, pos + 8),
                        _f64(data, pos + 16), _f64(data, pos + 24), a, b)


def city_hash64(data: bytes) -> int:
    """The 64-bit hash HashFS uses, which is not Google's CityHash64.

    This is the cityhash-c variant: see the module docstring for why the
    Google original gives answers that match nothing.
    """
    length = len(data)
    if length <= 16:
        return _hash_len0to16(data, length)
    if length <= 32:
        return _hash_len17to32(data, length)
    if length <= 64:
        return _hash_len33to64(data, length)
    x = _f64(data, length - 40)
    y = (_f64(data, length - 16) + _f64(data, length - 56)) & _M64
    z = _hash_len16((_f64(data, length - 48) + length) & _M64,
                    _f64(data, length - 24))
    v = _weak_hash_at(data, length - 64, length, z)
    w = _weak_hash_at(data, length - 32, (y + _K1) & _M64, x)
    x = ((x * _K1) + _f64(data, 0)) & _M64
    pos = 0
    remaining = (length - 1) & ~63
    while True:
        x = (_rot((x + y + v[0] + _f64(data, pos + 8)) & _M64, 37) * _K1) & _M64
        y = (_rot((y + v[1] + _f64(data, pos + 48)) & _M64, 42) * _K1) & _M64
        x ^= w[1]
        y = (y + v[0] + _f64(data, pos + 40)) & _M64
        z = (_rot((z + w[0]) & _M64, 33) * _K1) & _M64
        v = _weak_hash_at(data, pos, (v[1] * _K1) & _M64, (x + w[0]) & _M64)
        w = _weak_hash_at(data, pos + 32, (z + w[1]) & _M64,
                          (y + _f64(data, pos + 16)) & _M64)
        z, x = x, z
        pos += 64
        remaining -= 64
        if remaining == 0:
            break
    first = (((_hash_len16(v[0], w[0]) + (_shift_mix(y) * _K1)) & _M64) + z) & _M64
    second = ((_hash_len16(v[1], w[1]) + x) & _M64)
    return _hash_len16(first, second)


def hash_path(path: str, salt: int = 0) -> int:
    """The identifier HashFS stores for `path`.

    The leading slash is stripped, which is why the root directory's listing
    hashes to the same value as the empty string. A non-zero salt is
    *prepended as decimal text*, not mixed into the hash -- ModGuard archives
    rely on that to break naive extractors.
    """
    if path.startswith("/"):
        path = path[1:]
    if salt:
        path = f"{salt}{path}"
    return city_hash64(path.encode("utf-8"))


class Entry:
    __slots__ = ("hash", "offset", "flags", "crc", "size", "compressed_size")

    def __init__(self, hash: int, offset: int, flags: int, crc: int,
                 size: int, compressed_size: int) -> None:
        self.hash = hash
        self.offset = offset
        self.flags = flags
        self.crc = crc
        self.size = size
        self.compressed_size = compressed_size

    @property
    def is_directory(self) -> bool:
        return bool(self.flags & _FLAG_DIRECTORY)

    @property
    def is_compressed(self) -> bool:
        return bool(self.flags & _FLAG_COMPRESSED)

    def __repr__(self) -> str:
        return (f"Entry({self.hash:016x}, offset={self.offset}, "
                f"size={self.size}, compressed={self.compressed_size})")


class HashFsArchive:
    """A HashFS v1 container, opened for reading.

    v2 exists as well (game 1.50+) but packs textures differently and is not
    handled here; `open()` reports the version it found so a caller can say so
    rather than reading garbage.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.version: int
        self.salt: int
        self.hash_method: str
        self._entries: dict[int, Entry] = {}
        self._damaged: dict[int, Entry] = {}
        with open(self.path, "rb") as handle:
            head = handle.read(24)
            if len(head) < 24 or head[:4] != MAGIC:
                raise HashFsError(f"{self.path}: not a HashFS container")
            self.version, self.salt = struct.unpack_from("<HH", head, 4)
            self.hash_method = head[8:12].decode("ascii", "replace")
            if self.version != 1:
                raise HashFsError(
                    f"{self.path}: HashFS v{self.version} is not supported "
                    "(only v1 is implemented)")
            count = struct.unpack_from("<I", head, 12)[0]
            # A u64. See the module docstring.
            table_start = struct.unpack_from("<Q", head, 16)[0]
            handle.seek(table_start)
            table = handle.read(count * _ENTRY_SIZE)
        if len(table) < count * _ENTRY_SIZE:
            raise HashFsError(f"{self.path}: entry table is truncated")
        for i in range(count):
            (h, offset, flags, crc, size, csize) = struct.unpack_from(
                "<QQIIII", table, i * _ENTRY_SIZE)
            entry = Entry(h, offset, flags, crc, size, csize)
            # A directory listing may be fragmented across several entries with
            # the same hash; keep the first, as the reference reader does.
            self._entries.setdefault(h, entry)

    # ---------------------------------------------------------------- reading
    def entry(self, path: str) -> Entry | None:
        return self._entries.get(hash_path(path, self.salt))

    def has(self, path: str) -> bool:
        return hash_path(path, self.salt) in self._entries

    def read(self, path: str) -> bytes:
        """The bytes of `path`, or None when the archive does not have it.

        Raises HashFsDamaged when the entry exists but its payload is
        corrupt, which is a different answer from "not here" and the caller
        should not confuse the two.
        """
        h = hash_path(path, self.salt)
        entry = self._entries.get(h)
        if entry is None:
            return None
        with open(self.path, "rb") as handle:
            handle.seek(entry.offset)
            raw = handle.read(entry.compressed_size)
        try:
            data = (raw if not entry.is_compressed
                    else zlib.decompress(raw))
        except zlib.error as exc:
            raise HashFsDamaged(
                f"{path}: payload is not a valid deflate stream ({exc})") from exc
        if len(data) != entry.size or (zlib.crc32(data) & 0xFFFFFFFF) != entry.crc:
            raise HashFsDamaged(
                f"{path}: payload does not match its recorded size or CRC")
        return data

    def damaged(self) -> list[str]:
        """Paths whose entry exists but whose payload is corrupt."""
        return sorted(self._damaged)

    # ------------------------------------------------------------- traversal
    def listing(self, directory: str) -> tuple[list[str], list[str]]:
        """The files and subdirectories of `directory`.

        A directory listing is an ordinary entry whose path is the directory's,
        so it is fetched by hashing the directory path. The root is `""` rather
        than `/`, because the leading slash is stripped before hashing and both
        must name the same entry.
        """
        key = directory.strip("/")
        data = self.read(key)
        if data is None:
            return ([], [])
        files: list[str] = []
        dirs: list[str] = []
        for line in data.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith(_DIR_MARKER):
                dirs.append(line[1:])
            else:
                files.append(line)
        return (files, dirs)

    def paths(self, start: str = "/") -> dict[str, str]:
        """Every file reachable from `start`, as {path: status}.

        Status is "ok", "damaged" (the entry exists and its payload is
        corrupt) or "missing" (a directory listing names it, but the packer
        left no entry behind -- DRM packers do this deliberately, and it is why
        a listed file cannot be assumed to exist).

        Reachability, not completeness: a directory whose listing is itself
        unreadable hides its whole subtree, which is how one bad file can cost a
        mod a hundred more. Those directories are recorded in `damaged()` too.
        """
        found: dict[str, str] = {}
        self._damaged = {}
        queue = [start.strip("/")]
        seen: set[str] = set()
        while queue:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            try:
                files, dirs = self.listing(current)
            except HashFsDamaged:
                self._damaged[current or "/"] = True
                continue
            for name in dirs:
                queue.append(((current + "/" + name).strip("/")))
            for name in files:
                path = ((current + "/" + name).strip("/")) or name
                if not self.has(path):
                    # Listed, but there is no entry for it. Reporting it as
                    # present is how a caller ends up writing a definition
                    # built from a file that does not exist.
                    found[path] = "missing"
                    continue
                try:
                    self.read(path)
                except HashFsDamaged:
                    self._damaged[path] = True
                    found[path] = "damaged"
                else:
                    found[path] = "ok"
        return found


def is_hashfs(path: Path) -> bool:
    """True when `path` starts with the HashFS magic."""
    try:
        with open(path, "rb") as handle:
            return handle.read(4) == MAGIC
    except OSError:
        return False


def version_of(path: Path) -> int | None:
    """The HashFS version of `path`, or None if it is not a container."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(6)
    except OSError:
        return None
    if len(head) < 6 or head[:4] != MAGIC:
        return None
    return struct.unpack_from("<H", head, 4)[0]