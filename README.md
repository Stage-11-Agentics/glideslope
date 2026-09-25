# Glideslope

<p align="center"><b><i>Token subscription optimization for the modern hyperengineer</i></b></p>

<p align="center"><img src="docs/screenshots/hero.png" width="720" alt="the Glideslope popup: every window of every account on one approach plot, with the register beneath naming each marker"></p>
<p align="center"><sub><i>Understand your various subscriptions, and understand where they all are, in both 5 and 7 hour usage, to optimize your utilization of the most valuable resource: intelligence</i></sub></p>

---

listen.

you are running coding agents on many accounts: Claude Max accounts, Codex plans, Kimi, and Grok, because one subscription is no where near enough to get build all of your in flight projects. Many meters. reset clocks. six pages, each with its own idea of a percent. you check them in the gaps between the work, and the one answer you actually need, *am I ahead or behind, and can i kick off this large job now?*, is on none of them.

## What is the Glidesloop

The Glidesloop is the path towards optimum usage: you are on the 7d glidesloop if, 3.5 days into your 7 day usage period, you have used exactly 50% of your tokens. You are ahead of the 5h glidesloop if, 1 hour into your 5 hour limit, you have used 33% of your tokens. We are mapping two different times into the same graph, which takes some getting used to, but once you wrap your head around this paragraph you will find it immensely information dense and effective. 

**Glideslope reads every subscription plan you fly into one single interface and puts one mark on every meter: usage is relative to the  ◆ glide slope.** above it, you are hot, and you will hit the ceiling early. below it, you are banking capacity that expires at the reset. one comparison, the same in every cell, for every provider, on every window.

this is built for the hyperengineer flying several subscriptions at once, on purpose, for maximum effect. it asks you to learn one idea and one picture. the idea takes a minute. the picture takes three looks. after that, a single percentage on a single page will feel like flying with one instrument covered.

---

## one frame. every window.

now the picture. the approach plot puts every window of every account on the same axes.

x is the window: 0% at its start, 100% at its reset. y is what you have used. the dashed beam is y = x, even burn, the glide slope itself. every marker is one window of one account. the letter is the account. the shape is the window: ○ seven-day budget, △ five-hour session, ◇ the weekly Fable meter. behind each marker is its trail, every sample the journal took this window.

here is the part that asks something of you. a five-hour session and a seven-day budget are on the same plot. not side by side. the same axes. the x axis is not time, it is *how much of this window has elapsed*, so hour three of a five-hour session and day four of a seven-day week sit on the same vertical. that is what lets six accounts across four providers, on three kinds of window, read as one picture. it is also why the picture is strange the first time you see it.

above the beam, ahead: the trail is climbing faster than the window is closing, and you will reach 100% before the reset does. below it, banking. the register beside the plot says the same thing as a number: `+66%` is Bravo sixty-six points above its own slope, `−53%` is Codex fifty-three points below it.

**the pool.** if you rotate between Claude accounts, the number you are actually flying is the pool: the mean of the useds against the mean of the phases. both are means of coordinates already on the frame, so the pool lands on the same beam as everything else, as ✳ CLAUDE POOL. Σ TOTAL POOL does it across every subscription you hold. you are not one account. you are many. the pool is the one you fly.

<p align="center"><img src="docs/screenshots/deck-plot.png" alt="the Detail view: approach plot with trails, the legend row beneath it, and the deviation register listing every window's distance from its own slope"></p>
<p align="center"><sub><i>the Detail view. the same frame with trails, and the key in the row beneath it: ○ 7 DAY budget, △ 5 HOUR throttle, ◇ Fable, letter = account, ✳ Claude pool. on the right, the deviation register: every window's distance from its own slope, as a bar you can read from across the room.</i></sub></p>

### a note on the learning curve.

the plot is not gentle. the first time you look at it you will see a scatter of letters and shapes, and that is your brain refusing to put a five-hour throttle and a seven-day budget on one axis. it is right to refuse. nothing else you use does that.

give it three looks. on the third, the scatter becomes a fleet: which account is hot, which one is banking, which window dies first, where the room is. that read takes under a second once you have it, and it is not available anywhere else. we built the instrument for the operator who will do the work of learning it, and we did not soften it for the one who won't.

## in the terminal.

