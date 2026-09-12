# Changelog

All notable changes to TubeAssistant are documented here.

---

## [1.1.0] — 2026-09-12

### Added
- AI video generation as clip source (`VIDEO_SOURCE=pexels|ibrido|ai`): Replicate, fal.ai, Luma,
  OpenRouter (image + local Ken Burns move), with a per-video spend cap and a separate cache
- Clip-source settings screen shared by the wizard (step 5/7) and the TUI (`tube-assistant clips`)
- Shared scheduling module (`moduli/scheduling.py`): one source of truth for production and
  publish hours used by the daemon, Telegram and the uploader
- SRT subtitles, on-screen key captions, delivery loudness normalisation (-14 LUFS)
- Render progress and explained errors on Telegram, live `/status`
- Disk guard with automatic cache cleanup, PID lockfile against double start
- `tube-assistant dry-run` and `tube-assistant preflight`
- Regression test suite (`tests/`) run by GitHub Actions

### Fixed
- `uv tool install` deployments shipped without background music and without the `.env`
  template: `assets/` and `.env.example` are now bundled in the wheel
- Launch scripts (`start.sh`, `installa.bat`, `avvia_agente.bat`) still called the old
  `youtube-ai-agent` command
- Stale duplicate of the wizard (`_wizard_standalone.py`) removed; one wizard for every install
- Model reasoning no longer leaks into video titles; random seed + temperature for varied content
- Telegram `Conflict` handled with retry/backoff; PID lock prevents double start

---

## [1.0.0] — 2026-05-16

### Added
- Full autonomous YouTube pipeline: analytics → script → TTS → footage → montage → thumbnail → upload
- 15 AI providers: OpenRouter, OpenAI, Anthropic, Gemini, Mistral, Groq, DeepSeek, xAI, Cohere, Together, Perplexity, Fireworks, Azure OpenAI, Ollama Cloud, Local Ollama
- Interactive TUI menu (`tube-assistant` with arrow-key navigation)
- Telegram bot for live control: force run, queue topics, skip, analytics, status
- Guided onboarding wizard via Telegram (4-step TUI)
- Pipeline checkpoint recovery — resumes from last completed step after crash
- `uv tool install` support — fast installation via uv
- Cross-platform: Windows, Linux, macOS
- All models overridable via env vars (`OPENAI_MODEL`, `GROQ_MODEL`, etc.)
- Background music support (chill, epic, mysterious, upbeat, tense)
- Analytics-driven topic and tone adaptation
- Long-term memory (`memoria_lungo_termine.json`)
- `CONTRIBUTING.md` and `LICENSE` (MIT)
