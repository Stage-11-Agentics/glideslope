# Glideslope — provider read paths

One section per provider Glideslope reports. Each answers the same four questions:
**where the numbers come from, what the units mean, what can go wrong, and what the
credential discipline is.** Every provider is normalized into the same account shape and
rendered by the same tables — nothing is a special case at the presentation layer.

Paths written `<store>/` are in the store directory: `store` in the config, default
`~/.glideslope/`, overridden by `GLIDESLOPE_HOME`.

The shared shape (`glideslope.py`):

```jsonc
{
  "provider": "Kimi",          // table column + color key
  "account":  "kimi",          // stable id for the sample store
  "display":  "Kimi",          // the call-sign; never a store alias or email
  "plan":     "Coding 15x",    // operator-declared
  "plan_raw": "LEVEL_INTERMEDIATE",  // whatever the provider itself said
  "active":   true,            // meaningful for Claude ONLY — see below
  "stale":    false,
  "dormant":  false,           // Claude ONLY, since 2026-09-11 — see below
  "dormant_since": null,       // "YYYY-MM-DD" when dormant, else null
  "observed_at": "…",          // the gauge's own observation time, not our clock
  "limits": [{ "meter_id": "session", "label": "5h session", "used_percent": 4.0,
               "window_minutes": 300, "resets_at": "…", "anchored": true }]
}
```

Two conventions the renderers depend on:

- **`meter_id: "weekly_all"` + `label: "Weekly · all models"`** is what puts a provider in
  the Weekly-status hero table. A provider whose main quota is weekly must use them.
- **`active`** means exactly *"the account Claude Code is logged into on THIS machine."* It is
  now the local half of a bigger fact: **`logins`**, the list of named satellites holding this
  account, local first. Claude is the only provider with accounts to switch between, so only
  Claude renders either — in the CLI, the Detail view, and the popup preview alike. A
  single-account subscription wearing the same badge dilutes the one signal the surfaces
  lead with.

---

## Claude — Alpha, Bravo, Charlie, Delta

| | |
|---|---|
| Source | `claude-account status --json` → `<store>/account-snapshot.json` |
| Windows | `session` (5h), `weekly_all` (7d), `weekly_fable` (7d, often the binding one) |
| Units | integer percent, 1% steps |
| Credential | **none held.** Borrows Claude Code's live access token, read-only |
| Request | the same GET Claude Code's own `/usage` makes, sent with the CLI's `User-Agent` and `anthropic-beta` header; the endpoint is not documented and answers the CLI's identity, so a 429 is possible under a fast cadence — a rate-limited read is served from cache, marked stale, and never retried on its own |

**The plan comes from the roster's tier, not from a constant.** The usage
payload never names the plan, so every Claude account used to be labelled `Max 20x` by
construction. When one account's renewal moved it to **Pro**, that constant was the reason
nobody noticed Fable had stopped being included: a Pro account has no Fable allowance, its
payload carries no `weekly_scoped` Fable limit at all, and Fable requests fail with *"out of
usage credits"*. `reconcile_plans()` reads `tier` from `~/.claude/accounts/roster.json`
(claude-account rewrites it on every status read) and maps it — `default_claude_max_20x` →
Max 20x, `default_claude_ai` → Pro; an unknown tier keeps its raw string. A Claude account
with no Fable meter now renders **`none`**, never a bare dash: the plan does not include it.

**The pool only averages equal plans.** Both halves of the pooled read are unweighted means,
which say something true only when the caps behind them match. So the pool is the largest
group of accounts sharing one plan, and the row names it — `Claude · pooled (2 · Max 20x)`.
A Pro account sits out rather than diluting two Max 20x budgets.

**Only a logged-in account is readable.** `claude-account` reads the access token Claude Code
already keeps in the keychain, and only while it is still valid — it never refreshes, never
writes the keychain, and stores no token of its own. So a read is a pure reader: no lock, no
contention, safe to run any time from anywhere.

