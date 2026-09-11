"""Manutenzione disco: guardia spazio + pulizia cache.

Evita il crash `OSError: [Errno 28] No space left on device` durante il
montaggio. Tutto configurabile via env, default sensati per chiunque:

  MIN_FREE_DISK_GB     spazio minimo libero richiesto prima del montaggio (default 5)
  MAX_CACHE_MB         tetto massimo della cache clip Pexels (default 2000 = 2GB)
  MAX_AI_CACHE_MB      tetto della cache clip generate dall'AI (default 3000)
  CACHE_DIR            cartella cache (default cache/pexels)
  VIDEO_AI_CACHE_DIR   cartella clip AI (default cache/ai_video)
"""

import os
import glob
import shutil
import time

CACHE_DIR = os.environ.get("CACHE_DIR", "cache/pexels")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


MIN_FREE_DISK_GB = _env_float("MIN_FREE_DISK_GB", 5.0)
MAX_CACHE_MB = _env_float("MAX_CACHE_MB", 2000.0)
AI_CACHE_DIR = os.environ.get("VIDEO_AI_CACHE_DIR", "cache/ai_video")
MAX_AI_CACHE_MB = _env_float("MAX_AI_CACHE_MB", 3000.0)


def spazio_libero_gb(path: str = ".") -> float:
    """GB liberi sul filesystem che contiene `path`."""
    try:
        target = path if os.path.exists(path) else "."
        free = shutil.disk_usage(target).free
        return free / (1024 ** 3)
    except OSError:
        return float("inf")  # non blocchiamo se non riusciamo a leggere


def assicura_spazio(min_gb: float = None, work_dir: str = "output", protetti: set = None) -> None:
    """Se lo spazio libero e' sotto la soglia, prima prova a liberare la cache,
    poi rilancia un errore chiaro. Da chiamare PRIMA del montaggio (lo step che
    riempie il disco con i file temporanei di render). `protetti` = clip che
    servono alla run corrente, mai cancellate."""
    min_gb = MIN_FREE_DISK_GB if min_gb is None else min_gb
    libero = spazio_libero_gb(work_dir)
    if libero >= min_gb:
        return
    # tentativo di recupero: svuota la cache clip (tranne le clip protette)
    liberati = pulisci_cache(max_mb=0, protetti=protetti)
    libero = spazio_libero_gb(work_dir)
    if libero >= min_gb:
        return
    raise RuntimeError(
        f"Spazio disco insufficiente: {libero:.1f} GB liberi, ne servono almeno "
        f"{min_gb:.1f} GB per il montaggio. Liberati {liberati:.0f} MB di cache "
        f"ma non basta. Libera spazio sul disco e riprova con /forza."
    )


def _cache_files(cartella: str = None) -> list[str]:
    cartella = CACHE_DIR if cartella is None else cartella
    if not os.path.isdir(cartella):
        return []
    return [p for p in glob.glob(os.path.join(cartella, "*.mp4")) if os.path.isfile(p)]


def cache_size_mb() -> float:
    return sum(os.path.getsize(p) for p in _cache_files()) / (1024 ** 2)


def _pota(cartella: str, max_mb: float, protetti_abs: set) -> float:
    """Tiene una cartella di clip sotto `max_mb` (LRU per mtime). MB liberati."""
    files = _cache_files(cartella)
    if not files:
        return 0.0
    total = sum(os.path.getsize(p) for p in files)
    limit = max_mb * 1024 ** 2
    if total <= limit:
        return 0.0
    # piu' vecchi prima
    files.sort(key=lambda p: os.path.getmtime(p))
    liberati = 0
    for p in files:
        if total <= limit:
            break
        if os.path.abspath(p) in protetti_abs:
            continue
        try:
            sz = os.path.getsize(p)
            os.remove(p)
            total -= sz
            liberati += sz
        except OSError:
            continue
    return liberati / (1024 ** 2)


def pulisci_cache(max_mb: float = None, protetti: set = None) -> float:
    """Tiene le cache clip sotto il rispettivo tetto cancellando i file piu'
    vecchi (LRU per mtime). `max_mb=0` svuota tutto. I path in `protetti` (clip
    che servono alla run corrente) non vengono mai cancellati. MB liberati.

    Le clip generate dall'AI hanno un tetto SEPARATO: sono costate soldi, e
    farle sfrattare da uno stock gratuito appena scaricato sarebbe il contrario
    di quello che serve.
    """
    protetti_abs = {os.path.abspath(p) for p in (protetti or ())}
    tetto_pexels = MAX_CACHE_MB if max_mb is None else max_mb
    tetto_ai = MAX_AI_CACHE_MB if max_mb is None else max_mb
    return (_pota(CACHE_DIR, tetto_pexels, protetti_abs)
            + _pota(AI_CACHE_DIR, tetto_ai, protetti_abs))


def pulisci_temp_render(work_dir: str = "output") -> int:
    """Rimuove cartelle temporanee di render orfane (render_*) lasciate da crash
    precedenti. Ritorna quante ne ha rimosse."""
    rimosse = 0
    if not os.path.isdir(work_dir):
        return 0
    for name in os.listdir(work_dir):
        if name.startswith("render_"):
            full = os.path.join(work_dir, name)
            try:
                if os.path.isdir(full):
                    shutil.rmtree(full, ignore_errors=True)
                else:
                    os.remove(full)
                rimosse += 1
            except OSError:
                continue
    return rimosse
