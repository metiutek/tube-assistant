"""Generazione delle clip con un'AI video, al posto (o accanto) a Pexels.

Perche' esiste
--------------
Le clip stock hanno un tetto di qualita' invalicabile: Pexels non ha un video
di "dendrite di litio che perfora il separatore", quindi la ricerca ripiega su
una centrifuga da laboratorio e lo spettatore vede immagini scollegate dal
racconto. Un modello text-to-video genera invece esattamente la scena descritta
dallo script.

Il costo pero' e' reale: un video da 8 minuti sono ~96 segmenti da 5s, e a
qualche centesimo di dollaro l'uno una sola pubblicazione puo' costare quanto
un mese di tutto il resto. Per questo il default e' `ibrido`: l'AI copre i
primi segmenti — l'aggancio, dove si decide la retention — e Pexels riempie il
resto. `VIDEO_AI_MAX_CLIPS` e' il tetto di spesa, e non viene mai superato.

Configurazione (.env, tutta scrivibile dalla TUI)
-------------------------------------------------
    VIDEO_SOURCE=pexels | ibrido | ai
    VIDEO_AI_PROVIDER=replicate | fal | luma | openrouter
    VIDEO_AI_MAX_CLIPS=6
    REPLICATE_API_TOKEN / FAL_KEY / LUMA_API_KEY / OPENROUTER_API_KEY
    VIDEO_AI_MODEL          modello del provider (ognuno ha il suo default)
    VIDEO_AI_TIMEOUT        secondi di attesa per clip (default 420)
    VIDEO_AI_PARALLELE      generazioni in parallelo (default 2)

La cache delle clip generate (`cache/ai_video/`) ha un tetto suo,
`MAX_AI_CACHE_MB`, ed e' potata da `moduli/manutenzione.py` insieme a quella
di Pexels.
"""

import hashlib
import os
import time

import requests

CACHE_DIR = os.environ.get("VIDEO_AI_CACHE_DIR", "cache/ai_video")

# Provider supportati e loro default. Il modello e' sempre sovrascrivibile con
# VIDEO_AI_MODEL: questi endpoint ne ospitano decine e cambiano in fretta.
PROVIDER_DEFAULTS = {
    "replicate": {
        "etichetta": "Replicate",
        "env_key": "REPLICATE_API_TOKEN",
        "modello": "wan-video/wan-2.1-t2v-480p",
        "hint": "replicate.com/account/api-tokens",
    },
    "fal": {
        "etichetta": "fal.ai",
        "env_key": "FAL_KEY",
        "modello": "fal-ai/ltx-video",
        "hint": "fal.ai/dashboard/keys",
    },
    "luma": {
        "etichetta": "Luma Dream Machine",
        "env_key": "LUMA_API_KEY",
        "modello": "ray-flash-2",
        "hint": "lumalabs.ai/dream-machine/api/keys",
    },
    # OpenRouter non espone modelli text-to-video: espone modelli che
    # restituiscono IMMAGINI. La clip la costruiamo noi, animando l'immagine
    # con un movimento di camera in ffmpeg (Ken Burns). Vale la pena perche'
    # l'immagine e' esattamente la scena dello script (cosa che lo stock non
    # da'), costa una frazione di un modello video, e la chiave e' la stessa
    # gia' usata per generare gli script.
    "openrouter": {
        "etichetta": "OpenRouter (immagine animata)",
        "env_key": "OPENROUTER_API_KEY",
        "modello": "google/gemini-2.5-flash-image",
        "hint": "openrouter.ai/keys",
        "nota": ("genera un'immagine e la anima con un movimento di camera: "
                 "molto piu' economico dei modelli video, ma niente movimento "
                 "reale dentro la scena. Serve ffmpeg (gia' richiesto)."),
    },
}