**Login homes — a satellite can hold every account at once (added 2026-09-12).** Claude Code
keeps one login per config directory: the default home (`~/.claude` + `~/.claude.json` +
keychain item `Claude Code-credentials`) and, for each `CLAUDE_CONFIG_DIR`, that directory's own
`.claude.json` and its own keychain item `Claude Code-credentials-<sha256(dir)[:8]>`. So
`~/.claude-profiles/<name>/` is one extra home per account — every shared entry (settings,
skills, hooks, `projects/` transcripts) symlinked back into `~/.claude`, only per-install
runtime state kept local — and Claude Code stays the only writer of every token, exactly as
with a single login. `claude-account status` reads each home's token; every account with a
home here is a **live** row, with `logged_in` / `home` in its JSON beside the old `active`.
Before this, the limit was one login per machine (from 2026-08-15, each satellite held one),
and every other account was journaled.

**Switching is choosing a home, not logging in.** `claude-account use <account|auto>` writes
`~/.claude/accounts/selection.json` — an email, never a token — and a shell `claude()`
wrapper (for example in `~/.zshrc`) starts every new session via `claude-account exec`, which sets
`CLAUDE_CONFIG_DIR` to the selected account's home. `CLAUDE_ACCOUNT=<account>` overrides one
launch; a session's children inherit its home (`CLAUDE_CONFIG_DIR` / `CLAUDE_ACCOUNT_HOME`), so
subagents and `claude -p` helpers stay on their parent's account. A selection whose login is
gone falls back to the default home's account, in the launcher and in Glideslope alike, so
`active` — the account new sessions here start in — can never disagree with what a fresh
`claude` actually gets. Running sessions are never moved; a switch only changes where the
next one starts.

**Logged in means selected.** A satellite may hold several login homes but is logged in to one
account: its selection. `logins` names the satellites whose selection an account is (a beacon's
`login_email`); `held_on` names every satellite holding a home for it (`login_emails`). The
presumed-fresh check and `--pick` read `held_on`, because an unselected home can still be launched
into; every "logged in here" mark reads `logins`.

**Switching from the popup.** Clicking a Claude row in the popup opens a confirm dialog; only its
confirm button sends the host a manifest action label, `Use <Account>` or `Log in <Account>`, over
the same `runAction` bridge as Refresh. The host maps each label to a command (for example
`tools/switch-account alpha`, which switches and then rebuilds the views from the cached gauge in
about 2 s so the popup redraws at once, or `tools/claude-login-pane alpha`, which opens a terminal at
Claude Code's `/login` for that account's home). The page never holds a command. Without a host the
dialog says which command to run instead.

**`auto` asks Glideslope.** `glideslope.py --pick --json` → `pick_account()`: sticky — stay on
the selected account while its 5h session and both weeklies sit under `NOTIFY_PERCENT`
(moving costs a rebuilt prompt cache on every resumed conversation); past the line, move to
`best_alternative()` among the accounts *this machine holds a login for* (a pick you cannot
launch into is no pick), skipping any whose own 5h session is already over. With no refuge,
stay and say so. `--pick` reads only Claude — a launch is waiting on the answer.

