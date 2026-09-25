# Glideslope

<p align="center"><b><i>Every flat-fee coding plan you pay for, on one glide path.</i></b></p>

---

You run coding agents on several subscriptions. A few Claude Max accounts. Codex. Kimi's coding plan. SuperGrok. Maybe some metered OpenRouter on the side. Each one meters you differently, resets on its own clock, and shows its usage on its own page.

Glideslope reads all of them and puts them in one table. Every provider is normalized to the same account shape, so a Claude weekly, a Codex weekly and a Kimi plan quota sit in the same column and mean the same thing.

Every metered cell carries the **◆ Glideslope mark**: where an even burn to the reset would put you right now. If a window is 40% of the way to its reset, ◆ reads 40%. Used above it means you are ahead of budget. Used below it means you are banking capacity.

## The thesis is two-sided

A glide slope is the line an aircraft flies down to the runway. Above it you land long. Below it you land short.

- **Over-burn flames out early.** Spend a weekly quota too fast and your agents stop mid-week.
- **Under-burn wastes a flat-fee plan.** Capacity left at the reset is gone. You paid for it and did not use it.

Most usage tools only warn about the first. Glideslope treats both as news. The ◆ mark says which side of the line you are on, and the pooled rows say it for all your accounts at once.

## What it looks like

`glideslope` prints GitHub-flavored markdown. It renders cleanly in a terminal, in an agent chat, or pasted into an issue. Example with two Claude accounts, Codex, Kimi and Grok (values illustrative):

**● Logged in · laptop: Claude · Alpha**

| Weekly status ||||
|:-:|:-:|:-:|:-:|
| **Account** | **Weekly** | **Fable** | **Next reset at** |
| Claude · Alpha ● laptop | A⃝ 41% (◆ 38.2%) | A⃟ 58% (◆ 38.2%) | 4d 7h – Mon Sep 28, 11:00 PM EDT |
| Claude · Bravo | B⃝ 22% (◆ 70.6%) | B⃟ 19% (◆ 70.6%) | 2d 1h – Sat Sep 26, 4:35 PM EDT |
| Claude · pooled (2 · Max 20x) | 31.5% (◆ 54.4%) · −22.9 banking | 38.5% (◆ 54.4%) · −15.9 banking | next Bravo in 2d 1h – Sat Sep 26, 4:35 PM EDT |
| Codex | X⃝ 64% (◆ 57.0%) | — | 3d 0h – Sun Sep 27, 3:24 PM EDT |
| Kimi | K⃝ 7% (◆ 12.4%) | — | 6d 3h – Wed Sep 30, 6:24 PM EDT |
| Grok | G⃝ 2% (◆ 75.2%) | — | 1d 17h – Sat Sep 26, 8:54 AM EDT |
| Total · pooled (4 · $630/mo, not Kimi) | 40.4% (◆ 56.2%) · −15.8 banking | — | next Grok in 1d 17h – Sat Sep 26, 8:54 AM EDT |
| OpenRouter | $6.81 | — | rolling 7d · $93 left of $100/monthly |

