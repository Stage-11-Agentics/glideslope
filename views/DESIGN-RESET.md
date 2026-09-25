> `validate-deck.py`, referenced below as the gate, was retired 2026-09-11: too heavy for a utility this small. The rulings stand; the unit tests and a rebuild-and-look are the gate now.

# Glideslope — design reset

*Current section on top. Earlier inventory below.* Dated engineering history: paths
written `<store>/` are in the store directory (default `~/.glideslope/`), and the popup is
the compact view built for a 364 × 560 menu-bar panel.

---

# AMENDMENT — the frame gets switches, and the pool becomes a mark (2026-08-17)

Ratified by the operator. Two changes to the deck's approach plot, and they are the
same change: the frame stops choosing for you, and everything on it is a first-class
series.

1. **The exclusive lens becomes independent switches, under the frame.** `7 DAY` and
   `5 HOUR` are latched on, each specialty model is latched off, and the
   anthropic-only hold sits beside them behind a rule — it filters *providers*, not
   window classes, so it is not the same kind of control and does not pretend to be.
   The controls moved from the plot's head to a row beneath the chart, which is where
   the operator asked for them and where a legend belongs anyway: the row is the key
   as much as it is the switchboard.

   **Click latches, resting borrows.** A latch is written down and rides the
   minute-ly reload (`?facets=…&anthropic=…`, plus localStorage). A hover shows that
   class *alone* for exactly as long as the pointer is there and hands the frame
   straight back — including a class that is switched off, which is the useful half:
   you can look at Fable without adopting it. This is only possible because neither
   a click nor a hover re-renders the plot. The frame draws every class on every
   render and the switches are a pass of class flips over the DOM (`applyEmphasis`),
   because a re-render would destroy the very button the pointer was resting on.

   On the glass a switched-off class is **gone**; in the register it only **recedes**.
   The register is a family roll-call that must list every model whatever the plot is
   showing — and a row that vanished would move every row under it.

2. **A specialty meter wears a code letter.** The lens was exclusive for a real
   reason: Alpha's all-models dot and Alpha's Fable dot are the same hue and the same
   7 DAY disc, and telling them apart by label is a puzzle. So the letter is the
   answer — **F** knocked out of the disc on the frame, `F` beside the class chip in
   the register (the column's own rule: only the class word wears the chip, a scope
   suffix stays plain text beside it), and the model spelled out in the gutter tag
   and every tooltip. The plot key teaches the letter.

   **One list registers a specialty model** (`SPECIAL_MODELS` in `deck.tmpl.html`):
   an entry there earns a switch, a code, a register row and a place in the meters
   the register rolls through. Codex's Spark is deliberately absent — the operator
   does not fly it and it carries no reset clock — so it stays a line in the full
   ledger and the validator asserts it never reaches the frame.

3. **The pooled Anthropic weekly is a mark, not a caption.** Both of its coordinates
   are means of coordinates already on the frame, so it earns the whole instrument:
   a trail, a reticle readout, the red flight paths, and a row of its own —
   **last inside the Anthropic box, under the accounts it is made of.** It is
   modelled as a synthetic account plus a synthetic window so all of that is the code
   that already draws an account, not a second implementation that can drift.

   Its clock is the **mean of its components' clocks**, which with equal spans is
   exactly the window whose phase the ◆ already plots (mean phase = (now − mean
   start)/span), so the countdown on the row cannot contradict the ◆ beside it. The
   next *actual* reset is a different event — it changes what the pool is made of —
   and is named in the tooltip rather than on the row. On the glass it is
   **CLAUDE POOL**, wearing Claude's burst and the ◆ ink, never a provider hue.

   **Known lean, ratified knowingly:** the pool is a floor whenever any component was
   last read stale, and a floor's rate is a floor too — so a pooled forecast says you
   have *longer* than you do. The rate is still measured from live samples, and the
   mark (`53%+`), the register row (`· FLOOR`) and the tooltip all say so. Disclosed
   rather than withheld.

