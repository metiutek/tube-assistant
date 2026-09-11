
"""
Autonomous YouTube AI Agent — runs 24/7 as a daemon.

Supports:
- N videos per day (videos_per_day in state.json)
- Custom trigger hours (trigger_hours_utc list in state.json)
- Auto-scheduling based on analytics (auto_scheduling in state.json)
- Force run and skip via Telegram
"""

import html
import os
import json
import time
import traceback
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv


def _h(value) -> str:
    """Escape per i messaggi Telegram con parse_mode=HTML."""
    return html.escape(str(value), quote=True)

load_dotenv()

# ── env validation ────────────────────────────────────────────────────────────
def _check_env() -> None:
    missing = []
    required = {
        "TELEGRAM_BOT_TOKEN": "Telegram bot token",
        "TELEGRAM_CHAT_ID":   "Telegram chat ID",
        "AI_SERVICE":         "AI service (run the wizard: python wizard.py)",
        "PEXELS_API_KEY":     "Pexels API key",
    }
    for key, label in required.items():
        if not os.environ.get(key, "").strip():
            missing.append(f"  ✗  {key}  ({label})")

    service = os.environ.get("AI_SERVICE", "")
    service_keys = {
        "openrouter":   "OPENROUTER_API_KEY",
        "openai":       "OPENAI_API_KEY",
        "anthropic":    "ANTHROPIC_API_KEY",
        "gemini":       "GEMINI_API_KEY",
        "mistral":      "MISTRAL_API_KEY",
        "groq":         "GROQ_API_KEY",
        "deepseek":     "DEEPSEEK_API_KEY",
        "xai":          "XAI_API_KEY",
        "cohere":       "COHERE_API_KEY",
        "together":     "TOGETHER_API_KEY",
        "perplexity":   "PERPLEXITY_API_KEY",
        "fireworks":    "FIREWORKS_API_KEY",
        "azure_openai": "AZURE_OPENAI_API_KEY",
        "ollama_cloud": "OLLAMA_API_KEY",
    }
    if service in service_keys:
        key = service_keys[service]
        if not os.environ.get(key, "").strip():
            missing.append(f"  ✗  {key}  (API key for {service})")

    if missing:
        print("\n[ERRORE] Variabili mancanti nel .env:")
        for m in missing:
            print(m)
        print("\nEsegui il wizard per configurare tutto: python wizard.py\n")
        sys.exit(1)

import sys
_check_env()

from moduli.cervello import genera_topic, genera_contenuto
from moduli.audio import genera_audio
from moduli.asset import scarica_clips, scarica_pool_clips
from moduli.montaggio import monta_video
from moduli.thumbnail import genera_thumbnail
from moduli.pubblica import pubblica_video, calcola_publish_slots
from moduli.analytics import leggi_performance
from moduli.strategia import calcola_strategia
from moduli.manutenzione import (
    assicura_spazio, pulisci_cache, pulisci_temp_render, spazio_libero_gb,
)
from moduli.notifiche import (
notify_start, notify_step, notify_done, notify_error, notify_analytics,
notify_config
)
from moduli.telegram_handler import start_bot
from moduli.state_io import load_state as _load_state, save_state as _save_state
from moduli.scheduling import (
    trigger_hours, publish_hours, PUBLISH_SPREADS,
    TRIGGER_LEAD_HOURS, DEFAULT_TRIGGER_HOURS, DEFAULT_VIDEOS_PER_DAY,
)

AUDIO_PATH = "output/narration.mp3"
VIDEO_PATH = "output/output_finale.mp4"
THUMB_PATH = "output/thumbnail.jpg"
CHECKPOINT_PATH = "output/pipeline_checkpoint.json"
PID_FILE   = ".agent.pid"


_pipeline_step = "init"


def _segna_step(nome: str, pct: int | None = None) -> None:
    """Aggiorna lo step corrente sia in memoria (per i crash report) sia in
    state.json (pipeline_status), così /status mostra cosa sta succedendo."""
    global _pipeline_step
    _pipeline_step = nome
    try:
        fresh = _load_state()
        status = {
            "step": nome,
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        }
        if pct is not None:
            status["pct"] = pct
        fresh["pipeline_status"] = status
        _save_state(fresh)
    except Exception:
        pass