| All windows |||||
|:-:|:-:|:-:|:-:|:-:|
| **Provider** | **Account** | **Window** | **Used** | **Next reset at** |
| Claude | Alpha ● laptop · Max 20x | Weekly · all models | A⃝ 41% (◆ 38.2%) | 4d 7h – Mon Sep 28, 11:00 PM EDT |
| Claude | Alpha ● laptop · Max 20x | Weekly · Fable | A⃟ 58% (◆ 38.2%) | 4d 7h – Mon Sep 28, 11:00 PM EDT |
| Claude | Alpha ● laptop · Max 20x | 5h session | A⃤ 20% (◆ 25.5%) | 3h 43m – Thu Sep 24, 6:55 PM EDT |
| Claude | Bravo · Max 20x | Weekly · all models | B⃝ 22% (◆ 70.6%) | 2d 1h – Sat Sep 26, 4:35 PM EDT |
| Claude | Bravo · Max 20x | Weekly · Fable | B⃟ 19% (◆ 70.6%) | 2d 1h – Sat Sep 26, 4:35 PM EDT |
| Codex | Codex · 20x | Weekly · all models | X⃝ 64% (◆ 57.0%) | 3d 0h – Sun Sep 27, 3:24 PM EDT |
| Kimi | Kimi · Coding 15x | 5h session | K⃤ 31% (◆ 44.0%) | 2h 48m – Thu Sep 24, 6:00 PM EDT |
| Kimi | Kimi · Coding 15x | Weekly · all models | K⃝ 7% (◆ 12.4%) | 6d 3h – Wed Sep 30, 6:24 PM EDT |
| Grok | Grok · SuperGrok | Weekly · all models | G⃝ 2% (◆ 75.2%) | 1d 17h – Sat Sep 26, 8:54 AM EDT |

| Recent switches (last 1) ||||
|:-:|:-:|:-:|:-:|
| **When** | **Switch** | **Left (5h · wk · Fable)** | **Entered (5h · wk · Fable)** |
| Sep 24, 9:12 AM | Bravo → Alpha · /login | Bravo: 5h 97% · wk 22% · Fable 19% | Alpha: 5h 0% · wk 38% · Fable 52% |

How to read it:

- **Meter badges.** The letter is the account (A Alpha, B Bravo, X Codex, K Kimi, G Grok). The outline is the window: circle ⃝ is weekly all models, triangle ⃤ is the 5h session, diamond ⃟ is weekly Fable.
- **Fable** is Anthropic's model tier with its own weekly meter (`weekly_fable`). It is often the binding weekly quota, so it gets its own column. A Claude account whose plan has no Fable allowance reads `none`. Providers without a Fable meter read `—`.
- **● laptop** marks the machine a Claude account is logged into. `◦ <name>` marks a login held on another machine. Only Claude rows carry it: the other providers are single-account subscriptions with nothing to switch.
- **Pooled rows** read several budgets as one. The Claude pool averages accounts on the same plan. The total pool weights Claude, Codex and Grok weeklies by plan price and leaves Kimi out. `ahead` means over the mark, `banking` means under it.
- **Honesty markers.** A cell never pretends. `stale` is a last-known read. `presumed` is a window that rolled over since the last read and that nothing could have spent. `unread` is a burn that is genuinely unknowable. `floor` means the truth is this or higher. `undecided` is a pooled floor below the mark, which decides nothing.

`--json` emits the same position as one machine-readable snapshot.

## Install

Glideslope is Python 3.11+ and the standard library. No runtime dependencies.

**From a clone (recommended).** The sampler, the launchd installers and the views live in the repo, so the full setup needs a clone.

```bash
git clone https://github.com/Stage-11-Agentics/glideslope.git
cd glideslope
python3 glideslope.py
```

**As a tool.** Installs the four console scripts (`glideslope`, `glideslope-sampler`, `glideslope-notify`, `claude-account`) on your PATH.

```bash
uv tool install git+https://github.com/Stage-11-Agentics/glideslope
glideslope
```

The tool install carries the Python modules only. It does not include `views/` or `tools/`.

**Platform.** The Claude read goes through the macOS keychain (`security find-generic-password`), and the background jobs are launchd agents. Codex, Kimi, Grok and OpenRouter reads are plain Python. The test suite runs on macOS and Linux.

> TODO: Linux support for the Claude read. Claude Code on Linux keeps its login in a file, not the keychain, and `claude-account` does not read that path today.

## Three minutes to working

**1. Configure.** Every key is optional. Start from the example.

```bash
mkdir -p ~/.glideslope
cp config.example.toml ~/.glideslope/config.toml
```

Set the plan labels you actually pay for, and your Claude call-signs if you want names other than Alpha, Bravo, Charlie. Put static API keys in `~/.glideslope/keys.txt` as `KEY=value` lines and `chmod 600` it.

