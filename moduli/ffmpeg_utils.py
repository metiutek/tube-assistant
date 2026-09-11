"""Localizzazione dei binari ffmpeg/ffprobe — condivisa da audio e montaggio."""

import os
import shutil
import sys

_CANDIDATI_FFMPEG = [
    r"C:\ffmpeg\bin\ffmpeg.exe",
    r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
    "/usr/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    "/opt/homebrew/bin/ffmpeg",
]


def ffmpeg_path() -> str:
    """Percorso di ffmpeg: FFMPEG_PATH > PATH > posizioni note. Solleva
    FileNotFoundError con istruzioni se non trovato."""
    env_path = os.environ.get("FFMPEG_PATH")
    if env_path and os.path.exists(env_path):
        return env_path
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    for p in _CANDIDATI_FFMPEG:
        if os.path.exists(p):
            return p
    raise FileNotFoundError(
        "ffmpeg not found. Install it and add to PATH, or set FFMPEG_PATH=/path/to/ffmpeg"
    )


def ffprobe_path() -> str:
    """ffprobe accanto a ffmpeg se possibile, altrimenti dal PATH."""
    try:
        ff = ffmpeg_path()
    except FileNotFoundError:
        return "ffprobe"
    if ff != "ffmpeg":
        for nome in ("ffprobe.exe", "ffprobe"):
            candidato = os.path.join(os.path.dirname(ff), nome)
            if os.path.exists(candidato):
                return candidato
    if shutil.which("ffprobe"):
        return "ffprobe"
    return "ffprobe"


def esporta_per_moviepy() -> str | None:
    """Allinea MoviePy allo stesso ffmpeg usato dalle chiamate dirette.

    MoviePy legge `FFMPEG_BINARY` dall'ambiente UNA SOLA VOLTA, all'import di
    `moviepy.config`, e i suoi reader/writer ne copiano il valore con
    `from moviepy.config import FFMPEG_BINARY`. Assegnare l'attributo dopo
    l'import non ha quindi alcun effetto: va impostata la variabile d'ambiente
    PRIMA che moviepy venga importato, altrimenti MoviePy usa il binario
    di imageio-ffmpeg e FFMPEG_PATH viene ignorato solo sul path MoviePy.
    Ritorna il percorso impostato, oppure None se ffmpeg non e' stato trovato.
    """
    if "moviepy.config" in sys.modules:
        return os.environ.get("FFMPEG_BINARY")
    try:
        percorso = ffmpeg_path()
    except FileNotFoundError:
        # niente crash all'import (es. CI senza ffmpeg): l'errore chiaro
        # arrivera' comunque al momento del render
        return None
    if percorso == "ffmpeg":
        # sul PATH: lascia decidere moviepy (auto-detect) senza path assoluto
        os.environ.setdefault("FFMPEG_BINARY", "auto-detect")
    else:
        os.environ["FFMPEG_BINARY"] = percorso
    return os.environ["FFMPEG_BINARY"]