4. **Three forecasts, scaled to the window they forecast.** One horizon set could
   not serve both classes. A 5 HOUR window is 300 minutes: a 24h lookback reaches
   back five whole windows past its own start, so the "rate" it measured was never
   a rate *inside* the window being forecast — it was arithmetic across a reset.
   And an hour of a 7 DAY budget is 0.6% of the window: a sliver too short to say
   anything, drawn as a stub too short to read. So a throttle is read in hours
   (**1h · 3h · total**) and a budget in days (**6h · 24h · total**) — three lines
   each, on the deck and in the popup alike.

   The density ramp is keyed on **rank**, not on the horizon's name: `far` sparse,
   `mid` medium, `near` solid and bare. Both classes wear one far/mid/near
   vocabulary, so retuning a lookback never touches CSS and adding a class never
   needs a new rule.

5. **Two bugs the operator found on the glass that the gate could not.** Both were
   the same mistake in different clothes, and the corrections are the durable part:

   - **The pooled mark had no hit area.** `pointer-events: all` on a `<g>` buys
     nothing — a group has no geometry, so hit testing falls through to the
     children, and the burst is eleven 1.5px rays whose very centre is the gap
     they radiate from. The most natural place to aim was a guaranteed miss. It
     now carries a transparent pad, on both surfaces. *The gate said pass because
     it hovered with a dispatched `pointerenter`, which proves the wiring and says
     nothing about reachability* — a known trap. It now
     moves a real pointer to the burst's centre, aimed without reference to
     whatever is supposed to catch it, and asserts via `elementFromPoint` that the
     mark **is** what is under the cursor. The first attempt at that still passed
     on the broken page, because the pool's own trail ends at the mark and a 1.1px
     stroke with the same `data-mark` lit the selection by accident.
   - **The popup drew the pool inert** — no `data-mark`, no reticle, no
     forecast — so the one thing on that frame that did not answer a pointer was
     the thing the operator went looking for, while the deck's answered fully. Two
     surfaces showing one position must behave like it; the panel's pool is now a
     mark, drawing the *same* forecast code (extracted from its mark loop rather
     than copied).

6. **A meter id is not unique.** On 2026-08-17 Codex began reporting a 5h **and** a
   weekly meter both named `codex_bengalfox`. Tooltips are bound by a key the
   binder resolves with `find`, so two elements sharing a key is not a cosmetic
   collision — the second silently reads out the first's numbers, the one failure
   this page must never have. The key now carries the window class
   (`account/meter/minutes`), one function builds it so emitter and binder cannot
   drift, and a gate asserts no two windows ever share one. A provider is free to
   reuse a meter id across classes; the page is not free to confuse them.

`validate-deck.py` gates all of it: the switch roster and its opening latches, one
switch at a time showing exactly the meters it commands, the hover solo (including
revealing a switched-off class), release restoring the latched frame, the register
keeping every row while receding, the code letter on every specialty mark and no
other, Spark's absence, the URL round trip in both directions (`facets=-` for the
deliberately empty frame), the pool's mark, name, flight paths and three-way
selection spine, the per-class horizon sets with their ranks, the pool reachable by
a REAL pointer on both surfaces, and no two windows sharing a tooltip key.
Mutation-checked throughout: each deliberate break produces its own named failure.

---

# AMENDMENT — the approach plot, unrolled onto a real clock (2026-08-15)

The deck's approach plot answers *where am I on this window* by normalizing every
window onto one 0–100 rail, which is exactly why the ◆ even-burn beam can be a
single diagonal shared by five accounts. The cost of that normalization is that
last week does not exist: there is only ever one window in the frame.

**The history view** (`views/history.html`, built from
`views/history-src/`) is the same instrument with x turned back into wall
clock. Three consequences follow, and they are the whole design:

1. **One beam becomes a ladder.** A week no longer stretches to fit, so the
   shared diagonal splits into one beam per window instance — each running from
   that window's own start at 0% to its own reset at 100%. Above your beam is
   still ahead of budget; below it is still money left on the table. Every
   account's ladder now sits at its true phase, and the weeks stack up behind you.
2. **The deviation becomes an area.** Over three weeks the eye cannot read a
   vertical distance repeatedly, so the space between the track and its beam is
   filled and signed — orange ahead, cyan trailing. That is what makes a month of
   burn legible at a glance: you see that you ran hot Monday to Wednesday and
   coasted, without reading a single number.
3. **The sawtooth is the shape.** The drop at each reset is drawn, not hidden.
   The 5-hour meters become a work-rhythm chart nobody designed and everybody
   wanted.

**Reconstruction, and its honesty rules.** The store records gauges, not windows,
so windows are recovered from the samples (`history-src/build.py`, covered by
`tests/test_history_build.py`):