**2. First read.**

```bash
python3 glideslope.py
```

Providers you do not use fail soft to a warning. Skip them with `--skip-codex`, `--skip-kimi`, `--skip-grok` or `--skip-openrouter`.

**3. Install the sampler.** A launchd agent that reads the position every 60 seconds and journals it to `samples.db` in the store.

```bash
bash tools/install-sampler.sh
```

Each run costs about 11 seconds. Gaps are fine: the percentages are cumulative, so a missed sample costs resolution, not information. The sampler logs failures and exits 0. It never pages anyone.

**4. Open the Detail view.**

```bash
bash tools/refresh.sh --no-gauge
open views/deck.html
```

Trails thicken as history accrues. Every sample rebuilds the views.

## Configuration

`~/.glideslope/config.toml`. Override the path with `GLIDESLOPE_CONFIG=<path>`. Override the store directory with `GLIDESLOPE_HOME`. Every key is optional. See [`config.example.toml`](config.example.toml) for the annotated version.

```toml
timezone = "Europe/Berlin"             # an IANA name; default: the system's local zone
store = "~/.glideslope"                # samples.db, spend.json, alerts/, state files (default)
keys_file = "~/.glideslope/keys.txt"   # KEY=value lines, mode 0600; static API keys only

[satellite]
name = "laptop"                        # this machine's display name; default: the short hostname

[[satellites]]                         # other machines that hold a Claude Code login
name = "studio"
host = "studio"                        # a plain ssh host name, no options

[claude]
call_signs = { work = "Alpha", personal = "Bravo" }   # roster alias -> display name

[codex]
plan = "20x"
[kimi]
plan = "Coding 15x"
[grok]
plan = "SuperGrok"

[notify]
sink = "macos"                         # macos | url | none
url = ""                               # url sink only
percent = 90
```

Unlisted Claude accounts get the next NATO letter (Alpha, Bravo, Charlie…) in roster order. Plan labels are operator-declared because the providers' APIs do not name the plan.

A satellite `host` must be a plain ssh host name or alias: letters, digits, dots, dashes and underscores, optionally prefixed `user@`. Anything else, including a value that starts with `-` or carries ssh options, is refused. Put ports, keys and jump hosts in `~/.ssh/config` under that alias. The first connection to a satellite accepts and pins its host key (`StrictHostKeyChecking=accept-new`), so make that first connection on a network you trust, or add the key to `~/.ssh/known_hosts` yourself beforehand.

## Providers

| Provider | What is read | From where | Credential |
|---|---|---|---|
| **Claude** | 5h session, weekly all models, weekly Fable. Integer percent. | `https://api.anthropic.com/api/oauth/usage`, via `claude-account status --json` | The access token Claude Code already holds in the keychain, read-only, and only while it is still valid |
| **Codex** | Every native meter, discovered, plus banked full-reset credits | The Codex app-server protocol (`codex app-server`, `account/rateLimits/read`) | None. The Codex CLI owns its login. Its auth file is read for the login email only |
| **Kimi** | Plan quota (7-day cycle) and the rolling 300-minute burst window | `GET https://api.kimi.com/coding/v1/usages` | A static platform key, `KIMI_API_KEY` |
| **Grok** | SuperGrok's weekly compute pool (`creditUsagePercent`). No 5h session is published | `GET https://cli-chat-proxy.grok.com/v1/billing?format=credits`, the endpoint Grok Build's `/usage` uses | The Grok Build login in `~/.grok/auth.json`, refreshed in place when expired (see below) |
| **OpenRouter** | Dollars, rolling 7 days. Metered, so no ◆ mark | `GET https://openrouter.ai/api/v1/key` | A static API key, `OPENROUTER_API_KEY` |

[`PROVIDERS.md`](PROVIDERS.md) has the full contract for each one: payload shapes, units, the failure modes the reader must not get wrong, and how to add the next provider.

