import os
import time
import random
import threading
import requests

# ── sampling context ──────────────────────────────────────────────────────────
# Senza temperature/seed il modello tende a rigenerare sempre lo stesso testo
# (titolo e contenuto identici). Ogni chiamata riceve un seed casuale e una
# temperatura alta così l'output varia. Thread-safe: il bot Telegram gira in
# un thread separato e non deve condividere il seed con la pipeline.
_sampling_ctx = threading.local()


def _set_sampling(temperature=None, seed=None) -> None:
    _sampling_ctx.temperature = temperature
    _sampling_ctx.seed = seed


def _temp(default: float = 0.9) -> float:
    t = getattr(_sampling_ctx, "temperature", None)
    return default if t is None else t


def _seed() -> int:
    s = getattr(_sampling_ctx, "seed", None)
    return s if s is not None else random.randint(1, 2_147_483_647)

# ── service routing ───────────────────────────────────────────────────────────
# Set AI_SERVICE in .env — supported values:
#   openrouter | openai | anthropic | gemini | mistral | groq | deepseek
#   xai | cohere | together | perplexity | fireworks | azure_openai
#   ollama_cloud | ollama_local
AI_SERVICE = os.environ.get("AI_SERVICE", "openrouter")

# ── model defaults (all overridable via env) ──────────────────────────────────
OPENROUTER_URL   = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")

OPENAI_MODEL     = os.environ.get("OPENAI_MODEL",     "gpt-4o-mini")
ANTHROPIC_MODEL  = os.environ.get("ANTHROPIC_MODEL",  "claude-3-5-haiku-20241022")
GEMINI_MODEL     = os.environ.get("GEMINI_MODEL",     "gemini-2.0-flash")
MISTRAL_MODEL    = os.environ.get("MISTRAL_MODEL",    "mistral-small-latest")
GROQ_MODEL       = os.environ.get("GROQ_MODEL",       "llama-3.3-70b-versatile")
DEEPSEEK_MODEL   = os.environ.get("DEEPSEEK_MODEL",   "deepseek-chat")
XAI_MODEL        = os.environ.get("XAI_MODEL",        "grok-2-1212")
COHERE_MODEL     = os.environ.get("COHERE_MODEL",     "command-r-plus-08-2024")
TOGETHER_MODEL   = os.environ.get("TOGETHER_MODEL",   "meta-llama/Llama-3.3-70B-Instruct-Turbo")
PERPLEXITY_MODEL = os.environ.get("PERPLEXITY_MODEL", "llama-3.1-sonar-large-128k-online")
FIREWORKS_MODEL  = os.environ.get("FIREWORKS_MODEL",  "accounts/fireworks/models/llama-v3p3-70b-instruct")
AZURE_OPENAI_MODEL      = os.environ.get("AZURE_OPENAI_MODEL",      "gpt-4o-mini")
AZURE_OPENAI_ENDPOINT   = os.environ.get("AZURE_OPENAI_ENDPOINT",   "")
AZURE_OPENAI_API_VERSION= os.environ.get("AZURE_OPENAI_API_VERSION","2024-08-01-preview")

OLLAMA_CLOUD_URL   = "https://ollama.com/api/chat"
OLLAMA_CLOUD_MODEL = os.environ.get("OLLAMA_CLOUD_MODEL", "nemotron-3-super:cloud")
OLLAMA_LOCAL_URL   = "http://localhost:11434/api/chat"
OLLAMA_LOCAL_MODEL = os.environ.get("OLLAMA_LOCAL_MODEL", "llama3.2")


# ── helpers ───────────────────────────────────────────────────────────────────

def _openai_compat(messages: list, max_tokens: int, base_url: str, api_key: str,
                   model: str, referer: str = "tube-assistant") -> str:
    """Generic OpenAI-compatible endpoint (OpenAI, DeepSeek, xAI, Together, Perplexity, Fireworks)."""
    try:
        from openai import OpenAI
    except ImportError:
        raise RuntimeError("openai package not installed. Run: pip install openai")
    client = OpenAI(api_key=api_key, base_url=base_url)
    resp = client.chat.completions.create(
        model=model, messages=messages, max_tokens=max_tokens,
        temperature=_temp(), seed=_seed(),
    )
    return resp.choices[0].message.content.strip()


# ── providers ─────────────────────────────────────────────────────────────────

