# Contributing to Glideslope

Contributions from humans, from humans with agents, and from agents with humans reviewing are all welcome. They read the same way to us. What matters is whether the change is correct, small enough to review, and honest about what it reads.

## Before you start

- **Read [`CLAUDE.md`](CLAUDE.md).** It is written for agents but it is the shortest accurate account of the rules: hermetic tests, credential discipline, the shared account shape.
- **Read the provider's section in [`PROVIDERS.md`](PROVIDERS.md)** before changing how a provider is read.
- **Open an issue first for anything non-trivial.** A two-line sketch is cheaper than a rejected PR. New providers especially: say which endpoint, which credential, and what the units mean.

## Setup

```bash
git clone https://github.com/Stage-11-Agentics/glideslope.git
cd glideslope
python3 -m pip install -e ".[dev]"
python3 -m pytest tests -q
```

Python 3.11+. No runtime dependencies. Pillow is used only for the alert image and by one test.

## What a good PR looks like

- **Tests pass and stay hermetic.** No test reads your real config, store, keychain or auth files, and none reaches a real endpoint. Feed providers saved payloads through the `--*-snapshot` seams.
- **Redact saved payloads.** Before committing a provider response as a fixture, remove emails, account ids and anything that identifies you.
- **No new credential handling.** Glideslope holds no credentials and mints no tokens. A change that stores, copies or refreshes a credential will not be merged, with the one documented Grok exception.
- **UI changes come with a screenshot** of the rebuilt page.
- **Commit messages explain why.** The diff already shows what. Use `Co-Authored-By` trailers when an agent did meaningful work on the change.

## Security

Do not open public issues for security bugs. See [`SECURITY.md`](SECURITY.md).

## License

By contributing, you agree that your changes are licensed under the project's [MIT License](LICENSE).