Only a logged-in Claude account is readable. The others are served from the state journaled while they were live, with their windows rolled forward to the current ones. Each read is journaled per account as limits and observation time only.

A provider whose live read fails (a Codex app-server timeout, a Kimi HTTP error, a Grok billing error) degrades to its last-known-good read, marked stale. It does not vanish from the position, and one provider's outage never breaks the others.

## Credential discipline

This is the part to read before you run it.

**Glideslope has no credential store.** It holds no OAuth token, refresh token or password of its own. It mints no tokens. The sample store contains usage numbers and reset clocks only.

- **Claude.** `claude-account` reads the access token Claude Code already keeps in the keychain, and only while that token is still valid. It never performs an OAuth refresh, never writes the keychain, and stores no token. Claude Code stays the only process that ever writes or refreshes a Claude token. A read is a pure reader: no lock, no contention, safe to run any time.
- **Codex.** Read through the documented app-server protocol. Codex's credential files are never read for tokens, copied or refreshed.
- **Kimi.** A static platform key. Nothing rotates, so there is nothing to serialize.
- **OpenRouter.** A static API key.
- **Static keys** come from the environment first, then from `keys_file` (`KEY=value` lines, mode `0600`). The file exists because a launchd job carries no shell environment. Only static keys are ever read this way.
- **Claude Code's prompt history.** `~/.claude/history.jsonl` is read only after a login change has been observed, and only to timestamp the `/login` that caused it. Only `/login` entries are looked at, and nothing from the file is stored: the switch log keeps that command's time and the two roster aliases, never a prompt.
- **Grok is the one exception, and it is narrow.** SuperGrok's consumer meter has no static key. It sits behind the Grok Build login, whose access tokens last about six hours. Grok refreshes them in the background while a Grok process is running. A sampler on a 60-second clock cannot stay live on a six-hour token, so when it ticks against an expired token it performs the same refresh Grok does: an OIDC `refresh_token` grant against the first-party issuer `https://auth.x.ai`, written back to `~/.grok/auth.json` atomically (temp file plus rename, under `auth.json.lock`, re-reading the file under the lock so a concurrent Grok refresh is not clobbered). The token is never copied anywhere else and never printed. A custom identity provider is refused rather than guessed. This is the one credential write in the program.

The history behind the Claude rule: an earlier design kept every account's refresh token so it could read any account on demand. Refresh tokens rotate and are single-use, so every read had to write the store back, and a concurrent read could strand an account. Login homes removed the need for any of it.

See [`SECURITY.md`](SECURITY.md) for these promises restated as invariants.

## claude-account: several Claude logins on one machine

Claude Code keeps one login per config directory. With `CLAUDE_CONFIG_DIR` unset it uses `~/.claude`, `~/.claude.json` and the keychain item `Claude Code-credentials`. With `CLAUDE_CONFIG_DIR=<dir>` it uses `<dir>/.claude.json` and its own keychain item, `Claude Code-credentials-<first 8 hex of sha256(dir)>`.

So one machine can hold every account at once, one **login home** each:

```
~/.claude                      the default home (whatever account it holds)
~/.claude-profiles/<name>/     one extra home per account; shared entries (settings,
                               skills, hooks, projects/ transcripts) symlink back into ~/.claude
```

Switching is choosing which home a **new** session starts in. Running sessions are never moved.

```
claude-account status [--json]     usage for every account holding a login here
claude-account homes [--json]      every login home on this machine and who it holds
claude-account use <account|auto>  route new sessions to an account (auto = Glideslope's pick)
claude-account home [account]      print the CLAUDE_CONFIG_DIR a new session should use
claude-account exec [account] -- cmd   run cmd under an account's home
claude-account login <account|name>    create a login home and open Claude Code in it for /login
claude-account link                refresh the shared-entry symlinks in every profile home
claude-account list                the roster (identities only)
claude-account which               where new sessions go, and where that account is logged in
claude-account save [alias]        record every logged-in account's identity in the roster
claude-account remove <alias>      drop an account from the roster
```

