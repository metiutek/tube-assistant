"""
Telegram bot — listens for user commands and topic suggestions.
Runs in a background thread alongside the agent daemon.
"""
import os
import json
import asyncio
import threading
import requests
import html
import uuid
from datetime import datetime, timezone, timedelta

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

MAX_HISTORY = 20  # messaggi per chat
PENDING_ACTION_TTL_SECONDS = 3600


def _h(value) -> str:
    return html.escape(str(value), quote=True)


def _authorized_chat_ids() -> set[str]:
    raw = ",".join(
        value for value in (
            os.environ.get("TELEGRAM_CHAT_ID", ""),
            os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", ""),
        )
        if value
    )
    return {item.strip() for item in raw.split(",") if item.strip()}


def _is_authorized(update: Update) -> bool:
    allowed = _authorized_chat_ids()
    if not allowed:
        return False
    chat = update.effective_chat
    return bool(chat and str(chat.id) in allowed)


async def _deny_unauthorized(update: Update) -> None:
    chat = update.effective_chat
    chat_id = getattr(chat, "id", None)
    print(f"[Telegram] Unauthorized chat blocked: {chat_id}", flush=True)
    if update.message:
        await update.message.reply_text(
            "Accesso non autorizzato.\n\n"
            f"Il tuo chat ID è: {chat_id}\n"
            "Per autorizzarti, imposta nel file .env:\n"
            f"TELEGRAM_CHAT_ID={chat_id}\n"
            "poi riavvia l'agente."
        )