- Reset clocks jitter by a second or two between reads — instances group with a
  tolerance, never by equality.
- Codex's weekly is a *rolling* window whose percent sags mid-cycle as old usage
  ages out. A bare "fell by 5 points" rule reads every sag as a reset and shatters
  the week; a roll is a fall from something substantial to near nothing.
- `resets_at` is NULL after a reset until first use. Those zeros are real
  observations belonging to the window whose beam has already started.
- A window's span is clipped to its own reset AND to the next window's opening,
  whichever comes first. Codex has rolled early (2026-08-08); a locked-out
  account has sat past its reset with no sample to prove the roll. One rule, both.
- **A gap is drawn as a gap.** Past 90 minutes the line breaks and goes dotted —
  well above a missed sample or two, because a break that cries wolf stops being
  read. And an unread stretch containing a reset is routed *through* it: hold the
  last known level to the reset, drop, then dotted onward. A straight dotted line
  from 100% down to the next known 2% draws a gradual decline that never happened.

**Cost and placement.** The page reads only `samples.db` — no provider, no
credential, no lock — in 0.23s, so the 5-minute sampler rebuilds it beside the
deck, and opening it can never cost a token. The deck's masthead carries the way
in (`HISTORY`, the same box as `RELOAD`, because a control row that resizes when
one of its controls is a link is a control row that moves).

**Open question, deliberately not resolved here.** The hero overlays one weekly
lens across all five accounts, each with its own ladder. At three weeks that is
five ladders in one frame — legible, but busy. The alternative is a deviation
hero (track minus beam, one shared zero line, no ladder at all), where every
account is comparable regardless of window phase at the cost of the absolute
read. Both are cheap; neither has been flown for a week yet.

# AMENDMENT — a read is placed, a lens is chosen, a provider never vanishes (2026-08-04)

Three corrections, all from one session: the operator hit the 5h ceiling on one account,
switched to another, and the surfaces went quiet about exactly the thing they had just done.