the same position, as text. relay-ready markdown, because the first reader is usually an agent.

```
glideslope
```

**● Logged in · Hyperion: Claude · Charlie**   ◦ Atlas: Claude · Bravo

| Weekly status ||||
|:-:|:-:|:-:|:-:|
| **Account** | **Weekly** | **Fable** | **Next reset at** |
| Claude · Alpha | A⃝ 81% (◆ 81.2%) | A⃟ 100% (◆ 81.2%) | 1d 7h – Sat Sep 26, 11:00 PM EDT |
| *Claude · Bravo ◦ Atlas* | B⃝ 0% (◆ 20.5%) | B⃟ 0% (◆ 20.5%) | 5d 13h – Thu Oct 1, 5:00 AM EDT |
| Claude · Charlie ● Hyperion | C⃝ 8% (◆ 1.5%) | C⃟ 10% (◆ 1.5%) | 6d 21h – Fri Oct 2, 12:59 PM EDT |
| Claude · pooled (3 · Max 20x) | 29.7% (◆ 34.4%) · −4.8 banking | 36.7% (◆ 34.4%) · +2.2 ahead | next Alpha in 1d 7h – Sat Sep 26, 11:00 PM EDT |
| Codex ● Hyperion ◦ Atlas | X⃝ 3% (◆ 56.4%) | — | 3d 1h – Mon Sep 28, 4:39 PM EDT |
| Kimi | K⃝ 8% (◆ 55.4%) | — | 3d 2h – Mon Sep 28, 6:20 PM EDT |
| Grok ● Hyperion | G⃝ 1% (◆ 44.0%) | — | 3d 22h – Tue Sep 29, 1:31 PM EDT |
| Total · pooled (5 · $830/mo, not Kimi) | 22.2% (◆ 40.1%) · −17.9 banking | — | next Alpha in 1d 7h – Sat Sep 26, 11:00 PM EDT |
| OpenRouter | $6.26 | — | rolling 7d · $89 left of $100/monthly |

the position on the afternoon this page was written. the letter is the account, the outline is the window: circle weekly, triangle five-hour session, diamond weekly Fable. `● Hyperion` is the machine an account is logged into; `◦ Atlas` is a login held on another one. a second table lists every window of every account, and `--json` gives the same position as one snapshot.

**the cells never pretend.** `stale` is a last-known read. `presumed` is a window that rolled over with nothing logged in to spend it. `floor` means the truth is this or higher. `undecided` is a pooled floor sitting below the mark, which cannot say ahead or behind and does not try. an instrument that marks the edge of its own knowledge is worth more than one that rounds. every one of those markers exists because the instrument was once wrong in exactly that way.

**the API-equivalent block** prices every token at list. it is a valuation, never money spent, and it is the reason the juggling is worth it. on the afternoon this page was written the Claude line read:

```
Claude, last 30 days: $14,885 API-equivalent on $600/mo of plans = 24.8×
```

## the views.

self-contained HTML, opened from disk, rebuilt by the sampler every sixty seconds. nothing hosted.

- **Detail view** (`views/deck.html`): every window on the plot with its trail, the deviation register, the full ledger, the reset horizon, the switch log.
- **Popup** (`views/popup.html`): the plot and the register alone, sized for a menu bar or a side panel.
- **History** (`views/history.html`): the same plot on a real clock. weeks stacked behind you.

<p align="center"><img src="docs/screenshots/deck-ledger.png" alt="the Detail view ledger: every window of every account as a burn clock, with state, reset and freshness"></p>
<p align="center"><sub><i>the ledger. every window of every account as a burn clock: used against the ◆ mark, its state, its reset, and how fresh the read is. the tooltip does the arithmetic out loud.</i></sub></p>

## for agents.

the agent is on the same window you are. a skill ships in the repo; install it and Claude Code can ask, mid-task, where it is on usage, and get the table rather than a guess:

```bash
ln -s "$PWD/skills/glideslope" ~/.claude/skills/glideslope
```

> where am I on usage?

the table, relayed verbatim, honesty markers intact.

> I have a three-hour session ahead of me. which account has the room?

ranked by slack against each account's own slope, never by raw percent. 60% six days into a week is capacity about to expire; 20% one day in is already behind.

> Bravo is at 90%. point new sessions at whichever account has the most slack.

