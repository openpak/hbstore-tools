# hbstore-tools

How the OpenPak eShop gets its homebrew: every [hb-appstore](https://switch.cdn.fortheusers.org)
(ForTheUsers) package with an NRO, plus homebrew found on GitHub, is wrapped as a no-rights-ID
Switch title (`01fe…000`) and served by **nx-shop**. Updates come from each app's own GitHub
releases wherever it publishes them (`sources.json`), 4TU only for the rest; OpenPak itself ships the same way as `01fe000000000000`. Installing one
needs OpenPak's boot package (FS NCA-signature patch + Loader ACID gate for `0x01FE…`, built by
openpak-nro's Enable).

## Pieces

| Path | What |
| --- | --- |
| `hb2nsp.py` | the packager and store client (commands below) |
| `run-hb.sh` | runs `hb2nsp.py` against prod through a self-healing SSH tunnel to nx-baas's store admin (`127.0.0.1:20070` on the box, forwarded to nx-shop), with `NX_INTERNAL_KEY` from the box's `.env` |
| `loader/` | the title's program: an nx-hbloader fork that copies `romfs:/sd` (the package's SD files, never `atmosphere/` or `bootloader/`) onto the SD on first launch, runs the NRO from its own path, and on return ends the app the way an application must |
| `hacBrewPack/` | hacBrewPack 3.05 (upstream `745b16e`) with two local changes: `HBP_TITLE_VERSION` sets the CNMT version, `HBP_PATCH=1` builds a full-content patch (CNMT 0x81) |
| `mkpkg3.py` | swaps a rebuilt Loader + fusee into a package3 (the reference the openpak-nro builder was checked against byte for byte) |
| `deploy/` | the Traefik router helper and the quadlets as installed on the box for nx-shop / nx-penne / nx-pctl |

## Setup

```sh
pacman -S switch-dev devkitARM switch-libjpeg-turbo      # devkitPro repos, see pkg.devkitpro.org
make -C loader                                            # -> loader/hbl.nso, hbl.npdm
cp hacBrewPack/config.mk.template hacBrewPack/config.mk && make -C hacBrewPack
# keys: hacbrewpack and hactool reject the 34-digit lines of a recent prod.keys
awk -F' *= *' 'length($2)==32||length($2)==64||length($2)==128||length($2)==512||length($2)==1024' \
    ~/.switch/prod.keys > ~/.switch/hbp.keys && chmod 600 ~/.switch/hbp.keys
```
Also needs `hactool`, ImageMagick (`identify`, `magick`), Python 3 with `requests`.
Working data goes to `$HBSTORE_DATA` (default `~/REPOS/Openpak/scratch/hbstore`):
`out/uploaded.json` is the ledger of what is in the store; `out/repo.json` caches 4TU's index,
so the store keeps working (upstream updates included) if 4TU goes away. GitHub API calls use
`GITHUB_TOKEN` or `gh auth token`.

## Commands (`./run-hb.sh <command> …` against prod; the URL is appended)

| Command | Does |
| --- | --- |
| `sync` | **daily**: add packages the store lacks; for a package in `sources.json`, ship its upstream's newer latest GitHub release as an update (one store version up; 4TU's other files kept, release notes as the changelog; `out/upstream.json` holds the tag the store has); for the rest, an update when 4TU dates the package after the store's `revised_at`; enrich what changed; then the `github` titles. Packages that fail are skipped until their source changes (`out/sync-broken.json`). `HBSTORE_DRY=1` only prints |
| `sources` | rewrite `sources.json` (committed): every 4TU package whose GitHub repo publishes releases. Rerun now and then to pick up new packages |
| `discover [yyyy-mm-dd]` | search GitHub (topics, readmes) for repos whose latest release since that date (default 2022-01-01) carries a Switch NRO that neither 4TU nor the store has: `out/discover.json`, for review |
| `adopt` | publish every entry left in `out/discover.json` as a `github` title |
| `since <dd/mm/yyyy>` | build, upload and publish every package updated since then (skips what is in the store) |
| `add <package>…` | the same for named packages, then `enrich` them |
| `enrich` | category, full description, changelog, release/update dates, screenshots for every store title |
| `rebuild [<package>…]` | rebuild bases with the current loader/icons and replace them |
| `github <owner/repo> <tag> [category]` | a title from a GitHub release's `.nro` asset or the NRO in its zip (id from `gh:<owner/repo>`, name from the NACP, the owner's avatar when the NRO has no icon), recorded in `out/github.json` so `sync` ships each new release as an update |
| `openpak <tag>` / `openpak-update <tag>` | OpenPak from its GitHub release as the base / as an update (patch `01fe000000000800`) |
| `loaderfix <na id>` | ship a rebuilt loader to every title an account owns, as updates one packaging revision up |
| `build <package>…`, `upload <URL> <tid>…` | the two halves by hand (no tunnel: run `hb2nsp.py` directly) |

## Daily sync
`deploy/workstation/hbstore-sync.{service,timer}` run `./run-hb.sh sync` at 05:30 as a systemd
user timer on the workstation (it needs `~/.switch/hbp.keys` and the OpenPak SSH agent);
output in `$HBSTORE_DATA/sync.log`. Install: copy both to `~/.config/systemd/user/`, then
`systemctl --user enable --now hbstore-sync.timer`.

## Versions
Store version = `(major*10000 + minor*100 + micro) << 16`; the low 16 bits are a packaging
revision (a loader or icon fix without a new app version). A base is version 0; anything
above it is served as an update.

## Lessons from hardware (2026-10-04/05)
- **Icons must be baseline JPEGs.** ns shrinks the icon to 174×174 through capsrv's jpegdec,
  which runs out of memory on progressive JPEGs: the title never gets its name/icon cache,
  the HOME tile stays blank and launch fails **2206-1017**. `jpeg_256` re-encodes anything
  that is not `JPEG 256 256 None`.
- **The loader's exit**, in order: end the process before unmapping the app (its threads
  data-abort otherwise), zero the heap used as copy buffer, and send
  `ISelfController::Exit` (appletOE → OpenApplicationProxy → GetSelfController → Exit)
  before `svcExitProcess` — an NRO never does it, and without it every exit is "The
  software was closed because an error occurred".
- A stuck half-installed title is removed with **Goldleaf** (ns `DeleteApplicationCompletely`);
  the tile's + menu and Data Management cannot.
- ftpd 3.2.1 crashes on exit on 22.5.0 everywhere (upstream, not the loader).