def _openrouter(messages: list, max_tokens: int = 4096) -> str:
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY missing in .env")
    for attempt in range(5):
        resp = requests.post(
            OPENROUTER_URL,
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json",
                     "HTTP-Referer": "tube-assistant"},
            json={"model": OPENROUTER_MODEL, "messages": messages, "max_tokens": max_tokens,
                  "temperature": _temp(), "seed": _seed()},
            timeout=180,
        )
        if resp.status_code == 429:
            wait = 10 * (attempt + 1)
            print(f"[OpenRouter] 429 — waiting {wait}s...", flush=True)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"].get("content")
        if content:
            return content.strip()
        time.sleep(10 * (attempt + 1))
    raise RuntimeError("OpenRouter: empty content after retries")


def _openai(messages: list, max_tokens: int = 4096) -> str:
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY missing in .env")
    return _openai_compat(messages, max_tokens, "https://api.openai.com/v1", api_key, OPENAI_MODEL)


def _anthropic(messages: list, max_tokens: int = 4096) -> str:
    try:
        import anthropic as sdk
    except ImportError:
        raise RuntimeError("anthropic package not installed. Run: pip install anthropic")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY missing in .env")
    system = ""
    chat_msgs = []
    for m in messages:
        if m["role"] == "system":
            system = m["content"]
        else:
            chat_msgs.append(m)
    client = sdk.Anthropic(api_key=api_key)
    kwargs = {"model": ANTHROPIC_MODEL, "max_tokens": max_tokens, "messages": chat_msgs,
              "temperature": _temp()}
    if system:
        kwargs["system"] = system
    resp = client.messages.create(**kwargs)
    return resp.content[0].text.strip()


def _gemini(messages: list, max_tokens: int = 4096) -> str:
    try:
        import google.generativeai as genai
    except ImportError:
        raise RuntimeError("google-generativeai not installed. Run: pip install google-generativeai")
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY missing in .env")
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(GEMINI_MODEL)
    history, prompt = [], ""
    for m in messages:
        if m["role"] == "system":
            history += [{"role": "user", "parts": [m["content"]]},
                        {"role": "model", "parts": ["Understood."]}]
        elif m["role"] == "user":
            prompt = m["content"]
        elif m["role"] == "assistant":
            history.append({"role": "model", "parts": [m["content"]]})
    chat = model.start_chat(history=history)
    resp = chat.send_message(prompt, generation_config={"max_output_tokens": max_tokens,
                                                         "temperature": _temp()})
    return resp.text.strip()


def _mistral(messages: list, max_tokens: int = 4096) -> str:
    try:
        from mistralai import Mistral
    except ImportError:
        raise RuntimeError("mistralai not installed. Run: pip install mistralai")
    api_key = os.environ.get("MISTRAL_API_KEY", "")
    if not api_key:
        raise RuntimeError("MISTRAL_API_KEY missing in .env")
    client = Mistral(api_key=api_key)
    resp = client.chat.complete(model=MISTRAL_MODEL, messages=messages, max_tokens=max_tokens,
                                temperature=_temp(), random_seed=_seed())
    return resp.choices[0].message.content.strip()


def _groq(messages: list, max_tokens: int = 4096) -> str:
    try:
        from groq import Groq
    except ImportError:
        raise RuntimeError("groq not installed. Run: pip install groq")
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY missing in .env")
    client = Groq(api_key=api_key)
    resp = client.chat.completions.create(model=GROQ_MODEL, messages=messages, max_tokens=max_tokens,
                                          temperature=_temp(), seed=_seed())
    return resp.choices[0].message.content.strip()


def _deepseek(messages: list, max_tokens: int = 4096) -> str:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY missing in .env")
    return _openai_compat(messages, max_tokens, "https://api.deepseek.com/v1", api_key, DEEPSEEK_MODEL)


