"""Read-only runtime checks for the local TubeAssistant workspace."""

import importlib.util
import os
import shutil
from pathlib import Path


AI_KEY_BY_SERVICE = {
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "groq": "GROQ_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "xai": "XAI_API_KEY",
    "cohere": "COHERE_API_KEY",
    "together": "TOGETHER_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
    "azure_openai": "AZURE_OPENAI_API_KEY",
    "ollama_cloud": "OLLAMA_API_KEY",
}


# Valori che il template `.env.example` scrive gia' compilati: sono segnaposto,
# non configurazione. Senza questo controllo il preflight diceva "OK
# PEXELS_API_KEY - configurato" su un workspace appena creato e l'errore vero
# usciva molto piu' tardi, come 401 di Pexels a meta' pipeline.
_PLACEHOLDER_MARKERS = (
    "your_", "yourkey", "inserisci", "changeme", "xxxx", "<", "put_your",
    "api_key_here", "token_here",
)


def _e_placeholder(value: str) -> bool:
    v = (value or "").strip().lower()
    if not v:
        return False
    return any(m in v for m in _PLACEHOLDER_MARKERS)


def _has_env(name: str) -> bool:
    value = os.environ.get(name, "").strip()
    return bool(value) and not _e_placeholder(value)


def _dettaglio_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        return "mancante"
    if _e_placeholder(value):
        return f"valore segnaposto del template ({value[:24]}) — va sostituito"
    return "configurato"


def _analytics_api(ws: Path):
    """(ok, warn, dettaglio) sullo stato di YouTube Analytics API, o None.

    Se l'API non e' abilitata nel progetto Google la pipeline non si ferma:
    ogni run stampa un muro di HttpError 403 e poi tratta i dati mancanti
    come metriche a zero, cosi' la strategia impara da numeri inesistenti.
    Meglio dirlo qui. Il controllo non fa mai partire un login interattivo:
    se il token non e' utilizzabile, si limita a non pronunciarsi.
    """
    token = ws / "token.json"
    if not token.exists():
        return None
    try:
        from datetime import date
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials.from_authorized_user_file(str(token))
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        if not creds.valid:
            return (True, True, "non verificabile (token da rinnovare)")
        yta = build("youtubeAnalytics", "v2", credentials=creds)
        yta.reports().query(
            ids="channel==MINE", startDate="2020-01-01",
            endDate=date.today().isoformat(), metrics="views",
        ).execute()
        return (True, False, "abilitata")
    except Exception as e:
        testo = str(e)
        if ("accessNotConfigured" in testo or "SERVICE_DISABLED" in testo
                or "has not been used in project" in testo):
            return (False, False,
                    "API disabilitata nel progetto Google — abilitala su "
                    "https://console.cloud.google.com/apis/library/youtubeanalytics.googleapis.com "
                    "(senza, CTR e retention risultano 0 e la strategia impara dati falsi)")
        return (True, True, f"non verificabile ora ({testo[:70]})")


def run_checks(workspace: str | Path = ".") -> list[dict]:
    ws = Path(workspace)
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str = "", warn: bool = False) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "warn": bool(warn)})

    add("workspace", ws.exists(), str(ws.resolve()))
    add(".env", (ws / ".env").exists(), "presente" if (ws / ".env").exists() else "mancante")
    add("credentials.json", (ws / "credentials.json").exists(), "OAuth YouTube")
    add("token.json", (ws / "token.json").exists(), "token esistente" if (ws / "token.json").exists() else "verra creato al primo login")

    ffmpeg = os.environ.get("FFMPEG_PATH") or shutil.which("ffmpeg")
    add("ffmpeg", bool(ffmpeg), ffmpeg or "non trovato in PATH/FFMPEG_PATH")

    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "PEXELS_API_KEY", "AI_SERVICE"):
        add(key, _has_env(key), _dettaglio_env(key))

    service = os.environ.get("AI_SERVICE", "").strip()
    service_key = AI_KEY_BY_SERVICE.get(service)
    if service_key:
        dettaglio = _dettaglio_env(service_key)
        add(service_key, _has_env(service_key),
            f"chiave per {service}" if dettaglio == "configurato" else f"{dettaglio} — chiave per {service}")

    provider = os.environ.get("IMAGE_PROVIDER", "pollinations").lower()
    if provider == "huggingface":
        add("huggingface_hub", importlib.util.find_spec("huggingface_hub") is not None, "richiesto da IMAGE_PROVIDER=huggingface")
        add("HF_API_KEY", _has_env("HF_API_KEY"), "chiave HuggingFace")
    elif provider == "openrouter":
        add("OPENROUTER_API_KEY", _has_env("OPENROUTER_API_KEY"), "thumbnail via OpenRouter")
    else:
        add("image provider", True, provider)

    # Sorgente clip: con VIDEO_SOURCE diverso da pexels serve la chiave del
    # provider video, altrimenti ogni run genera zero clip e ripiega in
    # silenzio sullo stock — l'utente crede di pagare per l'AI e non la usa.
    try:
        from moduli.video_ai import PROVIDER_DEFAULTS, provider, sorgente_video
        sorgente = sorgente_video()
        if sorgente == "pexels":
            add("sorgente clip", True, "Pexels (stock)")
        else:
            conf = PROVIDER_DEFAULTS[provider()]
            ha_chiave = _has_env(conf["env_key"])
            etichetta = "solo AI" if sorgente == "ai" else "ibrido AI + Pexels"
            add("sorgente clip", ha_chiave,
                f"{etichetta} via {conf['etichetta']}" if ha_chiave
                else f"{etichetta} ma {conf['env_key']} {_dettaglio_env(conf['env_key'])}")
    except Exception as e:
        add("sorgente clip", True, f"non verificabile ({str(e)[:60]})", warn=True)

    analytics = _analytics_api(ws)
    if analytics is not None:
        ok_an, warn_an, dettaglio_an = analytics
        add("youtube analytics API", ok_an, dettaglio_an, warn=warn_an)

    output = ws / "output"
    cache = ws / "cache"
    add("output dir", output.exists() or os.access(ws, os.W_OK), str(output))
    add("cache dir", cache.exists() or os.access(ws, os.W_OK), str(cache))
    return checks


def format_checks(checks: list[dict]) -> str:
    lines = []
    for check in checks:
        if not check["ok"]:
            mark = "FAIL"
        elif check.get("warn"):
            mark = "WARN"
        else:
            mark = "OK"
        detail = f" - {check['detail']}" if check.get("detail") else ""
        lines.append(f"{mark:4} {check['name']}{detail}")
    return "\n".join(lines)
