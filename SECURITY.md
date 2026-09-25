# Security

Found something that looks like a security issue in Glideslope? Report it privately. Public issues turn bad news into worse news.

## How to report

- **Preferred: GitHub Security Advisories.** <https://github.com/Stage-11-Agentics/glideslope/security/advisories/new>
- **Fallback: email.** `hello@stage11.ai`, subject line `glideslope security`.

Include what you found, how to trigger it, and what you already know about impact. We will acknowledge within three business days, work with you on reproduction and a fix, and coordinate disclosure after the fix ships. Credit however you want it: your name, a handle, or not at all.

## The invariants

Glideslope sits next to the login material of several paid accounts. These are the promises it makes about that material. A violation of any of them is a security bug.

1. **No credential store.** Glideslope keeps no OAuth token, refresh token, password or session of its own. Nothing it writes (the sample store, caches, beacons, logs, built HTML) contains a credential.
2. **No token is minted.** Glideslope never performs an OAuth refresh for Claude and never writes the keychain. It reads the access token Claude Code already holds, and only while that token is still valid.
3. **Codex is read through its app-server protocol.** Its auth file is read for the login email only. Its tokens are never read, copied or refreshed.
4. **Static keys are static.** `KIMI_API_KEY` and `OPENROUTER_API_KEY` come from the environment, then from the keys file (mode 0600). No other secret is ever read that way.
5. **One scoped exception: Grok.** SuperGrok's meter sits behind the Grok Build login, whose access tokens last about six hours. When the sampler finds that token expired, it performs the same refresh Grok's own client performs: an OIDC `refresh_token` grant against the first-party issuer `https://auth.x.ai` only, written back to `~/.grok/auth.json` atomically under a lock, re-read under that lock so a concurrent Grok refresh is not clobbered. The token is never copied elsewhere and never printed. A custom identity provider is refused.
6. **No credential crosses the network between machines.** Satellite beacons carry numbers, reset clocks and login emails only.
7. **Failed reads are not retried in a loop.** A provider that fails degrades to its last-known-good read. Glideslope does not hammer an endpoint with repeated authenticated requests.
8. **Nothing is hosted.** The views are local files. Glideslope serves nothing on the network.
9. **Prompt history is read for one timestamp.** `~/.claude/history.jsonl` is read only after a login change, to timestamp the `/login` that caused it. Nothing from it is stored, logged or transmitted.

## What counts

In scope:

- A path by which Glideslope stores, logs, prints or transmits a credential or a token.
- A path by which Glideslope refreshes or rotates a credential other than the Grok exception above, or drives an issuer other than `https://auth.x.ai`.
- A way to corrupt or clobber another program's login file (Claude Code, Codex, Grok Build).
- Code execution through a crafted provider payload, beacon, config file or sample store.
- Leaks of account identity (emails, account ids) into the built HTML.

Out of scope:

- Bugs in the providers' own services or clients. Report those to the provider.
- Anything that requires an already-compromised machine or user account.
- Social engineering.

## Supported versions

Glideslope is pre-1.0. Fixes land on `main`. Older versions are not maintained.

## Safe harbor

We will not pursue legal action against researchers who act in good faith, reach out before public disclosure, avoid privacy violations and service disruption, and stay inside the minimum needed to demonstrate the issue. Do not test against provider accounts that are not yours.
