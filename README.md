# Glideslope

<p align="center"><b><i>Every flat-fee coding plan you pay for, on one glide path.</i></b></p>

---

If you run coding agents on more than one subscription (a couple of Claude Max accounts, Codex, Kimi, Grok), you have several meters, each on its own reset clock, each on its own page. Glideslope reads all of them into one table and puts one mark on every meter: the **◆ glide slope**.

## The glide slope

A flat-fee plan is a fixed budget on a fixed window: 100% of a weekly quota, gone at the reset. The glide slope is the straight line from 0% at the window's start to 100% at its reset. At any moment it tells you where you *would* be if you were spending evenly.

```
100% ┤                                      ╱ reset
     │                                  ╱
     │                             ╱  ◆ 62%   ← where even burn puts you now
     │                        ╱
     │  used 78% ──────●───╱─────── above the line: ahead, you will run out early
     │              ╱
     │  used 41% ──╱───●─────────── below the line: banking, you will leave budget unused
     │         ╱
  0% ┼────╱───────────────────────────────────
     start                    now           reset
```

Every metered cell reads `used (◆ slope)`. That one comparison answers the only two questions that matter on a flat-fee plan:

- **Above the line, you will hit the ceiling before the reset.** Slow down, or switch to an account with room.
- **Below the line, you are leaving paid capacity on the table.** Capacity does not roll over; at the reset it is gone.

Most usage tools only warn about the first. On a subscription both sides cost money, so Glideslope treats both as news, for every provider, in the same column.

## What it looks like

```
glideslope
```

| Weekly status ||||
|:-:|:-:|:-:|:-:|
| **Account** | **Weekly** | **Fable** | **Next reset at** |
| Claude · Alpha ● laptop | A⃝ 41% (◆ 38.2%) | A⃟ 58% (◆ 38.2%) | 4d 7h – Mon Sep 28, 11:00 PM |
| Claude · Bravo | B⃝ 22% (◆ 70.6%) | B⃟ 19% (◆ 70.6%) | 2d 1h – Sat Sep 26, 4:35 PM |
| Claude · pooled (2 · Max 20x) | 31.5% (◆ 54.4%) · banking | 38.5% (◆ 54.4%) · banking | next Bravo in 2d 1h |
| Codex | X⃝ 64% (◆ 57.0%) | — | 3d 0h – Sun Sep 27, 3:24 PM |
| Kimi | K⃝ 7% (◆ 12.4%) | — | 6d 3h – Wed Sep 30, 6:24 PM |
| Grok | G⃝ 2% (◆ 75.2%) | — | 1d 17h – Sat Sep 26, 8:54 AM |
| Total · pooled (4 · $630/mo) | 40.4% (◆ 56.2%) · banking | — | next Grok in 1d 17h |

Values illustrative. The letter is the account, the outline is the window (circle weekly, triangle 5h session, diamond Fable weekly). A second table lists every window of every account; `--json` gives the same position as one snapshot. Cells never pretend: `stale`, `presumed`, `floor` and `undecided` mark exactly how much was actually read.

The **pooled rows** treat your accounts as one budget. If you rotate between accounts, that is the number you are actually flying.

## The views

The same position, drawn. The approach plot puts every window on one glide slope frame, so five accounts across four providers read as one picture.

- **Detail view** (`views/deck.html`): every window on the plot with its history trail, the full ledger, reset horizon, and switch log.
- **Popup** (`views/popup.html`): the plot alone, sized for a menu bar or side panel.
- **History** (`views/history.html`): weeks stacked behind you on a real clock.

Self-contained HTML, opened from disk, nothing hosted. Screenshots coming.

## Install

Python 3.11+, standard library only. macOS for the Claude read and the background jobs; the rest is plain Python.

```bash
git clone https://github.com/Stage-11-Agentics/glideslope.git
cd glideslope
python3 glideslope.py                 # the position, now
bash tools/install-sampler.sh         # every 60s: journal the position, rebuild the views
open views/deck.html
```

Or `uv tool install git+https://github.com/Stage-11-Agentics/glideslope` for the `glideslope` and `claude-account` commands alone.

Configuration is one optional file, `~/.glideslope/config.toml`: plan labels, account names, other machines, alert sink. Every key has a default. See [`config.example.toml`](config.example.toml).

## Providers

| Provider | Meters | Credential |
|---|---|---|
| **Claude** | 5h session, weekly, weekly Fable, per account | the token Claude Code already holds, read-only |
| **Codex** | every native meter plus banked reset credits | none; read through the Codex app-server |
| **Kimi** | plan quota and 5h burst window | a static platform key |
| **Grok** | SuperGrok weekly compute pool | the Grok Build login |
| **OpenRouter** | rolling 7-day dollars (metered, so no slope) | a static API key |

Full read paths, units and failure modes: [`PROVIDERS.md`](PROVIDERS.md).

**Glideslope holds no credentials and mints no tokens.** Claude is read with the access token Claude Code already keeps, only while it is valid. Static keys come from the environment or a `0600` keys file. The one exception is Grok, whose six-hour token is refreshed in place the same way Grok's own client does it. The promises, stated as invariants: [`SECURITY.md`](SECURITY.md).

## Several Claude logins on one machine

`claude-account`, shipped here, gives each Claude account its own login home (one `CLAUDE_CONFIG_DIR` each) so one machine holds every login at once, and routes new sessions between them:

```bash
claude-account homes              # every login on this machine
claude-account use bravo          # new sessions start as Bravo
claude-account use auto           # let the glide slope pick the account with room
```

Running sessions never move, and nothing here stores a token. `claude-account --help` has the rest.

## For agents

The output is relay-ready markdown, and a skill ships in the repo. Install it for Claude Code and ask "where am I on usage?":

```bash
ln -s "$PWD/skills/glideslope" ~/.claude/skills/glideslope
```

Developing Glideslope with an agent: [`CLAUDE.md`](CLAUDE.md).

## Origin

Born inside Gregorovich, Atin Woodard's personal infrastructure, where it has flown a multi-account rotation since July 2026. [MIT](LICENSE). A [Stage 11 Agentics](https://stage11.ai) project.