def _pulisci_status_pipeline() -> None:
    try:
        fresh = _load_state()
        if fresh.pop("pipeline_status", None) is not None:
            _save_state(fresh)
    except Exception:
        pass


def _spiega_errore(err: str) -> str:
    """Traduce gli errori comuni in un messaggio umano con l'azione da fare."""
    e = err.lower()
    if "uploadlimitexceeded" in e or "quotaexceeded" in e or "dailylimitexceeded" in e:
        return ("Quota YouTube esaurita per oggi: si resetta a mezzanotte ora del "
                "Pacifico (~09:00 in Italia). Riprova più tardi con /forza.")
    if "invalid_grant" in e or "refresherror" in e or ("token" in e and ("expired" in e or "revoked" in e)):
        return "Login Google scaduto o revocato: cancella token.json e riavvia l'agente per rifare il login."
    if "no space left" in e or "errno 28" in e or "spazio disco insufficiente" in e:
        return "Disco pieno: libera spazio e riprova con /forza."
    if "ffmpeg not found" in e:
        return "ffmpeg non trovato: installalo e aggiungilo al PATH, oppure imposta FFMPEG_PATH nel .env."
    if "pexels" in e and ("401" in e or "403" in e or "unauthorized" in e):
        return "Chiave Pexels rifiutata: controlla PEXELS_API_KEY nel .env."
    if "no pexels results" in e or "nessuna clip" in e:
        return "Nessuna clip trovata per le keyword generate: riprova con /forza (verranno generate keyword nuove)."
    if "429" in e or "ratelimit" in e or "rate limit" in e or "too many requests" in e:
        return "Servizio esterno in rate limit (troppe richieste): aspetta qualche minuto e usa /forza."
    if ("connection" in e or "timed out" in e or "timeout" in e
            or "getaddrinfo" in e or "name resolution" in e or "unreachable" in e):
        return "Problema di rete: controlla la connessione. Riproverò al prossimo orario, oppure usa /forza."
    if "script troppo corto" in e:
        return "L'AI ha generato uno script troppo corto due volte: riprova con /forza o cambia topic."
    return ""


_pid_lock_handle = None
# byte del file di lock riservato al lock del sistema operativo: sta oltre
# qualsiasi PID scritto all'inizio del file
_LOCK_OFFSET = 4096


def _prova_lock_esclusivo(fh) -> bool:
    """Prende un lock esclusivo non bloccante sul file aperto `fh`.

    Il PID da solo non basta come lock: dopo un kill duro `atexit` non gira e
    `.agent.pid` resta orfano; se nel frattempo il sistema riassegna quel PID a
    un processo qualsiasi, `os.kill(pid, 0)` riesce e l'avvio viene bloccato da
    un'istanza che non esiste. Il lock invece lo tiene il sistema operativo: se
    il processo muore — anche ammazzato — il lock cade da solo.

    Su Windows il lock e' obbligatorio (non advisory) e blocca anche le
    letture: per questo si prende un byte oltre il PID (`_LOCK_OFFSET`) invece
    del byte 0, cosi' `cat .agent.pid` continua a funzionare.
    """
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(_LOCK_OFFSET)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False
    except ImportError:
        # nessun meccanismo di lock disponibile: si ripiega sul PID
        return True


def _pid_vivo(pid: int) -> bool:
    try:
        # signal 0 = verifica esistenza senza inviare nulla (su Windows
        # CPython tratta a parte sig 0: non termina il processo)
        os.kill(pid, 0)
        return True
    except PermissionError:
        # il processo esiste ma non abbiamo i permessi per segnalarlo
        return True
    except (ValueError, TypeError, ProcessLookupError, OSError):
        return False


