# eShop Homebrew Tools

OpenPak's eShop Homebrew Tools.

| | |
| --- | --- |
| Console | Switch |
| Kind | Packaging tools |
| Licence | AGPL-3.0-only, see [LICENSE](LICENSE) |

## What it does

- Wraps every [hb-appstore](https://switch.cdn.fortheusers.org) (ForTheUsers) package with an NRO, and homebrew found on GitHub, as a no-rights-ID Switch title (`01fe…000`), served by **nx-shop**.
- Takes updates from each app's own GitHub releases where it publishes them (`sources.json`), and from 4TU for the rest.
- Ships OpenPak itself the same way, as `01fe000000000000`.
- Installing a title needs OpenPak's boot package, built by OpenPak Switch Patcher's Enable.

## Install

```sh
pacman -S switch-dev devkitARM switch-libjpeg-turbo      # devkitPro repos, see pkg.devkitpro.org
```

Also needs `hactool`, ImageMagick and Python 3 with `requests`. The full setup, including the keys file, is in [deploy](docs/deploy.md).

## Build

```sh
make -C loader                                            # -> loader/hbl.nso, hbl.npdm
cp hacBrewPack/config.mk.template hacBrewPack/config.mk && make -C hacBrewPack
```

## Run

```sh
./run-hb.sh sync      # daily sync against prod, through an SSH tunnel
```

The other commands are listed in [deploy](docs/deploy.md).

## Docs

- [Protocol](docs/protocol.md)
- [Evidence](docs/evidence.md)
- [Deploy](docs/deploy.md)
- [Notes](docs/notes.md)

## Licence

See [LICENSE](LICENSE).
