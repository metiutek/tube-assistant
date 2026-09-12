# Contributing to TubeAssistant

Thanks for your interest in contributing. Here's everything you need to know.

---

## Ways to contribute

- **Report a bug** — open an issue describing what happened and how to reproduce it
- **Suggest a feature** — open an issue explaining the use case before writing code
- **Fix a bug** — pick an open issue, comment that you're working on it, submit a PR
- **Add an AI provider** — follow the pattern in `moduli/ai_client.py`
- **Improve the README** — typos, clarity, better examples

---

## Development setup

```bash
git clone https://github.com/metiu1/tube-assistant.git
cd tube-assistant

pip install uv
uv tool install -e .
```

Requirements: Python 3.11+, FFmpeg in PATH.

---

## Reporting a bug

Open an issue and include:

1. What you did
2. What you expected
3. What actually happened
4. Your OS, Python version, and `AI_SERVICE` value
5. Relevant logs from `<workspace>/logs/` (`tube-assistant workspace` prints the path)

---

## Submitting a pull request

1. Fork the repo
2. Create a branch: `git checkout -b fix/your-fix` or `feat/your-feature`
3. Make your changes
4. Run the test suite: `python -m unittest discover -s tests` (CI runs the same command)
5. Test manually: `tube-assistant dry-run` for the pipeline, `tube-assistant onboard` for the wizard
6. Open a PR with a clear description of what changed and why

Keep PRs focused — one fix or feature per PR.

---

## Adding a new AI provider

1. Add a `_yourprovider(messages, max_tokens)` function in `moduli/ai_client.py`
2. Add it to the `_PROVIDERS` dispatch table
3. Add the model constant and env var at the top of the file
4. Add the provider to `AI_SERVICES` in `wizard.py` with connection test
5. Document the key in `youtube_ai_agent/template/.env.example` and in `AI_KEY_BY_SERVICE` (`moduli/preflight.py`)

Follow the existing pattern — lazy imports, clear error messages when the key is missing.

---

## Code style

- Match the style of the surrounding code
- Comments and logs are in Italian (project convention) — code identifiers in English
- No unnecessary abstractions — keep it simple and readable
- Add a regression test in `tests/` for behaviour changes; keep `python -m unittest discover -s tests` green

---

## Questions?

Open an issue or message via Telegram if you have the bot running.
