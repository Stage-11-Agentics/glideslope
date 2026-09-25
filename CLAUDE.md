# Glideslope

A cross-provider subscription position for people who run coding agents on several flat-fee plans. Claude (several accounts), Codex, Kimi and Grok, plus metered OpenRouter, normalized into one account shape and rendered as one table with the ◆ even-burn mark. Also ships `claude-account`, the login-homes tool.

This file is for agents developing the repo. `AGENTS.md` is a symlink to it. Users start at `README.md`. Each provider's contract is in `PROVIDERS.md`: read the section for any provider you touch before you touch it.

## Quick reference

| Item | Value |
|---|---|
| Language | Python 3.11+ (`tomllib`), standard library only |
| Optional | Pillow, for the alert image only |
| Tests | pytest, hermetic, ~255 tests in ~2s |
| Entry points | `glideslope`, `glideslope-sampler`, `glideslope-notify`, `claude-account` |
| Config | `~/.glideslope/config.toml` (see below) |

## Layout

```
glideslope.py        the CLI and module: provider reads, normalization, pools, rendering
sampler.py           one sample: read the position, append to samples.db, rebuild the views
notify.py            threshold alerts on the position the sampler already holds
claude_account.py    login homes and the Claude usage read (tools/claude-account is a thin shim)
PROVIDERS.md         per-provider read path, units, failure modes, credential discipline
views/
  deck-src/          Detail view: build.py + template
  popup-src/         compact approach-plot popup: template
  history-src/       History view: build.py + template
                     built HTML lands in views/ and is gitignored
tools/               probes, refresh.sh, launchd installers, beacon, login watcher, hooks
tests/               pytest
skills/glideslope/   the agent skill users install
```

There is no `data/` directory. All state lives in the store.

## Run

```bash
python3 glideslope.py                     # the position, as markdown
python3 glideslope.py --json              # the normalized snapshot
python3 glideslope.py --open [deck|popup|history]   # rebuild one view from this run and open it in the browser
python3 glideslope.py --help              # read-only
python3 glideslope.py --skip-claude --codex-snapshot saved.json     # offline, from a saved payload
```

Prefer the `--*-snapshot` flags and `--skip-*` flags when you need output while developing. They exercise the real normalization and rendering without a live read.

## Test

```bash
python3 -m pytest tests -q
```

The suite must stay hermetic and fast. A test:

- never reads the real config (`~/.glideslope/config.toml`) or the real store. Pin `GLIDESLOPE_CONFIG` and `GLIDESLOPE_HOME` to a temp directory.
- never touches a real credential: no keychain, no `~/.grok/auth.json`, no `~/.codex/auth.json`, no keys file.
- never reaches a real endpoint. Providers are fed saved payloads through the snapshot seams.
- never spawns a real `codex app-server`, `ssh`, `launchctl` or `osascript`.

If a test's failure mode changes from "assertion" to "it reached something real", that is an incident, not a flake. Add the guard before the test, not after.

CI runs the suite on ubuntu-latest and macos-latest, Python 3.11 and 3.13, with no secrets. Code that only works on macOS (keychain, launchd) must fail soft on Linux and be tested through seams.

## Rebuild the views

```bash
bash tools/refresh.sh --no-gauge          # both views from the current position, no Claude usage GET
bash tools/refresh.sh                     # gauge step included; still read-only, mints nothing
python3 views/deck-src/build.py           # Detail view + popup, cached gauge
python3 views/history-src/build.py        # History view, store only
```

After editing a template: run the tests, rebuild, and open the built page. Look at it. The popup is sized by its host and every provider costs register rows out of that budget, so check that the last window still clears the footer.

## Hard rules

1. **This program holds no credentials and mints no tokens.** Claude is read with the access token Claude Code already holds, only while it is valid; never refresh it, never write the keychain, never store a token. Codex is read through its app-server protocol; its auth file is read for the login email claim only. Kimi and OpenRouter use static keys from the environment, then the keys file. The single exception is Grok's in-place refresh of `~/.grok/auth.json` (see `PROVIDERS.md`); do not widen it and do not add a second one.
2. **A failed provider read degrades to last-known-good and is never retried in a loop.** Raise `PositionError`, let `gather()` turn it into a warning, and serve the cached read marked stale. One provider's outage must never break the others. The next sampler tick is the retry.
3. **Never bypass a provider's read path with ad-hoc requests to debug.** No raw `curl`, no one-off `urllib` scripts, no rapid variants against an endpoint. Use `tools/kimi_probe.py` or `tools/grok_probe.py` (one request, raw payload beside the normalization), or save a payload and debug offline. Rapid failed auth attempts look like abuse to a provider.
4. **The sample store is append-only.** `samples.db` is history. Never rewrite, dedupe or delete rows. A schema change adds columns or tables.
5. **Every provider lands in the same account shape, and nothing is a special case at the presentation layer.** A provider whose main quota is weekly uses `meter_id: "weekly_all"` and `label: "Weekly · all models"`; a 5h-class throttle uses `session`. `active` and `logins` mean "the Claude account this machine is using" (its selected login, one per machine) and render on Claude rows only; `held_on` lists every machine holding a login home for the account, selected or not. If a renderer needs an `if provider == ...`, the normalization is wrong.
6. **Never render an unknown as a calm number.** Stale, presumed, unread, floor and undecided exist so a cell never claims more than was read. A missing field is not zero.
7. **The config contract is fixed.** Document any change to it in `README.md`, `config.example.toml` and here in the same commit.

## The config contract

`~/.glideslope/config.toml`, overridable with `GLIDESLOPE_CONFIG=<path>`. The store directory is overridable with `GLIDESLOPE_HOME`. Every key is optional.

| Key | Default | Meaning |
|---|---|---|
| `timezone` | system local zone | reset clocks and switch times |
| `store` | `~/.glideslope` | `samples.db`, `spend.json`, `alerts/`, state files |
| `keys_file` | `~/.glideslope/keys.txt` | `KEY=value` lines, mode 0600, static keys only (`KIMI_API_KEY`, `OPENROUTER_API_KEY`) |
| `[satellite] name` | short hostname | this machine's display name |
| `[[satellites]] name, host` | none | other machines with a Claude login, read over ssh from their `~/.glideslope/satellite.json` |
| `[claude] call_signs` | NATO letters in roster order | roster alias to display name |
| `[codex] plan`, `[kimi] plan`, `[grok] plan` | none | operator-declared plan labels |
| `[notify] sink` | `macos` | `macos` (osascript banner), `url` (POST JSON), `none` |
| `[notify] url` | `""` | url sink only |
| `[notify] percent` | `90` | alert line for windows on the active Claude account |

launchd labels are `ai.stage11.glideslope.<job>`.

## Adding a provider

Follow "Adding the next provider" in `PROVIDERS.md`: `query_x()` raising `PositionError`, `normalize_x()` into the shared shape, `x_accounts()`, `--skip-x` and `--x-snapshot`, colors in both templates, the key name in `sampler.py` if static, tests from a saved payload, then rebuild and look at both pages.

## Vocabulary

- **◆ Glideslope mark**: the share of the window already elapsed, which is where even burn would put used right now.
- **Ahead / banking**: used above / below the mark.
- **Fable**: Anthropic's model tier with its own weekly meter, `weekly_fable`. Public copy says Fable, never an abbreviation.
- **Satellite**: a machine that holds a Claude login.
- **Login home**: one `CLAUDE_CONFIG_DIR` per Claude account.
