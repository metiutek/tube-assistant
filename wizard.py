#!/usr/bin/env python3
"""
YouTube AI Agent — First-time Setup Wizard
Run once before starting the agent: python wizard.py  (or: tube-assistant onboard)
"""

import os
import sys
import json
import re
import shutil
import asyncio
from pathlib import Path

# ── rich check (install it first if missing) ──────────────────────────────────
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Prompt, Confirm
    from rich.table import Table
    from rich.align import Align
    from rich.progress import Progress, SpinnerColumn, TextColumn
    from rich.rule import Rule
except ImportError:
    print("\n[!] Missing 'rich'. Installing now...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "rich", "-q"])
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Prompt, Confirm
    from rich.table import Table
    from rich.align import Align
    from rich.progress import Progress, SpinnerColumn, TextColumn
    from rich.rule import Rule

try:
    from telegram import Update
    from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
except ImportError:
    print("\n[!] Missing python-telegram-bot. Run: pip install -r requirements.txt")
    sys.exit(1)

try:
    from dotenv import set_key, dotenv_values
except ImportError:
    print("\n[!] Missing python-dotenv. Run: pip install -r requirements.txt")
    sys.exit(1)

console = Console()

ENV_FILE      = ".env"
STATE_FILE    = "state.json"
PREF_FILE     = "preferenze_video.json"
MEMORY_FILE   = "memoria_lungo_termine.json"
CREDS_FILE    = "credentials.json"
SETUP_DONE    = ".setup_done"


# ── env helpers ───────────────────────────────────────────────────────────────

def load_env() -> dict:
    if Path(ENV_FILE).exists():
        return dict(dotenv_values(ENV_FILE))
    # bootstrap from the template (bundled in the package, or repo root)
    template = Path(".env.example")
    if not template.exists():
        try:
            from youtube_ai_agent._workspace import _find_template
            template = _find_template(".env.example")
        except ImportError:
            template = None
    if template and template.exists():
        shutil.copy(template, ENV_FILE)
        return dict(dotenv_values(ENV_FILE))
    return {}


def write_env_key(key: str, value: str) -> None:
    Path(ENV_FILE).touch(exist_ok=True)
    set_key(ENV_FILE, key, value, quote_mode="never")


# ── state / pref / memory helpers ─────────────────────────────────────────────

def load_state() -> dict:
    if Path(STATE_FILE).exists():
        try:
            return json.loads(Path(STATE_FILE).read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    Path(STATE_FILE).write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def load_pref() -> dict:
    default = {
        "ritmo": "medio", "tono_voce": "confident", "lingua": "english",
        "stile_clip": "cinematic", "stile_thumbnail": "cinematic dramatic lighting, dark moody",
        "argomenti_preferiti": ["AI", "technology", "future tech"],
        "argomenti_evitare": [], "durata_target_minuti": 8,
        "musica_volume": 0.1, "note_libere": "",
    }
    if Path(PREF_FILE).exists():
        try:
            data = json.loads(Path(PREF_FILE).read_text(encoding="utf-8"))
            return {**default, **data}
        except Exception:
            pass
    return default


def save_pref(pref: dict) -> None:
    Path(PREF_FILE).write_text(
        json.dumps(pref, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def add_memory(text: str) -> None:
    from datetime import datetime, timezone
    memories = []
    if Path(MEMORY_FILE).exists():
        try:
            memories = json.loads(Path(MEMORY_FILE).read_text(encoding="utf-8"))
        except Exception:
            pass
    memories.append({
        "testo": text.strip(),
        "data": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    })
    Path(MEMORY_FILE).write_text(
        json.dumps(memories, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ── display helpers ───────────────────────────────────────────────────────────

def header(title: str, subtitle: str = "") -> None:
    console.print()
    body = f"[bold cyan]{title}[/]"
    if subtitle:
        body += f"\n[dim]{subtitle}[/]"
    console.print(Panel(body, border_style="cyan", padding=(1, 4)))
    console.print()


def ok(msg: str):  console.print(f"  [bold green]✓[/]  {msg}")
def info(msg: str): console.print(f"  [dim cyan]→[/]  {msg}")
def warn(msg: str): console.print(f"  [bold yellow]⚠[/]  {msg}")
def err(msg: str):  console.print(f"  [bold red]✗[/]  {msg}")


# ── step 0 — agent language ───────────────────────────────────────────────────

def step_agent_language() -> None:
    console.clear()
    header("Step 1 / 7 — Agent Language", "What language should your Telegram assistant speak?")

    console.print("  [dim]Type any language: English, Italiano, Español, Français, Deutsch…[/]")
    console.print()

    lang = Prompt.ask("  Language", default="English").strip()
    if not lang:
        lang = "English"
    write_env_key("AGENT_LANGUAGE", lang)
    console.print()
    ok(f"Agent language set to [bold]{lang}[/]")


# ── step 1 — AI service ───────────────────────────────────────────────────────

AI_SERVICES = {
    "1":  {"name": "OpenRouter",   "desc": "Free tier — 100+ models",          "service_id": "openrouter",   "env_key": "OPENROUTER_API_KEY",   "hint": "openrouter.ai/keys"},
    "2":  {"name": "OpenAI",       "desc": "GPT-4o, GPT-4o-mini",              "service_id": "openai",       "env_key": "OPENAI_API_KEY",       "hint": "platform.openai.com/api-keys"},
    "3":  {"name": "Anthropic",    "desc": "Claude 3.5 Haiku / Sonnet",        "service_id": "anthropic",    "env_key": "ANTHROPIC_API_KEY",    "hint": "console.anthropic.com"},
    "4":  {"name": "Gemini",       "desc": "Gemini 2.0 Flash — free tier",     "service_id": "gemini",       "env_key": "GEMINI_API_KEY",       "hint": "aistudio.google.com/apikey"},
    "5":  {"name": "Mistral",      "desc": "Mistral Small / Large",            "service_id": "mistral",      "env_key": "MISTRAL_API_KEY",      "hint": "console.mistral.ai/api-keys"},
    "6":  {"name": "Groq",         "desc": "Ultra-fast inference — free tier", "service_id": "groq",         "env_key": "GROQ_API_KEY",         "hint": "console.groq.com/keys"},
    "7":  {"name": "DeepSeek",     "desc": "DeepSeek-V3 — very affordable",    "service_id": "deepseek",     "env_key": "DEEPSEEK_API_KEY",     "hint": "platform.deepseek.com/api_keys"},
    "8":  {"name": "xAI (Grok)",   "desc": "Grok-2 by xAI",                   "service_id": "xai",          "env_key": "XAI_API_KEY",          "hint": "console.x.ai"},
    "9":  {"name": "Cohere",       "desc": "Command R+",                       "service_id": "cohere",       "env_key": "COHERE_API_KEY",       "hint": "dashboard.cohere.com/api-keys"},
    "10": {"name": "Together AI",  "desc": "Open source models, fast",         "service_id": "together",     "env_key": "TOGETHER_API_KEY",     "hint": "api.together.ai/settings/api-keys"},
    "11": {"name": "Perplexity",   "desc": "Search-augmented LLMs",            "service_id": "perplexity",   "env_key": "PERPLEXITY_API_KEY",   "hint": "perplexity.ai/settings/api"},
    "12": {"name": "Fireworks AI", "desc": "Fast open source inference",       "service_id": "fireworks",    "env_key": "FIREWORKS_API_KEY",    "hint": "fireworks.ai/api-keys"},
    "13": {"name": "Azure OpenAI", "desc": "OpenAI via Microsoft Azure",       "service_id": "azure_openai", "env_key": "AZURE_OPENAI_API_KEY", "hint": "portal.azure.com"},
    "14": {"name": "Ollama Cloud", "desc": "Hosted nemotron-3-super",          "service_id": "ollama_cloud", "env_key": "OLLAMA_API_KEY",       "hint": "ollama.com"},
    "15": {"name": "Local Ollama", "desc": "Any model on your machine — free", "service_id": "ollama_local", "env_key": None,                   "hint": "ollama.com/download"},
}


def _test_openrouter(key: str) -> tuple[bool, str]:
    import requests
    try:
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": "meta-llama/llama-3.3-70b-instruct:free",
                  "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 5},
            timeout=25,
        )
        r.raise_for_status()
        return True, ""
    except Exception as e:
        return False, str(e)[:100]


def _test_ollama_cloud(key: str) -> tuple[bool, str]:
    import requests
    try:
        r = requests.post(
            "https://ollama.com/api/chat",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": "nemotron-3-super:cloud",
                  "messages": [{"role": "user", "content": "Hi"}],
                  "stream": False, "options": {"num_predict": 5}},
            timeout=25,
        )
        r.raise_for_status()
        return True, ""
    except Exception as e:
        return False, str(e)[:100]


def _test_ollama_local(model: str) -> tuple[bool, str]:
    import requests
    try:
        r = requests.post(
            "http://localhost:11434/api/chat",
            json={"model": model,
                  "messages": [{"role": "user", "content": "Hi"}],
                  "stream": False, "options": {"num_predict": 5}},
            timeout=10,
        )
        r.raise_for_status()
        return True, ""
    except Exception as e:
        return False, str(e)[:100]


def _test_openai(key: str) -> tuple[bool, str]:
    try:
        from openai import OpenAI
        client = OpenAI(api_key=key)
        client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=5,
        )
        return True, ""
    except Exception as e:
        return False, str(e)[:100]


def _test_anthropic(key: str) -> tuple[bool, str]:
    try:
        import anthropic as sdk
        client = sdk.Anthropic(api_key=key)
        client.messages.create(
            model="claude-3-5-haiku-20241022",
            max_tokens=5,
            messages=[{"role": "user", "content": "Hi"}],
        )
        return True, ""
    except Exception as e:
        return False, str(e)[:100]


def _test_gemini(key: str) -> tuple[bool, str]:
    try:
        import google.generativeai as genai
        genai.configure(api_key=key)
        model = genai.GenerativeModel("gemini-2.0-flash")
        model.generate_content("Hi", generation_config={"max_output_tokens": 5})
        return True, ""
    except Exception as e:
        return False, str(e)[:100]


def step_ai_service() -> None:
    console.clear()
    header("Step 2 / 7 — AI Service", "Choose which AI powers the agent")

    table = Table(show_header=True, header_style="bold cyan", border_style="dim")
    table.add_column("#", width=3)
    table.add_column("Service", width=16)
    table.add_column("Notes")
    for k, v in AI_SERVICES.items():
        table.add_row(k, v["name"], v["desc"])
    console.print(table)
    console.print()

    choice = Prompt.ask("Choose", choices=[str(i) for i in range(1, 16)], default="1")
    svc = AI_SERVICES[choice]

    write_env_key("AI_SERVICE", svc["service_id"])

    if svc["env_key"] is None:
        # local ollama
        import subprocess
        console.print()
        try:
            res = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=5)
            if res.returncode == 0:
                ok("Local Ollama found")
            else:
                warn("Ollama not responding. Start it with: ollama serve")
        except Exception:
            warn(f"Ollama not found in PATH. Install from: {svc['url']}")
        model = Prompt.ask("Model name", default="llama3.2")
        write_env_key("OLLAMA_LOCAL_MODEL", model)
        console.print()
        with Progress(SpinnerColumn(), TextColumn("[cyan]Testing..."), transient=True) as p:
            p.add_task("")
            passed, emsg = _test_ollama_local(model)
        if passed:
            ok(f"Local Ollama responding with model '{model}'")
        else:
            warn(f"Not reachable: {emsg}")
            warn("Make sure Ollama is running before starting the agent.")
    else:
        console.print()
        info(svc["hint"])
        key = Prompt.ask(f"[bold]{svc['name']} API key[/]", password=False)
        write_env_key(svc["env_key"], key)
        console.print()
        with Progress(SpinnerColumn(), TextColumn("[cyan]Testing connection..."), transient=True) as p:
            p.add_task("")
            sid = svc["service_id"]
            if sid == "openrouter":
                passed, emsg = _test_openrouter(key)
            elif sid == "openai":
                passed, emsg = _test_openai(key)
            elif sid == "anthropic":
                passed, emsg = _test_anthropic(key)
            elif sid == "gemini":
                passed, emsg = _test_gemini(key)
            elif sid == "ollama_cloud":
                passed, emsg = _test_ollama_cloud(key)
            else:
                # generic: save key and skip live test
                passed, emsg = True, ""
        if passed:
            ok("Connection successful")
        else:
            warn(f"Could not verify ({emsg})")
            if not Confirm.ask("  Continue anyway?", default=True):
                step_ai_service()
                return

    console.print()
    Prompt.ask("[dim]Press Enter to continue[/]", default="")


# ── step 2b — image provider ──────────────────────────────────────────────────

IMAGE_PROVIDERS = {
    "1": {
        "name": "Free (no API key)",
        "desc": "Pollinations.ai — free, no account needed, good quality",
        "provider_id": "pollinations",
        "env_key": None,
        "hint": None,
    },
    "2": {
        "name": "HuggingFace FLUX.1-schnell",
        "desc": "High quality AI images — free HF account required",
        "provider_id": "huggingface",
        "env_key": "HF_API_KEY",
        "hint": "huggingface.co/settings/tokens",
    },
    "3": {
        "name": "OpenRouter",
        "desc": "Uses your existing OpenRouter key — FLUX.1-schnell",
        "provider_id": "openrouter",
        "env_key": "OPENROUTER_API_KEY",
        "hint": None,
    },
}


def _test_hf_key(key: str) -> tuple[bool, str]:
    import requests
    if not key.startswith("hf_") or len(key) < 20:
        return False, "Token non valido. Deve iniziare con 'hf_' — copialo da huggingface.co/settings/tokens"
    try:
        # verifica token tramite API profilo utente
        r = requests.get(
            "https://huggingface.co/api/whoami-v2",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        )
        if r.status_code == 401:
            return False, "Token non riconosciuto. Vai su huggingface.co/settings/tokens e copia un token valido."
        if r.status_code not in (200, 403):
            return False, f"Errore inatteso ({r.status_code}). Riprova tra qualche minuto."
    except Exception as e:
        return False, f"Impossibile contattare HuggingFace: {str(e)[:80]}"

    # Auth ok: prova una generazione FLUX vera. whoami non basta — il provider
    # di inference (nscale) puo' restituire 401/402 anche con token valido.
    try:
        from huggingface_hub import InferenceClient
        InferenceClient(token=key).text_to_image(
            "test", model="black-forest-labs/FLUX.1-schnell", width=256, height=256,
        )
        return True, ""
    except Exception as e:
        msg = str(e)
        if "401" in msg or "403" in msg:
            return False, ("Token valido ma niente accesso all'inference FLUX. "
                           "Accetta i termini su huggingface.co/black-forest-labs/FLUX.1-schnell "
                           "o abilita l'inference nelle impostazioni del token.")
        if "402" in msg or "payment" in msg.lower() or "quota" in msg.lower():
            return False, ("Crediti inference HuggingFace esauriti per questo mese. "
                           "Aspetta il reset o usa un altro provider.")
        # rete/timeout: non blocco il setup, il token auth e' ok
        return True, f"⚠ Token ok ma test FLUX non concluso ({msg[:60]}). Si prova a runtime."


def step_image_provider() -> None:
    console.clear()
    header("Step 3 / 7 — Thumbnail Image Generation",
           "Choose how to generate your video thumbnails")

    console.print("  [bold]Available providers:[/]\n")
    for num, p in IMAGE_PROVIDERS.items():
        console.print(f"  [cyan]{num}[/]  [bold]{p['name']}[/]  [dim]— {p['desc']}[/]")
    console.print()

    choice = Prompt.ask("  Choose provider", choices=list(IMAGE_PROVIDERS.keys()), default="1")
    prov = IMAGE_PROVIDERS[choice]
    write_env_key("IMAGE_PROVIDER", prov["provider_id"])
    console.print()

    if prov["provider_id"] == "pollinations":
        info("No API key needed — thumbnails generated automatically")

    elif prov["provider_id"] == "huggingface":
        info(f"Get a free token at: {prov['hint']}")
        console.print("  [dim](Create account → Settings → Access Tokens → New token, type: Read)[/]")
        console.print()
        key = Prompt.ask("[bold]HuggingFace API token[/]", password=False)
        write_env_key("HF_API_KEY", key)
        console.print()
        with Progress(SpinnerColumn(), TextColumn("[cyan]Verifying token..."), transient=True) as p_:
            p_.add_task("")
            passed, emsg = _test_hf_key(key)
        if passed:
            if emsg:
                warn(emsg)
            else:
                ok("Token HuggingFace verificato — generazione FLUX funziona!")
        else:
            console.print()
            warn(emsg)
            console.print()
            if not Confirm.ask("  Vuoi scegliere un'altra opzione?", default=False):
                pass  # continua con il token salvato
            else:
                step_image_provider()
                return

    elif prov["provider_id"] == "openrouter":
        existing = dict(dotenv_values(ENV_FILE)).get("OPENROUTER_API_KEY", "")
        if existing:
            ok("Will use existing OpenRouter key for image generation")
        else:
            warn("No OpenRouter key found — set it in Step 2 (AI Service) or add manually to .env")

    console.print()
    ok(f"Image provider set to [bold]{prov['name']}[/]")
    console.print()
    Prompt.ask("[dim]Press Enter to continue[/]", default="")


# ── step 2c — pexels ──────────────────────────────────────────────────────────

def step_pexels() -> None:
    console.clear()
    header("Step 4 / 7 — Pexels API", "Free stock video clips for your videos")
    info("Create a free account and get a key at: [link]https://www.pexels.com/api/[/]")
    info("Serve comunque: e' la rete di sicurezza anche se generi le clip con l'AI.")
    console.print()
    key = Prompt.ask("[bold]Pexels API key[/]")
    write_env_key("PEXELS_API_KEY", key)
    console.print()
    ok("Pexels key saved")
    console.print()
    Prompt.ask("[dim]Press Enter to continue[/]", default="")


# ── step 2d — sorgente clip (stock o AI video) ────────────────────────────────

def step_sorgente_clip() -> None:
    """Stock, AI generativa o le due insieme.

    Sta dopo Pexels di proposito: qualunque cosa scelga l'utente, la chiave
    Pexels resta la rete di sicurezza quando il provider AI e' giu' o senza
    credito, e chiederla dopo aver appena configurato l'AI sembrerebbe un
    passaggio inutile da saltare.
    """
    from moduli import impostazioni_video

    def _intestazione() -> None:
        console.clear()
        header("Step 5 / 7 — Sorgente delle clip",
               "Clip stock da Pexels, generate da un'AI video, o entrambe")

    impostazioni_video.configura(console, workspace=".", intestazione=_intestazione)
    console.print()
    Prompt.ask("[dim]Press Enter to continue[/]", default="")


# ── step 3 — telegram token ───────────────────────────────────────────────────

_BOT_COMMANDS = [
    ("start",           "Mostra tutti i comandi"),
    ("status",          "Stato attuale dell'agente"),
    ("forza",           "Lancia la pipeline subito"),
    ("forzaora",        "Pubblica subito senza programmazione"),
    ("skip",            "Salta il video di oggi"),
    ("abort",           "Ferma la pipeline in corso"),
    ("recap",           "Analytics ultimi video"),
    ("coda",            "Mostra coda topic"),
    ("topic",           "Aggiungi topic alla coda"),
    ("deltopic",        "Rimuovi topic dalla coda"),
    ("orari",           "Programma attuale"),
    ("prossimi",        "Prossimo run e publish"),
    ("setvideogiorno",  "Imposta quanti video al giorno"),
    ("setpubblica",     "Ora/e pubblicazione video (UTC)"),
    ("preferenze",      "Mostra preferenze video"),
    ("setpref",         "Modifica una preferenza"),
    ("canale",          "Info canale YouTube"),
    ("video",           "Lista ultimi video"),
    ("commenti",        "Ultimi commenti"),
    ("memoria",         "Mostra memoria a lungo termine"),
    ("reset",           "Reset stato agente"),
]


def _register_bot_commands(token: str) -> bool:
    import requests
    commands = [{"command": cmd, "description": desc} for cmd, desc in _BOT_COMMANDS]
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/setMyCommands",
            json={"commands": commands},
            timeout=10,
        )
        return r.json().get("ok", False)
    except Exception:
        return False


def step_telegram_token() -> str:
    console.clear()
    header("Step 6 / 7 — Telegram Bot", "Your live control interface")

    console.print("  [bold]How to create a bot:[/]")
    console.print("  1. Open Telegram and start a chat with [cyan]@BotFather[/]")
    console.print("  2. Send [cyan]/newbot[/] and follow the instructions")
    console.print("  3. Copy the token BotFather gives you")
    console.print()

    token = Prompt.ask("[bold]Bot token[/]")
    write_env_key("TELEGRAM_BOT_TOKEN", token)

    if not re.match(r"^\d+:[A-Za-z0-9_-]{35,}$", token):
        warn("That doesn't look like a valid bot token — double-check it.")

    with Progress(SpinnerColumn(), TextColumn("[cyan]Registering bot commands..."), transient=True) as p:
        p.add_task("")
        registered = _register_bot_commands(token)

    if registered:
        ok("Token saved — comandi / registrati su Telegram")
    else:
        ok("Token saved")
        warn("Comandi / non registrati — verranno attivati al primo avvio dell'agente")

    console.print()
    Prompt.ask("[dim]Press Enter to continue[/]", default="")
    return token


# ── step 4 — telegram onboarding (async bot) ──────────────────────────────────

_QUESTIONS_EN = [
    ("user_name",        "👋 Hi! I'm your YouTube AI Agent.\n\nFirst — what's your name?"),
    ("channel_name",     "Nice to meet you, {user_name}!\n\nWhat is the name of your YouTube channel?"),
    ("channel_topic",    "What topic or niche is the channel about?\n(e.g. AI, cooking, travel, gaming, finance...)"),
    ("channel_goals",    "What are the goals for the channel?\n(e.g. reach 10k subscribers, monetize, build a personal brand...)"),
    ("channel_genre",    "What video genre/style do you want?\n(e.g. educational, entertaining, documentary, news, shorts...)"),
    ("thumbnail_style",  "🖼 How should your thumbnails look?\n\nDescribe the visual style freely.\n\nExamples:\n• dark cinematic, dramatic lighting, deep shadows\n• bright vibrant colors, energetic pop style\n• minimalist, clean white background, bold text\n• neon futuristic, cyberpunk, glowing outlines\n• warm tones, cozy lifestyle photography"),
    ("language",         "What language should the videos be in?\n\nReply: english  or  italian"),
]

_QUESTIONS_IT = [
    ("user_name",        "👋 Ciao! Sono il tuo YouTube AI Agent.\n\nPer iniziare — come ti chiami?"),
    ("channel_name",     "Piacere, {user_name}!\n\nCome si chiama il tuo canale YouTube?"),
    ("channel_topic",    "Di che argomento o nicchia tratta il canale?\n(es. AI, cucina, viaggi, gaming, finanza...)"),
    ("channel_goals",    "Quali sono gli obiettivi del canale?\n(es. arrivare a 10k iscritti, monetizzare, costruire un personal brand...)"),
    ("channel_genre",    "Che genere/stile di video vuoi fare?\n(es. educativo, intrattenimento, documentario, news, shorts...)"),
    ("thumbnail_style",  "🖼 Come devono essere le copertine dei tuoi video?\n\nDescrivi liberamente lo stile visivo.\n\nEsempi:\n• dark cinematografico, luci drammatiche, ombre profonde\n• colori vivaci e brillanti, stile energetico pop\n• minimal, sfondo bianco pulito, testo in grassetto\n• neon futuristico, cyberpunk, luci al neon\n• toni caldi, fotografia lifestyle accogliente"),
    ("language",         "In che lingua devono essere i video?\n\nRispondi: english  oppure  italian"),
]

_CREDS_MSG_EN = (
    "🔑 *Almost done!* I need access to your YouTube channel.\n\n"
    "Follow these steps:\n"
    "1. Go to [console.cloud.google.com](https://console.cloud.google.com)\n"
    "2. Create or select a project\n"
    "3. Enable *YouTube Data API v3*\n"
    "4. Go to Credentials → Create Credentials → OAuth 2.0 Client IDs\n"
    "5. Application type: *Desktop app*\n"
    "6. Download the JSON file\n"
    "7. Send it here as a file 📎\n\n"
    "I'll handle the rest!"
)

_CREDS_MSG_IT = (
    "🔑 *Quasi fatto!* Ho bisogno di accedere al tuo canale YouTube.\n\n"
    "Segui questi passi:\n"
    "1. Vai su [console.cloud.google.com](https://console.cloud.google.com)\n"
    "2. Crea o seleziona un progetto\n"
    "3. Abilita *YouTube Data API v3*\n"
    "4. Vai su Credenziali → Crea credenziali → ID client OAuth 2.0\n"
    "5. Tipo applicazione: *App desktop*\n"
    "6. Scarica il file JSON\n"
    "7. Inviamelo qui come file 📎\n\n"
    "Penso a tutto io!"
)

_LANG_PROMPT = (
    "👋 Hi / Ciao!\n\n"
    "Choose setup language:\n"
    "• Reply [bold]english[/bold]\n"
    "• Rispondi [bold]italiano[/bold]"
)


async def run_onboarding(token: str) -> dict:
    """Runs the Telegram onboarding bot and returns collected data."""

    result: dict = {}
    done_event = asyncio.Event()

    # state machine
    state: dict = {
        "chat_id":    None,
        "phase":      "lang",   # lang → questions → creds → done
        "lang":       "en",
        "q_idx":      0,
        "answers":    {},
    }

    async def send(ctx, chat_id, text):
        await ctx.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")

    async def on_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.message.chat_id
        state["chat_id"] = chat_id
        state["phase"]   = "lang"
        await send(ctx, chat_id, _LANG_PROMPT)

    async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if state["chat_id"] is None:
            await on_start(update, ctx)
            return

        chat_id = update.message.chat_id
        text = (update.message.text or "").strip()
        phase = state["phase"]

        if phase == "lang":
            lang = "it" if "ital" in text.lower() or "it" == text.lower().strip() else "en"
            state["lang"] = lang
            state["phase"] = "questions"
            state["q_idx"] = 0
            questions = _QUESTIONS_IT if lang == "it" else _QUESTIONS_EN
            await send(ctx, chat_id, questions[0][1])

        elif phase == "questions":
            questions = _QUESTIONS_IT if state["lang"] == "it" else _QUESTIONS_EN
            idx = state["q_idx"]
            key, _ = questions[idx]
            state["answers"][key] = text
            idx += 1
            state["q_idx"] = idx
            if idx < len(questions):
                _, q_text = questions[idx]
                formatted = q_text.format(**state["answers"])
                await send(ctx, chat_id, formatted)
            else:
                state["phase"] = "creds"
                msg = _CREDS_MSG_IT if state["lang"] == "it" else _CREDS_MSG_EN
                await send(ctx, chat_id, msg)

        elif phase == "creds":
            lang = state["lang"]
            reminder = (
                "Invia il file JSON di Google come *allegato* 📎"
                if lang == "it" else
                "Please send the Google JSON file as an *attachment* 📎"
            )
            await send(ctx, chat_id, reminder)

    async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if state["phase"] != "creds":
            return
        doc = update.message.document
        if not doc:
            return

        lang = state["lang"]
        tg_file = await ctx.bot.get_file(doc.file_id)
        tmp = "_creds_setup_tmp.json"
        await tg_file.download_to_drive(tmp)

        try:
            data = json.loads(Path(tmp).read_text(encoding="utf-8"))
            if "installed" not in data and "web" not in data:
                raise ValueError("not an OAuth credentials file")
            shutil.move(tmp, CREDS_FILE)
        except Exception as e:
            Path(tmp).unlink(missing_ok=True)
            bad_msg = (
                f"❌ File non valido: {e}\nRiscarica il file da Google Cloud Console."
                if lang == "it" else
                f"❌ Invalid file: {e}\nPlease re-download the JSON from Google Cloud Console."
            )
            await send(ctx, update.message.chat_id, bad_msg)
            return

        state["phase"] = "done"
        done_msg = (
            "✅ *Setup completato!*\n\nAvvia l'agente con:\n`python agent.py`\n\nA presto! 🚀"
            if lang == "it" else
            "✅ *Setup complete!*\n\nStart the agent with:\n`python agent.py`\n\nSee you there! 🚀"
        )
        await send(ctx, update.message.chat_id, done_msg)

        result.update(state["answers"])
        result["chat_id"] = str(state["chat_id"])
        result["lang"]    = lang
        done_event.set()

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    await done_event.wait()

    await app.updater.stop()
    await app.stop()
    await app.shutdown()

    return result


def step_telegram_onboarding(token: str) -> dict:
    console.clear()
    header("Step 7 / 7 — Channel Setup via Telegram", "Your AI agent will interview you")

    console.print(Panel(
        "[bold cyan]Open your Telegram bot and send /start[/]\n\n"
        "[dim]The agent will ask you a few questions about your channel,\n"
        "then guide you through connecting your Google/YouTube account.[/]",
        border_style="cyan",
        padding=(1, 4),
    ))
    console.print()
    info("Waiting for you on Telegram…")
    console.print()

    return asyncio.run(run_onboarding(token))


# ── save all results ──────────────────────────────────────────────────────────

def save_results(data: dict) -> None:
    # telegram chat id
    write_env_key("TELEGRAM_CHAT_ID", data.get("chat_id", ""))

    # language → preferenze
    lang_raw = data.get("language", "english").lower()
    lang = "italian" if ("ital" in lang_raw or "italian" in lang_raw) else "english"
    pref = load_pref()
    pref["lingua"] = lang

    # preferred topics from channel topic
    topic_raw = data.get("channel_topic", "")
    if topic_raw:
        topics = [t.strip() for t in re.split(r"[,/;]", topic_raw) if t.strip()]
        if topics:
            pref["argomenti_preferiti"] = topics[:6]

    save_pref(pref)

    # long-term memory
    # thumbnail style → preferenze
    thumb_style = data.get("thumbnail_style", "").strip()
    if thumb_style:
        pref["stile_thumbnail"] = thumb_style
        save_pref(pref)

    mappings = [
        ("user_name",        "Owner name"),
        ("channel_name",     "YouTube channel name"),
        ("channel_topic",    "Channel topic/niche"),
        ("channel_goals",    "Channel goals"),
        ("channel_genre",    "Video genre/style"),
        ("thumbnail_style",  "Thumbnail style"),
        ("language",         "Video language"),
    ]
    for key, label in mappings:
        value = data.get(key, "").strip()
        if value:
            add_memory(f"{label}: {value}")

    # mark setup done
    Path(SETUP_DONE).write_text("done", encoding="utf-8")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    console.clear()
    console.print(Panel(
        Align.center(
            "[bold cyan]YouTube AI Agent[/]\n"
            "[bold]Setup Wizard[/]\n\n"
            "[dim]Autonomous video pipeline: script → TTS → clips → montage → upload[/]\n\n"
            "[dim]This wizard configures everything in 7 steps.[/]"
        ),
        border_style="cyan",
        padding=(2, 8),
    ))
    console.print()

    if Path(SETUP_DONE).exists():
        warn("Setup already completed. Running again will overwrite previous settings.")
        if not Confirm.ask("  Continue?", default=False):
            console.print()
            info("Run [bold cyan]python agent.py[/] to start the agent.")
            console.print()
            sys.exit(0)

    if not Confirm.ask("  Start setup?", default=True):
        console.print()
        sys.exit(0)

    # ── steps ────────────────────────────────────────────────────────────────
    step_agent_language()
    step_ai_service()
    step_image_provider()
    step_pexels()
    step_sorgente_clip()
    token = step_telegram_token()
    data  = step_telegram_onboarding(token)
    save_results(data)

    # ── done screen ───────────────────────────────────────────────────────────
    console.clear()
    console.print(Panel(
        Align.center(
            "[bold green]✓  Setup Complete![/]\n\n"
            "[dim]All settings saved.[/]\n\n"
            "[bold cyan]Starting the agent now...[/]"
        ),
        border_style="green",
        padding=(2, 8),
    ))
    console.print()

    table = Table(show_header=False, border_style="dim", padding=(0, 2))
    table.add_column("Key", style="dim")
    table.add_column("Value", style="cyan")
    env_vals = dict(dotenv_values(ENV_FILE))
    table.add_row("AI Service",      env_vals.get("AI_SERVICE", "—"))
    table.add_row("Image Provider",  env_vals.get("IMAGE_PROVIDER", "pollinations"))
    from moduli.impostazioni_video import stato as _stato_clip
    table.add_row("Clip video",      _stato_clip("."))
    table.add_row("Language",        data.get("language", "english"))
    table.add_row("Channel",         data.get("channel_name", "—"))
    table.add_row("Credentials",     "✓ saved" if Path(CREDS_FILE).exists() else "✗ not found")
    table.add_row("Telegram",        f"chat_id {data.get('chat_id', '—')}")
    console.print(table)
    console.print()

    # auto-start: works for both git clone and uv tool install
    workspace = str(Path.cwd())
    os.execv(sys.executable, [sys.executable, "-m", "youtube_ai_agent._launcher", workspace, "agent"])


if __name__ == "__main__":
    main()