**A login change refreshes the board by itself (added 2026-09-13).** Two layers:
`load_claude_snapshot()` expires its cached gauge the moment it no longer describes this
machine's logins (`snapshot_matches_logins()`: the selected account must be the snapshot's
`active` one, and the logged-in set must match), so any read after a `/login` or
`claude-account use` fetches the new account's numbers instead of waiting out
`CLAUDE_CACHE_SECONDS`. And `tools/login_watch.py` runs under launchd `WatchPaths`
(`tools/install-login-watch.sh`: `~/.claude.json`, each home's `.claude.json`, the selection)
— those files churn constantly, so it compares a login fingerprint
(`<store>/login-fingerprint.json`) and only on a real change `kickstart -k`s the sampler.
Measured: selection change → fresh sample + rebuilt deck in ~20s. The sampler itself reads
with `--max-age-seconds 170`, so Claude usage is read about every 3 minutes while other providers
read every tick; a login change still forces a fresh read at once.

**The usage endpoint throttles, and the limit is shared.** At one read a minute, some 40% of
reads came back HTTP 429 (measured 2026-09), and a refused run refused every account at once.
So `claude-account` treats a 429 as a pause for the whole machine: it records
`~/.claude/accounts/.usage-backoff.json` (Retry-After seconds, bounded to 1 to 30 minutes, else 5), sends
no usage request until it passes, and serves the last good numbers marked stale with the time
of the next read. Reads within one run are spaced a couple of seconds apart.

Each satellite publishes every home's login (`login_emails`, beacon schema 3) beside the
selected one (`login_email`), and Glideslope folds them together — see **Satellites** below.

The other accounts are not a blind spot. Every read is journaled per account while an account
IS the live one, and `roll_forward_windows()` advances a journaled window whose reset has
since passed. A logged-out account therefore shows either its last-known position inside a
still-running window, or a **presumed**-fresh one past the reset (marked, never bare), or
**unread** where the burn is genuinely unknowable.

Each live account read is journaled per account as limits and observation time only. When a
later read cannot reach an account, Glideslope serves that account's last-known state,
advancing reset clocks arithmetically and marking burn presumed or unread according to the
rules above. No credential is stored or refreshed.

Who is logged in is read separately from `~/.claude.json`, which is free, instant, and needs
no API call, so a fresh login shows up immediately even on a warm gauge.

**Dormant accounts (added 2026-09-11).** `claude-account` now writes `"dormant": true,
"dormant_since": "YYYY-MM-DD"` onto a roster entry (`~/.claude/accounts/roster.json`) once its
subscription is no longer active. Glideslope joins this onto each
Claude account by email and folds it into the shared shape as `"dormant": bool` /
`"dormant_since": str | None`, the read-only half of the `--json` contract every surface
builds against.

**A live login always outranks the roster.** If any satellite is actually logged into a
dormant account, dormancy is refused (`dormant` stays `False`) and a warning names which
satellite — inventing dormancy over a login in progress would be the same lie
`roll_forward_windows()` already refuses to tell about a presumed-fresh window. This is the
seamless path back: resubscribe and log in, and every surface un-dims itself with no roster
edit at all.

**What it excludes.** A dormant account has no position, not a low one, so it is dropped
rather than zeroed: out of the pooled weekly (`claude_pool`), out of `best_alternative`'s
candidates, out of `notifiable_windows`, and `roll_forward_windows` gives its rolled-over
windows the unread treatment (`used_percent: None`, no `presumed` marker) rather than
presuming them fresh. It keeps exactly one row in **Weekly status** — dressed down, `—` in
every numeric column — and is omitted entirely from **All windows**, where a dead account's
windows would just be noise.

---

## Satellites — one live account per machine (added 2026-08-15)

| | |
|---|---|
| Source | `ssh <host> cat ~/.glideslope/satellite.json` — a file the satellite wrote for itself |
| Publisher | `tools/satellite_beacon.py` under launchd, every 300s (`tools/install-satellite.sh`) |
| Join key | **email** — store aliases are per-machine (`personal` on one satellite, `personal-2` on another) |
| Credential | none crosses the network; the beacon carries numbers, reset clocks, and login emails only |
| `holds` | single-account providers the satellite is also signed into (`{"Codex": "<email>"}`) — email decoded from the cached id_token's claims, the token never read past that |

**Shared single-account meters (Codex).** Codex's windows are server-side and account-global:
the same account signed in on two machines reports the same numbers from either, so the fleet's
whole burn is already inside the one local read (verified across two satellites, 2026-08-29).
The beacon's `holds` is what makes this legible — a Codex row whose holders match wears every
machine's mark (on a two-machine setup, say a laptop and a studio desktop:
`Codex ● laptop ◦ studio`), and a satellite holding a *different* Codex
account warns that its burn is NOT in these numbers. Accounts held only elsewhere render
italic (dim in the terminal): real burn, in the pool, not driven from this keyboard.