1. **A five-hour read is placed by its window, not by staleness.** The old rule blanked
   any 5h read from a stale account. But the account you just left is precisely the one
   nothing can burn, and its last read was taken *inside* the window still running — so
   it is a floor, not a mystery. The rule is now: observation inside the current window →
   show it, marked `last known`, with the ◆ mark withheld (a floor is not a position);
   observation before the window opened, or no observation time at all → `unknown`.
   This required a second fix underneath: a cached account now carries **its own**
   observation time (claude-account's `fetched_at`), not the snapshot's write time — the
   old behaviour aged a day-old read as if it were seconds old, and wrote a fresh sample
   row every five minutes for numbers that had not moved.
2. **The approach plot flies one weekly lens at a time.** `All models` | `Fable`, chosen
   at the top and remembered across the minute-ly reload. Two same-coloured weekly dots
   in one frame is a puzzle, not a plot. Five-hour throttles now ride alongside in a
   deliberately different symbol — **solid dot = weekly budget, hashed ring = 5h
   session** — because the two are read differently: above the line on a throttle costs
   nothing but the wait.
   *(Superseded 2026-08-17: the exclusive lens is now independent switches under the
   frame, and the puzzle this rule guarded against is answered by a per-model code
   letter instead. The symbols were swapped 2026-08-15 — the budget is the disc, the
   throttle the dashed triangle.)*
3. **A provider that fails to read degrades; it does not disappear.** A Codex app-server
   timeout used to delete Codex from the position outright — from the plot, the ledger,
   everything — which reads identically to "this subscription does not exist". Codex and
   Kimi now keep a last-known-good raw read (`<store>/provider-cache/`), served with its own
   observation time and marked stale when the live read fails. The Codex read also stops
   spending its whole timeout budget on the zero-percent refinement probe.

4. **The state is journaled while the account is readable.** An account can only be read
   live while it is the one logged in — the stored refresh token is single-use and Claude
   Code rotates it away. So every live read is now written down per account
   (`<store>/provider-cache/`), and a lockout is answered with whichever surviving copy was
   observed last. Numbers and reset clocks only; this never touches a credential. And a
   locked-out account cannot follow its window over the roll — the 5h read returns to
   `unknown` the moment the window it was observed in ends.
5. **A window at its ceiling reads `USED UP`.** "Significantly ahead of glideslope" was a
   flattering way to say you cannot work; it is retired everywhere and forbidden by the
   validator. The full-detail table drops the `168h window` subtitle (the label already
   says `Weekly · all models`) and its reset column is now **Resets in** — countdown as
   the read, exact date legible underneath, and a dash for OpenRouter, which has none.

6. **Three across the top, then full-width bands.** Ratified 2026-08-04 for the large
   monitors this is actually read on: the top band is **approach plot | deviation
   register | weekly status**, a third each. Under it the full ledger, then the reset
   horizon, then the switch log — each spanning the whole width, in the DOM as well as
   the grid. The plot leads because it is the read that answers *where am I on this
   window*; the tables are what you consult once it raises a question. This supersedes
   the 2026-07-25 verdict's "weekly status first" ordering and retires the right-hand
   rail; the weekly table itself is unchanged apart from stacking its reset date.
   Expanded, the plot takes two thirds, the register holds the third column, and the
   weekly read steps down to its own band.

   The horizon and switch log earn their new width: the horizon's SVG now uses one
   user unit per rendered pixel (a fixed viewBox stretched across a full-width band
   would have stretched every label with it), and the switch log lays its entries side
   by side instead of stacking five short rows down an empty page. The plot's kicker
   and the register's footer paragraph are gone — the mark key in the plot head says
   what they said, without the exposition.

7. **The popup says less.** Ratified 2026-08-04. It opens on the weekly lens every
   time — the lens rides the page's own snapshot reload (carried in the URL) but never
   outlives the panel, because opening the panel is asking the question fresh. Per-row
   freshness is gone: a "2m ago" repeated down every row said nothing the footer's gauge
   age did not, so only a stale account carries a read, and it says how stale. The reset
   column is **Reset at** and stacks three steps — countdown, then the day, then the
   clock, each a step down in weight — so the cell answers *how long* and *which day*
   before it answers *what time*.

8. **The mark.** Glideslope has an icon: the approach beam corner to corner on a dark
   instrument tile, with the ◆ even-burn diamond riding it dead centre — the product's
   whole thesis in one glyph, drawn to survive 16px. It lives as a real SVG
   (`views/glideslope-icon.svg`) and is inlined into both pages' `<link rel=icon>`
   at build time, so one drawing serves both and neither can carry a stale copy.

`validate-deck.py` gates all of it structurally: both lenses present and switchable, one
mark per plottable window in each lens (a provider missing from its lens fails the
build), session marks hollow and weekly marks filled, a 5h read never `unknown` when its
observation lies inside the running window and always `unknown` once that window rolls
under a locked-out account, and a ceiling window that reads anything but `USED UP`.

---

# VERDICT — popup preview + Detail view (2026-07-25)

Glideslope has two deliberately different depths:

1. **Popup preview — one compact table.** The in-panel surface is the simple all-windows
   read: account, window, used versus ◆, status, and reset. It fits the fixed
   364 × 560 menu-bar panel without scrollbars, reserves the native Back control, and
   ends with `DETAIL VIEW →`. It is built at `popup.html` from
   `popup-src/popup.tmpl.html`.
2. **Detail view — the rich analysis page.** The browser-sized surface leads with the
   same clear weekly status table as the CLI, then the full all-window ledger,
   approach plot with 6 h / 24 h / all history controls and expansion, deviation
   register, reset horizon, and recent switches. Its stable internal artifact remains
   `deck.html`, but the product-facing name is **Detail view**.
3. **One truth, two renders.** `deck-src/build.py` reads one provider position and one
   sample-store snapshot, strips private identities once, then atomically builds both
   pages. Percentages can never drift between the popup and the Detail view.
4. **Open pages do not fossilize.** Both pages reload the immutable built snapshot every
   60 seconds and whenever they return to the foreground. Countdowns may tick locally;
   percentages never extrapolate.
5. **Honesty remains structural.** Five-hour sessions are throttles, unanchored or stale
   sessions are unknown, reset times never go negative, and only Alpha, Bravo, Charlie,
   Codex, Kimi, and OpenRouter reach either page.

`validate-deck.py` enforces the split, shared snapshot, rich-view interaction controls,
compact fixed-frame fit, native Back reserve, no identity leaks, and refresh behavior.
The 5-minute sampler remains the sole writer of live provider state and history at
`<store>/samples.db`.

---

# PROPOSAL v2 — back to the brief: the glide chart (2026-07-17)

The operator's pull: *one big beautiful timeline of total usage per account against its
limits; maybe a 5h zoom; maybe one chart per account.* That **is** the BRIEF's
original thesis — beam, track, projection — which the bar design drifted from.

## The form: one glide chart per account

**Two stacked panels: Account A, Account B.** Each is a real chart, not a bar:

- **x** — wall-clock, spanning that account's *own* weekly window: opened → reset
- **y** — 0–100% of limit
- **the beam** — ONE diagonal, window-start 0% → reset 100%. The glide slope,
  drawn once, not as floating diamonds
- **two tracks** — `all models` and `Fable` climb against the same beam
  (they share the window & reset, so one diagonal serves both)
- **projection** — dashed continuation of each track at current burn rate;
  where it crosses 100% before the reset = your exhaust point, visibly
- **NOW** — one vertical hairline
- **track-head labels in the status-bar dialect** — `42%(◇18%)` — the ◇ number
  is just where the beam sits right now

```
 ACCOUNT B                            weekly · resets Thu 1 AM (5.7d left)
 100 ┤                                                          ╱
     │                                                     ╱ beam
  75 ┤              projection ┄┄┄┄┄┄┄┄┄┄╳ exhausts   ╱
     │                      ┄┄┄┄         Sun 6 AM╱
  50 ┤               ┄┄┄┄                   ╱
     │        ●━ all 42%(◇18%)         ╱
  25 ┤    ●━ Fable 26%(◇18%)      ╱
     │  ━╱                   ╱
   0 ┼━━┿━━━━━━━━━━━━╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━
      opened   NOW                              reset
```

Reading is instant and two-sided, with zero extra encodings:
- **track above the beam** → hot; the dashed projection shows *when* you flame out
- **track below the beam** → banking a flat fee; the vertical gap IS the unspent
- **track flatlined at 100** → you were capped — which is exactly what a real
  limit-hit looks like: the session track slamming the ceiling. The transcript
  limit events give us those moments truthfully.

## The 5h session: an inset lens

A small **session inset** inside each account panel (charts have natural dead
space opposite the track). Same construction, 5h span: tiny beam, tiny track,
`9%(◇6%)`. Zoom on click if we want more. Alternative: a third slim row per
account — decide by eye once it renders.

## What the front page is, in total

- Account A glide chart (+ session inset)
- Account B glide chart (+ session inset)
- ONE line of verdict text above them, status-bar terse
- nothing else. History scroll-back, limit forensics, the attribution dig —
  all demoted to a separate view you open on purpose.

## The radical alternative (kept for contrast): the ILS square

Normalize x to *% of window elapsed* instead of wall-clock. Then every window —
both accounts, weekly, Fable, session — lives on ONE square with ONE shared
diagonal; each is a dot (track) whose height off the diagonal is its deviation.
Maximum density, one mark per window. Cost: loses wall-clock ("when is the
reset" becomes a label, not a position). Probably the *widget* version of this
product, not the front page.

## Honesty note (matters for the build)

Per-account percent *tracks* require sampler history, which doesn't exist yet.
The prototype synthesizes the track shape (scaled local burn through the one
true point we have — the live gauge) and must say so on its face. The moment the
sampler runs, tracks become real. Limit-hit ceiling strikes are already real
(transcript events). This chart is, honestly, the best argument for building
the sampler.

## Open questions

1. Session as **inset** inside each account panel, or its own slim row?
2. Overlay both accounts on one chart? (Works only in normalized time — the two
   beams differ on wall-clock. My take: no for front page; the ILS square is the
   overlay, and it's a different artifact.)
3. Synthesized track shape until the sampler exists — acceptable, clearly labeled?
4. Besides the one verdict line, does anything else earn front-page space —
   e.g. "limit hit Tue 3:40 AM" as a small red annotation on the track
   (it would appear naturally as a ceiling touch)?

---

# Earlier: the honest inventory (v1, kept for context)

The Timeline panel had accreted ~15 encodings: 6 bars, ◇ diamonds, used/glide
labels, hatched unspent wedges, ▲ over tint, verdict tags, time-left, exhaust
ticks, a 35-day scrollable history band, limit callout + pins, scroll controls —
with the whole attribution dig stacked beneath. Each addition was locally
reasonable; the sum stopped reading. The one question, from the brief:
**"am I going to make it to the reset, and which account should I be on?"**
