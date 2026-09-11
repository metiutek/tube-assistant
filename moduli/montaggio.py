import os
import glob
import random
import subprocess
import time
import platform
import tempfile
import re

from moduli.ffmpeg_utils import (
    ffmpeg_path as _ffmpeg,
    ffprobe_path as _ffprobe,
    esporta_per_moviepy as _esporta_ffmpeg,
)

# DEVE restare prima di qualsiasi import di moviepy (anche lazy, nelle funzioni)
_esporta_ffmpeg()

SEGMENT_DURATION = 5.0


# Etichetta con cui `asset.scarica_pool_clips` marca le clip generate dall'AI.
# Costano soldi e sono le uniche che mostrano davvero la scena descritta dallo
# script: vanno sui PRIMI segmenti, dove si decide se lo spettatore resta.
PREFISSO_CLIP_AI = "ai#"


def _clip_prioritarie(keywords: list, clip_paths: dict, disponibili: list) -> list:
    ammesse = set(disponibili)
    return [clip_paths[k] for k in keywords
            if str(k).startswith(PREFISSO_CLIP_AI) and clip_paths.get(k) in ammesse]


def _build_clip_sequence(clip_files: list, n_segments: int,
                         prioritarie: list | None = None) -> list:
    """Assegna a ogni segmento una clip distinta. Ogni clip e' usata una sola
    volta finche' il pool non e' esaurito; solo allora si ricomincia (shuffle,
    senza ripetere la clip a cavallo tra un giro e l'altro). Se il pool e'
    grande quanto i segmenti, nessuna clip viene ripetuta nel video.

    `prioritarie` occupa i primi segmenti nell'ordine dato: lasciarle allo
    shuffle significherebbe pagare clip generate dall'AI per poi vederle
    comparire al minuto sette, quando chi doveva andarsene se n'e' gia' andato.
    """
    uniq = list(dict.fromkeys(p for p in clip_files if p))  # dedup file fisici
    if not uniq:
        raise RuntimeError("montaggio: nessuna clip disponibile")
    disponibili = set(uniq)
    prime = [p for p in dict.fromkeys(prioritarie or []) if p in disponibili][:n_segments]
    seq = list(prime)
    pool = []
    last = prime[-1] if prime else None
    for _ in range(n_segments - len(seq)):
        if not pool:
            pool = uniq[:]
            random.shuffle(pool)
            if last is not None and len(pool) > 1 and pool[0] == last:
                pool.append(pool.pop(0))
        clip = pool.pop(0)
        last = clip
        seq.append(clip)
    return seq


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _is_arm_device() -> bool:
    machine = platform.machine().lower()
    return machine in {"aarch64", "arm64", "armv7l", "armv6l"} or machine.startswith("arm")


PI_SAFE_MODE = os.environ.get("PI_SAFE_MODE", "auto").lower()
_PI_LIMITED = PI_SAFE_MODE in {"1", "true", "yes", "on"} or (PI_SAFE_MODE == "auto" and _is_arm_device())

OUTPUT_W = _env_int("VIDEO_WIDTH", 1280 if _PI_LIMITED else 1920)
OUTPUT_H = _env_int("VIDEO_HEIGHT", 720 if _PI_LIMITED else 1080)
OUTPUT_FPS = _env_int("VIDEO_FPS", 24 if _PI_LIMITED else 30)
FFMPEG_THREADS = _env_int("FFMPEG_THREADS", 2 if _PI_LIMITED else 4)
X264_CRF = _env_int("X264_CRF", 28 if _PI_LIMITED else 23)
X264_PRESET = os.environ.get("X264_PRESET", "ultrafast" if _PI_LIMITED else "fast")
USE_FFMPEG_RENDER = os.environ.get("USE_FFMPEG_RENDER", "auto").lower()
_FFMPEG_ONLY = USE_FFMPEG_RENDER in {"1", "true", "yes", "on"} or (
    USE_FFMPEG_RENDER == "auto" and _PI_LIMITED
)
VIDEO_CAPTIONS = os.environ.get("VIDEO_CAPTIONS", "1").lower() not in {"0", "false", "no", "off"}
MAX_KEY_CAPTIONS = _env_int("MAX_KEY_CAPTIONS", 8)
CAPTION_FONT_SIZE = _env_int("CAPTION_FONT_SIZE", 42 if OUTPUT_H >= 720 else 32)
CAPTION_MIN_GAP_SECONDS = _env_int("CAPTION_MIN_GAP_SECONDS", 12)

_GPU_CODEC = None

GPU_PROBE_TIMEOUT = _env_int("GPU_PROBE_TIMEOUT", 25)


