#!/usr/bin/env python3
"""Swap a rebuilt Loader KIP and fusee into a copy of Atmosphère 1.11.2 package3.

    mkpkg3.py <base package3> <loader.kip> <fusee.bin> <program.bin> <out>

Layout follows fusee/build_package3.py: KIPs are repacked 16-byte aligned from 0x100000
(offsets + sizes in the content table, offset/size/sha256 in the KIP metas at 0x400);
fusee sits at 0x7C0000 (max 0x20000, zero padded); the two MTC overlays at 0x20000/0x34000
come from program.bin with a 4-byte sha256 prefix in place of their last 4 bytes.
Every other byte is kept from the base.
"""
import hashlib, struct, sys

base, loader_p, fusee_p, program_p, out = sys.argv[1:]
b = bytearray(open(base, "rb").read())
loader, fusee, program = (open(p, "rb").read() for p in (loader_p, fusee_p, program_p))
assert len(b) == 0x800000 and b[:4] == b"PK31"
assert loader[:4] == b"KIP1" and loader[4:16].split(b"\0")[0] == b"Loader"

count = struct.unpack_from("<I", b, 0x30)[0]
comps = {}
for i in range(count):
    h = 0x40 + 0x20 * i
    off, size = struct.unpack_from("<II", b, h)
    comps[bytes(b[h + 16:h + 32]).split(b"\0")[0]] = (h, off, size)

# Every KIP meta hash must hold in the base before we touch anything.
num_kips = struct.unpack_from("<I", b, 0x10)[0]
for i in range(num_kips + 1):
    m = 0x400 + 0x30 * i
    rel, size = struct.unpack_from("<II", b, m + 8)
    assert hashlib.sha256(b[0x100000 + rel:0x100000 + rel + size]).digest() == b[m + 16:m + 48]

# KIP region: emummc then the stratosphere KIPs in table order, Loader replaced.
order = [n for n, (h, off, size) in sorted(comps.items(), key=lambda kv: kv[1][1]) if 0x100000 <= off < 0x400000]
assert order[0] == b"emummc" and b"Loader" in order
data = {n: (loader if n == b"Loader" else bytes(b[comps[n][1]:comps[n][1] + comps[n][2]])) for n in order}
region = bytearray(b"\xcc" * 0x300000)
ofs = 0x100000
for i, n in enumerate(order):
    k = data[n]
    region[ofs - 0x100000:ofs - 0x100000 + len(k)] = k
    h = comps[n][0]
    struct.pack_into("<II", b, h, ofs, len(k))
    m = 0x400 + 0x30 * i  # meta i follows the same order: emummc, then KIP_NAMES
    assert bytes(b[m:m + 8]) == k[0x10:0x18], n
    struct.pack_into("<II", b, m + 8, ofs - 0x100000, len(k))
    b[m + 16:m + 48] = hashlib.sha256(k).digest()
    ofs = (ofs + len(k) + 0xF) & ~0xF
assert ofs <= 0x400000, "KIPs no longer fit"
b[0x100000:0x400000] = region

# fusee
h, off, _ = comps[b"fusee"]
assert off == 0x7C0000 and len(fusee) <= 0x20000
b[off:off + 0x20000] = fusee + b"\0" * (0x20000 - len(fusee))
struct.pack_into("<I", b, h + 4, len(fusee))

# MTC overlays from the rebuilt program
for i, at in ((1, 0x20000), (2, 0x34000)):
    ovl = program[0x2B000 + 0x14000 * i:0x2B000 + 0x14000 * (i + 1)]
    assert len(ovl) == 0x14000
    b[at:at + 0x14000] = ovl[:-4] + hashlib.sha256(ovl[:-4]).digest()[:4]

assert len(b) == 0x800000
open(out, "xb").write(b)
print("package3 sha256:", hashlib.sha256(b).hexdigest())
for n in order:
    h = comps[n][0]
    print(" ", n.decode(), "0x%x +0x%x" % struct.unpack_from("<II", b, h))