**Why a beacon and not an SSH pull.** macOS will not release a keychain secret to an SSH
session — `security find-generic-password -w` returns `errSecInteractionNotAllowed` (exit 36)
because the ACL prompt has no session to draw in. The gauge can therefore only be taken *on*
the satellite, inside its GUI session. A LaunchAgent bootstrapped into `gui/<uid>` is exactly
that session and reads the keychain with no prompt once Claude Code has been logged in there
(verified on a remote satellite, 2026-08-15). So the satellite reads itself and leaves the answer where a
plain `cat` can collect it.

**The merge rule is one line: a remote reading only ever replaces a local one that is older.**
That keeps it safe in both directions — a stale beacon cannot overwrite a fresh local gauge,
and a fresh beacon always beats a floor journaled hours ago. Only the account a satellite is
*on* is taken as a reading; its other rows are its own journal, second-hand, and ignored.

**The presumption this fixed.** `roll_forward_windows()` zeroes a rolled window only where
nothing could have spent it, and that test used to read *"not logged in here."* The day a second
satellite took a login of its own that became false, and the board showed that account's rolled 5h
session as a calm `0% presumed` while the other satellite was free to burn it. The test is now *"no satellite is logged
into it"*; an account held elsewhere reads **unread**, never a presumed zero. Adding a
satellite is therefore an honesty fix before it is a feature.

Failure is soft and local: an unreachable satellite costs its own row and a warning, never the
position. A beacon older than an hour stops being a reading at all.

---

## The pooled Anthropic weekly

Several Claude quotas on the same plan, each on its own separately-anchored clock, read as
one budget:

    used = Σ usedᵢ / n        ◆ = Σ phaseᵢ / n

`phaseᵢ` is how far account *i* is through its own window, which is exactly what even burn
predicts `usedᵢ` to be — so the mean of the phases *is* the pool's even-burn mark, offset
windows and all, and the ◆ vocabulary carries over untouched. Both coordinates being means is
also why the pool lands on the approach plot's existing `y = x` beam with no new axes. (It
would generalize to unequal plans by weighting each term by its cap; today the pool only
averages equal plans, as above.)

## The flight path

Hovering any mark on the approach plot draws where that window ends up if nothing
changes: a red dotted ray forward from the mark, and — when it reaches the ceiling
before the reset — a dot on the 100% line labelled with the time until it lands
there. When it does not reach the ceiling the same ray runs to the reset edge and
says what it finishes at, because leaving 46% unspent is the other half of the
thesis, not an absence of news.

The geometry falls straight out of the axes. Constant rate means used ∝ elapsed,
so on (elapsed %, used %) the projection is exactly the ray from the window's own
origin through the mark. It is drawn **forward only** — the stretch behind the mark
is the trail, which is a reading, and redrawing it as a projection would dress
measurement up as a guess.

**Only a live reading is projected.** A presumed zero has no rate, an unread window
has no number, and a stale floor's rate stopped being true when the read did — a
countdown to 100% drawn from any of them is a confident time-of-death with nothing
behind it. `exhausts_at()` in `glideslope.py` is the one implementation; the Detail
view, the popup and the notifier all call it, so they cannot disagree about when a window
dies.

---

**A pooled read is a floor whenever any component is.** A stale account's used can only have
risen since it was read, and an unread window contributes at least zero, so the sum
understates and never overstates. That asymmetry buys one verdict for free — a floor already
above the mark is ahead of budget, certainly — and refuses the other: a floor *below* the mark
decides nothing, and the row says `undecided` rather than a comfortable "trailing."

---

## Threshold alerts (added 2026-08-15)

`notify.py`, run by the sampler on the position it already holds — no second read,
no second token. A window on **the account this machine is logged into** crossing
`NOTIFY_PERCENT` (`[notify] percent`, default 90) fires one banner through the configured
sink.