# Provider che restituiscono un'immagine invece di un video: cambia il prompt
# (chiedere "camera movement" a un modello di immagini e' rumore) e la clip
# viene assemblata in locale.
PROVIDER_IMMAGINE = ("openrouter",)

SORGENTI = ("pexels", "ibrido", "ai")


class VideoAIError(RuntimeError):
    """Errore di generazione: chi chiama ripiega su Pexels, non si ferma."""


# ── configurazione ────────────────────────────────────────────────────────────

def sorgente_video() -> str:
    valore = (os.environ.get("VIDEO_SOURCE") or "pexels").strip().lower()
    return valore if valore in SORGENTI else "pexels"


def provider() -> str:
    valore = (os.environ.get("VIDEO_AI_PROVIDER") or "replicate").strip().lower()
    return valore if valore in PROVIDER_DEFAULTS else "replicate"


def modello() -> str:
    scelto = (os.environ.get("VIDEO_AI_MODEL") or "").strip()
    return scelto or PROVIDER_DEFAULTS[provider()]["modello"]


def _chiave() -> str:
    return os.environ.get(PROVIDER_DEFAULTS[provider()]["env_key"], "").strip()


def _intero(nome: str, default: int) -> int:
    try:
        return int(os.environ.get(nome, default))
    except (TypeError, ValueError):
        return default


def max_clip() -> int:
    """Tetto di clip generate per video: e' il limite di spesa."""
    return max(0, _intero("VIDEO_AI_MAX_CLIPS", 6))


def _timeout() -> int:
    return max(60, _intero("VIDEO_AI_TIMEOUT", 420))


def _parallele() -> int:
    return max(1, min(4, _intero("VIDEO_AI_PARALLELE", 2)))


def configurato() -> tuple[bool, str]:
    """(pronto, motivo). Il motivo serve al preflight e alla TUI."""
    if sorgente_video() == "pexels":
        return False, "VIDEO_SOURCE=pexels — generazione AI disattivata"
    conf = PROVIDER_DEFAULTS[provider()]
    if not _chiave():
        return False, f"{conf['env_key']} mancante per {conf['etichetta']}"
    if max_clip() <= 0:
        return False, "VIDEO_AI_MAX_CLIPS=0 — nessuna clip verrebbe generata"
    return True, f"{conf['etichetta']} — modello {modello()}"


# ── prompt ────────────────────────────────────────────────────────────────────

# Cosa non deve mai comparire in una clip di b-roll: il testo generato dai
# modelli video e' sempre sbagliato e tradisce subito l'origine AI.
_NEGATIVI = ("no text, no captions, no watermark, no logo, no subtitles, "
             "no distorted faces, no extra limbs")


def costruisci_prompt(keyword: str, stile: str = "") -> str:
    """Dalla keyword di ricerca stock a un prompt per il modello video.

    Le `video_keywords` dell'LLM sono pensate per una ricerca ("laboratory
    battery testing equipment"): a un modello generativo vanno dette anche
    inquadratura, movimento e luce, altrimenti restituisce un fermo immagine
    animato male.
    """
    keyword = " ".join((keyword or "").split())
    stile = (stile or os.environ.get("VIDEO_AI_STILE", "") or "cinematic").strip()
    if provider() in PROVIDER_IMMAGINE:
        # a un modello di immagini si chiede un fotogramma, non una ripresa:
        # il movimento lo aggiunge ffmpeg dopo
        return (
            f"{keyword}. Cinematic film still, {stile}, shallow depth of field, "
            f"natural volumetric lighting, photorealistic, 16:9 widescreen, "
            f"ultra detailed. {_NEGATIVI}."
        )
    return (
        f"{keyword}. Cinematic b-roll shot, {stile}, shallow depth of field, "
        f"slow deliberate camera movement, natural volumetric lighting, "
        f"photorealistic, 16:9, high detail. {_NEGATIVI}."
    )


def _stile_utente() -> str:
    try:
        from moduli.preferenze import carica
        return (carica().get("stile_clip") or "").strip()
    except Exception:
        return ""


