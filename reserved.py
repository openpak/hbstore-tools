#!/usr/bin/env python3
"""Control packages for the platform pseudo-titles ("Harbor", "Pier", "PS3", ...).

A friend's presence names a game by its id. A console that does not have the game asks atum for
the title's control NCA (name and icon) and draws what it gets, so a friend who is live on Pier can
be listed as playing a title called "Pier" with Pier's picture. These ids are not in the eShop:
nx-shop serves each file from $NX_DATA/shop/reserved/<id>.nca outside the catalog, so they appear
in no storefront, update list or library.

    reserved.py build [key ...]    write <out>/<id>.nca for each platform (default: all)
    reserved.py list               print the ids

Pictures: icons/<key>.png (or .jpg) next to this file is used when present, resized to 256x256;
otherwise a name tile is drawn. <out> is $HBSTORE_DATA/reserved (default scratch/hbstore/reserved).
The ids are 01fe00ff<index>000: inside the application range, in the 01fe tag the OpenPak boot
package already lets through, and clear of the store's own ids (01fe + a hash).
"""
import hashlib, os, shutil, struct, subprocess, sys

import hb2nsp

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(hb2nsp.DATA, "reserved")
ICONS = os.path.join(HERE, "icons")

# key (what a presence client reports, or its namespace for a real console), name, tile colour.
# The order is the id: never reorder, only append.
PLATFORMS = [
    ("switch", "Switch", "#e60012"), ("harbor", "Harbor", "#0a6fb5"), ("ryujinx", "Ryujinx", "#2f6f9f"),
    ("eden", "Eden", "#4b8a3a"), ("citron", "Citron", "#c9a400"), ("pier", "Pier", "#7a4cc2"),
    ("ps3", "PS3", "#1b1f3a"), ("wharf", "Wharf", "#107c10"), ("xbox360", "Xbox 360", "#6bb700"),
    ("wii", "Wii", "#8fb4c9"), ("wiiu", "Wii U", "#009ac7"), ("3ds", "3DS", "#ce181e"),
    ("nds", "NDS", "#6e6e6e"), ("dolphin", "Dolphin", "#2a7fd4"), ("cemu", "Cemu", "#00a0e0"),
    ("azahar", "Azahar", "#d9602b"), ("melonds", "melonDS", "#b5651d"), ("xemu", "xemu", "#3c8d2f"),
    ("xenia", "Xenia", "#3a7d44"), ("pcsx2", "PCSX2", "#1e3a8a"), ("rpcs3", "RPCS3", "#1e5fa8"),
    ("ppsspp", "PPSSPP", "#444b5a"), ("vita3k", "Vita3K", "#0b6fb8"),
]


def title_id(index):
    return f"01fe00ff{index + 1:05x}000"


def icon_for(key, name, colour, tmp):
    for ext in ("png", "jpg"):
        p = os.path.join(ICONS, f"{key}.{ext}")
        if os.path.exists(p):
            return hb2nsp.jpeg_256(open(p, "rb").read(), tmp)
    tile = os.path.join(tmp, "tile.png")
    size = 76 if len(name) <= 4 else 52 if len(name) <= 6 else 40
    subprocess.run(["magick", "-size", "256x256", f"xc:{colour}", "-gravity", "center",
                    "-font", "Adwaita-Sans-Bold", "-fill", "white", "-pointsize", str(size),
                    "-annotate", "+0+0", name, tile], check=True)
    return hb2nsp.jpeg_256(open(tile, "rb").read(), tmp)


def nacp_for(name):
    nacp = bytearray(0x4000)
    entry = name.encode()[:0x1ff].ljust(0x200, b"\0") + b"OpenPak".ljust(0x100, b"\0")
    for i in range(16):
        nacp[i * 0x300:(i + 1) * 0x300] = entry
    struct.pack_into("<I", nacp, 0x302C, 1)
    nacp[0x3060:0x3070] = b"1.0".ljust(0x10, b"\0")
    return bytes(nacp)


def build(index):
    key, name, colour = PLATFORMS[index]
    tid = title_id(index)
    w = os.path.join(hb2nsp.WORK, "reserved-" + tid)
    shutil.rmtree(w, ignore_errors=True)
    for d in ("exefs", "romfs", "control"):
        os.makedirs(os.path.join(w, d))
    # hacbrewpack wants a whole title: the program is the store's loader, never installed or run.
    shutil.copy(os.path.join(hb2nsp.LOADER, "hbl.nso"), os.path.join(w, "exefs/main"))
    shutil.copy(os.path.join(hb2nsp.LOADER, "hbl.npdm"), os.path.join(w, "exefs/main.npdm"))
    open(os.path.join(w, "romfs/app.path"), "w").write("sdmc:/none")
    open(os.path.join(w, "control/control.nacp"), "wb").write(nacp_for(name))
    icon = icon_for(key, name, colour, w)
    for lang in hb2nsp.LANGS:
        open(os.path.join(w, f"control/icon_{lang}.dat"), "wb").write(icon)
    r = subprocess.run([hb2nsp.HBP, "-k", hb2nsp.HBP_KEYS, "--titleid", tid, "--nologo", "--keepncadir",
                        "--exefsdir", "exefs", "--romfsdir", "romfs", "--controldir", "control",
                        "--nspdir", "nsp", "--ncadir", "nca", "--tempdir", "temp", "--backupdir", "backup"],
                       cwd=w, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"{name}: hacbrewpack failed\n{r.stdout}\n{r.stderr}")
    nca_dir = os.path.join(w, "nca")
    control = next((f for f in sorted(os.listdir(nca_dir))
                    if hb2nsp.nca_type(os.path.join(nca_dir, f)) == "control"), None)
    if control is None:
        raise SystemExit(f"{name}: no control NCA came out")
    os.makedirs(OUT, exist_ok=True)
    dest = os.path.join(OUT, tid + ".nca")
    shutil.copy(os.path.join(nca_dir, control), dest)
    # The content id the console is told is the first half of the SHA-256, and atum derives it the
    # same way from the bytes: they must agree with the file name hacbrewpack gave it.
    data = open(dest, "rb").read()
    assert control.split(".")[0] == hashlib.sha256(data).hexdigest()[:32], "content id is not the hash"
    shutil.copy(os.path.join(w, "control/icon_AmericanEnglish.dat"), os.path.join(OUT, tid + ".jpg"))
    shutil.rmtree(w)
    print(f"{tid}  {key:8} {name:9} {len(data)} B", flush=True)


def main(argv):
    if not argv or argv[0] not in ("build", "list"):
        raise SystemExit(__doc__)
    if argv[0] == "list":
        for i, (key, name, _) in enumerate(PLATFORMS):
            print(f"{title_id(i)}  {key:8} {name}")
        return
    wanted = argv[1:] or [k for k, _, _ in PLATFORMS]
    for i, (key, _, _) in enumerate(PLATFORMS):
        if key in wanted:
            build(i)


if __name__ == "__main__":
    main(sys.argv[1:])