An `<account>` is a call-sign, a roster alias or an email. `CLAUDE_ACCOUNT=<account>` overrides the selection for one launch. The selection (`~/.claude/accounts/selection.json`) is an email, never a token.

To route every new `claude` through the selection, add a shell function (zsh):

```zsh
claude() {
  if [[ -n "$CLAUDE_ACCOUNT" || ( -z "$CLAUDE_CONFIG_DIR" && -z "$CLAUDE_ACCOUNT_HOME" ) ]] \
      && (( $+commands[claude-account] )); then
    claude-account exec -- claude "$@"
  else
    command claude "$@"
  fi
}
```

A session's children inherit its home, so subagents and `claude -p` helpers stay on their parent's account.

**`auto` asks Glideslope** (`glideslope --pick`). It is sticky: stay on the selected account while its 5h session and both weeklies sit under the notify line, because moving costs a rebuilt prompt cache on every resumed conversation. Past the line, move to the account with the most slack against its own ◆ mark, among the accounts this machine holds a login for, skipping any whose own 5h session is already over. With no refuge, stay and say so.

**Dormant accounts.** When a subscription lapses, `claude-account` marks the roster entry dormant. A dormant account keeps one dimmed row in Weekly status and drops out of All windows, the pools, the pick and the alerts. A live login always outranks the flag: log back in and every surface un-dims itself.

## Satellites: logins on other machines

Each machine reads exactly the Claude accounts it holds a login for. To see accounts logged in elsewhere, run a beacon there. It reads itself inside its own GUI session and writes numbers, reset clocks and login emails to `~/.glideslope/satellite.json`. This machine collects it with a plain `ssh <host> cat`.

```bash
bash tools/install-satellite.sh <ssh-host> [name]
```

Then list the machine under `[[satellites]]` in the config.

Why a beacon and not an SSH pull: macOS will not release a keychain secret to an SSH session (`errSecInteractionNotAllowed`, exit 36). The read has to happen on the satellite. No credential crosses the network.

The merge rule is one line: a remote reading only replaces a local one that is older. An unreachable satellite costs its own row and a warning, never the position. A beacon older than an hour stops being a reading.

## The views

Two built HTML pages render the position and the sample history. Each is self-contained and opens from disk. Nothing is hosted.

- **Detail view** (`views/deck.html`). Weekly status, the full all-window ledger, the approach plot with selectable sampler history, a deviation register, the reset horizon and the switch log. The approach plot flies one weekly lens at a time (All models or Fable), with the 5h throttles alongside in a different symbol. Hover any mark to see its flight path: a ray forward at the current rate, ending on the 100% line with a time-to-empty, or at the reset edge with what it finishes at.
- **Popup** (`views/popup.html`). A compact approach plot for a menu-bar or side panel. Its top bar filters 7 Day, 5 Hour, Specialty and Anthropic only. It plots the Claude pool and the total pool, and its `DETAIL VIEW →` link opens the Detail view.
- **History** (`views/history.html`). The approach plot on a real clock. Each window instance gets its own beam from its start to its reset, so weeks stack up behind you. It reads only the sample store, never a provider.

Rebuild without touching any provider:

```bash
bash tools/refresh.sh --no-gauge
```

The pages mark gauge age on their face. A cached 5h read taken inside the window still running shows as `last known` (a floor, no ◆ mark). One taken before that window opened reads `unknown`.

Screenshots are coming.

## Threshold alerts

`notify.py` (`glideslope-notify`) runs from the sampler on the position it already holds. No second read. When a window on the Claude account this machine is logged into crosses `percent` (default 90), it sends one alert.

