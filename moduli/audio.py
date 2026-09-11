import os
import shutil
import subprocess
import sys
import tempfile

from moduli.ffmpeg_utils import ffmpeg_path as _ffmpeg

VOICE_DEFAULT = "en-US-GuyNeural"
TTS_TIMEOUT = 45
CHUNK_WORDS = 300


def _split_chunks(text: str, max_words: int = CHUNK_WORDS) -> list[str]:
    sentences = text.replace("\n", " ").split(". ")
    chunks, current, count = [], [], 0
    for s in sentences:
        parole = s.split()
        # una frase piu' lunga di max_words (script senza punteggiatura) finiva
        # in un unico chunk gigante che sforava il timeout TTS
        pezzi = ([" ".join(parole[i:i + max_words]) for i in range(0, len(parole), max_words)]
                 if len(parole) > max_words else [s])
        for pezzo in pezzi:
            w = len(pezzo.split())
            if count + w > max_words and current:
                chunks.append(". ".join(current) + ".")
                current, count = [], 0
            current.append(pezzo)
            count += w
    if current:
        chunks.append(". ".join(current))
    return [c for c in chunks if c.strip(" .")]


def _tts_worker(text: str, output_path: str, voice: str) -> None:
    import asyncio
    import edge_tts

    async def _run():
        communicate = edge_tts.Communicate(text, voice)
        await asyncio.wait_for(communicate.save(output_path), timeout=TTS_TIMEOUT - 5)

    asyncio.run(_run())


# Radice del progetto (la cartella che CONTIENE `moduli/`), non la directory
# di lavoro: il subprocess TTS faceva `sys.path.insert(0, '.')` e quindi
# trovava il pacchetto solo se lanciato dalla root del sorgente. Installato
# come tool con una workspace separata — lo scenario descritto nel README —
# ogni chunk falliva con ModuleNotFoundError e la pipeline ripiegava in
# silenzio su gTTS, ignorando del tutto la preferenza `tts_voce`.
_RADICE_PROGETTO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _edge_tts_chunk(text: str, output_path: str, voice: str) -> bool:
    proc = subprocess.Popen(
        [sys.executable, "-c",
         f"import sys; sys.path.insert(0, {_RADICE_PROGETTO!r});"
         f"from moduli.audio import _tts_worker;"
         f"_tts_worker({text!r}, {output_path!r}, {voice!r})"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
    )
    try:
        _, stderr = proc.communicate(timeout=TTS_TIMEOUT)
        if proc.returncode == 0:
            return True
        err = stderr.decode(errors="replace").strip()
        print(f"[TTS] Edge TTS errore chunk: {err}", flush=True)
        return False
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        print(f"[TTS] Edge TTS TIMEOUT {TTS_TIMEOUT}s su chunk", flush=True)
        return False


def _concat_audio(parts: list[str], output_path: str) -> None:
    if len(parts) == 1:
        shutil.move(parts[0], output_path)
        return
    list_file = output_path + ".list.txt"
    try:
        ff = _ffmpeg()
        with open(list_file, "w", encoding="utf-8") as f:
            for p in parts:
                f.write(f"file '{os.path.abspath(p)}'\n")
        # -c copy tiene i frame MP3 originali ma ricostruisce l'header: senza
        # ffmpeg la durata dichiarata resta quella del primo chunk
        subprocess.run(
            [ff, "-y", "-f", "concat", "-safe", "0", "-i", list_file,
             "-c", "copy", output_path],
            check=True, capture_output=True,
        )
        return
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        # Il byte-concat grezzo usato prima come fallback produce un MP3 la cui
        # durata dichiarata e' quella del PRIMO chunk: il montaggio calcolava
        # i segmenti su quel valore e il video usciva lungo una frazione
        # dell'audio. Meglio fallire qui con un errore chiaro.
        raise RuntimeError(
            "Impossibile unire i chunk audio: ffmpeg non disponibile o concat "
            "fallito. Installa ffmpeg e aggiungilo al PATH (oppure imposta "
            f"FFMPEG_PATH nel .env). Dettaglio: {e}"
        ) from e
    finally:
        try:
            os.remove(list_file)
        except OSError:
            pass


def _edge_tts(text: str, output_path: str, voice: str) -> bool:
    chunks = _split_chunks(text)
    print(f"[TTS] Edge TTS — voce: {voice} — {len(chunks)} chunk da ~{CHUNK_WORDS} parole", flush=True)
    tmp_parts = []
    try:
        for i, chunk in enumerate(chunks):
            tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=f"_chunk{i}.mp3")
            tmp = tmp_file.name
            tmp_file.close()
            tmp_parts.append(tmp)
            ok = False
            for attempt in range(2):
                print(f"[TTS]   chunk {i+1}/{len(chunks)} tentativo {attempt+1}/2...", flush=True)
                if _edge_tts_chunk(chunk, tmp, voice):
                    ok = True
                    break
            if not ok:
                print(f"[TTS]   chunk {i+1} fallito definitivamente", flush=True)
                return False
        _concat_audio(tmp_parts, output_path)
        return True
    finally:
        for p in tmp_parts:
            if os.path.exists(p) and p != output_path:
                os.remove(p)