| | |
|---|---|
| Windows watched | `session`, `weekly_all`, `weekly_fable` |
| Delivery | `[notify] sink`: `macos` (an `osascript` banner, no image), `url` (`POST` one JSON object — `title`, `body`, `tenant`, `image` as a path — to `[notify] url`), or `none` |
| Dedup state | `<store>/notified.json` |
| Manual | `python3 notify.py --dry-run` prints what would be sent, and what already was |

Four rules it exists to keep:

1. **Local account only.** A notification is for the person at this keyboard, and
   the only account they can act on without a web login is the one they are on.
   A remote satellite crossing 90% is news nobody here can use, and nobody may be
   watching its screen.
2. **Once per window instance.** The sampler runs every five minutes; a naive
   threshold test would fire twelve banners an hour for as long as you stayed over
   the line. The dedup key is account + meter + **that window's own reset
   instant**, so the next window is free to warn again on its own merits.
3. **Undelivered is not sent.** State is written only after the sink accepts the
   banner, so a sink that was down postpones the warning instead of cancelling it.
4. **It ends in an action.** "Charlie is at 92%" is a fact you already had a
   dashboard for. The third clause — *"Bravo has the most room — 18% against a
   ◆ 33%"* — is the reason to read a banner instead of opening one.
   `best_alternative()` ranks by slack against each account's **own** beam, not by
   raw percent: 60% six days into a week is capacity about to expire, and 20% one
   day in is already behind budget.

**The attachment is the approach plot for that one window** — the beam, the real
track from `samples.db`, the mark, and the red flight path to the ceiling, in the
deck's own palette. A banner that says "92%" makes you open something to find out
what it means; a banner shaped like the instrument does not. Drawn with Pillow at
512px (`draw_approach`), with the percent set in whichever corner the drawing left
empty. The image travels only on the `url` sink, as an additive field: a receiver that
ignores keys it does not know still delivers the words and simply shows no picture.

---

## Codex

| | |
|---|---|
| Source | the Codex **app-server protocol**, read-only |
| Windows | every native meter, discovered — never a fixed bucket list |
| Units | `usedPercent`, coarse (an active new window can still round to 0%) |
| Credential | never read, copied, or refreshed by this project |

Two Codex-specific subtleties: `usedPercent: 0` is ambiguous between *not started* and
*just started*, so the live reader probes the reset clock to decide whether the window is
anchored; and banked **full-reset credits** (`rateLimitResetCredits`) mean the weekly cap is
effectively higher on demand, so they get their own table with expiry dates.

---

## Kimi — coding plan (added 2026-07-27)

| | |
|---|---|
| Source | `GET https://api.kimi.com/coding/v1/usages` |
| Auth | `Authorization: Bearer sk-kimi-…` · `Accept: application/json` |
| Windows | plan quota (7d cycle) → `weekly_all`; burst window (300 min) → `session` |
| Units | **normalized quota units, `limit` is always `100`** — percent, not tokens or calls |
| Credential | static platform key. Nothing rotates, nothing to serialize |
| Probe | `python3 tools/kimi_probe.py` |

Verified live 2026-07-27. Response shape:

```jsonc
{
  "user": { "membership": { "level": "LEVEL_INTERMEDIATE" }, "region": "REGION_OVERSEA" },
  "usage":  { "limit": "100", "remaining": "93", "resetTime": "…" },   // the plan quota
  "limits": [{ "window": { "duration": 300, "timeUnit": "TIME_UNIT_MINUTE" },
               "detail": { "limit": "100", "used": "4", "remaining": "96",
                           "resetTime": "…" } }],                       // the burst window
  "parallel": { "limit": "20" },
  "authentication": { "method": "METHOD_API_KEY", "scope": "FEATURE_CODING" }
}
```

Four things about this payload that the reader must not get wrong:

1. **Every figure is a decimal *string*.** `"100"`, not `100`.
2. **`used` is omitted entirely while it is zero.** It is derived from `limit - remaining`,
   never defaulted to 0 — a missing field must not render as a calm 0% when the truth is
   unknown. (`used` wins when both are present.)