- **Local account only.** An alert is for the person at this keyboard.
- **Once per window instance.** The dedup key is account, meter and that window's own reset instant, so the next window can warn again on its own merits.
- **Undelivered is not sent.** State is written only after the sink accepts the alert.
- **It ends in an action.** The alert names the account with the most room against its own ◆ mark.

`glideslope-notify --dry-run` prints what would be sent and what already was.

With [Pillow](https://pypi.org/project/pillow/) installed (the optional `images` extra), notify also draws a small approach plot of the alerting window. Without it, alerts are text only.

Sinks: `macos` posts a Notification Center banner through `osascript` (text only). `url` POSTs one JSON object, `{"title", "body", "tenant": "glideslope", "image": <path>}`, to the configured listener, which is how a menu-bar host delivers the banner under its own identity with the plot attached. `none` sends nothing.

## Other jobs

All optional, all launchd agents under `ai.stage11.glideslope.<job>`, all removable with `--remove`.

| Installer | What it does |
|---|---|
| `tools/install-sampler.sh` | The 60s sampler. Journals every read and rebuilds the views |
| `tools/install-login-watch.sh` | Restarts the sampler the moment this machine's Claude logins or selection change. Re-run after creating a new login home |
| `tools/install-satellite.sh <host> [name]` | Installs the beacon on another machine over ssh |
| `tools/install-kimi-resume.sh <surface>` | Nudges a stalled Kimi Code agent with `continue` when the burst window refills and the week's quota is not spent. Requires the [c11](https://github.com/Stage-11-Agentics/c11) terminal |

Grok Build can also feed a fresher reading between sampler ticks: register `tools/grok_hook.py` as a `SessionStart` and `Stop` command hook in Grok's hooks config. It writes a numbers-only snapshot that the reader prefers while it is under 90 seconds old. It prints nothing and always exits 0.

## CLI reference

```
glideslope                       the position, as markdown
glideslope --json                the normalized snapshot
glideslope --no-refresh-claude   use the cached Claude gauge (fast)
glideslope --skip-codex | --skip-kimi | --skip-grok | --skip-openrouter | --skip-claude
glideslope --skip-satellites     do not read other machines' beacons
glideslope --no-switches         omit the recent-switch history
glideslope --color auto|always|never   ANSI color (auto = TTY only; NO_COLOR is honored)
glideslope --watch [--interval 10] [--refetch-seconds 150]   live display for a dedicated pane
glideslope --pick [--json]       the Claude account new sessions here should use
```

`--codex-snapshot`, `--kimi-snapshot`, `--grok-snapshot` and `--claude-snapshot` read saved payloads instead of live providers. The tests use them. `tools/kimi_probe.py` and `tools/grok_probe.py` show a provider's raw payload beside Glideslope's normalization when a row looks wrong.

## Agent integration

Glideslope is built to be driven by an agent. The output is already relay-ready markdown, and a skill ships in the repo: [`skills/glideslope/SKILL.md`](skills/glideslope/SKILL.md). Install it for Claude Code by symlink:

```bash
ln -s "$PWD/skills/glideslope" ~/.claude/skills/glideslope
```

Then ask your agent "where am I on usage?" and it runs `glideslope` and relays the tables unfenced.

For agents developing Glideslope itself, [`CLAUDE.md`](CLAUDE.md) (also `AGENTS.md`) holds the rules.

## Private ledger

If `<store>/spend.json` exists, Glideslope renders an API-equivalent section: what the tokens behind your Claude and Codex meters would cost at list API prices. It is a valuation, never money spent. The producer of that ledger is not part of this repo. Without the file, the section is simply absent.

## Status

Early and actively developed. The config contract and the `--json` account shape are the stable surfaces. Expect the rest to move.

## Origin

Glideslope was born inside Gregorovich, Atin Woodard's personal infrastructure, where it has tracked a multi-account coding-agent rotation since July 2026. History before the public cut lives there.

## License

[MIT](LICENSE)

---

glideslope is a [Stage 11 Agentics](https://stage11.ai) project