def _gtts(text: str, output_path: str, lang: str = "en") -> bool:
    try:
        from gtts import gTTS
        print(f"[TTS] Uso gTTS (Google) come fallback — lingua: {lang}...", flush=True)
        tts = gTTS(text=text, lang=lang, slow=False)
        tts.save(output_path)
        return True
    except Exception as e:
        print(f"[TTS] gTTS errore: {e}", flush=True)
        return False


def _load_voice() -> str:
    try:
        from moduli.preferenze import carica
        return carica().get("tts_voce", VOICE_DEFAULT)
    except Exception:
        return VOICE_DEFAULT


def _gtts_lang(voice: str) -> str:
    """Estrae codice lingua da voice name Edge TTS (es. it-IT-DiegoNeural → it)."""
    parts = voice.split("-")
    return parts[0] if parts else "en"


def _audio_valido(output_path: str, parole: int) -> bool:
    """Un MP3 vuoto o troncato passava inosservato fino al montaggio, dove
    produceva un video di pochi secondi. Meglio accorgersene subito."""
    try:
        if os.path.getsize(output_path) < 1024:
            return False
    except OSError:
        return False
    try:
        from moduli.montaggio import _media_duration
        durata = _media_duration(output_path)
    except Exception:
        return True  # senza ffprobe non blocchiamo: il file esiste ed e' pieno
    # ~130 parole/minuto: sotto il 40% dell'atteso il file e' troncato
    attesa = parole / 130 * 60
    if attesa > 5 and durata < attesa * 0.4:
        print(f"[TTS] Audio sospetto: {durata:.0f}s per {parole} parole "
              f"(attesi ~{attesa:.0f}s)", flush=True)
        return False
    return True


# Edge TTS e gTTS restituiscono un mp3 mono a 24 kHz con media intorno ai
# -26 dB: passato cosi' com'e' al montaggio, il video finale suona molto piu'
# piano di qualunque altro su YouTube (che consegna a circa -14 LUFS).
LOUDNESS_TARGET_LUFS = os.environ.get("AUDIO_LUFS", "-14")
NORM_SAMPLE_RATE = os.environ.get("AUDIO_SAMPLE_RATE", "48000")
NORM_CHANNELS = os.environ.get("AUDIO_CHANNELS", "2")
NORM_BITRATE = os.environ.get("AUDIO_BITRATE", "192k")


def _normalizza(path: str) -> None:
    """Porta la narrazione al volume di consegna, in stereo a 48 kHz.

    Best-effort: se ffmpeg manca o il filtro fallisce si tiene il file
    originale — un audio piano e' comunque meglio di nessun audio.
    """
    tmp = path + ".norm.mp3"
    try:
        subprocess.run(
            [_ffmpeg(), "-y", "-nostdin", "-v", "error", "-i", path,
             "-af", f"loudnorm=I={LOUDNESS_TARGET_LUFS}:TP=-1.5:LRA=11",
             "-ar", NORM_SAMPLE_RATE, "-ac", NORM_CHANNELS,
             "-b:a", NORM_BITRATE, tmp],
            check=True, capture_output=True, stdin=subprocess.DEVNULL,
            timeout=600,
        )
        os.replace(tmp, path)
        print(f"[TTS] Volume normalizzato a {LOUDNESS_TARGET_LUFS} LUFS "
              f"({NORM_SAMPLE_RATE} Hz, {NORM_CHANNELS} canali)", flush=True)
    except Exception as e:
        print(f"[TTS] Normalizzazione saltata ({e}) — audio originale", flush=True)
        try:
            os.remove(tmp)
        except OSError:
            pass


def genera_audio(text: str, output_path: str) -> None:
    voice = _load_voice()
    words = len(text.split())
    print(f"[TTS] Edge TTS avvio — voce: {voice} ({words} parole)...", flush=True)
    if _edge_tts(text, output_path, voice) and _audio_valido(output_path, words):
        _normalizza(output_path)
        print(f"[TTS] Audio salvato: {output_path}", flush=True)
        return

    print("[TTS] Edge TTS fallito — provo gTTS...", flush=True)
    if _gtts(text, output_path, lang=_gtts_lang(voice)) and _audio_valido(output_path, words):
        _normalizza(output_path)
        print(f"[TTS] Audio salvato (gTTS): {output_path}", flush=True)
        return

    raise RuntimeError("TTS fallito: né Edge TTS né gTTS hanno prodotto un audio valido")