def _xai(messages: list, max_tokens: int = 4096) -> str:
    api_key = os.environ.get("XAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("XAI_API_KEY missing in .env")
    return _openai_compat(messages, max_tokens, "https://api.x.ai/v1", api_key, XAI_MODEL)


def _cohere(messages: list, max_tokens: int = 4096) -> str:
    try:
        import cohere
    except ImportError:
        raise RuntimeError("cohere not installed. Run: pip install cohere")
    api_key = os.environ.get("COHERE_API_KEY", "")
    if not api_key:
        raise RuntimeError("COHERE_API_KEY missing in .env")
    client = cohere.ClientV2(api_key=api_key)
    resp = client.chat(model=COHERE_MODEL, messages=messages, max_tokens=max_tokens,
                       temperature=_temp(), seed=_seed())
    return resp.message.content[0].text.strip()


def _together(messages: list, max_tokens: int = 4096) -> str:
    api_key = os.environ.get("TOGETHER_API_KEY", "")
    if not api_key:
        raise RuntimeError("TOGETHER_API_KEY missing in .env")
    return _openai_compat(messages, max_tokens, "https://api.together.xyz/v1", api_key, TOGETHER_MODEL)


def _perplexity(messages: list, max_tokens: int = 4096) -> str:
    api_key = os.environ.get("PERPLEXITY_API_KEY", "")
    if not api_key:
        raise RuntimeError("PERPLEXITY_API_KEY missing in .env")
    return _openai_compat(messages, max_tokens, "https://api.perplexity.ai", api_key, PERPLEXITY_MODEL)


def _fireworks(messages: list, max_tokens: int = 4096) -> str:
    api_key = os.environ.get("FIREWORKS_API_KEY", "")
    if not api_key:
        raise RuntimeError("FIREWORKS_API_KEY missing in .env")
    return _openai_compat(messages, max_tokens, "https://api.fireworks.ai/inference/v1", api_key, FIREWORKS_MODEL)


def _azure_openai(messages: list, max_tokens: int = 4096) -> str:
    try:
        from openai import AzureOpenAI
    except ImportError:
        raise RuntimeError("openai not installed. Run: pip install openai")
    api_key  = os.environ.get("AZURE_OPENAI_API_KEY", "")
    endpoint = AZURE_OPENAI_ENDPOINT
    if not api_key or not endpoint:
        raise RuntimeError("AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT required in .env")
    client = AzureOpenAI(api_key=api_key, azure_endpoint=endpoint,
                         api_version=AZURE_OPENAI_API_VERSION)
    resp = client.chat.completions.create(
        model=AZURE_OPENAI_MODEL, messages=messages, max_tokens=max_tokens,
        temperature=_temp(), seed=_seed(),
    )
    return resp.choices[0].message.content.strip()


class RispostaVuota(RuntimeError):
    """Il modello ha risposto senza contenuto utile (solo ragionamento)."""


# Quanti tentativi per chiamata e di quanto allargare il budget token a ogni
# ritentativo: il ragionamento va pagato in piu', non ignorato.
RITENTATIVI_OLLAMA = 2
FATTORE_BUDGET = 4


def _testo_ollama(payload: dict, modello: str) -> str:
    """Estrae il testo utile da una risposta Ollama.

    I modelli reasoning (nemotron, gpt-oss, qwen) riempiono `thinking` e
    lasciano `content` vuoto quando il ragionamento consuma tutto il budget.
    Il vecchio `content or thinking` spediva il monologo del modello ("We need
    to output a short phrase 3-7 words...") dritto nel titolo del video: meglio
    sollevare e lasciare che il chiamante ritenti con piu' token.
    """
    payload = payload or {}
    msg = payload.get("message", {}) or {}
    contenuto = (msg.get("content") or "").strip()
    if contenuto:
        return contenuto
    if (msg.get("thinking") or "").strip() or payload.get("done_reason") == "length":
        raise RispostaVuota(f"{modello}: budget token finito nel ragionamento")
    raise RispostaVuota(f"{modello}: risposta senza contenuto")


def _chat_ollama(url: str, modello: str, messages: list, max_tokens: int,
                 headers: dict = None, think: bool = None) -> str:
    """POST su un endpoint Ollama, con ritentativo a budget allargato."""
    budget = max(1, max_tokens)
    ultimo = None
    for _ in range(RITENTATIVI_OLLAMA):
        corpo = {"model": modello, "messages": messages, "stream": False,
                 "options": {"num_predict": budget,
                             "temperature": _temp(), "seed": _seed()}}
        if think is not None:
            corpo["think"] = think
        resp = requests.post(url, headers=headers, json=corpo, timeout=300)
        if resp.status_code == 404:
            # 404 da Ollama significa quasi sempre "modello non scaricato":
            # il nudo `404 Client Error` non diceva niente all'utente.
            raise RuntimeError(
                f"Ollama: modello '{modello}' non disponibile ({url}). "
                f"Scaricalo con: ollama pull {modello}")
        resp.raise_for_status()
        try:
            return _testo_ollama(resp.json(), modello)
        except RispostaVuota as e:
            print(f"[AI] {e} — ritento con num_predict {budget * FATTORE_BUDGET}", flush=True)
            ultimo = e
            budget *= FATTORE_BUDGET
    raise ultimo


def _ollama_cloud(messages: list, max_tokens: int = 8192) -> str:
    api_key = os.environ.get("OLLAMA_API_KEY", "")
    modello = os.environ.get("OLLAMA_CLOUD_MODEL", OLLAMA_CLOUD_MODEL)
    return _chat_ollama(
        OLLAMA_CLOUD_URL, modello, messages, max_tokens,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        think=False,
    )


def _ollama_local(messages: list, max_tokens: int = 8192) -> str:
    modello = os.environ.get("OLLAMA_LOCAL_MODEL", OLLAMA_LOCAL_MODEL)
    return _chat_ollama(OLLAMA_LOCAL_URL, modello, messages, max_tokens)


# ── dispatch table ────────────────────────────────────────────────────────────

_PROVIDERS = {
    "openrouter":   _openrouter,
    "openai":       _openai,
    "anthropic":    _anthropic,
    "gemini":       _gemini,
    "mistral":      _mistral,
    "groq":         _groq,
    "deepseek":     _deepseek,
    "xai":          _xai,
    "cohere":       _cohere,
    "together":     _together,
    "perplexity":   _perplexity,
    "fireworks":    _fireworks,
    "azure_openai": _azure_openai,
    "ollama_cloud": _ollama_cloud,
    "ollama_local": _ollama_local,
}


def _primary(messages: list, max_tokens: int = 8192) -> str:
    svc = os.environ.get("AI_SERVICE", AI_SERVICE)
    fn = _PROVIDERS.get(svc)
    if fn is None:
        raise RuntimeError(f"Unknown AI_SERVICE '{svc}'. Valid: {', '.join(_PROVIDERS)}")
    cap = 4096 if svc not in ("ollama_cloud", "ollama_local") else max_tokens
    return fn(messages, min(max_tokens, cap))


def _fallback(messages: list, max_tokens: int = 4096) -> str:
    """Prova i provider di riserva in base alle chiavi realmente disponibili."""
    svc = os.environ.get("AI_SERVICE", AI_SERVICE)
    candidates = []
    if svc != "openrouter" and os.environ.get("OPENROUTER_API_KEY", "").strip():
        candidates.append(("openrouter", _openrouter))
    if svc != "ollama_local":
        # nessuna chiave richiesta: vale sempre la pena tentare l'istanza locale
        candidates.append(("ollama_local", _ollama_local))
    last_err: Exception | None = None
    for name, fn in candidates:
        try:
            return fn(messages, min(max_tokens, 4096))
        except Exception as e:
            print(f"[AI] Fallback {name} fallito: {e}", flush=True)
            last_err = e
    raise RuntimeError(f"Tutti i provider AI di fallback hanno fallito: {last_err}")


def _non_vuota(testo: str) -> str:
    """Una risposta di soli spazi non e' una risposta: meglio il fallback."""
    if not (testo or "").strip():
        raise RispostaVuota("risposta vuota dal provider primario")
    return testo


# ── public API ────────────────────────────────────────────────────────────────

def chat(prompt: str, system: str = None, max_tokens: int = 8192) -> str:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    try:
        return _non_vuota(_primary(messages, max_tokens))
    except Exception as e:
        # provider primario giù non deve far perdere il video del giorno
        print(f"[AI] Primary failed ({e}), trying fallback...", flush=True)
        return _fallback(messages, max_tokens)


def chat_ollama(prompt: str, system: str = None, max_tokens: int = 8192) -> str:
    return chat(prompt, system, max_tokens)


def chat_openrouter(prompt: str, system: str = None, max_tokens: int = 4096) -> str:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return _openrouter(messages, max_tokens)


def chat_with_history(system: str, history: list, user_text: str, max_tokens: int = 2048) -> str:
    messages = [{"role": "system", "content": system}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})
    try:
        return _non_vuota(_primary(messages, max_tokens))
    except Exception as e:
        print(f"[AI] Primary failed ({e}), trying fallback...", flush=True)
        return _fallback(messages, max_tokens)