def _authorized(handler):
    async def _wrapped(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _is_authorized(update):
            await _deny_unauthorized(update)
            return
        return await handler(update, ctx)
    return _wrapped


def _pending_actions(state: dict) -> list[dict]:
    now = datetime.now(timezone.utc).timestamp()
    pending = []
    for action in state.get("pending_actions", []):
        try:
            created = float(action.get("created_at", 0))
        except (TypeError, ValueError):
            created = 0
        if now - created <= PENDING_ACTION_TTL_SECONDS:
            pending.append(action)
    state["pending_actions"] = pending
    return pending


def _queue_pending_action(state: dict, action_type: str, payload: str, label: str) -> str:
    pending = _pending_actions(state)
    action_id = uuid.uuid4().hex[:8]
    pending.append({
        "id": action_id,
        "type": action_type,
        "payload": payload,
        "label": label,
        "created_at": datetime.now(timezone.utc).timestamp(),
    })
    state["pending_actions"] = pending
    return (
        f"Richiede conferma: <b>{_h(label)}</b>\n"
        f"ID: <code>{action_id}</code>\n"
        f"Usa <code>/conferma {action_id}</code> entro 1 ora, oppure <code>/annulla {action_id}</code>."
    )

_AGENT_LANGUAGE = os.getenv("AGENT_LANGUAGE", "English")

SYSTEM_PROMPT = f"""You are the virtual employee of a YouTube channel. Your boss talks to you via Telegram.
Always reply in {_AGENT_LANGUAGE}. This is mandatory — never switch language unless the user explicitly asks you to.
You EXECUTE what they ask — you don't explain why you can't do it, you just do it.

WHAT YOU CAN DO (do it directly without asking for confirmation):
- Develop topics: write title, hook, structure, full script
- Plan videos: propose and add topics to the queue using the tag [AGGIUNGI_TOPIC: text]
- Rewrite/improve scripts, titles, descriptions, tags
- Give YouTube strategy advice based on data
- Answer any question about the channel, AI, content creation

AUTOMATIC PIPELINE (every day at the configured time):
1. Reads YouTube analytics → adapts strategy
2. Generates topic+script via AI → audio with Edge TTS
3. Downloads Pexels clips → edits video with MoviePy → generates thumbnail
4. Uploads to YouTube scheduled at the set time

CHANNEL MANAGEMENT VIA COMMANDS:
/forza /forzaora /skip /setpubblica /status /recap /prossimi
/coda /topic /deltopic
/canale /setdesc /video /playlist /nuovaplaylist /commenti

STANDALONE GENERATION ACTIONS (execute immediately, no pipeline, no queue):
[GENERA_COPERTINA: title] → generate thumbnail image and send it. Use for: "genera/crea/mostrami una copertina", "thumbnail di prova". ONLY this tag — no [FORZA_ORA], no [AGGIUNGI_TOPIC].
[GENERA_AUDIO: text] → generate TTS audio from text and send it as audio file. Use for: "genera audio", "sentimi la voce", "prova TTS", "come suona questo testo". ONLY this tag.
[GENERA_SCRIPT: topic] → generate full video script (title + script + description + tags) and send as text. Use for: "scrivi uno script", "genera uno script su X", "fammi vedere lo script". Does NOT start pipeline. ONLY this tag.
[INVIA_VIDEO] → send the last produced video file. Use for: "mandami il video", "inviami il video", "vedi il video". No payload.

QUEUE & PIPELINE ACTIONS (require explicit intent to produce/publish):
[AGGIUNGI_TOPIC: title] → add topic to end of queue
[PRIORITA_TOPIC: title] → add topic to head of queue (next video)
[FORZA_ORA: title] → add to head of queue AND start pipeline immediately (requires confirmation)

CHANNEL API ACTIONS (execute directly, no confirmation needed):
[AGGIORNA_DESC: description text] → update channel description via API
[AGGIORNA_KEYWORDS: keyword1, keyword2] → update channel keywords via API
[RINOMINA_CANALE: new name] → change channel name via API (requires confirmation — irreversible)
[ELIMINA_TOPIC: N] → remove topic number N from the queue (1 = first)

MEMORY & PREFERENCES:
[RICORDA: fact] → save to long-term memory
[DIMENTICA: text] → remove from memory
[SETPREF: key=value] → update video preference. Available keys: ritmo (lento/medio/veloce), tono_voce (confident/casual/dramatic/educational), lingua (english/italian), stile_clip (cinematic/fast cuts/minimal/documentary), stile_thumbnail (free text), argomenti_preferiti (comma-separated list), argomenti_evitare (comma-separated list), durata_target_minuti (number), musica_volume (0.0-1.0), note_libere (free text), thumbnail_testo_mostra (true/false), thumbnail_testo_colore (color name or R,G,B), thumbnail_testo_posizione (alto/basso), thumbnail_testo_scala (0.3-1.0), thumbnail_font_size (A/B/C/D — A largest, empty = auto), tts_voce (Edge TTS voice name). ALWAYS use this tag when the user expresses a preference.
Examples: "voce italiana" → [SETPREF: tts_voce=it-IT-DiegoNeural] | "voce femminile" → [SETPREF: tts_voce=en-US-AriaNeural] | "voce british" → [SETPREF: tts_voce=en-GB-SoniaNeural] | "video da 5 minuti" → [SETPREF: durata_target_minuti=5]

PUBLISHED VIDEO EDITING (requires confirmation — affects live content):
[VIDEO_TITOLO: video_id | new title] → change title of published video
[VIDEO_DESC: video_id | new description] → change description of published video
[VIDEO_TAGS: video_id | tag1, tag2, tag3] → change tags of published video
[VIDEO_THUMB: video_id] → re-upload thumbnail on published video (uses output/thumbnail.jpg)

PIPELINE CONTROL:
[RIAVVIA_MONTAGGIO] → clear checkpoint and restart pipeline (requires confirmation)
[BLOCCA_PIPELINE] → stop pipeline in progress (requires confirmation)

CRITICAL RULE ON SEARCHES: when the user asks for news or recent information, use the [WEB SEARCH] context already provided. Do NOT add topics to the queue, do NOT use [AGGIUNGI_TOPIC] or [FORZA_ORA].

CRITICAL RULE ON PREFERENCES/SETTINGS: use ONLY [SETPREF: key=value]. NEVER add [FORZA_ORA], [AGGIUNGI_TOPIC] or [PRIORITA_TOPIC] — changing a preference does NOT mean they want a new video now.
Examples: "metti il testo giallo" → [SETPREF: thumbnail_testo_colore=giallo] | "testo in alto" → [SETPREF: thumbnail_testo_posizione=alto] | "no testo sulla copertina" → [SETPREF: thumbnail_testo_mostra=false] | "testo più grande" → [SETPREF: thumbnail_testo_scala=1.0]

CRITICAL RULE ON TAGS: only use the tags listed above. Do NOT invent tags — they won't be executed.

DECISION GUIDE — which tag to use:
- "genera copertina / thumbnail" → [GENERA_COPERTINA: title]
- "genera audio / sentimi la voce" → [GENERA_AUDIO: text]
- "scrivi/genera uno script" → [GENERA_SCRIPT: topic]
- "mandami il video / invia il video" → [INVIA_VIDEO]
- "crea/pubblica questo video / fallo ora" → [FORZA_ORA: title]
- "prossimo video su X / priorità" → [PRIORITA_TOPIC: title]
- "aggiungi in coda" → [AGGIUNGI_TOPIC: title]
- "aggiorna descrizione canale" → [AGGIORNA_DESC: text]
- "aggiorna keyword canale" → [AGGIORNA_KEYWORDS: kw1, kw2]
- "rimuovi topic N" → [ELIMINA_TOPIC: N]
- "ricorda questa info" → [RICORDA: fact]

ABSOLUTE RULES:
- Always reply in {_AGENT_LANGUAGE} — non-negotiable
- When asked to DO something on the channel → use the API tag, don't explain
- When asked to develop a topic → do it immediately in full
- Never say "I can't" — use the available tags or generate the content
- Profile picture: impossible via API (Google limitation)
- Channel name: impossible via API for personal accounts — tell user to go to YouTube Studio → Customization → Basic info, or convert to Brand Account
"""


def _get_history(state: dict, chat_id: str) -> list:
    return state.get("chat_history", {}).get(str(chat_id), [])


def _save_history(state: dict, chat_id: str, history: list) -> None:
    if "chat_history" not in state:
        state["chat_history"] = {}
    state["chat_history"][str(chat_id)] = history[-MAX_HISTORY:]


_SEARCH_KEYWORDS = (
    "notizie", "news", "ultime", "recenti", "oggi", "questa settimana", "questo mese",
    "cosa sta succedendo", "aggiornamenti", "novità", "annunci", "annuncio",
    "latest", "recent", "update", "updates", "what's happening", "announced",
    "uscito", "rilasciato", "lanciato", "released", "launched",
)

_ACTION_KEYWORDS = (
    "crea", "fai", "produci", "pubblica", "carica", "genera", "avvia", "lancia",
    "make", "create", "publish", "upload", "generate", "start",
    "forza", "metti in coda", "aggiungi",
)

def _needs_web_search(text: str) -> bool:
    t = text.lower()
    has_search = any(k in t for k in _SEARCH_KEYWORDS)
    has_action = any(k in t for k in _ACTION_KEYWORDS)
    # se c'è un'azione (crea video, pubblica...) non è solo ricerca
    return has_search and not has_action

def _extract_search_query(text: str) -> str:
    """Estrae la query di ricerca dal messaggio utente."""
    import re
    # cerca "su X", "riguardo X", "di X" dopo keyword di ricerca
    patterns = [
        r"notizie su (.+)",
        r"news su (.+)",
        r"aggiornamenti su (.+)",
        r"novit[àa] su (.+)",
        r"ultimissime (?:notizie )?su (.+)",
        r"cosa sta succedendo (?:con |su )?(.+)",
        r"cosa succede (?:con |su )?(.+)",
        r"cercami (.+)",
        r"cerca (.+)",
        r"dimmi (?:di |su )?(.+)",
        r"riguardo (.+)",
    ]
    t = text.lower().strip()
    for pat in patterns:
        m = re.search(pat, t)
        if m:
            return m.group(1).strip()
    # fallback: rimuovi solo le parole trigger iniziali
    for prefix in ("ultime notizie", "ultimissime notizie", "notizie", "news", "aggiornamenti"):
        if t.startswith(prefix):
            return t[len(prefix):].strip() or text
    return text


def _is_preference_list_request(text: str) -> bool:
    t = text.lower()
    return "preferenz" in t and any(k in t for k in ("mostra", "lista", "vedere", "vedi", "quali", "attuali", "dimmi"))


_PREF_KEYWORDS = (
    "stile", "style", "thumbnail", "copertina", "ritmo", "tono", "lingua", "language",
    "clip", "montaggio", "volume", "musica", "durata", "argomenti", "topic",
    "evita", "preferisci", "preferenza", "impostazione", "setting",
    "cambia", "modifica", "imposta", "setta", "metti", "usa",
    "change", "set", "update", "modify", "use",
)

def _is_pref_only_request(text: str) -> bool:
    """Richiesta di cambio impostazione senza intenzione di creare un video."""
    t = text.lower()
    has_pref = any(k in t for k in _PREF_KEYWORDS)
    has_action = any(k in t for k in _ACTION_KEYWORDS)
    return has_pref and not has_action


def _format_preference_updates(modifiche: dict) -> str:
    lines = ["✅ Preferenze aggiornate:"]
    for chiave, valore in modifiche.items():
        val = ", ".join(valore) if isinstance(valore, list) else str(valore)
        lines.append(f"• <b>{html.escape(chiave)}</b>: <code>{html.escape(val)}</code>")
    return "\n".join(lines)


def _ask_ai(user_text: str, history: list, state: dict) -> tuple[str, list]:
    from moduli.ai_client import chat_with_history
    from moduli.memoria import come_contesto
    from moduli.web_search import cerca_notizie

    system_parts = [SYSTEM_PROMPT]
    memoria = come_contesto()
    if memoria:
        system_parts.append(memoria)

    from moduli.preferenze import come_contesto as pref_contesto
    system_parts.append(pref_contesto())

    queue = state.get("topic_queue", [])
    recent = state.get("recent_topics", [])
    ore_publish = publish_hours(state)
    ore_trigger = trigger_hours(state)
    vpd = state.get("videos_per_day", 1)
    video_ids = state.get("video_ids", [])
    video_links = ", ".join(f"https://youtu.be/{v}" for v in video_ids[:5]) if video_ids else "nessuno"
    is_search = _needs_web_search(user_text)
    if is_search:
        query = _extract_search_query(user_text)
        notizie = cerca_notizie(query, max_results=6)
        if notizie:
            system_parts.append(f"[RICERCA WEB IN TEMPO REALE — query: '{query}']\n{notizie}")

    _ora_locale = datetime.now().astimezone()
    _giorni = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
    _data_str = f"{_giorni[_ora_locale.weekday()]} {_ora_locale.strftime('%d/%m/%Y')}"
    _tz_str = _ora_locale.strftime("%Z") or "ora locale"
    system_parts.append(
        f"[CONTESTO ATTUALE — {_data_str}, ore {_ora_locale.strftime('%H:%M')} ({_tz_str}; "
        f"{datetime.now(timezone.utc).strftime('%H:%M')} UTC)]\n"
        f"IMPORTANTE: per data e ora usa SEMPRE ed ESATTAMENTE i valori qui sopra. Non calcolarli, non inventarli.\n"
        f"Video al giorno: {vpd} | Pipeline automatica: {ore_trigger} UTC | Pubblica: {ore_publish} UTC\n"
        f"Topic in coda ({len(queue)}):\n{chr(10).join(f'  {i+1}. {t}' for i, t in enumerate(queue)) if queue else '  nessuno'}\n"
        f"IMPORTANTE: quando l'utente dice 'fai topic N' o 'crea il topic N', usa ESATTAMENTE il testo del topic numero N dalla lista sopra nel tag [FORZA_ORA: testo esatto].\n"
        f"Ultimi topic prodotti: {', '.join(recent[:5]) if recent else 'nessuno'}\n"
        f"Video pubblicati: {video_links}\n"
        f"IMPORTANTE: per rispondere su video pubblicati, scrivi direttamente i link sopra — NON usare tag [VIDEO] o simili."
    )

    system = "\n\n".join(system_parts)

    effective_text = user_text
    if is_search:
        effective_text = (
            f"[SOLO INFORMAZIONE — NON aggiungere topic, NON usare FORZA_ORA, NON usare AGGIUNGI_TOPIC, NON usare PRIORITA_TOPIC]\n"
            f"{user_text}"
        )

    try:
        reply = chat_with_history(system, history, effective_text)
    except Exception as e:
        return f"⚠️ Errore AI: {e}", history

    new_history = history + [
        {"role": "user", "content": user_text},
        {"role": "assistant", "content": reply},
    ]
    return reply, new_history


def _execute_confirmed_action(state: dict, action: dict) -> str:
    """Run an action that was previously queued for explicit user confirmation."""
    action_type = action.get("type")
    payload = (action.get("payload") or "").strip()

    if action_type == "FORZA_ORA":
        queue = state.get("topic_queue", [])
        if payload and (not queue or queue[0] != payload):
            queue.insert(0, payload)
        state["topic_queue"] = queue
        state["force_run"] = True
        state["publish_immediately"] = True
        checkpoint = "output/pipeline_checkpoint.json"
        if os.path.exists(checkpoint):
            os.remove(checkpoint)
        return f"Confermato: pipeline immediata per <i>{_h(payload)}</i>."

    if action_type == "RINOMINA_CANALE":
        from moduli.canale import aggiorna_canale
        res = aggiorna_canale(title=payload)
        if "title_error" in res:
            return _h(res["title_error"])
        return f"Nome canale cambiato in <b>{_h(payload)}</b>."

    if action_type == "AGGIORNA_DESC":
        from moduli.canale import aggiorna_canale
        aggiorna_canale(description=payload)
        return "Descrizione canale aggiornata."

    if action_type == "AGGIORNA_KEYWORDS":
        from moduli.canale import aggiorna_canale
        aggiorna_canale(keywords=payload)
        return "Keyword canale aggiornate."

    if action_type in {"VIDEO_TITOLO", "VIDEO_DESC", "VIDEO_TAGS"}:
        from moduli.pubblica import aggiorna_video
        parts = payload.split("|", 1)
        if len(parts) != 2:
            raise ValueError("Formato non valido: serve video_id | valore")
        vid, value = parts[0].strip(), parts[1].strip()
        if action_type == "VIDEO_TITOLO":
            aggiorna_video(vid, title=value)
            return f'Titolo aggiornato su <a href="https://youtu.be/{_h(vid)}">youtu.be/{_h(vid)}</a>.'
        if action_type == "VIDEO_DESC":
            aggiorna_video(vid, description=value)
            return f'Descrizione aggiornata su <a href="https://youtu.be/{_h(vid)}">youtu.be/{_h(vid)}</a>.'
        tags = [t.strip() for t in value.split(",") if t.strip()]
        aggiorna_video(vid, tags=tags)
        return f'Tag aggiornati su <a href="https://youtu.be/{_h(vid)}">youtu.be/{_h(vid)}</a>.'

    if action_type == "VIDEO_THUMB":
        from moduli.pubblica import aggiorna_video
        thumb = "output/thumbnail.jpg"
        if not os.path.exists(thumb):
            raise FileNotFoundError("Thumbnail non trovata in output/thumbnail.jpg")
        res = aggiorna_video(payload, thumbnail_path=thumb)
        if res.get("thumbnail_error"):
            raise RuntimeError(res["thumbnail_error"])
        return f'Thumbnail caricata su <a href="https://youtu.be/{_h(payload)}">youtu.be/{_h(payload)}</a>.'

    if action_type == "ELIMINA_TOPIC":
        idx = int(payload) - 1
        queue = state.get("topic_queue", [])
        if idx < 0 or idx >= len(queue):
            raise IndexError(f"Topic {payload} non esiste (coda ha {len(queue)} elementi)")
        removed = queue.pop(idx)
        state["topic_queue"] = queue
        return f"Topic rimosso: <i>{_h(removed)}</i>. Rimanenti: {len(queue)}."

    if action_type == "RIAVVIA_MONTAGGIO":
        checkpoint = "output/pipeline_checkpoint.json"
        if os.path.exists(checkpoint):
            os.remove(checkpoint)
        state["force_run"] = True
        return "Checkpoint cancellato; pipeline riavviata."

    if action_type == "BLOCCA_PIPELINE":
        state["abort_pipeline"] = True
        state.pop("force_run", None)
        return "Abort confermato. La pipeline si fermera al prossimo checkpoint."

    raise ValueError(f"Azione sconosciuta: {action_type}")


def _execute_actions(reply: str, state: dict, is_search: bool = False,
                     is_pref_only: bool = False) -> tuple[str, list, list, list, list, list]:
    """Esegue i tag azione presenti nella risposta AI.

    Ritorna (testo_pulito, added_topics, saved_memories, removed_memories,
    api_results, deferred_tasks)."""
    import re
    from moduli.memoria import aggiungi as mem_aggiungi, _save as mem_save, _load as mem_load

    added_topics = []
    saved_memories = []
    removed_memories = []
    api_results = []
    deferred_tasks = []  # list of (task_type, payload) for async media generation

    queue = state.get("topic_queue", [])

    def _add_topic(m):
        topic = m.group(1).strip()
        queue.append(topic)
        added_topics.append(f"{topic} (in coda)")
        return ""

    def _priorita_topic(m):
        topic = m.group(1).strip()
        if not queue or queue[0] != topic:
            queue.insert(0, topic)
            added_topics.append(f"{topic} (in testa — prossimo video)")
        return ""

    def _forza_ora(m):
        topic = m.group(1).strip()
        api_results.append(_queue_pending_action(state, "FORZA_ORA", topic, f"avvia e pubblica subito: {topic}"))
        return ""

    def _ricorda(m):
        testo = m.group(1).strip()
        mem_aggiungi(testo)
        saved_memories.append(testo)
        return ""

    def _setpref(m):
        from moduli.preferenze import carica, salva, DEFAULT_PREF
        raw = m.group(1).strip()
        if "=" not in raw:
            return ""
        chiave, _, valore = raw.partition("=")
        chiave = chiave.strip()
        valore = valore.strip().strip('"').strip("'")
        if chiave not in DEFAULT_PREF:
            return ""
        pref = carica()
        default_val = DEFAULT_PREF[chiave]
        if isinstance(default_val, list):
            pref[chiave] = [v.strip() for v in valore.split(",") if v.strip()]
        elif isinstance(default_val, (int, float)):
            try:
                pref[chiave] = type(default_val)(valore)
            except ValueError:
                return ""
        else:
            pref[chiave] = valore
        salva(pref)
        api_results.append(f"⚙️ Preferenza aggiornata: <b>{_h(chiave)}</b> = <code>{_h(pref[chiave])}</code>")
        return ""

    def _dimentica(m):
        testo = m.group(1).strip().lower()
        memories = mem_load()
        nuove = [x for x in memories if testo not in x["testo"].lower()]
        rimossi = [x["testo"] for x in memories if testo in x["testo"].lower()]
        mem_save(nuove)
        removed_memories.extend(rimossi)
        return ""

    def _rinomina_canale(m):
        nome = m.group(1).strip()
        api_results.append(_queue_pending_action(state, "RINOMINA_CANALE", nome, f"rinomina canale: {nome}"))
        return ""

    def _aggiorna_desc(m):
        from moduli.canale import aggiorna_canale
        desc = m.group(1).strip()
        try:
            aggiorna_canale(description=desc)
            api_results.append("✅ Descrizione canale aggiornata.")
        except Exception as e:
            api_results.append(f"❌ Errore aggiornamento descrizione: {_h(str(e))}")
        return ""

    def _aggiorna_keywords(m):
        from moduli.canale import aggiorna_canale
        kw = m.group(1).strip()
        try:
            aggiorna_canale(keywords=kw)
            api_results.append("✅ Keyword canale aggiornate.")
        except Exception as e:
            api_results.append(f"❌ Errore keyword: {_h(str(e))}")
        return ""

    def _video_titolo(m):
        payload = m.group(1).strip()
        api_results.append(_queue_pending_action(state, "VIDEO_TITOLO", payload, "aggiorna titolo video"))
        return ""

    def _video_desc(m):
        payload = m.group(1).strip()
        api_results.append(_queue_pending_action(state, "VIDEO_DESC", payload, "aggiorna descrizione video"))
        return ""

    def _video_tags(m):
        payload = m.group(1).strip()
        api_results.append(_queue_pending_action(state, "VIDEO_TAGS", payload, "aggiorna tag video"))
        return ""

    def _elimina_topic(m):
        raw = m.group(1).strip()
        try:
            idx = int(raw) - 1
        except ValueError:
            api_results.append(f"❌ [ELIMINA_TOPIC] numero non valido: {_h(raw)}")
            return ""
        q = state.get("topic_queue", [])
        if idx < 0 or idx >= len(q):
            api_results.append(f"❌ Topic {_h(raw)} non esiste (coda ha {len(q)} elementi)")
            return ""
        rimosso = q.pop(idx)
        state["topic_queue"] = q
        api_results.append(f"🗑 Topic rimosso: <i>{_h(rimosso)}</i> — rimasti: {len(q)}")
        return ""

    def _riavvia_montaggio(m):
        api_results.append(_queue_pending_action(state, "RIAVVIA_MONTAGGIO", "", "riavvia montaggio"))
        return ""

    def _blocca_pipeline(m):
        api_results.append(_queue_pending_action(state, "BLOCCA_PIPELINE", "", "blocca pipeline"))
        return ""

    def _genera_copertina(m):
        deferred_tasks.append(("thumbnail", m.group(1).strip()))
        return ""

    def _genera_audio(m):
        deferred_tasks.append(("audio", m.group(1).strip()))
        return ""

    def _genera_script(m):
        deferred_tasks.append(("script", m.group(1).strip()))
        return ""

    def _invia_video(m):
        deferred_tasks.append(("video", None))
        return ""

    def _video_thumb(m):
        vid = m.group(1).strip()
        api_results.append(_queue_pending_action(state, "VIDEO_THUMB", vid, f"aggiorna thumbnail video {vid}"))
        return ""

    clean = re.sub(r'\[GENERA_COPERTINA:\s*([^\]]+)\]', _genera_copertina, reply)
    clean = re.sub(r'\[GENERA_AUDIO:\s*([^\]]+)\]', _genera_audio, clean)
    clean = re.sub(r'\[GENERA_SCRIPT:\s*([^\]]+)\]', _genera_script, clean)
    clean = re.sub(r'\[INVIA_VIDEO\]', _invia_video, clean)
    if is_search or is_pref_only:
        # rimuovi silenziosamente i tag pipeline — utente voleva solo info o cambiare setting
        clean = re.sub(r'\[AGGIUNGI_TOPIC:\s*[^\]]+\]', '', clean)
        clean = re.sub(r'\[PRIORITA_TOPIC:\s*[^\]]+\]', '', clean)
        clean = re.sub(r'\[FORZA_ORA:\s*[^\]]+\]', '', clean)
    else:
        clean = re.sub(r'\[AGGIUNGI_TOPIC:\s*([^\]]+)\]', _add_topic, clean)
        clean = re.sub(r'\[PRIORITA_TOPIC:\s*([^\]]+)\]', _priorita_topic, clean)
        clean = re.sub(r'\[FORZA_ORA:\s*([^\]]+)\]', _forza_ora, clean)
    clean = re.sub(r'\[RICORDA:\s*([^\]]+)\]', _ricorda, clean)
    clean = re.sub(r'\[SETPREF:\s*([^\]]+)\]', _setpref, clean)
    clean = re.sub(r'\[DIMENTICA:\s*([^\]]+)\]', _dimentica, clean)
    clean = re.sub(r'\[RINOMINA_CANALE:\s*([^\]]+)\]', _rinomina_canale, clean)
    clean = re.sub(r'\[AGGIORNA_DESC:\s*([^\]]+)\]', _aggiorna_desc, clean)
    clean = re.sub(r'\[AGGIORNA_KEYWORDS:\s*([^\]]+)\]', _aggiorna_keywords, clean)
    clean = re.sub(r'\[VIDEO_TITOLO:\s*([^\]]+)\]', _video_titolo, clean)
    clean = re.sub(r'\[VIDEO_DESC:\s*([^\]]+)\]', _video_desc, clean)
    clean = re.sub(r'\[VIDEO_TAGS:\s*([^\]]+)\]', _video_tags, clean)
    clean = re.sub(r'\[VIDEO_THUMB:\s*([^\]]+)\]', _video_thumb, clean)
    clean = re.sub(r'\[ELIMINA_TOPIC:\s*([^\]]+)\]', _elimina_topic, clean)
    clean = re.sub(r'\[RIAVVIA_MONTAGGIO\]', _riavvia_montaggio, clean)
    clean = re.sub(r'\[BLOCCA_PIPELINE\]', _blocca_pipeline, clean)

    if added_topics:
        state["topic_queue"] = queue

    return clean.strip(), added_topics, saved_memories, removed_memories, api_results, deferred_tasks

from moduli.state_io import load_state as _load_state, save_state as _save_state
from moduli.scheduling import publish_hours, trigger_hours, PUBLISH_SPREADS


def _publish_hours(state: dict) -> list[int]:
    return publish_hours(state)


def _trigger_hours(state: dict) -> list[int]:
    return trigger_hours(state)


def _next_schedule(state: dict):
    now = datetime.now(timezone.utc)
    done = state.get("runs_today", {}).get(now.strftime("%Y-%m-%d"), 0)
    if done >= state.get("videos_per_day", 1):
        now = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    publish = _publish_hours(state)
    trigger = _trigger_hours(state)
    upcoming = []
    for idx, (trigger_hour, publish_hour) in enumerate(zip(trigger, publish)):
        trigger_dt = now.replace(hour=trigger_hour, minute=0, second=0, microsecond=0)
        publish_dt = now.replace(hour=publish_hour, minute=0, second=0, microsecond=0)
        if publish_dt <= trigger_dt:
            publish_dt += timedelta(days=1)
        if trigger_dt <= now:
            trigger_dt += timedelta(days=1)
            publish_dt += timedelta(days=1)
        upcoming.append((trigger_dt, publish_dt, idx))
    upcoming.sort(key=lambda item: item[0])
    return upcoming[0] if upcoming else (None, None, done)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 <b>YouTube AI Agent attivo</b>\n\n"
        "Produco e pubblico video ogni giorno in autonomia.\n\n"
        "<b>📹 Pipeline:</b>\n"
        "/status — stato attuale\n"
        "/recap — analytics ultimi video\n"
        "/forza — lancia pipeline subito\n"
        "/forzaora &lt;topic&gt; — pubblica subito senza programmazione\n"
        "/skip — salta il video di oggi\n"
        "/setvideogiorno &lt;1-5&gt; — quanti video al giorno\n"
        "/setpubblica &lt;ora1&gt; [ora2] — ora/e pubblicazione (UTC)\n"
        "/autoscheduling on|off — orari automatici da analytics\n"
        "/orari — vedi programma attuale\n"
        "/prossimi — prossimo run e prossimo publish\n"
        "/coda — topic in coda\n"
        "/topic &lt;testo&gt; — aggiungi topic\n"
        "/deltopic &lt;numero&gt; — rimuovi topic\n\n"
        "<b>📺 Canale:</b>\n"
        "/canale — info e statistiche\n"
        "/setdesc &lt;testo&gt; — aggiorna descrizione\n"
        "/video — ultimi video\n"
        "/playlist — playlist del canale\n"
        "/nuovaplaylist &lt;nome&gt; — crea playlist\n"
        "/commenti &lt;videoId&gt; — leggi commenti\n\n"
        "<b>🧠 Memoria:</b>\n"
        "/memoria — vedi cosa ricorda\n"
        "/dimentica &lt;n&gt; — rimuovi un ricordo\n\n"
        "💬 Scrivi qualsiasi cosa per parlare con l'AI.\n\n"
        "<b>Esempi:</b>\n"
        "• <i>genera una copertina di prova</i> → thumbnail standalone\n"
        "• <i>fai il prossimo video su X</i> → pipeline completa",
        parse_mode="HTML"
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state = _load_state()
    last = state.get("last_run_date", "Mai")
    queue = state.get("topic_queue", [])
    recent = state.get("recent_topics", [])
    last_video = state.get("video_ids", [None])[0]

    ps = state.get("pipeline_status")
    if ps:
        pct = ps.get("pct")
        attivita = (f"🔄 In produzione — step: <b>{_h(ps.get('step', '?'))}</b>"
                    + (f" ({pct}%)" if pct else "")
                    + f"\n   agg. {_h(ps.get('updated_at', ''))}")
    else:
        trigger_dt, _, _ = _next_schedule(state)
        if trigger_dt:
            attivita = f"⏳ In attesa — prossima pipeline: {trigger_dt.strftime('%d/%m %H:%M UTC')}"
        else:
            attivita = "⏳ In attesa — nessun orario configurato"

    # sorgente clip: con la generazione AI attiva ogni run costa, e va detto
    # dove si guarda lo stato, non solo nella TUI
    try:
        from moduli.video_ai import riassunto as _riassunto_clip
        riga_clip = f"🎞 Clip: {_h(_riassunto_clip())}\n"
    except Exception:
        riga_clip = ""

    msg = (
        f"📡 <b>Status Agent</b>\n\n"
        f"{attivita}\n\n"
        f"🗓 Ultimo run: {last}\n"
        f"📋 Topic in coda: {len(queue)}\n"
        f"📹 Video prodotti: {len(state.get('video_ids', []))}\n"
        f"{riga_clip}"
    )
    if last_video:
        msg += f"🔗 Ultimo: https://youtu.be/{last_video}\n"
    if recent:
        msg += f"\n<b>Ultimi topic:</b>\n" + "\n".join(f"• {_h(t)}" for t in recent[:5])

    await update.message.reply_text(msg, parse_mode="HTML")


async def cmd_preferenze(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    from moduli.preferenze import formato_html
    await update.message.reply_text(formato_html(), parse_mode="HTML")


async def cmd_setpref(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    from moduli.preferenze import aggiorna, DEFAULT_PREF
    if len(ctx.args) < 2:
        await update.message.reply_text(
            "Uso: /setpref <chiave> <valore>\n"
            "Chiavi: " + ", ".join(DEFAULT_PREF.keys())
        )
        return
    chiave = ctx.args[0]
    valore = " ".join(ctx.args[1:])
    if chiave not in DEFAULT_PREF:
        await update.message.reply_text(f"Chiave sconosciuta: {chiave}\nDisponibili: {', '.join(DEFAULT_PREF.keys())}")
        return
    try:
        pref = aggiorna(chiave, valore)
    except ValueError:
        await update.message.reply_text(f"Valore non valido per {chiave}.")
        return
    await update.message.reply_text(f"✅ <b>{_h(chiave)}</b> aggiornato: <code>{_h(pref[chiave])}</code>", parse_mode="HTML")


async def cmd_recap(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("📊 Recupero analytics...")
    loop = asyncio.get_event_loop()
    try:
        from moduli.analytics import leggi_performance
        videos = await loop.run_in_executor(None, lambda: leggi_performance(n_video=10))
        if not videos:
            await update.message.reply_text("Nessun video trovato sul canale.")
            return
        lines = ["📊 <b>Performance ultimi video:</b>\n"]
        for v in videos:
            retention = int(v.get("avg_view_duration_seconds", 0) / max(v.get("duration_seconds", 480), 1) * 100)
            lines.append(
                f"▪️ <b>{_h(v['title'][:50])}</b>\n"
                f"   👁 {v['views']} views · 👀 {v.get('impressions', 0)} impr · 📈 CTR {v.get('ctr_percent', 0)}%\n"
                f"   ❤️ {v['likes']} · 💬 {v['comments']} · ⏱ {retention}% retention · ⏰ {int(v.get('avg_view_duration_seconds', 0))}s avg"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ Errore analytics: {e}")


async def cmd_forza(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state = _load_state()
    state["force_run"] = True
    state.pop("publish_immediately", None)
    _save_state(state)
    await update.message.reply_text("⚡ <b>Pipeline forzata!</b>\nPartirà entro 60 secondi.", parse_mode="HTML")


async def cmd_forzaora(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    topic = " ".join(ctx.args).strip() if ctx.args else ""
    state = _load_state()
    if topic:
        queue = state.get("topic_queue", [])
        queue.insert(0, topic)
        state["topic_queue"] = queue
    state["force_run"] = True
    state["publish_immediately"] = True
    _save_state(state)
    detail = f"\nTopic: <i>{html.escape(topic)}</i>" if topic else ""
    await update.message.reply_text(
        f"⚡ <b>Pipeline forzata con pubblicazione immediata.</b>{detail}\nPartirà entro 60 secondi.",
        parse_mode="HTML",
    )


async def cmd_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state = _load_state()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    vpd = state.get("videos_per_day", 1)
    runs = state.get("runs_today", {})
    runs[today] = vpd
    state["runs_today"] = runs
    state["last_run_date"] = today
    state.pop("force_run", None)
    state.pop("publish_immediately", None)
    _save_state(state)
    await update.message.reply_text(f"⏭️ Video di oggi saltati: {vpd}/{vpd}.")


async def cmd_abort(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state = _load_state()
    state["abort_pipeline"] = True
    state.pop("force_run", None)
    _save_state(state)
    await update.message.reply_text(
        "⛔ <b>Abort inviato.</b>\n\n"
        "La pipeline si fermerà al prossimo step.\n"
        "<i>Nota: se il montaggio è in corso, terminerà prima di fermarsi.</i>",
        parse_mode="HTML"
    )


async def cmd_coda(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state = _load_state()
    queue = state.get("topic_queue", [])
    if not queue:
        await update.message.reply_text("📋 Coda vuota — il prossimo topic sarà generato automaticamente.")
    else:
        msg = "📋 <b>Topic in coda:</b>\n" + "\n".join(f"{i+1}. {_h(t)}" for i, t in enumerate(queue))
        await update.message.reply_text(msg, parse_mode="HTML")


async def cmd_topic(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(ctx.args).strip() if ctx.args else ""
    if not text:
        await update.message.reply_text("Uso: /topic <testo del topic>")
        return

    state = _load_state()
    queue = state.get("topic_queue", [])
    queue.append(text)
    state["topic_queue"] = queue
    _save_state(state)

    await update.message.reply_text(
        f"✅ <b>Topic aggiunto in coda</b>\n\n"
        f"📌 <i>{_h(text)}</i>\n\n"
        f"Posizione {len(queue)} in coda.",
        parse_mode="HTML"
    )


async def cmd_setvideogiorno(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args:
        await update.message.reply_text("Uso: /setvideogiorno <1-5>")
        return
    try:
        n = int(ctx.args[0])
        if not 1 <= n <= 5:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Numero non valido. Usa un valore tra 1 e 5.")
        return

    publish_spreads = PUBLISH_SPREADS

    state = _load_state()
    state["videos_per_day"] = n
    state["publish_hours_utc"] = publish_spreads[n]
    state.pop("trigger_hours_utc", None)
    _save_state(state)

    # orari effettivi dal resolver condiviso: con auto_scheduling attivo
    # possono differire dallo spread appena scritto
    trigger_str = ", ".join(f"{h:02d}:00" for h in _trigger_hours(state))
    publish_str = ", ".join(f"{h:02d}:00" for h in _publish_hours(state))
    await update.message.reply_text(
        f"✅ <b>{n} video al giorno</b>\n\n"
        f"🎬 Pipeline automatica: {trigger_str} UTC\n"
        f"📅 Pubblicazione: {publish_str} UTC\n\n"
        f"Usa /autoscheduling per ottimizzare automaticamente gli orari.",
        parse_mode="HTML"
    )


async def cmd_autoscheduling(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    arg = ctx.args[0].lower() if ctx.args else ""
    if arg not in ("on", "off"):
        await update.message.reply_text("Uso: /autoscheduling on oppure /autoscheduling off")
        return
    state = _load_state()
    state["auto_scheduling"] = (arg == "on")
    _save_state(state)
    if arg == "on":
        await update.message.reply_text(
            "🤖 <b>Auto-scheduling attivo</b>\n\n"
            "L'agente analizzerà le analytics e sceglierà automaticamente gli orari migliori per pipeline e pubblicazione.",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text("⏹ Auto-scheduling disattivato. Orari manuali attivi.")


async def cmd_orari(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state = _load_state()
    vpd = state.get("videos_per_day", 1)
    publish = _publish_hours(state)
    trigger = _trigger_hours(state)
    auto = state.get("auto_scheduling", False)
    done = state.get("runs_today", {}).get(
        datetime.now(timezone.utc).strftime("%Y-%m-%d"), 0
    )

    trigger_str = "\n".join(
        f"  {i+1}. Pipeline alle {h:02d}:00 UTC → pubblica alle "
        f"{f'{publish[i]:02d}' if i < len(publish) else '?'}:00 UTC"
        for i, h in enumerate(trigger)
    )
    await update.message.reply_text(
        f"📅 <b>Programma attuale</b>\n\n"
        f"📹 Video al giorno: {vpd}\n"
        f"✅ Prodotti oggi: {done}/{vpd}\n"
        f"🤖 Auto-scheduling: {'ON' if auto else 'OFF'}\n\n"
        f"{trigger_str}",
        parse_mode="HTML"
    )


async def cmd_prossimi(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state = _load_state()
    queue = state.get("topic_queue", [])
    vpd = state.get("videos_per_day", 1)
    now = datetime.now(timezone.utc)
    done = state.get("runs_today", {}).get(now.strftime("%Y-%m-%d"), 0)
    remaining = max(0, vpd - done)
    trigger_dt, publish_dt, idx = _next_schedule(state)
    next_topic = queue[0] if queue else "generato automaticamente"
    if trigger_dt is None:
        await update.message.reply_text("Nessun orario configurato.")
        return
    await update.message.reply_text(
        "⏭️ <b>Prossimi eventi</b>\n\n"
        f"🎬 Prossima pipeline: {trigger_dt.strftime('%d/%m/%Y %H:%M UTC')}\n"
        f"📅 Prossima pubblicazione: {publish_dt.strftime('%d/%m/%Y %H:%M UTC')}\n"
        f"📹 Video rimasti oggi: {remaining}/{vpd}\n"
        f"📌 Prossimo topic: <i>{html.escape(next_topic)}</i>",
        parse_mode="HTML",
    )


async def cmd_setpubblica(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args:
        await update.message.reply_text(
            "Uso: /setpubblica &lt;ora1&gt; [ora2 ...]\n"
            "Esempio: /setpubblica 14 → 1 video alle 14:00 UTC\n"
            "Esempio: /setpubblica 12 20 → 1° video alle 12:00, 2° alle 20:00 UTC\n\n"
            "Il trigger di produzione viene calcolato automaticamente 3 ore prima di ciascun orario.",
            parse_mode="HTML"
        )
        return
    try:
        ore = [int(a) for a in ctx.args]
        if any(not 0 <= o <= 23 for o in ore):
            raise ValueError
    except ValueError:
        await update.message.reply_text("Ore non valide. Usa numeri tra 0 e 23.")
        return
    ore = sorted(set(ore))
    state = _load_state()
    state["publish_hours_utc"] = ore
    # cancella trigger_hours_utc espliciti: la produzione deriva sempre dalla pubblicazione
    state.pop("trigger_hours_utc", None)
    # scelta manuale esplicita: spegne l'auto-scheduling, che altrimenti
    # continuerebbe a produrre su best_hours_utc rendendo falso il messaggio qui sotto
    era_auto = bool(state.pop("auto_scheduling", False))
    _save_state(state)
    richiesti = len(ore)
    ore = _publish_hours(state)
    trigger_ore = _trigger_hours(state)
    lines = [f"  Video {i+1}: produzione alle <b>{t:02d}:00</b>, pubblicazione alle <b>{p:02d}:00</b> UTC"
             for i, (t, p) in enumerate(zip(trigger_ore, ore))]
    nota = ("\n\n🤖 Auto-scheduling disattivato: valgono questi orari manuali. "
            "Riattivalo con <code>/autoscheduling on</code>." if era_auto else "")
    # gli orari oltre videos_per_day vengono scartati: senza avviso l'utente
    # imposta due pubblicazioni e ne vede una sola, senza capire perche'
    if richiesti > len(ore):
        nota += (f"\n\n⚠️ Hai indicato {richiesti} orari ma produci "
                 f"{state.get('videos_per_day', 1)} video al giorno: uso solo "
                 f"{'il primo' if len(ore) == 1 else f'i primi {len(ore)}'}. "
                 f"Per usarli tutti: <code>/setvideogiorno {richiesti}</code>.")
    await update.message.reply_text(
        "✅ Orari aggiornati:\n" + "\n".join(lines) + nota,
        parse_mode="HTML"
    )


async def cmd_deltopic(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state = _load_state()
    queue = state.get("topic_queue", [])

    if not queue:
        await update.message.reply_text("📋 Coda già vuota.")
        return

    if not ctx.args:
        await update.message.reply_text(
            "Uso: /deltopic <numero>\n\n"
            "Usa /coda per vedere i numeri."
        )
        return

    try:
        idx = int(ctx.args[0]) - 1
        if idx < 0 or idx >= len(queue):
            raise ValueError
    except ValueError:
        await update.message.reply_text(f"Numero non valido. La coda ha {len(queue)} topic.")
        return

    rimosso = queue.pop(idx)
    state["topic_queue"] = queue
    _save_state(state)

    await update.message.reply_text(
        f"🗑 <b>Topic rimosso:</b>\n<i>{_h(rimosso)}</i>\n\n"
        f"Topic rimanenti in coda: {len(queue)}",
        parse_mode="HTML"
    )


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.message.chat_id)
    state = _load_state()
    if "chat_history" in state and chat_id in state["chat_history"]:
        state["chat_history"][chat_id] = []
        _save_state(state)
    await update.message.reply_text(
        "🔄 <b>Contesto resettato.</b>\nL'AI non ricorda i messaggi precedenti di questa chat.\n"
        "La memoria lungo termine (<code>/memoria</code>) è invece intatta.",
        parse_mode="HTML"
    )


async def cmd_memoria(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    from moduli.memoria import lista
    memories = lista()
    if not memories:
        await update.message.reply_text("🧠 Memoria vuota.")
        return
    righe = "\n".join(f"{i+1}. {_h(m['testo'])} <i>({_h(m['data'])})</i>" for i, m in enumerate(memories))
    await update.message.reply_text(f"🧠 <b>Memoria lungo termine:</b>\n\n{righe}", parse_mode="HTML")


async def cmd_dimentica(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    from moduli.memoria import rimuovi, svuota, lista
    if ctx.args and ctx.args[0].lower() == "tutto":
        svuota()
        await update.message.reply_text("🗑 Memoria completamente svuotata.")
        return
    if not ctx.args:
        await update.message.reply_text("Uso: /dimentica <numero> oppure /dimentica tutto\nUsa /memoria per vedere i numeri.")
        return
    try:
        idx = int(ctx.args[0]) - 1
        rimossa = rimuovi(idx)
        if rimossa:
            await update.message.reply_text(f"🗑 Rimosso: <i>{rimossa}</i>", parse_mode="HTML")
        else:
            await update.message.reply_text("Numero non valido.")
    except ValueError:
        await update.message.reply_text("Usa un numero. Es: /dimentica 2")


async def cmd_conferma(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    action_id = ctx.args[0].strip() if ctx.args else ""
    if not action_id:
        await update.message.reply_text("Uso: /conferma <id>")
        return
    state = _load_state()
    pending = _pending_actions(state)
    action = next((a for a in pending if a.get("id") == action_id), None)
    if not action:
        _save_state(state)
        await update.message.reply_text("Conferma non trovata o scaduta.")
        return
    try:
        result = _execute_confirmed_action(state, action)
        state["pending_actions"] = [a for a in pending if a.get("id") != action_id]
        _save_state(state)
        await update.message.reply_text(f"✅ {result}", parse_mode="HTML")
    except Exception as e:
        _save_state(state)
        await update.message.reply_text(f"❌ Errore esecuzione confermata: {_h(e)}", parse_mode="HTML")


async def cmd_annulla(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    action_id = ctx.args[0].strip() if ctx.args else ""
    if not action_id:
        await update.message.reply_text("Uso: /annulla <id>")
        return
    state = _load_state()
    pending = _pending_actions(state)
    remaining = [a for a in pending if a.get("id") != action_id]
    if len(remaining) == len(pending):
        _save_state(state)
        await update.message.reply_text("Azione non trovata o gia scaduta.")
        return
    state["pending_actions"] = remaining
    _save_state(state)
    await update.message.reply_text("Azione annullata.")


def _run_canale(fn, *args, **kwargs):
    from moduli.canale import (
        info_canale, aggiorna_canale, lista_video, lista_playlist,
        crea_playlist, lista_commenti, rispondi_commento, elimina_commento,
    )
    funcs = {
        "info_canale": info_canale, "aggiorna_canale": aggiorna_canale,
        "lista_video": lista_video, "lista_playlist": lista_playlist,
        "crea_playlist": crea_playlist, "lista_commenti": lista_commenti,
        "rispondi_commento": rispondi_commento, "elimina_commento": elimina_commento,
    }
    return funcs[fn](*args, **kwargs)


async def cmd_canale(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🔄 Recupero info canale...")
    loop = asyncio.get_event_loop()
    try:
        info = await loop.run_in_executor(None, _run_canale, "info_canale")
        msg = (
            f"📺 <b>{_h(info['title'])}</b>\n\n"
            f"👥 Iscritti: {info['subscribers']:,}\n"
            f"👁 Visualizzazioni: {info['views']:,}\n"
            f"📹 Video: {info['video_count']}\n"
            f"🌍 Paese: {_h(info['country'])}\n\n"
            f"📝 <b>Descrizione:</b>\n{_h(info['description'][:300]) or '—'}\n\n"
            f"🔑 <b>Keyword:</b> {_h(info['keywords'][:200]) or '—'}"
        )
    except Exception as e:
        msg = f"❌ Errore: {e}"
    await update.message.reply_text(msg, parse_mode="HTML")


async def cmd_setdesc(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    desc = " ".join(ctx.args).strip() if ctx.args else ""
    if not desc:
        await update.message.reply_text("Uso: /setdesc <nuova descrizione>")
        return
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, lambda: _run_canale("aggiorna_canale", description=desc))
        await update.message.reply_text("✅ Descrizione canale aggiornata.")
    except Exception as e:
        await update.message.reply_text(f"❌ Errore: {e}")


async def cmd_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🔄 Recupero video...")
    loop = asyncio.get_event_loop()
    try:
        videos = await loop.run_in_executor(None, _run_canale, "lista_video")
        if not videos:
            await update.message.reply_text("Nessun video trovato.")
            return
        lines = [f"📹 <b>Ultimi video:</b>"]
        for v in videos:
            lines.append(f"▪️ <a href='https://youtu.be/{v['video_id']}'>{_h(v['title'][:50])}</a> — {v['published']}")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        await update.message.reply_text(f"❌ Errore: {e}")


async def cmd_playlist(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🔄 Recupero playlist...")
    loop = asyncio.get_event_loop()
    try:
        playlists = await loop.run_in_executor(None, _run_canale, "lista_playlist")
        if not playlists:
            await update.message.reply_text("Nessuna playlist trovata.")
            return
        lines = ["📋 <b>Playlist:</b>"]
        for p in playlists:
            lines.append(f"▪️ {_h(p['title'])} ({p['count']} video) — <code>{p['id']}</code>")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ Errore: {e}")


async def cmd_nuovaplaylist(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    nome = " ".join(ctx.args).strip() if ctx.args else ""
    if not nome:
        await update.message.reply_text("Uso: /nuovaplaylist <nome>")
        return
    loop = asyncio.get_event_loop()
    try:
        pid = await loop.run_in_executor(None, lambda: _run_canale("crea_playlist", nome))
        await update.message.reply_text(f"✅ Playlist <b>{nome}</b> creata.\nID: <code>{pid}</code>", parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ Errore: {e}")


async def cmd_commenti(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    video_id = ctx.args[0] if ctx.args else ""
    if not video_id:
        await update.message.reply_text("Uso: /commenti <videoId>")
        return
    loop = asyncio.get_event_loop()
    try:
        commenti = await loop.run_in_executor(None, lambda: _run_canale("lista_commenti", video_id))
        if not commenti:
            await update.message.reply_text("Nessun commento trovato.")
            return
        lines = [f"💬 <b>Commenti:</b>"]
        for c in commenti:
            lines.append(f"<b>{_h(c['author'])}</b> (❤️{c['likes']})\n{_h(c['text'][:150])}\n<code>{c['id']}</code>")
        await update.message.reply_text("\n\n".join(lines), parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ Errore: {e}")


async def handle_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    caption = (msg.caption or "").strip().lower()

    # determina scope
    state_for_scope = _load_state()
    queue_for_scope = state_for_scope.get("topic_queue", [])
    if not caption or "next" in caption or "prossimo" in caption:
        scope = "next"
    elif "all" in caption or "tutti" in caption or "sempre" in caption:
        scope = "all"
    else:
        # cerca numero nella didascalia → usa il topic corrispondente
        import re as _re
        num_match = _re.search(r'\d+', caption)
        if num_match:
            idx = int(num_match.group()) - 1  # 1-based → 0-based
            if 0 <= idx < len(queue_for_scope):
                scope = queue_for_scope[idx]
            else:
                await msg.reply_text(f"Numero topic non valido. Hai {len(queue_for_scope)} topic in coda.")
                return
        else:
            scope = caption  # testo libero come topic parziale

    # determina tipo e file
    if msg.photo:
        media_type = "thumbnail"
        file_obj = msg.photo[-1]  # massima risoluzione
        folder = "assets/custom/thumbnails"
        ext = ".jpg"
    elif msg.video or (msg.document and msg.document.mime_type and msg.document.mime_type.startswith("video")):
        media_type = "video"
        file_obj = msg.video or msg.document
        folder = "assets/custom/clips"
        ext = ".mp4"
    elif msg.audio or msg.voice or (msg.document and msg.document.mime_type and msg.document.mime_type.startswith("audio")):
        media_type = "audio"
        file_obj = msg.audio or msg.voice or msg.document
        folder = "assets/custom/audio"
        mime = getattr(file_obj, "mime_type", "audio/mpeg")
        ext = ".ogg" if msg.voice else (".mp3" if "mpeg" in mime or "mp3" in mime else ".m4a" if "m4a" in mime or "mp4" in mime else ".ogg")
    else:
        await msg.reply_text("Formato non supportato. Invia foto, video (MP4) o audio (MP3/M4A).")
        return

    os.makedirs(folder, exist_ok=True)
    file_id = file_obj.file_id
    fname = f"{file_id[:16]}{ext}"
    dest = os.path.join(folder, fname)

    await msg.reply_text(f"Ricevuto {media_type} — salvataggio in corso...")
    tg_file = await ctx.bot.get_file(file_id)
    await tg_file.download_to_drive(dest)

    state = _load_state()
    custom = state.setdefault("custom_media", {"clips": [], "audio": [], "thumbnails": []})
    key = {"video": "clips", "audio": "audio", "thumbnail": "thumbnails"}[media_type]
    custom[key] = [e for e in custom.get(key, []) if e.get("file_id") != file_id]
    custom[key].append({"path": dest, "scope": scope, "file_id": file_id})
    _save_state(state)

    scope_label = {"next": "solo nel prossimo video", "all": "in tutti i video futuri"}.get(scope, f"nel video: '{scope[:60]}'")
    type_label = {"video": "Clip video", "audio": "Audio", "thumbnail": "Copertina"}[media_type]

    await msg.reply_text(
        f"✅ {type_label} salvata e verra usata {scope_label}.\n"
        f"Percorso: <code>{dest}</code>\n\n"
        f"Didascalia: vuota=prossimo, 'all'=tutti, numero=topic N della coda.",
        parse_mode="HTML"
    )


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    if not text:
        return

    chat_id = str(update.message.chat_id)
    await update.message.chat.send_action("typing")
    loop = asyncio.get_event_loop()

    state = _load_state()
    history = _get_history(state, chat_id)

    if _is_preference_list_request(text):
        from moduli.preferenze import formato_html
        await update.message.reply_text(formato_html(), parse_mode="HTML")
        return

    from moduli.preferenze import aggiorna_da_testo
    pref_updates = aggiorna_da_testo(text)
    if pref_updates and not any(k in text.lower() for k in _ACTION_KEYWORDS):
        await update.message.reply_text(_format_preference_updates(pref_updates), parse_mode="HTML")
        return

    future = loop.run_in_executor(None, _ask_ai, text, history, state)
    while not future.done():
        await asyncio.sleep(4)
        if not future.done():
            await update.message.chat.send_action("typing")

    reply, new_history = await future

    # Ricarica lo state: l'AI può aver impiegato 30-60s e nel frattempo la
    # pipeline o altri comandi possono aver scritto — evita lost-update.
    state = _load_state()

    # esegui azioni embedded nella risposta
    clean_reply, added_topics, saved_memories, removed_memories, api_results, deferred_tasks = _execute_actions(
        reply, state,
        is_search=_needs_web_search(text),
        is_pref_only=_is_pref_only_request(text),
    )

    _save_history(state, chat_id, new_history)
    _save_state(state)

    # Se la risposta era SOLO tag (clean vuoto) ma ci sono azioni in coda,
    # non rimandare il tag grezzo all'utente: lo confermano i messaggi azione.
    had_actions = bool(deferred_tasks or api_results or added_topics
                       or saved_memories or removed_memories)
    if clean_reply:
        msg = clean_reply
    elif had_actions:
        msg = None  # le conferme azione sotto parlano da sole
    else:
        msg = reply or "⚠️ Risposta vuota dal modello AI. Riprova."
    if msg:
        if len(msg) > 4096:
            for i in range(0, len(msg), 4096):
                await update.message.reply_text(msg[i:i+4096])
        else:
            await update.message.reply_text(msg)
    if deferred_tasks:
        labels = {"thumbnail": "🖼 Genero la copertina…", "audio": "🎙 Genero l'audio…",
                  "script": "📝 Scrivo lo script…", "video": "🎬 Recupero il video…"}
        for tt, _p in deferred_tasks:
            await update.message.reply_text(labels.get(tt, "⏳ Eseguo…"))

    if added_topics:
        elenco = "\n".join(f"• {_h(t)}" for t in added_topics)
        await update.message.reply_text(
            f"✅ <b>Aggiunti {len(added_topics)} topic in coda:</b>\n{elenco}",
            parse_mode="HTML"
        )
    if saved_memories:
        elenco = "\n".join(f"• {_h(m)}" for m in saved_memories)
        await update.message.reply_text(
            f"🧠 <b>Memorizzato:</b>\n{elenco}",
            parse_mode="HTML"
        )
    if removed_memories:
        elenco = "\n".join(f"• {_h(m)}" for m in removed_memories)
        await update.message.reply_text(
            f"🗑 <b>Rimosso dalla memoria:</b>\n{elenco}",
            parse_mode="HTML"
        )
    for msg in api_results:
        await update.message.reply_text(msg, parse_mode="HTML")

    for task_type, payload in deferred_tasks:
        if task_type == "thumbnail":
            await update.message.chat.send_action("upload_photo")
            preview_path = "output/thumbnail_preview.jpg"
            try:
                from moduli.thumbnail import genera_thumbnail
                await loop.run_in_executor(None, lambda t=payload: genera_thumbnail(t, preview_path))
                with open(preview_path, "rb") as f:
                    await update.message.reply_photo(f, caption=f"🖼 <b>{_h(payload)}</b>", parse_mode="HTML")
            except Exception as e:
                await update.message.reply_text(f"❌ Errore copertina: {_h(str(e))}", parse_mode="HTML")

        elif task_type == "audio":
            await update.message.chat.send_action("record_voice")
            audio_path = "output/audio_preview.mp3"
            try:
                from moduli.audio import genera_audio
                await loop.run_in_executor(None, lambda t=payload: genera_audio(t, audio_path))
                with open(audio_path, "rb") as f:
                    await update.message.reply_audio(f, title="Audio preview")
            except Exception as e:
                await update.message.reply_text(f"❌ Errore audio: {_h(str(e))}", parse_mode="HTML")

        elif task_type == "script":
            await update.message.chat.send_action("typing")
            try:
                from moduli.cervello import genera_contenuto
                from moduli.strategia import calcola_strategia
                def _gen_script(topic=payload):
                    strat = calcola_strategia([])
                    return genera_contenuto(topic, strategy=strat)
                content = await loop.run_in_executor(None, _gen_script)
                script_msg = (
                    f"📝 <b>{_h(content['title'])}</b>\n\n"
                    f"<b>Script ({len(content.get('script','').split())} parole):</b>\n"
                    f"{_h(content.get('script','')[:3500])}"
                )
                for i in range(0, len(script_msg), 4096):
                    await update.message.reply_text(script_msg[i:i+4096], parse_mode="HTML")
                if content.get("description"):
                    desc_msg = f"<b>Descrizione:</b>\n{_h(content['description'][:1000])}"
                    await update.message.reply_text(desc_msg, parse_mode="HTML")
                if content.get("tags"):
                    await update.message.reply_text(f"🏷 {_h(', '.join(content['tags']))}", parse_mode="HTML")
            except Exception as e:
                await update.message.reply_text(f"❌ Errore script: {_h(str(e))}", parse_mode="HTML")

        elif task_type == "video":
            video_path = "output/output_finale.mp4"
            if os.path.exists(video_path):
                size_mb = os.path.getsize(video_path) / (1024 * 1024)
                if size_mb <= 50:
                    await update.message.chat.send_action("upload_video")
                    with open(video_path, "rb") as f:
                        await update.message.reply_video(f, caption="🎬 Ultimo video prodotto")
                else:
                    fresh = _load_state()
                    video_ids = fresh.get("video_ids", [])
                    if video_ids:
                        await update.message.reply_text(
                            f"📦 Video troppo grande per Telegram ({size_mb:.0f} MB).\n"
                            f"🔗 <a href='https://youtu.be/{_h(video_ids[0])}'>Guardalo su YouTube</a>",
                            parse_mode="HTML", disable_web_page_preview=False
                        )
                    else:
                        await update.message.reply_text(
                            f"📦 Video troppo grande per Telegram ({size_mb:.0f} MB) e nessun video ancora pubblicato.",
                            parse_mode="HTML"
                        )
            else:
                await update.message.reply_text("❌ Nessun video trovato in output/output_finale.mp4 — avvia la pipeline prima.")


def start_bot() -> threading.Thread | None:
    """Avvia il bot in un thread daemon. Ritorna il thread (None senza token)
    così il chiamante può fare watchdog con is_alive()."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("[Telegram] No token — bot disabled")
        return None

    async def _run():
        app = (
            Application.builder()
            .token(token)
            .read_timeout(30)
            .write_timeout(30)
            .build()
        )
        app.add_handler(CommandHandler("start", _authorized(cmd_start)))
        app.add_handler(CommandHandler("status", _authorized(cmd_status)))
        app.add_handler(CommandHandler("recap", _authorized(cmd_recap)))
        app.add_handler(CommandHandler("preferenze", _authorized(cmd_preferenze)))
        app.add_handler(CommandHandler("setpref", _authorized(cmd_setpref)))
        app.add_handler(CommandHandler("forza", _authorized(cmd_forza)))
        app.add_handler(CommandHandler("forzaora", _authorized(cmd_forzaora)))
        app.add_handler(CommandHandler("skip", _authorized(cmd_skip)))
        app.add_handler(CommandHandler("abort", _authorized(cmd_abort)))
        app.add_handler(CommandHandler("conferma", _authorized(cmd_conferma)))
        app.add_handler(CommandHandler("annulla", _authorized(cmd_annulla)))
        app.add_handler(CommandHandler("coda", _authorized(cmd_coda)))
        app.add_handler(CommandHandler("topic", _authorized(cmd_topic)))
        app.add_handler(CommandHandler("deltopic", _authorized(cmd_deltopic)))
        app.add_handler(CommandHandler("setvideogiorno", _authorized(cmd_setvideogiorno)))
        app.add_handler(CommandHandler("autoscheduling", _authorized(cmd_autoscheduling)))
        app.add_handler(CommandHandler("orari", _authorized(cmd_orari)))
        app.add_handler(CommandHandler("prossimi", _authorized(cmd_prossimi)))
        app.add_handler(CommandHandler("setpubblica", _authorized(cmd_setpubblica)))
        app.add_handler(CommandHandler("canale", _authorized(cmd_canale)))
        app.add_handler(CommandHandler("setdesc", _authorized(cmd_setdesc)))
        app.add_handler(CommandHandler("video", _authorized(cmd_video)))
        app.add_handler(CommandHandler("playlist", _authorized(cmd_playlist)))
        app.add_handler(CommandHandler("nuovaplaylist", _authorized(cmd_nuovaplaylist)))
        app.add_handler(CommandHandler("commenti", _authorized(cmd_commenti)))
        app.add_handler(CommandHandler("reset", _authorized(cmd_reset)))
        app.add_handler(CommandHandler("memoria", _authorized(cmd_memoria)))
        app.add_handler(CommandHandler("dimentica", _authorized(cmd_dimentica)))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _authorized(handle_message)))
        app.add_handler(MessageHandler(filters.VIDEO | filters.AUDIO | filters.VOICE | filters.PHOTO | filters.Document.VIDEO | filters.Document.AUDIO, _authorized(handle_media)))

        await app.initialize()
        await app.bot.set_my_commands([
            ("start",          "Mostra tutti i comandi"),
            ("status",         "Stato attuale dell'agent"),
            ("recap",          "Analytics ultimi video"),
            ("forza",          "Lancia la pipeline subito"),
            ("forzaora",       "Pubblica subito senza programmazione"),
            ("skip",           "Salta il video di oggi"),
            ("abort",          "Ferma la pipeline in corso"),
            ("conferma",       "Conferma un'azione sensibile"),
            ("annulla",        "Annulla un'azione sensibile"),
            ("orari",          "Programma attuale"),
            ("prossimi",       "Prossimo run e publish"),
            ("setvideogiorno", "Imposta quanti video al giorno"),
            ("setpubblica",    "Ora/e pubblicazione video (UTC)"),
            ("autoscheduling", "Orari automatici da analytics"),
            ("coda",           "Topic in coda"),
            ("topic",          "Aggiungi topic alla coda"),
            ("deltopic",       "Rimuovi topic dalla coda"),
            ("canale",         "Info e statistiche canale"),
            ("setdesc",        "Aggiorna descrizione canale"),
            ("video",          "Ultimi video pubblicati"),
            ("playlist",       "Playlist del canale"),
            ("nuovaplaylist",  "Crea nuova playlist"),
            ("commenti",       "Leggi commenti di un video"),
            ("reset",          "Resetta il contesto conversazione"),
            ("memoria",        "Vedi memoria lungo termine"),
            ("dimentica",      "Rimuovi un ricordo"),
        ])
        _conflict_count = {"n": 0}

        async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
            from telegram.error import Conflict
            if isinstance(context.error, Conflict):
                _conflict_count["n"] += 1
                if _conflict_count["n"] == 1:
                    print(
                        "[Telegram] Conflict: un'altra sessione è attiva (o connessione stale).\n"
                        "           Attendo 60s che scada. Se il problema persiste, ferma\n"
                        "           tutti i processi tube-assistant sulle altre macchine.",
                        flush=True,
                    )
                elif _conflict_count["n"] >= 4:
                    print(
                        "\n[Telegram] FATALE: Conflict persiste dopo 4 tentativi.\n"
                        "Ferma l'altra istanza (PC/server) e riavvia:\n"
                        "  pkill -f tube-assistant   (su ogni macchina)\n",
                        flush=True,
                    )
                    os._exit(1)
                return
            print(f"[Telegram] Errore: {context.error}", flush=True)

        app.add_error_handler(_on_error)

        # Il bootstrap di start_polling (primo getUpdates/deleteWebhook) NON passa
        # dall'error_handler: un Conflict o errore di rete qui ucciderebbe il thread.
        # Retry con backoff, coerente con la gestione Conflict del loop.
        from telegram.error import Conflict, NetworkError, TimedOut
        for tentativo in range(1, 7):
            try:
                await app.start()
                await app.updater.start_polling(
                    drop_pending_updates=True,
                    bootstrap_retries=-1,
                )
                break
            except (Conflict, NetworkError, TimedOut) as e:
                wait = min(60, 5 * tentativo)
                tipo = type(e).__name__
                print(
                    f"[Telegram] Bootstrap {tipo} (tentativo {tentativo}/6). "
                    f"Ritento tra {wait}s. Se persiste, ferma le altre istanze.",
                    flush=True,
                )
                try:
                    await app.updater.stop()
                except Exception:
                    pass
                try:
                    await app.stop()
                except Exception:
                    pass
                await asyncio.sleep(wait)
        else:
            print(
                "\n[Telegram] FATALE: impossibile avviare il polling dopo 6 tentativi.\n"
                "Ferma l'altra istanza (PC/server) e riavvia:\n"
                "  pkill -f tube-assistant   (su ogni macchina)\n",
                flush=True,
            )
            os._exit(1)
        print("[Telegram] Bot polling started")

        # Sorveglia il polling invece di dormire e basta: con `sleep(3600)` il
        # thread restava vivo anche a updater morto, quindi il watchdog di
        # agent.py (che guarda `is_alive()`) non lo riavviava mai.
        while True:
            await asyncio.sleep(30)
            updater = getattr(app, "updater", None)
            if updater is not None and not updater.running:
                print("[Telegram] Polling non piu' attivo — chiudo il thread "
                      "per farlo riavviare dal watchdog", flush=True)
                break
        try:
            await app.updater.stop()
        except Exception:
            pass
        try:
            await app.stop()
            await app.shutdown()
        except Exception:
            pass

    def _thread():
        try:
            asyncio.run(_run())
        except Exception as e:
            print(f"[Telegram] Thread terminato con errore: {type(e).__name__}: {e}", flush=True)

    t = threading.Thread(target=_thread, daemon=True)
    t.start()
    return t
