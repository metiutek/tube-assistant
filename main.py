import os
import sys
from dotenv import load_dotenv

load_dotenv()

from moduli.cervello import genera_topic, genera_contenuto
from moduli.audio import genera_audio
from moduli.asset import scarica_pool_clips
from moduli.montaggio import monta_video
from moduli.thumbnail import genera_thumbnail
from moduli.pubblica import pubblica_video
from moduli.analytics import leggi_performance
from moduli.strategia import calcola_strategia

AUDIO_PATH = "output/narration.mp3"
VIDEO_PATH = "output/output_finale.mp4"
THUMB_PATH = "output/thumbnail.jpg"


def _topic_dalla_coda(consuma: bool = True):
    """Primo topic di state.json, come fa il daemon.

    Senza questo, `tube-assistant run` generava un topic a caso ignorando
    quelli messi in coda con /topic o a mano: nella modalita' one-shot la
    coda sembrava non servire a nulla.

    `consuma=False` lo legge soltanto: in dry-run non si pubblica niente, e
    togliere comunque il topic dalla coda faceva perdere all'utente un
    argomento pianificato a ogni prova.
    """
    try:
        from moduli.state_io import load_state, save_state
    except Exception:
        return None
    stato = load_state()
    coda = stato.get("topic_queue") or []
    if not coda:
        return None
    topic = coda[0]
    if consuma:
        stato["topic_queue"] = coda[1:]
        save_state(stato)
    return topic


def _recent_topics() -> list:
    """Topic gia' usati di recente, per non rigenerare sempre lo stesso."""
    try:
        from moduli.state_io import load_state
        return load_state().get("recent_topics", []) or []
    except Exception:
        return []


def _registra_pubblicazione(topic: str, video_id: str) -> None:
    """Aggiorna state.json dopo un upload one-shot.

    Il daemon lo fa gia'; `tube-assistant run` invece pubblicava senza
    lasciare traccia: `/status` non vedeva il video, la deduplica dei topic
    restava cieca e il tetto giornaliero non veniva contato.
    """
    try:
        from datetime import datetime, timezone
        from moduli.state_io import load_state, save_state
    except Exception:
        return
    try:
        stato = load_state()
        if topic:
            recent = [t for t in stato.get("recent_topics", []) if t != topic]
            stato["recent_topics"] = ([topic] + recent)[:10]
        stato["video_ids"] = ([video_id] + [v for v in stato.get("video_ids", []) if v != video_id])[:20]
        oggi = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        stato["last_run_date"] = oggi
        runs = stato.get("runs_today", {})
        runs = {oggi: runs.get(oggi, 0) + 1}  # runs_today tiene un solo giorno
        stato["runs_today"] = runs
        save_state(stato)
    except Exception as e:
        print(f"  [!] state.json non aggiornato: {e}")


def _carica_sottotitoli(video_id: str, content: dict) -> None:
    """Genera l'SRT dallo script e lo carica sul video appena pubblicato.

    Il daemon lo faceva gia'; `tube-assistant run` no: gli stessi contenuti
    uscivano con o senza sottotitoli a seconda di come era stata avviata la
    pipeline. Best-effort: un errore qui non deve invalidare un upload
    riuscito.
    """
    try:
        from moduli.montaggio import _media_duration
        from moduli.sottotitoli import genera_srt
        from moduli.pubblica import carica_sottotitoli
        from moduli.preferenze import carica as _carica_pref
        srt = genera_srt(content.get("script", ""), _media_duration(AUDIO_PATH),
                         "output/sottotitoli.srt")
        if not srt:
            return
        lingua = "it" if _carica_pref().get("lingua") == "italian" else "en"
        carica_sottotitoli(video_id, srt, language=lingua)
        print("  Subtitles uploaded")
    except Exception as e:
        print(f"  Subtitles skipped: {e}")


def run(dry_run: bool = False):
    # stessa protezione encoding del daemon: senza, una freccia nei log
    # fa esplodere la pipeline su Windows con output rediretto
    from moduli.logsetup import forza_utf8
    forza_utf8()
    os.makedirs("output", exist_ok=True)

    print("=== [Analytics] Reading channel performance ===")
    try:
        performance = leggi_performance(n_video=5)
        print(f"  {len(performance)} videos analyzed")
    except Exception as e:
        print(f"  Skipped (new channel): {e}")
        performance = []

    print("=== [Strategy] Adapting strategy ===")
    strategy = calcola_strategia(performance)
    print(f"  {strategy.get('notes', '')}")

    print("\n=== [A] Generating topic & content ===")
    topic = _topic_dalla_coda(consuma=not dry_run)
    dalla_coda = bool(topic)
    if topic:
        suffisso = " (dry-run: resta in coda)" if dry_run else ""
        print(f"  Topic dalla coda: {topic}{suffisso}")
    else:
        topic = genera_topic(strategy=strategy, recent_topics=_recent_topics())
        print(f"  Topic generato: {topic}")
    content = genera_contenuto(topic, strategy=strategy, topic_esplicito=dalla_coda)
    print(f"  Title: {content['title']}")

    print("\n=== [B] Generating audio ===")
    genera_audio(content["script"], AUDIO_PATH)
    print(f"  Saved: {AUDIO_PATH}")

    print("\n=== [C] Fetching video clips ===")
    clip_paths = scarica_pool_clips(content.get("video_keywords", []), AUDIO_PATH)

    if not clip_paths:
        print("ERROR: No clips downloaded. Aborting.")
        sys.exit(1)

    print("\n=== [D] Rendering video ===")
    from moduli.manutenzione import (
        assicura_spazio, pulisci_cache, pulisci_temp_render, spazio_libero_gb,
    )
    pulisci_temp_render("output")
    # `protetti`: senza, la pulizia cache puo' cancellare le clip appena
    # scaricate (sono le piu' vecchie per mtime) e il render crasha su ffprobe
    protetti = set(clip_paths.values())
    pulisci_cache(protetti=protetti)
    assicura_spazio(work_dir="output", protetti=protetti)
    print(f"  Disk free: {spazio_libero_gb('output'):.1f} GB")
    monta_video(AUDIO_PATH, list(clip_paths.keys()), clip_paths, VIDEO_PATH,
                mood=content.get("mood"), captions_text=content.get("script"))
    print(f"  Saved: {VIDEO_PATH}")

    print("\n=== [E] Generating thumbnail ===")
    genera_thumbnail(
        content["title"], THUMB_PATH,
        mood=content.get("mood"),
        thumbnail_description=content.get("thumbnail_description"),
        thumbnail_phrase=content.get("thumbnail_phrase"),
        thumbnail_font_size=content.get("thumbnail_font_size"),
    )
    print(f"  Saved: {THUMB_PATH}")

    if dry_run:
        print("\n=== [F] Dry run ===")
        print("  Upload skipped. Video and thumbnail are ready in output/.")
        return

    print("\n=== [F] Publishing to YouTube ===")
    video_id = pubblica_video(VIDEO_PATH, THUMB_PATH, content)
    _registra_pubblicazione(topic, video_id)
    _carica_sottotitoli(video_id, content)
    print(f"  Done: https://youtu.be/{video_id}")


if __name__ == "__main__":
    run()