3. **`limits[]` is unordered and may grow.** The burst window is matched by *duration and
   time unit*, never by list position. Third-party trackers index `[0]`; a new window would
   silently be mislabeled the 5h session. Unknown windows are still reported, labeled by
   their own length (`1h · burst`).
4. **The plan name is not in the payload.** Only a coarse `LEVEL_*`. The multiplier in
   `KIMI_PLAN` is operator-declared, exactly like the Claude and Codex plan strings; the raw
   level travels as `plan_raw` so the two never get confused.

**Window semantics.** The plan quota cycles every 7 days from the subscription date (fixed,
not rolling) and is mapped onto `weekly_all` so it sits beside Claude's and Codex's weekly
rows. The 300-minute window is rolling and **bites even with plan quota left** — the same
throttle-vs-budget distinction as Claude's 5h session.

**Not the same thing as the Moonshot open-platform balance.** That is a separate prepaid
account (`https://api.moonshot.ai/v1/users/me/balance`, a different key) and its numbers do
not reconcile with the coding plan's. Glideslope reads only the coding plan. Do not mix them
into one column.

**`/coding/v1/usages` is the whole surface.** `/coding/v1/me`, `/subscription`, and
`/memberships` all 404 (probed 2026-07-27). There is nothing else to ask.

---

## Grok — SuperGrok weekly pool (added 2026-09-13)

| | |
|---|---|
| Source | `GET https://cli-chat-proxy.grok.com/v1/billing?format=credits` — the same endpoint Grok Build's `/usage` modal uses |
| Auth | Grok Build OIDC session in `~/.grok/auth.json`. Access token under `key`; refresh in place when expired |
| Windows | plan quota (7d cycle, `USAGE_PERIOD_TYPE_WEEKLY`) → `weekly_all`. No 5h session is published today |
| Units | `creditUsagePercent`, 0–100 |
| Credential | the Grok CLI's own session. This program refreshes it the same way Grok does (OIDC `refresh_token` grant against `auth.x.ai`), atomically, under flock, never copying the token elsewhere |
| Probe | `python3 tools/grok_probe.py` |
| In-session boost | `~/.grok/hooks/glideslope-usage.json` → `tools/grok_hook.py` on `SessionStart` and `Stop` only |

Verified live 2026-09-13. Response shape:

```jsonc
{
  "config": {
    "currentPeriod": {
      "type": "USAGE_PERIOD_TYPE_WEEKLY",
      "start": "…",
      "end": "…"
    },
    "creditUsagePercent": 2.0,
    "productUsage": [
      { "product": "GrokBuild", "usagePercent": 1.0 },
      { "product": "GrokChat", "usagePercent": 1.0 }
    ]
  }
}
```

Four things about this payload that the reader must not get wrong:

1. **The percentage is of the included weekly compute pool, not a message count.**
   xAI does not publish the absolute size. A chat is cheap; a long Build turn or a
   video is not. Product rows are shares of that same pool, not separate meters.
2. **There is no 5h session.** Daily per-product limits ended in June 2026. If a
   later payload grows a 300-minute period it is mapped onto `session` by duration,
   never invented.
3. **The weekly clock is account-specific**, shown as `currentPeriod.end`. It is
   not a global Monday midnight.
4. **The pool is account-global.** Chat, Imagine, Voice and Build all draw from
   it. A Grok Build hook cannot see phone/web burn by itself, which is why the
   sampler live-reads even when Build is closed.

**Token refresh.** Access tokens last ~6 hours. Grok refreshes them in the
background while a Grok process is running. The sampler does the same operation
when it ticks against an expired token, so the position stays current for grok.com /
mobile usage too. Only the first-party issuer `https://auth.x.ai` is driven;
a custom IdP is refused rather than guessed. Persist is temp-file + rename under
`auth.json.lock`, and the file is re-read under the lock so a concurrent Grok
refresh is not clobbered.

