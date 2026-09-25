---
name: glideslope
description: Report the live cross-provider subscription position (Claude accounts, Codex, Kimi, Grok, OpenRouter) as one usage table with the ◆ Glideslope even-burn mark (used above it = ahead of budget). Load on any usage, limit, quota, position, "where am I", "how much is left", or "which account should I use" check.
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
| `--no-switches` | Omit the recent-switch history |
| `--json` | Machine-readable snapshot, for when you need to compute on the numbers |
| `--pick` | Name the Claude account new sessions on this machine should use |

## How to read it

- **◆ mark**: where an even burn to the reset would put used right now. Used above it is ahead of budget. Used below it is banking capacity, which on a flat-fee plan means capacity that expires unused at the reset.
- **Meter badges**: the letter is the account, the outline is the window. Circle ⃝ weekly all models, triangle ⃤ 5h session, diamond ⃟ weekly Fable.
- **Fable** is Anthropic's model tier with its own weekly meter. It is often the binding weekly quota. `none` on a Claude row means the plan does not include it.
- **● name** marks the machine a Claude account is logged into. `◦ name` is a login held elsewhere. Only Claude rows carry it.
- **Honesty markers**: `stale` (last-known read), `presumed` (rolled over, nothing could have spent it), `unread` (unknowable), `floor` (the truth is this or higher), `undecided` (a pooled floor below the mark). Never restate one of these as a plain number.
- **API-equivalent**, when present, is a valuation at list API prices, never money spent. Say so if you summarize it.

Output order: login banner, Weekly status, All windows, OpenRouter, API-equivalent (only if the user keeps a ledger), Codex reset banks (only when credits are banked), Recent switches. Relay all of it, in order.

## Switching Claude accounts

`claude-account use <account>` routes new Claude Code sessions to that account's login home. `claude-account use auto` lets Glideslope pick: it stays put while the current account is under the alert line, then moves to the account with the most slack against its own ◆ mark. Running sessions are never moved. Suggest a switch. Do not run it unless the user asks.

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