# ── cache su disco ────────────────────────────────────────────────────────────

def _percorso_cache(prompt: str) -> str:
    """Una clip generata costa: si tiene indicizzata per prompt+modello, cosi'
    un rilancio della pipeline sullo stesso topic non la ripaga."""
    impronta = hashlib.sha256(f"{provider()}|{modello()}|{prompt}".encode()).hexdigest()[:16]
    return os.path.join(CACHE_DIR, f"ai_{impronta}.mp4")


def _scarica(url: str, dest: str) -> None:
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    parziale = dest + ".part"
    try:
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(parziale, "wb") as f:
                for blocco in r.iter_content(chunk_size=65536):
                    if blocco:
                        f.write(blocco)
        if os.path.getsize(parziale) < 10_000:
            raise VideoAIError("file video troppo piccolo, download incompleto")
        os.replace(parziale, dest)
    except Exception:
        # mai lasciare mezzi file in cache: il montaggio li aprirebbe come clip
        try:
            os.remove(parziale)
        except OSError:
            pass
        raise


# ── provider: Replicate ───────────────────────────────────────────────────────

def _genera_replicate(prompt: str, durata: int) -> str:
    token = _chiave()
    testa = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    corpo = {"input": {"prompt": prompt, "num_frames": 81, "aspect_ratio": "16:9"}}
    r = requests.post(
        f"https://api.replicate.com/v1/models/{modello()}/predictions",
        headers=testa, json=corpo, timeout=60,
    )
    if r.status_code == 422:
        # il modello non accetta uno degli input opzionali: si riprova col
        # solo prompt, l'unico campo che tutti i text-to-video hanno
        r = requests.post(
            f"https://api.replicate.com/v1/models/{modello()}/predictions",
            headers=testa, json={"input": {"prompt": prompt}}, timeout=60,
        )
    r.raise_for_status()
    dati = r.json()
    scadenza = time.time() + _timeout()
    while dati.get("status") in ("starting", "processing"):
        if time.time() > scadenza:
            raise VideoAIError(f"timeout dopo {_timeout()}s")
        time.sleep(4)
        p = requests.get(dati["urls"]["get"], headers=testa, timeout=30)
        p.raise_for_status()
        dati = p.json()
    if dati.get("status") != "succeeded":
        raise VideoAIError(f"{dati.get('status')}: {str(dati.get('error'))[:160]}")
    uscita = dati.get("output")
    if isinstance(uscita, list):
        uscita = uscita[0] if uscita else None
    if not isinstance(uscita, str):
        raise VideoAIError("nessun URL video nella risposta Replicate")
    return uscita


# ── provider: fal.ai ──────────────────────────────────────────────────────────

def _genera_fal(prompt: str, durata: int) -> str:
    testa = {"Authorization": f"Key {_chiave()}", "Content-Type": "application/json"}
    r = requests.post(f"https://queue.fal.run/{modello()}",
                      headers=testa, json={"prompt": prompt}, timeout=60)
    r.raise_for_status()
    coda = r.json()
    stato_url = coda.get("status_url")
    risposta_url = coda.get("response_url")
    if not stato_url or not risposta_url:
        raise VideoAIError("risposta fal.ai senza status_url")
    scadenza = time.time() + _timeout()
    while True:
        if time.time() > scadenza:
            raise VideoAIError(f"timeout dopo {_timeout()}s")
        time.sleep(4)
        s = requests.get(stato_url, headers=testa, timeout=30)
        s.raise_for_status()
        stato = s.json().get("status")
        if stato == "COMPLETED":
            break
        if stato in ("FAILED", "ERROR"):
            raise VideoAIError(f"generazione fallita ({stato})")
    finale = requests.get(risposta_url, headers=testa, timeout=60)
    finale.raise_for_status()
    dati = finale.json()
    video = dati.get("video") or {}
    url = video.get("url") if isinstance(video, dict) else None
    if not url:
        raise VideoAIError("nessun URL video nella risposta fal.ai")
    return url