def _acquire_pid_lock() -> None:
    """Blocca il doppio avvio: esce se un'altra istanza è già in esecuzione."""
    global _pid_lock_handle
    vecchio_pid = None
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE) as f:
                vecchio_pid = int(f.read().strip())
        except (OSError, ValueError):
            vecchio_pid = None

    try:
        fh = open(PID_FILE, "r+") if os.path.exists(PID_FILE) else open(PID_FILE, "w+")
    except OSError as e:
        print(f"\n[ERRORE] Impossibile aprire il file di lock {PID_FILE}: {e}\n")
        sys.exit(1)

    if not _prova_lock_esclusivo(fh):
        fh.close()
        print(f"\n[ERRORE] Agent già in esecuzione (PID {vecchio_pid}).")
        print("Ferma l'istanza esistente prima di avviarne un'altra:")
        print(f"  kill {vecchio_pid}")
        print("  oppure: pkill -f tube-assistant")
        sys.exit(1)

    # Il lock è nostro: il contenuto precedente è di un processo morto, anche
    # se il suo PID risulta ancora "vivo" perché nel frattempo riciclato.
    if vecchio_pid is not None and vecchio_pid != os.getpid():
        nota = (" (il PID risulta ancora attivo: è stato riciclato)"
                if _pid_vivo(vecchio_pid) else "")
        print(f"[lock] Lock orfano del PID {vecchio_pid} rimosso{nota}.", flush=True)

    _pid_lock_handle = fh
    try:
        fh.seek(0)
        fh.truncate(0)
        fh.write(str(os.getpid()))
        fh.flush()
    except OSError:
        pass

    import atexit
    atexit.register(_release_pid_lock)


def _release_pid_lock() -> None:
    global _pid_lock_handle
    fh, _pid_lock_handle = _pid_lock_handle, None
    if fh is not None:
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(_LOCK_OFFSET)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            fh.close()
        except OSError:
            pass
    try:
        os.remove(PID_FILE)
    except OSError:
        pass


class PipelineAbort(RuntimeError):
    pass


def _check_abort() -> None:
    if _load_state().get("abort_pipeline"):
        raise PipelineAbort("Pipeline interrotta su richiesta Telegram")


def _runs_today(state: dict) -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return state.get("runs_today", {}).get(today, 0)


def _increment_runs_today(state: dict) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    runs = state.get("runs_today", {})
    runs = {today: runs.get(today, 0) + 1}  # keep only today
    state["runs_today"] = runs


def _get_trigger_hours(state: dict) -> list[int]:
    """Orari UTC in cui la pipeline deve partire oggi.

    Delega a moduli.scheduling: la stessa funzione alimenta /status, /orari,
    /prossimi e il calcolo dell'orario di pubblicazione, così il bot non può
    più mostrare un programma diverso da quello che il daemon esegue.
    """
    return trigger_hours(state)


def _should_run(state: dict, now: datetime, last_attempt: tuple | None = None) -> bool:
    if state.get("force_run"):
        return True
    videos_per_day = state.get("videos_per_day", DEFAULT_VIDEOS_PER_DAY)
    if _runs_today(state) >= videos_per_day:
        return False
    trigger_hours = _get_trigger_hours(state)
    # finestra = tutta l'ora (non solo minute==0): un tick saltato per drift
    # del loop non fa perdere il video. last_attempt evita doppi trigger.
    key = (now.strftime("%Y-%m-%d"), now.hour)
    return now.hour in trigger_hours and key != last_attempt


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(line.encode(encoding, errors="replace").decode(encoding), flush=True)


# ── checkpoint: riprende la pipeline dal punto del crash invece di rifare tutto ─

