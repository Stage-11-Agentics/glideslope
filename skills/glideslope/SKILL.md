---
name: glideslope
description: Report the live cross-provider subscription position (Claude accounts, Codex, Kimi, Grok, OpenRouter) as one usage table with the ◆ Glideslope even-burn mark (used above it = ahead of budget), or run and explain API-equivalent spend. Load on any usage, limit, quota, position, spend, "where am I", "how much is left", or "which account should I use" check.
homepage: https://github.com/Stage-11-Agentics/glideslope
---

# Glideslope: position check

Run this, then relay the result:

```bash
glideslope
```

If `glideslope` is not on PATH, run it from the clone: `python3 <path-to-clone>/glideslope.py`.

**Relay stdout verbatim as markdown. Never put it inside a code fence.** The script already emits GitHub-flavored markdown. Fenced, it shows raw `| pipes |`. Unfenced, it renders as clean tables. Add nothing and reformat nothing: the script owns the format. Keep the meter badges and the honesty markers exactly as printed.

## Flags

| Flag | Use |
|---|---|
| `--no-refresh-claude` | Fast. Use the cached Claude gauge when the position was just checked |
| `--skip-codex`, `--skip-kimi`, `--skip-grok`, `--skip-openrouter`, `--skip-claude` | Trim providers the user does not have or did not ask about |
| `--skip-satellites` | Do not read other machines' beacons (offline) |
| `--skip-seats` | Leave out the remote agent seats line |
| `--no-switches` | Omit the recent-switch history |
| `--json` | Machine-readable snapshot, for when you need to compute on the numbers |
| `--pick` | Name the Claude account new sessions on this machine should use |

## How to read it

- **◆ mark**: where an even burn to the reset would put used right now. Used above it is ahead of budget. Used below it is banking capacity, which on a flat-fee plan means capacity that expires unused at the reset.
- **Meter badges**: the letter is the account, the outline is the window. Circle ⃝ weekly all models, triangle ⃤ 5h session, diamond ⃟ weekly Fable.
- **Fable** is Anthropic's model tier with its own weekly meter. It is often the binding weekly quota. `none` on a Claude row means the plan does not include it.
- **● name** marks the machine a Claude account is logged into. `◦ name` is a login held elsewhere. Only Claude rows carry it.
- **Honesty markers**: `stale` (last-known read), `presumed` (rolled over, nothing could have spent it), `unread` (unknowable), `floor` (the truth is this or higher), `undecided` (a pooled floor below the mark). Never restate one of these as a plain number.
- **On remote seats**, when present, names the coding agents running on cloud sandboxes and the account each bills: `6 Grok (Grok) · 1 Claude (Alpha)`. Mention it in a position check. That burn is already inside each account's meters, so it says who is spending, never extra usage; never add it to a number. A `stale` part is an old file, not the current fleet: say so.
- **API-equivalent**, when present, is a valuation at list API prices, never money spent. Say so if you summarize it.
- **💸 amount** on a 5-hour or 7-day all-models cell is usage-credit spend during that window. Real money. The percent and the ◆ stay. The 5-hour amount is inside the 7-day amount. Absence means no known burn. `floor` after the amount means it understates. Relay it exactly. It is not the API-equivalent panel, and it is not on Fable.

Output order: login banner, On remote seats (only when seats are live), Weekly status, All windows, OpenRouter, API-equivalent (only if the user keeps a ledger), Codex reset banks (only when credits are banked), Recent switches. Relay all of it, in order.

## API-equivalent spend

When asked for a spend estimate from local coding-agent records, run:

~~~bash
glideslope-spend --print
~~~

If the command is not on PATH, run `python3 <path-to-clone>/spend.py --print`. It updates
`<store>/spend.json` and prints combined 24-hour, 7-day, 30-day and all-time totals plus
7-day, 30-day and all-time rows for each Claude account, Codex, Grok and unattributed spend.
Claude and Codex dollars use API list rates. Grok Build dollars use the
`costUsdTicks` supplied by its local CLI, not the LiteLLM rate table. The totals are estimates,
not subscription charges or an invoice. It covers local Claude Code, Codex and Grok Build only.