`claude-account use auto`, suggested, not run. the agent proposes the switch and the operator makes it.

an agent that knows its own budget can pace the work, or tell you the account it is on is about to run dry before the 429 does. `--json` is the same position as one object, for the agent that would rather compute than read.

developing Glideslope with an agent: [`CLAUDE.md`](CLAUDE.md).

## install.

Python 3.11+, standard library only. macOS for the Claude read and the background jobs; the rest is plain Python.

```bash
git clone https://github.com/Stage-11-Agentics/glideslope.git
cd glideslope
python3 glideslope.py                 # the position, now
bash tools/install-sampler.sh         # every 60s: journal the position, rebuild the views
open views/deck.html
```

or `uv tool install git+https://github.com/Stage-11-Agentics/glideslope` for the `glideslope` and `claude-account` commands alone.

configuration is one optional file, `~/.glideslope/config.toml`: plan labels, account names, other machines, alert sink. every key has a default. see [`config.example.toml`](config.example.toml).

## providers.

| Provider | Meters | Credential |
|---|---|---|
| **Claude** | 5h session, weekly, weekly Fable, per account | the token Claude Code already holds, read-only |
| **Codex** | every native meter plus banked reset credits | none; read through the Codex app-server |
| **Kimi** | plan quota and 5h burst window | a static platform key |
| **Grok** | SuperGrok weekly compute pool | the Grok Build login |
| **OpenRouter** | rolling 7-day dollars (metered, so no slope) | a static API key |

full read paths, units and failure modes, and the checklist for adding the next provider: [`PROVIDERS.md`](PROVIDERS.md). every provider is normalized into one shared account shape and drawn by the same tables and views. nothing is a special case at the presentation layer, and one provider's outage becomes a warning row, never a missing subscription.

**Glideslope holds no credentials and mints no tokens.** Claude is read with the access token Claude Code already keeps, only while it is valid. static keys come from the environment or a `0600` keys file. the one exception is Grok, whose six-hour token is refreshed in place the same way Grok's own client does it. the promises, stated as invariants: [`SECURITY.md`](SECURITY.md).

## several Claude logins on one machine.

`claude-account`, shipped here, gives each Claude account its own login home (one `CLAUDE_CONFIG_DIR` each) so one machine holds every login at once, and routes new sessions between them:

```bash
claude-account homes              # every login on this machine
claude-account use bravo          # new sessions start as Bravo
claude-account use auto           # let the glide slope pick the account with room
```

running sessions never move, and nothing here stores a token. login is a place, not a boolean: a machine reads the one account it is logged into live, and other machines' logins arrive by beacon (`tools/install-satellite.sh`), so an account held elsewhere reads `unread` rather than a comfortable `0%`. `claude-account --help` has the rest.

one honest gap: we only jump between Claude accounts, so `claude-account` only knows Claude. multi-account switching for Codex is not built. it is the same shape, one login home per account, and it should be a short job for your agent. we would welcome that pull request.

---

## lineage.

the name is borrowed from the instrument landing system. a glide slope is a radio beam, tested from 1929, flown by a scheduled airliner into Pittsburgh through a snowstorm in 1938, standardized by ICAO in 1949, that tells a pilot in cloud one thing: above the path to the runway, or below it. the pilot flies the needle, not the ground. we needed the same instrument for a different kind of descent.

Glideslope was born inside Gregorovich, Atin Woodard's personal infrastructure, where it has flown a multi-account rotation since July 2026. the ◆ mark came first. the plot, the pool, the satellites and the honesty markers each came from a way the instrument turned out to be wrong: a login held on another machine, a stale read presumed fresh, a pool that is only a floor. it reads what the providers publish about your own account and nothing else, and it will not always be right. it will always say how sure it is.

## license.

[MIT](LICENSE). Stage 11 Agentics Corporation.

---

*we believe in the deployment of intelligence. more of it, in more hands, on real work. not waited for. not hoarded. used.*

*the hyperengineer is the one deploying it. we build so that every hour of mind they can reach lands on the work, all of it, and so they can see it landing.*

*as much intelligence deployed as the world can hold, as well as we can manage it. the beautiful future is on the far side of that. not this side.*

*let's build it together.*

---

Glideslope is a [Stage 11 Agentics](https://stage11.ai) project.