# ── provider: Luma ────────────────────────────────────────────────────────────

def _genera_luma(prompt: str, durata: int) -> str:
    testa = {"Authorization": f"Bearer {_chiave()}", "Content-Type": "application/json"}
    corpo = {"prompt": prompt, "model": modello(), "resolution": "720p",
             "duration": f"{max(5, durata)}s", "aspect_ratio": "16:9"}
    r = requests.post("https://api.lumalabs.ai/dream-machine/v1/generations",
                      headers=testa, json=corpo, timeout=60)
    r.raise_for_status()
    gen_id = r.json().get("id")
    if not gen_id:
        raise VideoAIError("risposta Luma senza id generazione")
    scadenza = time.time() + _timeout()
    while True:
        if time.time() > scadenza:
            raise VideoAIError(f"timeout dopo {_timeout()}s")
        time.sleep(4)
        s = requests.get(
            f"https://api.lumalabs.ai/dream-machine/v1/generations/{gen_id}",
            headers=testa, timeout=30)
        s.raise_for_status()
        dati = s.json()
        stato = dati.get("state")
        if stato == "completed":
            url = (dati.get("assets") or {}).get("video")
            if not url:
                raise VideoAIError("generazione completata ma senza video")
            return url
        if stato == "failed":
            raise VideoAIError(str(dati.get("failure_reason"))[:160])


# ── provider: OpenRouter (immagine + movimento di camera) ─────────────────────

def _risoluzione() -> tuple[int, int, int]:
    """Stessi default del montaggio: una clip generata piu' piccola del video
    finale verrebbe riscalata in su e si vedrebbe."""
    larghezza = max(640, _intero("VIDEO_WIDTH", 1920))
    altezza = max(360, _intero("VIDEO_HEIGHT", 1080))
    fps = max(12, _intero("VIDEO_FPS", 30))
    return larghezza - larghezza % 2, altezza - altezza % 2, fps


def _immagine_openrouter(prompt: str) -> bytes:
    testa = {"Authorization": f"Bearer {_chiave()}",
             "Content-Type": "application/json",
             "HTTP-Referer": "tube-assistant",
             "X-Title": "tube-assistant"}
    corpo = {"model": modello(),
             "messages": [{"role": "user", "content": prompt}],
             "modalities": ["image", "text"]}
    r = requests.post("https://openrouter.ai/api/v1/chat/completions",
                      headers=testa, json=corpo, timeout=min(300, _timeout()))
    r.raise_for_status()
    dati = r.json()
    if dati.get("error"):
        raise VideoAIError(str(dati["error"])[:160])
    scelte = dati.get("choices") or []
    messaggio = (scelte[0].get("message") or {}) if scelte else {}
    immagini = messaggio.get("images") or []
    url = None
    for voce in immagini:
        if isinstance(voce, str):
            url = voce
        elif isinstance(voce, dict):
            campo = voce.get("image_url")
            url = campo.get("url") if isinstance(campo, dict) else campo
        if url:
            break
    if not url:
        # tipico quando il modello scelto e' solo testuale: dirlo chiaro,
        # altrimenti sembra un problema di chiave
        raise VideoAIError(
            f"il modello {modello()} non ha restituito immagini — su OpenRouter "
            f"serve un modello con output image (es. google/gemini-2.5-flash-image)")
    if url.startswith("data:"):
        import base64
        try:
            return base64.b64decode(url.split(",", 1)[1])
        except (IndexError, ValueError) as e:
            raise VideoAIError(f"data URI illeggibile: {e}") from e
    img = requests.get(url, timeout=120)
    img.raise_for_status()
    return img.content


