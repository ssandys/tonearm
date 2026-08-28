# Vendored dependencies

## roonapi 0.1.6

Source: https://github.com/pavoni/pyroon (PyPI `roonapi`)
License: Apache-2.0 — see `roonapi/LICENSE`. Retained per §4 of the licence.

Vendored rather than packaged because Arch ships no `python-roonapi`, and the
AUR package is 0.1.4 — behind upstream, zero votes, untouched since May 2023.
Vendoring pins the version and needs no network at install time.

Its declared dependencies (`requests`, `six`, `ifaddr`) are over-declared: the
source files import `websocket` as a hard dependency, and conditionally
`simplejson` (which falls back to stdlib `json` when absent, adding no
installation requirement). System Python already has both `websocket` and `json`.

To refresh: bump the version, repeat the copy, re-run `./bin/test`, and check
`RoonApi.__init__`'s signature has not changed.
