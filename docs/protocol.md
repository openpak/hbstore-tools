# Protocol: hbstore-tools

How the OpenPak eShop gets its homebrew: every [hb-appstore](https://switch.cdn.fortheusers.org)
(ForTheUsers) package with an NRO, plus homebrew found on GitHub, is wrapped as a no-rights-ID
Switch title (`01fe…000`) and served by **nx-shop**. Updates come from each app's own GitHub
releases wherever it publishes them (`sources.json`), 4TU only for the rest; OpenPak itself ships the same way as `01fe000000000000`. Installing one
needs OpenPak's boot package (FS NCA-signature patch + Loader ACID gate for `0x01FE…`, built by
OpenPak Switch Patcher's Enable).

## Versions
Store version = `(major*10000 + minor*100 + micro) << 16`; the low 16 bits are a packaging
revision (a loader or icon fix without a new app version). A base is version 0; anything
above it is served as an update.