def _movimento(prompt: str, fotogrammi: int) -> str:
    """Un movimento di camera diverso per clip: quattro clip che si muovono
    uguali nello stesso video si notano subito. La scelta e' deterministica sul
    prompt, cosi' una clip ripescata dalla cache si muove sempre allo stesso
    modo."""
    zoom_max = 1.18
    passo = round((zoom_max - 1.0) / max(1, fotogrammi - 1), 6)
    centro_x, centro_y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    avanzamento = f"(on/{max(1, fotogrammi - 1)})"
    quale = int(hashlib.sha256(prompt.encode()).hexdigest(), 16) % 4
    if quale == 0:                                    # zoom in
        return f"z='min(zoom+{passo},{zoom_max})':x='{centro_x}':y='{centro_y}'"
    if quale == 1:                                    # zoom out
        return (f"z='if(eq(on,0),{zoom_max},max(1.0,zoom-{passo}))':"
                f"x='{centro_x}':y='{centro_y}'")
    if quale == 2:                                    # panoramica verso destra
        return f"z='{zoom_max}':x='(iw-iw/zoom)*{avanzamento}':y='{centro_y}'"
    return f"z='{zoom_max}':x='(iw-iw/zoom)*(1-{avanzamento})':y='{centro_y}'"


def _clip_da_immagine(dati: bytes, durata: int, prompt: str, dest: str) -> str:
    """Immagine ferma -> clip con movimento di camera, via ffmpeg."""
    import subprocess

    from moduli.ffmpeg_utils import ffmpeg_path

    larghezza, altezza, fps = _risoluzione()
    durata = max(2, int(durata or 5))
    fotogrammi = durata * fps
    # si lavora al doppio della risoluzione finale: lo zoompan campiona
    # dall'immagine grande, altrimenti il movimento "scalinetta"
    grande_w, grande_h = larghezza * 2, altezza * 2
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    sorgente = dest + ".png"
    parziale = dest + ".part.mp4"
    with open(sorgente, "wb") as f:
        f.write(dati)
    filtro = (
        f"scale={grande_w}:{grande_h}:force_original_aspect_ratio=increase,"
        f"crop={grande_w}:{grande_h},"
        f"zoompan={_movimento(prompt, fotogrammi)}:d=1:"
        f"s={larghezza}x{altezza}:fps={fps},format=yuv420p"
    )
    comando = [ffmpeg_path(), "-y", "-loop", "1", "-i", sorgente,
               "-t", str(durata), "-vf", filtro, "-r", str(fps),
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
               "-pix_fmt", "yuv420p", "-an", "-movflags", "+faststart",
               parziale]
    try:
        esito = subprocess.run(comando, capture_output=True, text=True,
                               timeout=max(120, durata * 30))
        if esito.returncode != 0 or not os.path.exists(parziale):
            coda = (esito.stderr or "").strip().splitlines()[-3:]
            raise VideoAIError("ffmpeg non ha creato la clip: " + " | ".join(coda))
        if os.path.getsize(parziale) < 10_000:
            raise VideoAIError("clip generata vuota")
        os.replace(parziale, dest)
        return dest
    except FileNotFoundError as e:
        raise VideoAIError("ffmpeg non trovato: serve per animare le immagini") from e
    except subprocess.TimeoutExpired as e:
        raise VideoAIError("ffmpeg troppo lento nell'animare l'immagine") from e
    finally:
        for scarto in (sorgente, parziale):
            try:
                os.remove(scarto)
            except OSError:
                pass


def _genera_openrouter(prompt: str, durata: int) -> str:
    """Ritorna un PATH locale, non un URL: la clip la assembliamo noi."""
    return _clip_da_immagine(_immagine_openrouter(prompt), durata, prompt,
                             _percorso_cache(prompt))


_GENERATORI = {
    "replicate": _genera_replicate,
    "fal": _genera_fal,
    "luma": _genera_luma,
    "openrouter": _genera_openrouter,
}