def _load_checkpoint() -> dict:
    """Checkpoint valido solo se creato oggi — altrimenti si riparte da zero."""
    try:
        with open(CHECKPOINT_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if data.get("date") != today or not isinstance(data.get("steps"), list):
        return {}
    return data


def _save_checkpoint(cp: dict) -> None:
    cp["date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        tmp = CHECKPOINT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cp, f, indent=2, ensure_ascii=False)
        os.replace(tmp, CHECKPOINT_PATH)
    except OSError as e:
        _log(f"  ! Checkpoint non salvato: {e}")


def _clear_checkpoint() -> None:
    try:
        os.remove(CHECKPOINT_PATH)
    except OSError:
        pass


def _scarica_tutte_le_clips(keywords: list) -> dict:
    """Scarica abbastanza clip distinte da coprire tutti i segmenti del video."""
    return scarica_pool_clips(keywords, AUDIO_PATH, log=_log)


def run_pipeline(state: dict, dry_run: bool = False) -> None:
    os.makedirs("output", exist_ok=True)
    _log("━━━ PIPELINE AVVIATA ━━━")

    # ── ANALYTICS ──────────────────────────────────────────────────────────
    _segna_step("analytics")
    _log("📊 [1/7] Analytics — lettura performance canale...")
    notify_step("strategy", "Analizzo le performance del canale...")
    try:
        performance = leggi_performance(n_video=10)
        _log(f"  → {len(performance)} video analizzati")
        if performance:
            for v in performance:
                _log(f"     · {v['title'][:50]} — {v['views']} views, {v['likes']} likes")
            notify_analytics(performance)
            if state.get("auto_scheduling"):
                _update_best_hours(state, performance)
        else:
            _log("  → Nessun video precedente (canale nuovo)")
    except Exception as e:
        _log(f"  → Skipped: {e}")
        performance = []

    strategy = calcola_strategia(performance)
    _check_abort()
    _log(f"  → Strategia: {strategy.get('notes', '')}")

    # ── TOPIC & CONTENUTO ───────────────────────────────────────────────────
    _segna_step("contenuto")
    _log("🧠 [2/7] Topic & Contenuto — generazione script...")
    cp = _load_checkpoint()
    if "content" in cp.get("steps", []) and cp.get("content"):
        topic = cp.get("topic", "")
        content = cp["content"]
        _log(f"  → Ripreso da checkpoint: {topic}")
    else:
        cp = {"steps": []}
        queue = state.get("topic_queue", [])
        dalla_coda = bool(queue)
        if queue:
            topic = queue.pop(0)
            state["topic_queue"] = queue
            # persisti subito la rimozione dalla coda (ricaricando lo state più
            # recente: il bot può aver scritto nel frattempo) — altrimenti lo
            # stesso topic verrebbe riprodotto a ogni run.
            fresh = _load_state()
            fresh_queue = fresh.get("topic_queue", [])
            if topic in fresh_queue:
                fresh_queue.remove(topic)
                fresh["topic_queue"] = fresh_queue
                _save_state(fresh)
            _log(f"  → Topic dalla coda: {topic}")
        else:
            _log("  → Coda vuota, genero topic con AI...")
            topic = genera_topic(strategy=strategy, recent_topics=state.get("recent_topics", []))
            _log(f"  → Topic generato: {topic}")

        notify_start(topic)
        _log("  → Generazione script in corso (può richiedere 30-60s)...")
        content = genera_contenuto(topic, strategy=strategy, topic_esplicito=dalla_coda)
        cp.update({"topic": topic, "content": content, "steps": ["content"]})
        _save_checkpoint(cp)
    _log(f"  → Titolo: {content['title']}")
    _log(f"  → Tag: {', '.join(content.get('tags', [])[:5])}...")
    _log(f"  → Keyword video: {', '.join(content.get('video_keywords', [])[:4])}...")
    script_words = len(content.get('script', '').split())
    _check_abort()
    _log(f"  → Script: ~{script_words} parole")

    # ── AUDIO ───────────────────────────────────────────────────────────────
    _segna_step("audio")
    _log("🎙️ [3/7] Audio — sintesi vocale con Edge TTS...")
    if "audio" in cp["steps"] and os.path.exists(AUDIO_PATH):
        _log(f"  → Ripreso da checkpoint: {AUDIO_PATH}")
    else:
        notify_step("audio", f"Sintetizzo audio: <i>{_h(content['title'])}</i>")
        genera_audio(content["script"], AUDIO_PATH)
        cp["steps"].append("audio")
        _save_checkpoint(cp)
    size = os.path.getsize(AUDIO_PATH) // 1024
    _check_abort()
    _log(f"  → Audio salvato: {AUDIO_PATH} ({size} KB)")

    # ── CLIP ────────────────────────────────────────────────────────────────
    _segna_step("clips")
    _log("🎞️ [4/7] Clip — scarico video da Pexels...")
    keywords = content.get("video_keywords", [])
    clip_paths = {}
    if "clips" in cp["steps"] and cp.get("clip_paths"):
        cached = {k: p for k, p in cp["clip_paths"].items() if os.path.exists(p)}
        if cached:
            clip_paths = cached
            _log(f"  → Ripreso da checkpoint: {len(clip_paths)} clip")
    if not clip_paths:
        notify_step("clips", "Scarico clip video da Pexels...")
        clip_paths = _scarica_tutte_le_clips(keywords)
        cp["clip_paths"] = clip_paths
        if "clips" not in cp["steps"]:
            cp["steps"].append("clips")
        _save_checkpoint(cp)

    # le clip appena scaricate servono al montaggio: mai potarle (LRU per mtime
    # le cancellerebbe perche' scaricate per prime → ffprobe crash al render)
    liberati = pulisci_cache(protetti=set(clip_paths.values()))
    if liberati:
        _log(f"  → Cache potata: {liberati:.0f} MB liberati (tetto {os.environ.get('MAX_CACHE_MB', '2000')} MB)")
    if not clip_paths:
        msg = "Nessuna clip scaricata — pipeline interrotta"
        _log(f"  ✗ ERRORE: {msg}")
        notify_error(msg)
        raise RuntimeError(msg)

    # ── MONTAGGIO ───────────────────────────────────────────────────────────
    _check_abort()
    _segna_step("montaggio")
    _log("⚙️ [5/7] Montaggio — rendering video (5-15 min)...")
    # guardia disco: il render riempie output/ di file temporanei. Se lo spazio
    # e' poco, prova a liberare la cache, altrimenti aborta con messaggio chiaro
    # invece di crashare a meta' rendering (OSError 28: No space left on device).
    pulisci_temp_render("output")
    try:
        assicura_spazio(work_dir="output", protetti=set(clip_paths.values()))
    except RuntimeError as e:
        _log(f"  ✗ {e}")
        notify_error(str(e))
        raise
    _log(f"  → Spazio disco: {spazio_libero_gb('output'):.1f} GB liberi")
    if "montage" in cp["steps"] and os.path.exists(VIDEO_PATH):
        _log(f"  → Ripreso da checkpoint: {VIDEO_PATH}")
    else:
        notify_step("rendering", "Montaggio in corso (5-15 min)...")

        # progresso su Telegram a 25/50/75% e in state.json (per /status):
        # senza, durante il render l'utente resta 15 minuti al buio
        _soglie_notifica = [25, 50, 75]

        def _avanzamento(pct, eta):
            _segna_step("montaggio", pct)
            if _soglie_notifica and pct >= _soglie_notifica[0]:
                while _soglie_notifica and pct >= _soglie_notifica[0]:
                    _soglie_notifica.pop(0)
                notify_step("rendering", f"Rendering {pct}% — ETA {eta}")

        monta_video(
            AUDIO_PATH, list(clip_paths.keys()), clip_paths, VIDEO_PATH,
            mood=content.get("mood"),
            on_progress=_avanzamento,
            captions_text=content.get("script"),
        )
        cp["steps"].append("montage")
        _save_checkpoint(cp)
    _check_abort()
    size = os.path.getsize(VIDEO_PATH) // (1024 * 1024)
    _log(f"  → Video salvato: {VIDEO_PATH} ({size} MB)")

    # ── THUMBNAIL ───────────────────────────────────────────────────────────
    _segna_step("thumbnail")
    _log("🖼️ [6/7] Thumbnail — generazione con AI...")
    if "thumbnail" in cp["steps"] and os.path.exists(THUMB_PATH):
        _log(f"  → Ripresa da checkpoint: {THUMB_PATH}")
    else:
        notify_step("thumbnail", "Genero la thumbnail con AI...")
        genera_thumbnail(
            content["title"], THUMB_PATH,
            mood=content.get("mood"),
            thumbnail_description=content.get("thumbnail_description"),
            thumbnail_phrase=content.get("thumbnail_phrase"),
            thumbnail_font_size=content.get("thumbnail_font_size"),
        )
        cp["steps"].append("thumbnail")
        _save_checkpoint(cp)
    _log(f"  → Thumbnail salvata: {THUMB_PATH}")

    # ── UPLOAD ──────────────────────────────────────────────────────────────
    _check_abort()
    _segna_step("upload")
    if dry_run:
        _segna_step("dry_run")
        _log("DRY RUN: upload saltato. Video e thumbnail sono pronti in output/.")
        _pulisci_status_pipeline()
        return
    _log("📤 [7/7] Upload — caricamento su YouTube...")
    if cp.get("video_id"):
        # upload già riuscito in un run precedente crashato subito dopo:
        # NON ricaricare lo stesso video (duplicato sul canale)
        video_id = cp["video_id"]
        publish_label = cp.get("publish_time", "già programmata")
        _log(f"  → Upload già completato (checkpoint): https://youtu.be/{video_id}")
    else:
        notify_step("upload", "Upload su YouTube in corso...")
        run_index = _runs_today(state)
        immediate = bool(state.get("publish_immediately"))
        publish_at = None if immediate else calcola_publish_slots(state, run_index)
        publish_label = "immediata" if immediate else publish_at.strftime("%d/%m/%Y %H:%M UTC")
        if immediate:
            _log("  -> Pubblicazione immediata")
        else:
            _log(f"  → Programmato per: {publish_label}")
        video_id = pubblica_video(
            VIDEO_PATH,
            THUMB_PATH,
            content,
            publish_at,
            immediate=immediate,
            privacy_status=os.environ.get("YOUTUBE_IMMEDIATE_PRIVACY", "public") if immediate else "private",
        )
        cp["video_id"] = video_id
        cp["publish_time"] = publish_label
        _save_checkpoint(cp)
    _log(f"  → ✓ https://youtu.be/{video_id}")

    # sottotitoli: best-effort, un fallimento qui non deve bloccare la pipeline
    if "captions" not in cp["steps"]:
        try:
            from moduli.montaggio import _media_duration
            from moduli.sottotitoli import genera_srt
            from moduli.pubblica import carica_sottotitoli
            from moduli.preferenze import carica as _carica_pref
            srt = genera_srt(content.get("script", ""), _media_duration(AUDIO_PATH),
                             "output/sottotitoli.srt")
            if srt:
                lingua = "it" if _carica_pref().get("lingua") == "italian" else "en"
                carica_sottotitoli(video_id, srt, language=lingua)
                cp["steps"].append("captions")
                _save_checkpoint(cp)
                _log("  → Sottotitoli caricati")
        except Exception as e:
            _log(f"  → Sottotitoli saltati: {e}")

    notify_done(
        title=content["title"],
        video_id=video_id,
        publish_time=publish_label,
        thumb_path=THUMB_PATH,
    )

    # Ricarica lo state più recente (il bot Telegram può aver scritto durante la pipeline)
    # e applica solo le modifiche prodotte dalla pipeline — evita lost-update.
    _segna_step("salvataggio")
    fresh = _load_state()
    recent = fresh.get("recent_topics", [])
    if topic:
        recent.insert(0, topic)
    fresh["recent_topics"] = recent[:10]
    fresh["last_run_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fresh["video_ids"] = ([video_id] + [v for v in fresh.get("video_ids", []) if v != video_id])[:20]
    fresh.pop("force_run", None)
    fresh.pop("publish_immediately", None)
    fresh.pop("abort_pipeline", None)
    _increment_runs_today(fresh)
    fresh.pop("pipeline_status", None)
    _save_state(fresh)
    _clear_checkpoint()
    _log("━━━ PIPELINE COMPLETATA ━━━\n")


def _update_best_hours(state: dict, performance: list) -> None:
    """Pick best publish hours from analytics data."""
    # Use top-views hours; fallback to spread across day
    videos_per_day = state.get("videos_per_day", DEFAULT_VIDEOS_PER_DAY)
    # Smart defaults per slot count — production/morning/evening spread
    spreads = PUBLISH_SPREADS
    scored: dict[int, list[float]] = {}
    for video in performance:
        hour = video.get("published_hour_utc")
        if hour is None:
            continue
        views = float(video.get("views") or 0)
        ctr = float(video.get("ctr_percent") or 0)
        duration = max(float(video.get("duration_seconds") or 1), 1.0)
        retention = float(video.get("avg_view_duration_seconds") or 0) / duration * 100
        score = views + (ctr * 25) + (retention * 5)
        scored.setdefault(int(hour), []).append(score)

    if scored:
        ranked = sorted(
            ((sum(scores) / len(scores), hour) for hour, scores in scored.items()),
            reverse=True,
        )
        hours = sorted(hour for _, hour in ranked[:videos_per_day])
        fallback = [h for h in spreads.get(videos_per_day, [20]) if h not in hours]
        hours = sorted((hours + fallback)[:videos_per_day])
    else:
        hours = spreads.get(videos_per_day, [20])
    state["best_hours_utc"] = hours
    # persisti subito: lo state passato alla pipeline non viene risalvato
    # integralmente a fine run (pattern anti lost-update)
    fresh = _load_state()
    if fresh.get("best_hours_utc") != hours:
        fresh["best_hours_utc"] = hours
        _save_state(fresh)
    print(f"  Auto-scheduling: best hours set to {hours}")


def _cleanup_stale_state() -> None:
    """Rimuove flag che non devono persistere tra riavvii."""
    state = _load_state()
    changed = False
    if state.pop("force_run", None) is not None:
        _log("  → force_run rimosso (stale da sessione precedente)")
        changed = True
    if state.pop("publish_immediately", None) is not None:
        _log("  -> publish_immediately rimosso (stale da sessione precedente)")
        changed = True
    if state.pop("abort_pipeline", None) is not None:
        _log("  -> abort_pipeline rimosso (stale da sessione precedente)")
        changed = True
    if state.pop("pipeline_status", None) is not None:
        _log("  -> pipeline_status rimosso (stale da sessione precedente)")
        changed = True
    # rimuovi topic "…" o vuoti finiti in coda per errore
    queue = state.get("topic_queue", [])
    clean_queue = [t for t in queue if t.strip() and t.strip() not in ("…", "...")]
    if len(clean_queue) != len(queue):
        _log(f"  → Rimossi {len(queue) - len(clean_queue)} topic non validi dalla coda")
        state["topic_queue"] = clean_queue
        changed = True
    # resetta runs_today se è un giorno diverso
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    runs = state.get("runs_today", {})
    if runs and today not in runs:
        state["runs_today"] = {}
        changed = True
    if changed:
        _save_state(state)
    # solo i primi topic: la coda intera erano 10+ righe lunghissime di rumore
    # a ogni avvio, il conteggio completo lo stampa gia' `main()`
    _coda = state.get("topic_queue", [])
    if _coda:
        _anteprima = " | ".join(t[:60] for t in _coda[:3])
        _resto = f" (+{len(_coda) - 3} altri)" if len(_coda) > 3 else ""
        _log(f"  Topic in coda dopo pulizia: {len(_coda)} — {_anteprima}{_resto}")
    else:
        _log("  Topic in coda dopo pulizia: 0")
    # manutenzione disco al boot: temp orfani + cache sotto soglia
    n_temp = pulisci_temp_render("output")
    if n_temp:
        _log(f"  → Rimossi {n_temp} render temporanei orfani")
    # proteggi le clip del checkpoint: servono se la run interrotta riprende
    _cp_boot = _load_checkpoint()
    liberati = pulisci_cache(protetti=set((_cp_boot.get("clip_paths") or {}).values()))
    if liberati:
        _log(f"  → Cache potata: {liberati:.0f} MB liberati")
    _log(f"  Spazio disco libero: {spazio_libero_gb('.'):.1f} GB")


def main():
    # Il daemon si ferma con Ctrl+C — lo annuncia anche la CLI. Senza questo
    # handler l'utente si prendeva un traceback di KeyboardInterrupt invece di
    # un'uscita pulita, sia con `python agent.py` sia con `tube-assistant start`.
    try:
        _main()
    except KeyboardInterrupt:
        # solo ASCII: l'interruzione puo' arrivare prima di forza_utf8() e su
        # Windows con stdout cp1252 un box-drawing qui diventerebbe un
        # UnicodeEncodeError al posto del messaggio di uscita
        print("\n--- Agente fermato (Ctrl+C) ---", flush=True)
        sys.exit(130)


def _avvisa_configurazione_rotta() -> None:
    """Preflight all'avvio: i problemi di configurazione vanno detti subito.

    I controlli esistevano gia' ma solo dentro `tube-assistant preflight`, un
    comando che nessuno lancia dopo il primo giorno. Cosi' l'API Analytics
    spenta e' rimasta invisibile per settimane mentre la strategia continuava a
    imparare da CTR e retention assenti.
    """
    try:
        from moduli.preflight import run_checks
        problemi = [c for c in run_checks(".") if not c["ok"]]
    except Exception as e:
        _log(f"  Preflight non eseguibile: {e}")
        return
    if not problemi:
        return
    righe = [f"{c['name']}: {c['detail']}" for c in problemi]
    for riga in righe:
        _log(f"  ⚠ {riga}")
    notify_config(righe)


def _main():
    from moduli.logsetup import setup as _log_su_file
    _log_su_file()
    _acquire_pid_lock()
    _log("━━━ YouTube AI Agent avviato ━━━")
    _cleanup_stale_state()

    state = _load_state()
    trigger_hours = _get_trigger_hours(state)
    vpd = state.get("videos_per_day", DEFAULT_VIDEOS_PER_DAY)
    _log(f"  Video al giorno: {vpd} | Trigger: {trigger_hours} UTC")
    _log(f"  Topic in coda: {len(state.get('topic_queue', []))}")
    _avvisa_configurazione_rotta()

    bot_thread = start_bot()
    bot_restarts = 0

    last_attempt = None  # (data, ora) dell'ultimo trigger schedulato

    while True:
        state = _load_state()
        now = datetime.now(timezone.utc)

        # watchdog: senza bot l'agente resta vivo ma non più controllabile
        # da Telegram — riavvialo (max 3 volte, poi avvisa e arrenditi)
        if bot_thread is not None and not bot_thread.is_alive():
            if bot_restarts < 3:
                bot_restarts += 1
                _log(f"Bot Telegram terminato — riavvio ({bot_restarts}/3)...")
                notify_error(f"Bot Telegram crashato — riavvio automatico ({bot_restarts}/3).")
                bot_thread = start_bot()
            else:
                _log("Bot Telegram crashato troppe volte — stop riavvii automatici.")
                notify_error(
                    "Bot Telegram crashato ripetutamente: controllo remoto disattivato. "
                    "La pipeline continua da sola; riavvia l'agente per ripristinare il bot."
                )
                bot_thread = None

        if now.minute == 0:
            trigger_hours = _get_trigger_hours(state)
            vpd = state.get("videos_per_day", DEFAULT_VIDEOS_PER_DAY)
            done = _runs_today(state)
            print(f"[{now.strftime('%Y-%m-%d %H:%M UTC')}] tick — {done}/{vpd} video oggi — trigger: {trigger_hours}")

        if _should_run(state, now, last_attempt):
            forced = bool(state.get("force_run"))
            if not forced:
                # segna il tentativo PRIMA di partire: un crash non fa
                # ripartire la pipeline in loop nella stessa ora
                last_attempt = (now.strftime("%Y-%m-%d"), now.hour)
            _log(f"Trigger ({'FORCED' if forced else 'scheduled'}) — avvio pipeline")
            # Pulisci force_run PRIMA di partire: se la pipeline crasha,
            # il flag è già rimosso e il daemon non entra in loop infinito.
            state.pop("force_run", None)
            state.pop("abort_pipeline", None)
            _save_state(state)
            try:
                run_pipeline(state)
            except PipelineAbort:
                state = _load_state()
                state.pop("abort_pipeline", None)
                _save_state(state)
                notify_error("Pipeline interrotta su richiesta Telegram.")
            except Exception:
                err = traceback.format_exc()
                _log(err)
                last_line = err.strip().split("\n")[-1][:300]
                spiegazione = _spiega_errore(err)
                notify_error(
                    f"Crash allo step {_pipeline_step}\n\n"
                    f"{last_line}\n\n"
                    + (spiegazione or "Controlla i log. Usa /forza per riprovare.")
                )
            finally:
                _pulisci_status_pipeline()
            state = _load_state()

        time.sleep(60)


if __name__ == "__main__":
    main()