**The Stop hook is a boost, not the backbone.** It writes
`<store>/grok-billing.json` (numbers only). `query_grok()`
prefers that file when it is under 90 seconds old, then falls back to a live
GET. The hook prints nothing on stdout (Stop stdout is a decision) and always
exits 0.

**Not the developer API.** Console prepaid credits, RPS/TPM tiers, and
`XAI_API_KEY` are a different meter. Glideslope reads the SuperGrok weekly pool.

---

## OpenRouter (metered, not a subscription)

| | |
|---|---|
| Source | `GET https://openrouter.ai/api/v1/key` |
| Units | **dollars**, rolling 7-day (`usage_weekly`) — no percent, so no ◆ mark |
| Credential | static API key |

Reported as spend, plus, optionally, a per-harness attribution table read from a local
telemetry DB (`<store>/telemetry/telemetry-<host>.db`) whose collector is not part of this
repo; without it the table is simply absent. The attribution is **not** reconciled against the `/key`
gauge — observed spend does not cleanly match it (the harnesses appear to bill a different
key), and a residual would mislead.

---

## API-equivalent spend (optional input, added 2026-09-23)

Not a provider: a valuation of the tokens behind the Claude and Codex meters. Glideslope does
not compute it. When `<store>/spend.json` exists, the Detail view and the popup draw it; when it
is absent or malformed they draw an empty panel and the rebuild carries on. The producer (a
collector that prices local Claude Code, Codex, Kimi and Grok requests at API list rates and
credits each one to the Claude account logged in on its machine at that moment) is not part of
this repo.

| | |
|---|---|
| Path | `<store>/spend.json` |
| Keys the views read | `generated_at`, `collected_at`, `first_request_at`, `attribution`, `totals.{d1,d7,d30,all}`, `machines`, `accounts[]`, `leverage`, `providers`, `meters["<display>/<meter_id>"]`, `daily.{days,series}` (see `spend_view()` in `views/deck-src/build.py`) |
| Per meter | every anchored meter's current window, priced from the same records: the popup's $ column. The Fable row counts Fable only |
| Credential | none. Glideslope only reads the file |

**$ / 1%** divides the window's spend up to the meter's last read by that percent, so a stale
read (an account nobody is logged into) is never divided into spend it did not see. 100× it is
what one full week of that account is worth at list price.

---

## Credentials

Static keys come from the environment first, then from the configured `keys_file`
(default `<store>/keys.txt`, `KEY=value` lines, mode `0600`): `OPENROUTER_API_KEY`,
`KIMI_API_KEY`. The file fallback is what lets the launchd sampler work at all — it carries
no shell environment. **Only static keys are ever read this way.** Claude's OAuth blob is
read by `claude-account` alone, and only ever read. Grok is the exception: its consumer
meter has no static key, so the sampler refreshes the Grok Build session in
`~/.grok/auth.json` in place — see **Grok** above.

## Adding the next provider

1. `query_x()` → raise `PositionError` on every failure mode; never let one provider's
   outage break the others (`gather()` turns each into a warning row).
2. `normalize_x()` → the shared shape. Use `weekly_all` / `session` meter ids when the
   windows mean the same thing, so the hero read and the deck trails pick them up free.
3. `x_accounts()` → wrap it, and add `--skip-x` / `--x-snapshot` to the CLI.
4. Colors: add `--x` / `--x-hot` and a `COLOR_KEYS` entry in **both** `deck.tmpl.html` and
   `popup.tmpl.html`, plus a legend swatch in the deck. Without them the provider silently
   borrows OpenRouter's violet.
5. If the key is static, add its name to `sampler.py`'s `KEY_NAMES`.
6. Tests (`python3.11 -m pytest tests -q`), then rebuild and look: `python3.11 views/deck-src/build.py`
   and open the two pages. The popup is a fixed 364 × 560
   panel: every provider costs rows out of the same budget, so open the rebuilt page and
   check that the last window still clears the footer.
