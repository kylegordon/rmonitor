# Changelog

## [0.1.12](https://github.com/kylegordon/rmonitor/compare/v0.1.11...v0.1.12) (2026-08-16)

## What's Changed
* feat(relay): switch GUI theming to ttkbootstrap, fix startup race by @kylegordon in https://github.com/kylegordon/rmonitor/pull/65


**Full Changelog**: https://github.com/kylegordon/rmonitor/compare/v0.1.11...v0.1.12


## [0.1.11](https://github.com/kylegordon/rmonitor/compare/v0.1.10...v0.1.11) (2026-07-25)

## What's Changed
* feat(relay): prettify GUI with ttk styling and live connectivity indicators by @kylegordon in https://github.com/kylegordon/rmonitor/pull/63


**Full Changelog**: https://github.com/kylegordon/rmonitor/compare/v0.1.10...v0.1.11


## [0.1.10](https://github.com/kylegordon/rmonitor/compare/v0.1.9...v0.1.10) (2026-07-25)

## What's Changed
* fix(relay): install binutils in CI, document RelayGuiApp by @kylegordon in https://github.com/kylegordon/rmonitor/pull/58
* Create dependabot.yml by @kylegordon in https://github.com/kylegordon/rmonitor/pull/57
* Revert "Create dependabot.yml" by @kylegordon in https://github.com/kylegordon/rmonitor/pull/60
* Feature/cross platform relay gui app by @kylegordon in https://github.com/kylegordon/rmonitor/pull/61
* feat(relay): surface Server URL as a GUI dialog field by @kylegordon in https://github.com/kylegordon/rmonitor/pull/62


**Full Changelog**: https://github.com/kylegordon/rmonitor/compare/v0.1.9...v0.1.10


## [0.1.9](https://github.com/kylegordon/rmonitor/compare/v0.1.8...v0.1.9) (2026-07-25)

## What's Changed
* docs: note Orbits' Refresh Scoreboard Feed button for missing names/classes by @kylegordon in https://github.com/kylegordon/rmonitor/pull/54
* feat(relay): standalone cross-platform GUI build (Windows/Linux) by @kylegordon in https://github.com/kylegordon/rmonitor/pull/56


**Full Changelog**: https://github.com/kylegordon/rmonitor/compare/v0.1.8...v0.1.9


## [0.1.8](https://github.com/kylegordon/rmonitor/compare/v0.1.7...v0.1.8) (2026-07-12)

## What's Changed
* fix(relay): exit on connection failure instead of retrying forever by @kylegordon in https://github.com/kylegordon/rmonitor/pull/45
* fix(server): reset race state when the feed recovers from an outage by @kylegordon in https://github.com/kylegordon/rmonitor/pull/47
* fix(server): discard stale persisted state on restart by @kylegordon in https://github.com/kylegordon/rmonitor/pull/48
* fix: harden ingest handling, close broadcast/auth/retry gaps found in review by @kylegordon in https://github.com/kylegordon/rmonitor/pull/49
* docs: add badges to README by @kylegordon in https://github.com/kylegordon/rmonitor/pull/51
* fix(relay): reconnect when the feed goes silent without erroring by @kylegordon in https://github.com/kylegordon/rmonitor/pull/50
* fix(deploy): move SERVER_URL from up.sh default into .env by @kylegordon in https://github.com/kylegordon/rmonitor/pull/52
* fix(server): tie state staleness to last data change, not last save by @kylegordon in https://github.com/kylegordon/rmonitor/pull/53


**Full Changelog**: https://github.com/kylegordon/rmonitor/compare/v0.1.7...v0.1.8


## [0.1.7](https://github.com/kylegordon/rmonitor/compare/v0.1.6...v0.1.7) (2026-07-11)

## What's Changed
* Replace release-please with custom release workflow by @kylegordon in https://github.com/kylegordon/rmonitor/pull/38
* Compact mobile headers by @kylegordon in https://github.com/kylegordon/rmonitor/pull/37
* Merge pull request #37 from kylegordon/master by @kylegordon in https://github.com/kylegordon/rmonitor/pull/40
* fix: don't show stale race data on first daily server startup by @kylegordon in https://github.com/kylegordon/rmonitor/pull/41
* chore(master): release 0.1.5 by @github-actions[bot] in https://github.com/kylegordon/rmonitor/pull/39
* chore(master): release 0.1.6 by @github-actions[bot] in https://github.com/kylegordon/rmonitor/pull/42
* fix(release): stop release-PR loop and restore artifact publishing by @kylegordon in https://github.com/kylegordon/rmonitor/pull/44


**Full Changelog**: https://github.com/kylegordon/rmonitor/compare/v0.1.4...v0.1.7


## [0.1.6](https://github.com/kylegordon/rmonitor/compare/v0.1.5...v0.1.6) (2026-07-11)

## What's Changed
* Replace release-please with custom release workflow by @kylegordon in https://github.com/kylegordon/rmonitor/pull/38
* Compact mobile headers by @kylegordon in https://github.com/kylegordon/rmonitor/pull/37
* Merge pull request #37 from kylegordon/master by @kylegordon in https://github.com/kylegordon/rmonitor/pull/40
* fix: don't show stale race data on first daily server startup by @kylegordon in https://github.com/kylegordon/rmonitor/pull/41
* chore(master): release 0.1.5 by @github-actions[bot] in https://github.com/kylegordon/rmonitor/pull/39


**Full Changelog**: https://github.com/kylegordon/rmonitor/compare/v0.1.4...v0.1.6


## [0.1.5](https://github.com/kylegordon/rmonitor/compare/v0.1.4...v0.1.5) (2026-07-11)

## What's Changed
* Replace release-please with custom release workflow by @kylegordon in https://github.com/kylegordon/rmonitor/pull/38
* Compact mobile headers by @kylegordon in https://github.com/kylegordon/rmonitor/pull/37
* Merge pull request #37 from kylegordon/master by @kylegordon in https://github.com/kylegordon/rmonitor/pull/40
* fix: don't show stale race data on first daily server startup by @kylegordon in https://github.com/kylegordon/rmonitor/pull/41


**Full Changelog**: https://github.com/kylegordon/rmonitor/compare/v0.1.4...v0.1.5


## [0.1.4](https://github.com/kylegordon/rmonitor/compare/v0.1.3...v0.1.4) (2026-05-31)


### Features

* compact mobile header with branding left, pills right ([678fcd8](https://github.com/kylegordon/rmonitor/commit/678fcd86da524ed2c5e6dba0bedbb28934d6f66b))

## [0.1.3](https://github.com/kylegordon/rmonitor/compare/v0.1.2...v0.1.3) (2026-05-31)


### Bug Fixes

* integrate Docker publish into release-please workflow ([5b0e172](https://github.com/kylegordon/rmonitor/commit/5b0e172043c3ded7def69cccfe9ff9e177b7f838))

## [0.1.2](https://github.com/kylegordon/rmonitor/compare/v0.1.1...v0.1.2) (2026-05-31)


### Bug Fixes

* trigger Docker publish on release event, not tag push ([7cf3179](https://github.com/kylegordon/rmonitor/commit/7cf3179ad61a7b5a104d0368f886d98939640fec))

## [0.1.1](https://github.com/kylegordon/rmonitor/compare/v0.1.0...v0.1.1) (2026-05-31)


### Features

* responsive layout with horizontal table scroll ([05c5bbc](https://github.com/kylegordon/rmonitor/commit/05c5bbc590a6166b580785a97efa4544812c601c))