Claude account totals use active-login samples and switch-log events, with aliases resolved
through `glideslope.call_sign`. Attribution starts at the earliest login evidence from either
source. Requests without earlier evidence or with evidence more than 24 hours old remain
`unattributed`. `--no-collect` rebuilds spend from cached per-file records, writes
`<store>/spend.json`, and does not reread transcripts; add `--print` to also display totals.
It can miss transcript changes since the last normal collection and may still refresh the price
table.

A cold first collection took about 100 seconds and reached 1,059 MiB maximum resident memory
in one benchmark. A separate warm full-producer run took about 18 seconds and reached 653 MiB
maximum resident memory. It reuses unchanged files from the store's per-file cache. Normal
collection checks a SHA-256 hash of the collector source, file size and modification time;
`--no-collect` checks the cached parser hash and payload without checking transcript files.
These measurements are approximate and depend on history size and machine. Normal runs refresh
the public price table when it is missing or more than 24 hours old. Use `glideslope-spend
prices` to inspect rates or `glideslope-spend prices --refresh` to force a refresh. From a
clone, run `python3 <path-to-clone>/spend.py prices` or add `--refresh` to force a refresh.

The `pricing` object in `spend.json` contains counters, not pricing status labels. Run
`glideslope-spend prices` to see rate labels in its `status` column. The `standard` fallback
status is omitted from that column; affected tokens appear only under `underpriced_tokens`.

Read these pricing labels and counters alongside the totals:

- `list` uses an available list rate; there is no separate `list_tokens` counter.
- `standard` means a tiered speed used the standard rate because no separate rate was available. This includes `flex`; those tokens count in `underpriced_tokens`. For a cheaper tier such as `flex`, the counter name does not mean the estimate is below the actual cost.
- `derived` means a missing tier-specific input, output or cache rate was derived by applying the tier-to-base input-rate ratio to the base rate; its tokens appear in `derived_tokens`.
- `estimated` means an explicit model-and-speed multiplier was used for a premium rate that is not published; its tokens appear in `estimated_tokens`.
- `unpriced_tokens` lists up to ten model entries with no usable rate. Those tokens remain counted but have no dollar value.
- `speed_evidence` counts requests, or Grok model calls, by provider, speed and evidence source. `unknown` means the request has no speed evidence.
- `fast_mode_armed` lists positive detections of premium speed defaults in local Claude Code or Codex settings. An absent provider is inconclusive because settings may be unarmed, missing or unreadable. It does not show which requests used premium speed.
- `litellm_age_h` is the cached LiteLLM price table's age in hours; `null` means no table is available. Old or missing rates can make list-rate totals a floor. This age does not describe Grok's cost ticks.

## Switching Claude accounts

`claude-account use <account>` routes new Claude Code sessions to that account's login home. `claude-account use auto` lets Glideslope pick: it stays put while the current account is under the alert line, then moves to the account with the most slack against its own ◆ mark. Running sessions are never moved. Suggest a switch. Do not run it unless the user asks.

`claude-account whose <token-file> --expect <account> --json` says which account a `claude setup-token` token really bills, from the organization ID the API returns, never from the file's name. Run it before handing a token to remote agents; exit 0 means it bills the expected account. An account nobody is logged into still reads live when a meter token for it sits in `~/.claude/accounts/meter-tokens/` on a satellite (rows marked `source: meter-token`).

## The views

Offer a view when the user wants to see the position rather than read it. One command rebuilds the page from the position it just read and opens it in the default browser:

```bash
glideslope --open            # the Detail view: weekly status, all windows, the approach plot with sampler history, reset horizon, switch log
glideslope --open popup      # the approach plot and register alone
glideslope --open history    # the approach plot on a real clock, from the sample store only
```

The pages are self-contained HTML files under `views/` in the clone, so `--open` needs the clone, not a tool install. They reload themselves every minute. Never upload or host one.

## When a row looks wrong

Read the provider's section in `PROVIDERS.md`. `tools/kimi_probe.py` and `tools/grok_probe.py` make one request and print the raw payload beside Glideslope's normalization. A Grok probe may refresh the Grok Build access token in `~/.grok/auth.json` in place. Never debug a provider with repeated ad-hoc requests.

## Installing this skill

From the clone:

```bash
mkdir -p ~/.claude/skills
ln -s "$PWD/skills/glideslope" ~/.claude/skills/glideslope
```

A symlink keeps the skill current with `git pull`.