# ── API pubblica ──────────────────────────────────────────────────────────────

def genera_clip(keyword: str, durata: int = 5, stile: str = "") -> str:
    """Genera (o ripesca dalla cache) UNA clip per la keyword. Ritorna il path.

    Solleva `VideoAIError` su qualsiasi problema: il chiamante deve poter
    ripiegare su Pexels senza far fallire l'intero video.
    """
    pronto, motivo = configurato()
    if not pronto:
        raise VideoAIError(motivo)
    prompt = costruisci_prompt(keyword, stile or _stile_utente())
    dest = _percorso_cache(prompt)
    if os.path.exists(dest) and os.path.getsize(dest) > 10_000:
        return dest
    genera = _GENERATORI[provider()]
    try:
        url = genera(prompt, durata)
    except VideoAIError:
        raise
    except requests.HTTPError as e:
        risposta = getattr(e, "response", None)
        dettaglio = (risposta.text[:200] if risposta is not None else str(e))
        codice = risposta.status_code if risposta is not None else "?"
        if codice in (401, 403):
            raise VideoAIError(
                f"chiave {PROVIDER_DEFAULTS[provider()]['env_key']} rifiutata "
                f"({codice})") from e
        if codice == 402:
            raise VideoAIError("credito esaurito sul provider video") from e
        raise VideoAIError(f"HTTP {codice}: {dettaglio}") from e
    except Exception as e:
        raise VideoAIError(str(e)[:200]) from e
    # i provider video ritornano un URL da scaricare, quelli a immagini hanno
    # gia' scritto il file: in entrambi i casi si finisce nella stessa cache
    if not str(url).lower().startswith("http"):
        if not os.path.exists(url) or os.path.getsize(url) < 10_000:
            raise VideoAIError("clip generata non valida")
        if os.path.abspath(url) != os.path.abspath(dest):
            os.replace(url, dest)
        return dest
    _scarica(url, dest)
    return dest


def genera_pool(keywords: list[str], quante: int, durata: int = 5,
                log=print) -> list[str]:
    """Genera fino a `quante` clip distinte, una per keyword (in ordine).

    Le keyword arrivano gia' ordinate come le ha scritte l'LLM, cioe' seguendo
    il filo dello script: prendere le prime significa coprire l'inizio del
    video, che e' dove la retention si vince o si perde.
    """
    quante = min(quante, max_clip(), len(keywords or []))
    if quante <= 0:
        return []
    pronto, motivo = configurato()
    if not pronto:
        log(f"  → Generazione AI non attiva: {motivo}")
        return []
    conf = PROVIDER_DEFAULTS[provider()]
    log(f"  → Genero {quante} clip con {conf['etichetta']} ({modello()})")
    stile = _stile_utente()
    scelte = list(keywords)[:quante]
    prodotte: dict[int, str] = {}

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=_parallele()) as pool:
        futuri = {pool.submit(genera_clip, kw, durata, stile): (i, kw)
                  for i, kw in enumerate(scelte)}
        for futuro in as_completed(futuri):
            indice, kw = futuri[futuro]
            try:
                prodotte[indice] = futuro.result()
                log(f"     ✓ [{indice + 1}/{quante}] {kw}")
            except VideoAIError as e:
                log(f"     ✗ [{indice + 1}/{quante}] {kw}: {e}")
    # l'ordine delle keyword e' l'ordine del racconto: va conservato
    return [prodotte[i] for i in sorted(prodotte)]


def riassunto() -> str:
    """Riga di stato per TUI, /status e preflight."""
    sorgente = sorgente_video()
    if sorgente == "pexels":
        return "clip stock da Pexels"
    pronto, motivo = configurato()
    etichetta = "solo AI" if sorgente == "ai" else f"ibrido (max {max_clip()} clip AI)"
    return f"{etichetta} — {motivo}" if pronto else f"{etichetta} — NON pronto: {motivo}"
