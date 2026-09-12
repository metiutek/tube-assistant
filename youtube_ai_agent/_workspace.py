"""
Workspace management.

Priority:
1. YOUTUBE_AI_WORKSPACE env var
2. Current directory if it contains .setup_done or agent.py (git-clone scenario)
3. ~/.youtube-ai-agent/
"""

import os
import shutil
from pathlib import Path

DEFAULT_HOME = Path.home() / ".youtube-ai-agent"

SCAFFOLD_DIRS = [
    "output",
    "cache/pexels",
    "cache/ai_video",
    "assets/custom/clips",
    "assets/custom/thumbnails",
    "assets/custom/audio",
    "logs",
]


def get() -> Path:
    """Return the active workspace path (does not create it)."""
    env = os.environ.get("YOUTUBE_AI_WORKSPACE")
    if env:
        return Path(env).expanduser().resolve()
    cwd = Path.cwd()
    if (cwd / ".setup_done").exists() or (cwd / "agent.py").exists():
        return cwd
    return DEFAULT_HOME


def scaffold(workspace: Path) -> None:
    """Create workspace directories and copy template files if missing."""
    workspace.mkdir(parents=True, exist_ok=True)

    for d in SCAFFOLD_DIRS:
        (workspace / d).mkdir(parents=True, exist_ok=True)

    # .env from template
    env_dest = workspace / ".env"
    if not env_dest.exists():
        template = _find_template(".env.example")
        if template:
            shutil.copy(template, env_dest)
        else:
            env_dest.write_text(
                "AI_SERVICE=ollama_cloud\n"
                "OLLAMA_API_KEY=\n"
                "OPENROUTER_API_KEY=\n"
                "OLLAMA_LOCAL_MODEL=llama3.2\n"
                "PEXELS_API_KEY=\n"
                "TELEGRAM_BOT_TOKEN=\n"
                "TELEGRAM_CHAT_ID=\n",
                encoding="utf-8",
            )

    # music assets — copy from bundled package assets if not already present
    bundled_music = _package_assets() / "music"
    ws_music = workspace / "assets" / "music"
    if bundled_music.exists() and not ws_music.exists():
        shutil.copytree(bundled_music, ws_music)


def _find_template(filename: str) -> Path | None:
    """Find a template file: bundled in the package, or at the git clone root."""
    candidates = [
        Path(__file__).parent / "template" / filename,
        Path(__file__).parent.parent / filename,   # git clone root (legacy layout)
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _package_assets() -> Path:
    """Return the bundled assets directory.

    In a wheel (`uv tool install`) pyproject maps the repo `assets/` into
    `youtube_ai_agent/assets/`. In a git clone that directory does not exist
    and the repo root is used. Checked in this order because `scaffold()`
    creates `<root>/assets/custom/...` first: an empty root `assets/` used to
    win and the music never got copied into the workspace.
    """
    bundled = Path(__file__).parent / "assets"
    if (bundled / "music").exists():
        return bundled
    return Path(__file__).parent.parent / "assets"