def _detect_gpu_codec() -> str:
    """Rileva codec H.264 hardware disponibile (NVENC/AMF/QSV), fallback libx264.

    Il probe gira alla risoluzione REALE di output: quello precedente
    codificava un 64x64 e passava anche su macchine dove poi l'encoder si
    impianta a 1080p, lasciando ffmpeg a `frame=0` per sempre (nessuna delle
    chiamate ha un timeout, quindi il daemon restava appeso senza errore).
    `VIDEO_CODEC=libx264` nel .env salta del tutto il rilevamento.
    """
    global _GPU_CODEC
    if _GPU_CODEC is not None:
        return _GPU_CODEC
    forzato = os.environ.get("VIDEO_CODEC", "").strip()
    if forzato:
        _GPU_CODEC = forzato
        print(f"[montaggio] Codec forzato da VIDEO_CODEC: {forzato}", flush=True)
        return _GPU_CODEC
    try:
        out = subprocess.run(
            [_ffmpeg(), "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=10,
            stdin=subprocess.DEVNULL,
        ).stdout
        for codec in ("h264_nvenc", "h264_amf", "h264_qsv"):
            if codec not in out:
                continue
            try:
                test = subprocess.run(
                    [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin",
                     "-f", "lavfi",
                     "-i", f"color=black:s={OUTPUT_W}x{OUTPUT_H}:r={OUTPUT_FPS}:d=0.5",
                     "-c:v", codec, "-f", "null", "-"],
                    capture_output=True, timeout=GPU_PROBE_TIMEOUT,
                    stdin=subprocess.DEVNULL,
                )
            except subprocess.TimeoutExpired:
                print(f"[montaggio] {codec} bloccato a {OUTPUT_W}x{OUTPUT_H} — scartato",
                      flush=True)
                continue
            if test.returncode == 0:
                _GPU_CODEC = codec
                print(f"[montaggio] GPU codec: {codec}", flush=True)
                return codec
    except Exception as e:
        print(f"[montaggio] Rilevamento GPU fallito: {e}", flush=True)
    _GPU_CODEC = "libx264"
    print("[montaggio] Nessuna GPU compatibile — uso CPU (libx264)", flush=True)
    return _GPU_CODEC
BG_MUSIC_PATH = "assets/background.mp3"
BG_MUSIC_DIR = "assets/music"
BG_MUSIC_VOLUME = 0.1
ALLOWED_MOODS = {"epic", "chill", "mysterious", "upbeat", "tense"}


def _bg_volume() -> float:
    """Volume musica di sottofondo dalla preferenza utente `musica_volume`."""
    try:
        from moduli.preferenze import carica
        vol = float(carica().get("musica_volume", BG_MUSIC_VOLUME))
        return max(0.0, min(1.0, vol))
    except Exception:
        return BG_MUSIC_VOLUME


def _pick_music(mood: str | None) -> str | None:
    if mood and mood.lower() in ALLOWED_MOODS:
        tracks = glob.glob(os.path.join(BG_MUSIC_DIR, mood.lower(), "*.mp3"))
        if tracks:
            return random.choice(tracks)
    tracks = glob.glob(os.path.join(BG_MUSIC_DIR, "*.mp3"))
    if tracks:
        return random.choice(tracks)
    if os.path.exists(BG_MUSIC_PATH):
        return BG_MUSIC_PATH
    return None



def _resize_to_target(src: str) -> str:
    root, ext = os.path.splitext(src)
    target_suffix = f"_{OUTPUT_H}p"
    if root.endswith(target_suffix):
        return src
    for suffix in ("_1080p", "_720p", "_480p"):
        if root.endswith(suffix):
            root = root[:-len(suffix)]
            break
    dst = f"{root}{target_suffix}{ext}"
    if os.path.exists(dst):
        return dst
    codec = _detect_gpu_codec()
    if codec == "h264_nvenc":
        cmd = [_ffmpeg(), '-y', '-nostdin', '-hwaccel', 'cuda', '-i', src,
               '-vf', f'scale={OUTPUT_W}:{OUTPUT_H}:force_original_aspect_ratio=disable',
               '-c:v', codec, '-preset', 'p4', '-cq', '23', '-an', dst]
    elif codec in ("h264_amf", "h264_qsv"):
        cmd = [_ffmpeg(), '-y', '-nostdin', '-i', src,
               '-vf', f'scale={OUTPUT_W}:{OUTPUT_H}:force_original_aspect_ratio=disable',
               '-c:v', codec, '-quality', 'speed', '-an', dst]
    else:
        cmd = [_ffmpeg(), '-y', '-nostdin', '-i', src,
               '-vf', f'scale={OUTPUT_W}:{OUTPUT_H}:force_original_aspect_ratio=disable',
               '-c:v', 'libx264', '-preset', X264_PRESET, '-crf', str(X264_CRF),
               '-threads', str(FFMPEG_THREADS), '-an', dst]
    subprocess.run(cmd, check=True, capture_output=True,
                   timeout=FFMPEG_TIMEOUT, stdin=subprocess.DEVNULL)
    # NON cancellare `src`: il checkpoint della pipeline e il set `protetti`
    # della pulizia cache referenziano il path originale. Cancellandolo, un
    # crash a meta' render rendeva il checkpoint inservibile ("nessuna clip
    # leggibile") proprio nel caso per cui esiste. Lo spazio resta comunque
    # limitato da MAX_CACHE_MB (pulizia LRU in manutenzione.py).
    return dst


def _media_duration(path: str) -> float:
    cmd = [
        _ffprobe(),
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True,
                         timeout=60, stdin=subprocess.DEVNULL).stdout.strip()
    return max(float(out or 0), 0.01)


def _risolvi_variante(path: str) -> str:
    """Se `path` non esiste, cerca una variante ridimensionata dello stesso
    file (`_720p`/`_1080p`): un checkpoint ripreso dopo un render MoviePy
    referenzia il nome originale, non quello convertito."""
    if os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    for suffix in ("_1080p", "_720p", "_480p"):
        if root.endswith(suffix):
            root = root[:-len(suffix)]
            break
    for candidato in (f"{root}_{OUTPUT_H}p{ext}", f"{root}_1080p{ext}",
                      f"{root}_720p{ext}", f"{root}{ext}"):
        if os.path.exists(candidato):
            return candidato
    return path


def _filtra_clip_leggibili(clip_files: list) -> tuple[list, dict]:
    """Scarta clip mancanti o illeggibili (es. cancellate dalla pulizia cache o
    download troncati) invece di far crashare il render. Ritorna i file validi
    e le loro durate gia' misurate (evita un secondo ffprobe per segmento)."""
    validi, durate = [], {}
    for p in dict.fromkeys(_risolvi_variante(p) for p in clip_files if p):
        try:
            durate[p] = _media_duration(p)
            validi.append(p)
        except (subprocess.CalledProcessError, OSError, ValueError):
            print(f"[montaggio] clip illeggibile o mancante, salto: {p}", flush=True)
    return validi, durate


def _concat_file_line(path: str) -> str:
    safe = os.path.abspath(path).replace("\\", "/").replace("'", "'\\''")
    return f"file '{safe}'\n"


def _caption_filter(caption: str | None) -> str:
    vf = [
        f"scale={OUTPUT_W}:{OUTPUT_H}:force_original_aspect_ratio=disable",
        f"fps={OUTPUT_FPS}",
        "format=yuv420p",
    ]
    if caption:
        text = _escape_drawtext(_caption_text(caption))
        font_part = _drawtext_font_part()
        vf.append(
            "drawtext="
            f"{font_part}"
            # expansion=none: senza, un '%' nel testo fa "Stray %" e drawtext
            # NON disegna nulla uscendo con codice 0 — la caption spariva in
            # silenzio ogni volta che la frase conteneva una percentuale
            "expansion=none:"
            f"text='{text}':"
            "x=(w-text_w)/2:"
            "y=h-text_h-72:"
            f"fontsize={CAPTION_FONT_SIZE}:"
            "fontcolor=white:"
            "borderw=3:"
            "bordercolor=black:"
            "box=1:"
            "boxcolor=black@0.55:"
            "boxborderw=22"
        )
    return ",".join(vf)


def _caption_font_path() -> str | None:
    candidates = [
        "assets/font_bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\Arial.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def _drawtext_font_part() -> str:
    path = _caption_font_path()
    if not path:
        return ""
    safe = os.path.abspath(path).replace("\\", "/").replace(":", "\\:")
    return f"fontfile='{safe}':"


def _escape_drawtext(text: str) -> str:
    # Gli apici non sono gestiti qui: dentro text='...' ffmpeg non accetta
    # nessuna forma di escaping per l'apice singolo, quindi `_caption_text`
    # li sostituisce a monte con le varianti tipografiche.
    # '%' NON va escapato: con expansion=none e' un carattere normale, mentre
    # "\%" resta letterale e sporca il testo
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace(",", "\\,")
    )


# apici sostituiti con le varianti tipografiche: visivamente identici ma non
# sono delimitatori per il parser dei filtri ffmpeg (una caption tipo "DON'T"
# faceva fallire il segmento e quindi l'intera pipeline)
_APICI = {"'": "’", '"': "”", "`": "’", "´": "’"}

# parole su cui una caption troncata non deve mai finire (IT + EN)
_PAROLE_APPESE = {
    "THE", "A", "AN", "OF", "TO", "IN", "ON", "FOR", "AND", "OR", "BUT", "WITH",
    "AT", "BY", "FROM", "AS", "THAT", "THIS", "IS", "ARE", "WAS", "WERE", "IT",
    "ITS", "THEIR", "YOUR", "OUR", "HIS", "HER", "MORE", "THAN", "SO", "IF",
    "IL", "LO", "LA", "I", "GLI", "LE", "UN", "UNO", "UNA", "DI", "DEL", "DELLA",
    "DEI", "DELLE", "DA", "DAL", "PER", "CON", "SU", "TRA", "FRA", "CHE", "E",
    "ED", "O", "MA", "SE", "COME", "NON", "AL", "ALLA", "AI", "ALLE", "NEL",
    "NELLA", "SUL", "SULLA", "È",
}


def _max_caption_chars() -> int:
    """Caratteri che stanno su una riga alla risoluzione corrente. drawtext non
    va a capo e non riduce il font: un limite fisso di 78 caratteri finiva
    tagliato ai bordi appena il video non era 1920px."""
    larghezza_utile = OUTPUT_W * 0.92
    larghezza_char = max(1.0, CAPTION_FONT_SIZE * 0.55)
    return max(16, int(larghezza_utile / larghezza_char))


def _caption_text(text: str) -> str:
    cleaned = " ".join(text.replace("\n", " ").split())
    cleaned = cleaned.strip(" -–—:;,.!?\"'").upper()
    for grezzo, tipografico in _APICI.items():
        cleaned = cleaned.replace(grezzo, tipografico)
    words = cleaned.split()
    if len(words) > 10:
        words = words[:10]
    # taglia a parole intere finche' la riga non entra nello schermo
    limite = _max_caption_chars()
    while words and len(" ".join(words)) > limite:
        words.pop()
    # una caption troncata non deve finire su una parola funzionale
    # ("...REDUCTION IN COST FOR THE"): meglio una frase piu' corta ma chiusa
    while len(words) > 3 and words[-1].strip(".,;:!?") in _PAROLE_APPESE:
        words.pop()
    # niente punteggiatura penzolante dopo il taglio ("...THIS SHIFT,")
    return " ".join(words).rstrip(" ,;:-–—")


def _split_sentences(script: str) -> list[str]:
    text = re.sub(r"\s+", " ", script or "").strip()
    if not text:
        return []
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _score_sentence(sentence: str) -> int:
    s = sentence.lower()
    score = 0
    strong_terms = (
        "breakthrough", "future", "cost", "energy", "power", "privacy",
        "smaller", "faster", "cheaper", "smarter", "offline", "cloud",
        "data center", "revolution", "shift", "why", "not", "but",
        "means", "winning", "practical", "access", "efficiency",
    )
    score += sum(2 for term in strong_terms if term in s)
    if re.search(r"\d", sentence):
        score += 4
    if "—" in sentence or "-" in sentence:
        score += 1
    word_count = len(sentence.split())
    if 5 <= word_count <= 18:
        score += 3
    elif word_count > 28:
        score -= 3
    return score


def _select_key_captions(script: str, total_duration: float, n_segments: int) -> dict[int, str]:
    if not VIDEO_CAPTIONS:
        return {}
    sentences = _split_sentences(script)
    candidates = []
    for idx, sentence in enumerate(sentences):
        caption = _caption_text(sentence)
        if len(caption.split()) < 4:
            continue
        candidates.append((_score_sentence(sentence), idx, caption))
    candidates.sort(key=lambda item: (-item[0], item[1]))

    max_by_duration = max(1, int(total_duration // CAPTION_MIN_GAP_SECONDS))
    target_count = min(MAX_KEY_CAPTIONS, max_by_duration, max(1, n_segments // 3), len(candidates))
    if target_count <= 0:
        return {}

    selected = sorted(candidates[:target_count], key=lambda item: item[1])
    captions = {}
    for pos, (_, _, caption) in enumerate(selected):
        segment_index = min(n_segments - 1, int((pos + 0.5) * n_segments / target_count))
        captions[segment_index] = caption
    return captions


def _codec_args(codec: str) -> list[str]:
    if codec == "h264_nvenc":
        # `-rc vbr -b:v 0` serve a rendere `-cq` un vero constant-quality:
        # senza, NVENC applica il proprio rate control di default e produce
        # file enormi (misurato: 304MB contro i 75MB di libx264 a parita' di
        # durata e risoluzione)
        return ["-c:v", codec, "-preset", "p4", "-rc", "vbr",
                "-cq", str(X264_CRF), "-b:v", "0"]
    if codec in ("h264_amf", "h264_qsv"):
        return ["-c:v", codec, "-quality", "speed", "-global_quality", str(X264_CRF)]
    return ["-c:v", "libx264", "-preset", X264_PRESET, "-crf", str(X264_CRF),
            "-threads", str(FFMPEG_THREADS)]


def _codec_segmento() -> str:
    """Codec per un segmento. Il path ffmpeg usava sempre libx264 anche quando
    `_detect_gpu_codec()` aveva trovato un encoder hardware."""
    return "libx264" if _PI_LIMITED else _detect_gpu_codec()


# un segmento dura SEGMENT_DURATION secondi: oltre questo tempo di encoding
# l'encoder e' bloccato, non lento
SEGMENT_TIMEOUT = _env_int("SEGMENT_TIMEOUT", 180)
# concat e mux lavorano sul video intero: piu' generoso, ma mai infinito
FFMPEG_TIMEOUT = _env_int("FFMPEG_TIMEOUT", 1800)


def _render_workers() -> int:
    """Quanti segmenti rendere in parallelo. Un video da 8 minuti sono ~96
    invocazioni ffmpeg: in serie il render e' inutilmente lungo."""
    override = _env_int("RENDER_WORKERS", 0)
    if override > 0:
        return override
    if _PI_LIMITED:
        return 1  # Raspberry & co.: la CPU e' gia' satura con un processo
    if _detect_gpu_codec() != "libx264":
        return 2  # le sessioni encoder hardware sono limitate (NVENC: 2-3)
    cpu = os.cpu_count() or 2
    return max(1, min(4, cpu // max(1, FFMPEG_THREADS)))


def _render_segment_ffmpeg(src: str, dst: str, offset: float, duration: float, caption: str | None = None, src_duration: float | None = None) -> None:
    if src_duration is None:
        src_duration = _media_duration(src)
    vf = _caption_filter(caption)
    if src_duration < duration:
        ingresso = ["-stream_loop", "-1", "-i", src, "-t", f"{duration:.3f}"]
    else:
        # L'offset va limitato a (durata_sorgente - durata_segmento): con
        # `offset % src_duration` il seek poteva cadere a ridosso della fine
        # della clip e ffmpeg produceva un segmento piu' corto del richiesto.
        # Sommati, quei segmenti corti rendevano il video piu' breve della
        # narrazione, che veniva quindi troncata da `-shortest` nel mux.
        massimo = src_duration - duration
        inizio = (offset % massimo) if massimo > 0.05 else 0.0
        ingresso = ["-ss", f"{inizio:.3f}", "-i", src, "-t", f"{duration:.3f}"]

    codec = _codec_segmento()
    ultimo_errore = ""
    # se l'encoder hardware si impianta o fallisce, si riprova su CPU: senza
    # timeout un ffmpeg bloccato appendeva il daemon per sempre, senza errore
    for tentativo in (codec, "libx264"):
        cmd = ([_ffmpeg(), "-y", "-nostdin"] + ingresso + ["-vf", vf, "-an"]
               + _codec_args(tentativo) + ["-movflags", "+faststart", dst])
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=SEGMENT_TIMEOUT, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            ultimo_errore = f"{tentativo}: nessun output entro {SEGMENT_TIMEOUT}s (encoder bloccato)"
            print(f"[montaggio] {ultimo_errore}", flush=True)
        else:
            if result.returncode == 0:
                return
            ultimo_errore = f"{tentativo}: {result.stderr[-1500:]}"
        if tentativo == "libx264":
            break
        print(f"[montaggio] {tentativo} fallito — ripiego su libx264", flush=True)
        _disabilita_gpu()
    raise RuntimeError(f"ffmpeg segment failed:\n{ultimo_errore}")


def _disabilita_gpu() -> None:
    """Dopo un fallimento dell'encoder hardware, i segmenti successivi vanno
    direttamente su CPU invece di ritentare (e fallire) uno per uno."""
    global _GPU_CODEC
    if _GPU_CODEC != "libx264":
        print("[montaggio] Encoder GPU disattivato per questo render", flush=True)
        _GPU_CODEC = "libx264"


# Loudness di consegna. YouTube normalizza a circa -14 LUFS: consegnare piu'
# piano non fa alzare il volume, fa solo suonare il video piu' debole degli
# altri nel feed. Il mp3 di edge-tts arriva a -26 dB di media, mono 24 kHz.
LOUDNESS_TARGET_LUFS = float(os.environ.get("AUDIO_LUFS", "-14"))
AUDIO_SAMPLE_RATE = os.environ.get("AUDIO_SAMPLE_RATE", "48000")
AUDIO_CHANNELS = os.environ.get("AUDIO_CHANNELS", "2")
AUDIO_BITRATE = os.environ.get("AUDIO_BITRATE", "192k")


def _filtro_loudnorm() -> str:
    """loudnorm a passata singola: porta la narrazione al target LUFS.

    `tp=-1.5` lascia margine di picco (niente clipping dopo la ricodifica AAC),
    `lra=11` e' il range dinamico consigliato per il parlato.
    """
    return f"loudnorm=I={LOUDNESS_TARGET_LUFS}:TP=-1.5:LRA=11"


def _mux_audio_ffmpeg(video_path: str, audio_path: str, output_path: str, music_path: str | None, duration: float) -> None:
    # Senza questi parametri l'audio finale ereditava il formato del TTS
    # (mono 24 kHz, ~72 kb/s): il video usciva piu' piano e piu' povero di
    # qualunque altro sul canale.
    codifica_audio = [
        "-c:a", "aac",
        "-b:a", AUDIO_BITRATE,
        "-ar", AUDIO_SAMPLE_RATE,
        "-ac", AUDIO_CHANNELS,
    ]
    if music_path:
        cmd = [
            _ffmpeg(), "-y", "-nostdin",
            "-i", video_path,
            "-i", audio_path,
            "-stream_loop", "-1", "-i", music_path,
            "-filter_complex",
            f"[1:a]volume=1.0[a0];[2:a]volume={_bg_volume()},atrim=0:{duration:.3f}[a1];"
            "[a0][a1]amix=inputs=2:duration=first:dropout_transition=0,"
            f"{_filtro_loudnorm()}[a]",
            "-map", "0:v:0",
            "-map", "[a]",
            "-c:v", "copy",
            *codifica_audio,
            "-shortest",
            "-movflags", "+faststart",
            output_path,
        ]
    else:
        cmd = [
            _ffmpeg(), "-y", "-nostdin",
            "-i", video_path,
            "-i", audio_path,
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-af", _filtro_loudnorm(),
            "-c:v", "copy",
            *codifica_audio,
            "-shortest",
            "-movflags", "+faststart",
            output_path,
        ]
    result = subprocess.run(cmd, capture_output=True, text=True,
                            timeout=FFMPEG_TIMEOUT, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg mux failed:\n{result.stderr[-2000:]}")


def _monta_video_ffmpeg(audio_path: str, keywords: list, clip_paths: dict, output_path: str, mood: str = None, on_progress=None, custom_music: str = None, captions_text: str = None) -> None:
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    total_duration = _media_duration(audio_path)
    n_segments = int(total_duration / SEGMENT_DURATION) + 1
    captions = _select_key_captions(captions_text or "", total_duration, n_segments)
    music_path = custom_music if custom_music and os.path.exists(custom_music) else _pick_music(mood)
    if music_path:
        print(f"[montaggio] BG music: {music_path} (mood={mood})", flush=True)

    print(
        f"[montaggio] FFmpeg low-power — {total_duration:.0f}s, "
        f"{OUTPUT_W}x{OUTPUT_H}, {OUTPUT_FPS}fps, threads={FFMPEG_THREADS}",
        flush=True,
    )
    if captions:
        print(f"[montaggio] Testi chiave: {len(captions)} frasi", flush=True)

    if not keywords:
        raise RuntimeError("montaggio: nessuna keyword fornita — impossibile selezionare clip")

    clip_files, durate = _filtra_clip_leggibili([clip_paths[k] for k in keywords])
    if not clip_files:
        raise RuntimeError(
            "montaggio: nessuna clip leggibile — la cache e' stata svuotata o i "
            "download sono corrotti. Rilancia la pipeline con /forza."
        )
    clip_sequence = _build_clip_sequence(
        clip_files, n_segments,
        prioritarie=_clip_prioritarie(keywords, clip_paths, clip_files))
    workers = _render_workers()
    print(f"[montaggio] Render segmenti: {workers} in parallelo", flush=True)
    with tempfile.TemporaryDirectory(prefix="render_", dir=os.path.dirname(output_path) or None) as tmpdir:
        lavori = []
        for i in range(n_segments):
            seg_start = i * SEGMENT_DURATION
            remaining = total_duration - seg_start
            if remaining <= 0:
                break
            clip_path = clip_sequence[i]
            lavori.append((
                i, clip_path, os.path.join(tmpdir, f"seg_{i:04d}.mp4"),
                seg_start, min(SEGMENT_DURATION, remaining),
                captions.get(i), durate.get(clip_path),
            ))
        segment_paths = [j[2] for j in lavori]

        fatti = 0
        ultimo_pct = 0
        inizio = time.time()

        def _avanza():
            """Progresso su segmenti completati: con il render in parallelo
            l'indice del ciclo non e' piu' un indicatore di avanzamento."""
            nonlocal fatti, ultimo_pct
            fatti += 1
            pct = int(fatti / max(1, len(lavori)) * 80)
            if pct < ultimo_pct + 10:
                return
            ultimo_pct = pct
            trascorso = time.time() - inizio
            eta = (trascorso / fatti * (len(lavori) - fatti)) if fatti else 0
            eta_str = f"{int(eta // 60)}m {int(eta % 60)}s"
            print(f"[montaggio] Segmenti: {pct}% — ETA {eta_str}", flush=True)
            if on_progress:
                on_progress(pct, eta_str)

        if workers <= 1:
            for _, src, dst, off, dur, cap, sdur in lavori:
                _render_segment_ffmpeg(src, dst, off, dur, cap, sdur)
                _avanza()
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(_render_segment_ffmpeg, src, dst, off, dur, cap, sdur): dst
                    for _, src, dst, off, dur, cap, sdur in lavori
                }
                for fut in as_completed(futures):
                    fut.result()  # rilancia il primo errore, annullando il render
                    _avanza()

        # I segmenti valgono l'80% della barra: senza queste tappe il
        # progresso si fermava a "80% — ETA 0m 0s" per tutta la durata di
        # concat e mux, e sembrava che il render si fosse piantato.
        if on_progress:
            on_progress(80, "unisco i segmenti")
        list_path = os.path.join(tmpdir, "segments.txt")
        with open(list_path, "w", encoding="utf-8") as f:
            for path in segment_paths:
                f.write(_concat_file_line(path))

        silent_video = os.path.join(tmpdir, "video_senza_audio.mp4")
        subprocess.run(
            [_ffmpeg(), "-y", "-nostdin", "-v", "error", "-f", "concat", "-safe", "0",
             "-i", list_path, "-c", "copy", silent_video],
            check=True, capture_output=True,
            timeout=FFMPEG_TIMEOUT, stdin=subprocess.DEVNULL,
        )
        if on_progress:
            on_progress(90, "audio e musica")
        _mux_audio_ffmpeg(silent_video, audio_path, output_path, music_path, total_duration)
        if on_progress:
            on_progress(100, "fatto")


def _make_segment(clip_path: str, time_offset: float, duration: float, cache: dict | None = None):
    """`cache` mappa path -> VideoFileClip riusato tra i segmenti.

    Prima ogni segmento apriva un VideoFileClip nuovo e non lo chiudeva mai:
    un video da 8 minuti significava ~96 reader ffmpeg e altrettanti handle di
    file aperti fino alla fine del processo. Riusando il clip per path, gli
    oggetti aperti scendono al numero di clip distinte e `_chiudi_clip()` li
    chiude tutti a fine render."""
    from moviepy import VideoFileClip, concatenate_videoclips
    path = _resize_to_target(clip_path)
    if cache is None:
        clip = VideoFileClip(path, audio=False)
    else:
        clip = cache.get(path)
        if clip is None:
            clip = VideoFileClip(path, audio=False)
            cache[path] = clip
    if clip.duration < duration:
        times = int(duration / clip.duration) + 1
        clip = concatenate_videoclips([clip] * times)
    offset = time_offset % clip.duration
    end = min(offset + duration, clip.duration)
    if end - offset < duration and clip.duration >= duration:
        offset = 0
        end = duration
    return clip.subclipped(offset, end)


def _chiudi_clip(clips) -> None:
    for clip in clips:
        try:
            clip.close()
        except Exception:
            pass


from proglog import ProgressBarLogger


class _ProgressLogger(ProgressBarLogger):
    """Estende proglog per stampare ETA ogni 10% e chiamare un callback."""

    def __init__(self, on_progress=None):
        super().__init__()
        self._start = time.time()
        self._last_pct = -1
        self._on_progress = on_progress

    def bars_callback(self, bar, attr, value, old_value=None):
        if attr != "index":
            return
        total = self.bars[bar].get("total", 0)
        if not total:
            return
        pct = int(value / total * 100)
        if pct == self._last_pct or pct % 10 != 0:
            return
        self._last_pct = pct
        elapsed = time.time() - self._start
        eta = (elapsed / pct * (100 - pct)) if pct > 0 else 0
        eta_str = f"{int(eta // 60)}m {int(eta % 60)}s"
        print(f"[montaggio] Rendering: {pct}% — ETA: {eta_str}", flush=True)
        if self._on_progress:
            try:
                self._on_progress(pct, eta_str)
            except Exception:
                pass


def _overlay_captions_moviepy(video, captions_text: str | None,
                              total_duration: float, n_segments: int):
    """Sovrappone i testi chiave anche nel percorso MoviePy (prima li
    disegnava solo il render ffmpeg low-power). Best-effort: qualsiasi
    problema con font/TextClip non deve far fallire il render."""
    captions = _select_key_captions(captions_text or "", total_duration, n_segments)
    if not captions:
        return video
    font = _caption_font_path()
    if not font:
        print("[montaggio] Nessun font trovato — caption saltate", flush=True)
        return video
    try:
        from moviepy import CompositeVideoClip, TextClip
        overlays = []
        for idx, caption in captions.items():
            start = idx * SEGMENT_DURATION
            dur = min(SEGMENT_DURATION, total_duration - start)
            if dur <= 0:
                continue
            txt = TextClip(
                font=font,
                text=caption,
                font_size=CAPTION_FONT_SIZE,
                color="white",
                stroke_color="black",
                stroke_width=3,
                method="caption",
                size=(int(OUTPUT_W * 0.9), None),
            )
            txt = (txt.with_start(start)
                      .with_duration(dur)
                      .with_position(("center", OUTPUT_H - txt.h - 72)))
            overlays.append(txt)
        if not overlays:
            return video
        print(f"[montaggio] Testi chiave: {len(overlays)} frasi", flush=True)
        return CompositeVideoClip([video, *overlays], size=(OUTPUT_W, OUTPUT_H))
    except Exception as e:
        print(f"[montaggio] Caption MoviePy saltate: {e}", flush=True)
        return video


def monta_video(audio_path: str, keywords: list, clip_paths: dict, output_path: str, mood: str = None, on_progress=None, custom_music: str = None, captions_text: str = None) -> None:
    if _FFMPEG_ONLY:
        _monta_video_ffmpeg(audio_path, keywords, clip_paths, output_path, mood, on_progress, custom_music, captions_text)
        return

    from moviepy import AudioFileClip, CompositeAudioClip, concatenate_videoclips, concatenate_audioclips
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    narration = AudioFileClip(audio_path)
    total_duration = narration.duration

    n_segments = int(total_duration / SEGMENT_DURATION) + 1
    segments = []

    clip_files, _ = _filtra_clip_leggibili([clip_paths[k] for k in keywords])
    if not clip_files:
        raise RuntimeError(
            "montaggio: nessuna clip leggibile — la cache e' stata svuotata o i "
            "download sono corrotti. Rilancia la pipeline con /forza."
        )
    clip_sequence = _build_clip_sequence(
        clip_files, n_segments,
        prioritarie=_clip_prioritarie(keywords, clip_paths, clip_files))
    aperti: dict = {}  # path -> VideoFileClip, riusato tra i segmenti
    for i in range(n_segments):
        seg_start = i * SEGMENT_DURATION
        remaining = total_duration - seg_start
        if remaining <= 0:
            break
        seg_dur = min(SEGMENT_DURATION, remaining)

        clip_path = clip_sequence[i]

        seg = _make_segment(clip_path, seg_start, seg_dur, cache=aperti)
        segments.append(seg)

    if not segments:
        raise RuntimeError("montaggio: nessun segmento video creato — audio troppo corto?")
    video = concatenate_videoclips(segments, method="compose")
    video = _overlay_captions_moviepy(video, captions_text, total_duration, n_segments)

    audio_tracks = [narration]

    music_path = custom_music if custom_music and os.path.exists(custom_music) else _pick_music(mood)
    if music_path:
        print(f"[montaggio] BG music: {music_path} (mood={mood})", flush=True)
        bg = AudioFileClip(music_path).with_volume_scaled(_bg_volume())
        if bg.duration < total_duration:
            loops = int(total_duration / bg.duration) + 1
            bg = concatenate_audioclips([bg] * loops)
        bg = bg.subclipped(0, total_duration)
        audio_tracks.append(bg)

    final_audio = CompositeAudioClip(audio_tracks)
    video = video.with_audio(final_audio)

    prog_logger = _ProgressLogger(on_progress=on_progress)
    print(
        f"[montaggio] Inizio rendering — {video.duration:.0f}s di video a "
        f"{OUTPUT_FPS}fps, {OUTPUT_W}x{OUTPUT_H}, threads={FFMPEG_THREADS}, "
        f"preset={X264_PRESET}, crf={X264_CRF}",
        flush=True,
    )

    codec = _detect_gpu_codec()
    preset = "p4" if codec == "h264_nvenc" else X264_PRESET
    try:
        video.write_videofile(
            output_path,
            fps=OUTPUT_FPS,
            codec=codec,
            audio_codec="aac",
            # senza, moviepy scrive l'audio a 44.1 kHz e bitrate di default:
            # stessa perdita di qualita' che il percorso ffmpeg evita
            audio_fps=int(AUDIO_SAMPLE_RATE),
            audio_bitrate=AUDIO_BITRATE,
            threads=FFMPEG_THREADS,
            preset=preset,
            logger=prog_logger,
        )
    finally:
        # chiusura anche in caso di errore: altrimenti i reader ffmpeg
        # restano appesi e su Windows i file di cache non sono cancellabili.
        # Vanno chiuse anche le tracce audio: il reader della musica di
        # sottofondo, lasciato al garbage collector, stampa a fine processo
        # un "OSError: [WinError 6] Handle non valido" dal suo __del__.
        _chiudi_clip([narration, video, final_audio, *audio_tracks, *aperti.values()])
