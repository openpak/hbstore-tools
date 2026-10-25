# Notes: hbstore-tools

## Pieces

| Path | What |
| --- | --- |
| `hb2nsp.py` | the packager and store client (commands below) |
| `run-hb.sh` | runs `hb2nsp.py` against prod through a self-healing SSH tunnel to nx-baas's store admin (`127.0.0.1:20070` on the box, forwarded to nx-shop), with `NX_INTERNAL_KEY` from the box's `.env` |
| `loader/` | the title's program: an nx-hbloader fork that copies `romfs:/sd` (the package's SD files, never `atmosphere/` or `bootloader/`) onto the SD on first launch, runs the NRO from its own path, and on return ends the app the way an application must |
| `hacBrewPack/` | hacBrewPack 3.05 (upstream `745b16e`) with two local changes: `HBP_TITLE_VERSION` sets the CNMT version, `HBP_PATCH=1` builds a full-content patch (CNMT 0x81) |
| `mkpkg3.py` | swaps a rebuilt Loader + fusee into a package3 (the reference the OpenPak Switch Patcher builder was checked against byte for byte) |
| `deploy/` | the Traefik router helper and the quadlets as installed on the box for nx-shop / nx-penne / nx-pctl |
