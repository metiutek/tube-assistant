"""Montaggio con vedit (https://github.com/metiutek/editorvideo-ai).

Alternativa al montaggio interno di `montaggio.py`, scelta con
`VIDEO_EDITOR=vedit` nel `.env`. Le scelte di cosa mettere in timeline restano
le stesse (sequenza clip con le AI in testa, frasi chiave, musica per mood):
cambia chi monta. vedit costruisce un progetto vero — tracce, transizioni,
keyframe, ducking della musica, look colore — e lo renderizza in un solo
filter_complex di ffmpeg.

Il progetto viene anche salvato in `output/progetto_vedit.json`: chi vuole
ritoccare un video a mano lo apre con `vedit ui output/progetto_vedit.json`
e ritrova la stessa timeline che ha prodotto il file.

Regola che non si rompe: **vedit non fa mai fallire il video.** Pacchetto
assente o errore di render, `montaggio.monta_video` ripiega sul montaggio
interno. vedit e' un extra facoltativo: pacchetto `vedit-mcp`, extra `editor`.
"""

import math
import os
from pathlib import Path

PROGETTO_PATH = os.path.join("output", "progetto_vedit.json")

EDITORI = ("interno", "vedit")

INSTALLA = ('uv tool install --force "tube-assistant[editor] @ '
            'git+https://github.com/metiu1/tube-assistant.git" '
            '(da un clone: pip install -e ".[editor]")')

# Mood dello script -> look di vedit (presets.LOOKS). I mood senza voce
# restano col colore delle clip: un look su tutto e' una scelta, non un default.
LOOK_PER_MOOD = {
    "epic": "cinema_teal_orange",
    "mysterious": "freddo_notturno",
    "tense": "freddo_notturno",
    "chill": "caldo_tramonto",
}

# zoom lento sulle clip: senza, lo stock fermo sembra una slide
KENBURNS_ZOOM = 1.06


class VeditNonDisponibile(RuntimeError):
    pass


def editore() -> str:
    valore = os.environ.get("VIDEO_EDITOR", "interno").strip().lower()
    return valore if valore in EDITORI else "interno"


def attivo() -> bool:
    return editore() == "vedit"


def disponibile() -> bool:
    try:
        import vedit  # noqa: F401
        return True
    except ImportError:
        return False


def _env_bool(nome: str, default: bool) -> bool:
    v = os.environ.get(nome)
    if v is None or not v.strip():
        return default
    return v.strip().lower() not in {"0", "false", "no", "off"}


def _env_float(nome: str, default: float) -> float:
    try:
        return float(os.environ.get(nome, default))
    except (TypeError, ValueError):
        return default


def _allinea_ffmpeg() -> None:
    """vedit cerca ffmpeg in VEDIT_FFMPEG > PATH > ~/.vedit/bin. Se la pipeline
    ne usa uno fuori dal PATH (FFMPEG_PATH, C:\\ffmpeg...), va passato a vedit,
    altrimenti i due montaggi girerebbero su binari diversi."""
    from moduli.ffmpeg_utils import ffmpeg_path, ffprobe_path
    try:
        ff = ffmpeg_path()
    except FileNotFoundError:
        return
    if ff != "ffmpeg":
        os.environ.setdefault("VEDIT_FFMPEG", ff)
        probe = ffprobe_path()
        if probe != "ffprobe":
            os.environ.setdefault("VEDIT_FFPROBE", probe)


def scegli_look(mood: str | None) -> str | None:
    """`VEDIT_LOOK`: auto (dal mood) | nessuno | id di un look di vedit."""
    from vedit import presets
    scelta = os.environ.get("VEDIT_LOOK", "auto").strip().lower()
    if scelta in {"", "auto"}:
        scelta = LOOK_PER_MOOD.get((mood or "").lower(), "")
    if scelta in {"", "nessuno", "none", "off"}:
        return None
    if not presets.find(scelta):
        print(f"[vedit] look sconosciuto {scelta!r}, ignorato", flush=True)
        return None
    return scelta


def _pezzi(durata_sorgente: float, offset: float, durata: float) -> list[tuple[float, float]]:
    """Tratti (attacco, durata) della sorgente che coprono un segmento.

    Stessa regola del montaggio interno: attacco = offset modulo lo spazio utile,
    cosi' la stessa clip riusata piu' avanti mostra un altro tratto. Una clip
    piu' corta del segmento si ripete (il montaggio interno usa -stream_loop).
    """
    if durata_sorgente >= durata:
        massimo = durata_sorgente - durata
        inizio = (offset % massimo) if massimo > 0.05 else 0.0
        return [(inizio, durata)]
    out, resto = [], durata
    while resto > 1e-3:
        d = min(durata_sorgente, resto)
        out.append((0.0, d))
        resto -= d
    return out


def costruisci_progetto(audio_path: str, keywords: list, clip_paths: dict,
                        mood: str | None = None, custom_music: str | None = None,
                        captions_text: str | None = None):
    """Costruisce la timeline vedit e ritorna lo Store (nessun render).

    Separato dal render perche' e' la parte con le decisioni di montaggio, ed
    e' quella che i test controllano senza aspettare ffmpeg.
    """
    _allinea_ffmpeg()
    from vedit import presets
    from vedit.store import Store

    from moduli import montaggio as m

    durata_tot = m._media_duration(audio_path)
    n_segmenti = int(durata_tot / m.SEGMENT_DURATION) + 1
    if not keywords:
        raise RuntimeError("montaggio vedit: nessuna keyword fornita")
    clip_files, durate = m._filtra_clip_leggibili([clip_paths[k] for k in keywords])
    if not clip_files:
        raise RuntimeError("montaggio vedit: nessuna clip leggibile")
    sequenza = m._build_clip_sequence(
        clip_files, n_segmenti,
        prioritarie=m._clip_prioritarie(keywords, clip_paths, clip_files))
    captions = m._select_key_captions(captions_text or "", durata_tot, n_segmenti)
    musica = custom_music if custom_music and os.path.exists(custom_music) else m._pick_music(mood)

    st = Store.create(name="tube-assistant", preset="1080p",
                      width=m.OUTPUT_W, height=m.OUTPUT_H, fps=float(m.OUTPUT_FPS))
    transizione = os.environ.get("VEDIT_TRANSIZIONE", "dissolve").strip().lower()
    if transizione in {"", "nessuna", "none", "off"}:
        transizione = None
    d_trans = max(0.1, _env_float("VEDIT_TRANSIZIONE_DURATA", 0.5))
    # una transizione ogni N stacchi: tutte dissolvenze stancano quanto tutti
    # tagli secchi, e il ritmo del parlato vuole soprattutto stacchi
    ogni = max(1, int(_env_float("VEDIT_TRANSIZIONE_OGNI", 3)))
    kenburns = _env_bool("VEDIT_KENBURNS", True)
    look = scegli_look(mood)
    effetti_look = presets.find(look)["effects"] if look else []

    with st.batch():
        st.import_media(clip_files)
        per_path = {p: st.project.media_by_path(str(Path(p).resolve())).id for p in clip_files}

        clip_video = []
        for i in range(n_segmenti):
            inizio = i * m.SEGMENT_DURATION
            resto = durata_tot - inizio
            if resto <= 0:
                break
            dur = min(m.SEGMENT_DURATION, resto)
            src = sequenza[i]
            ultimo = inizio + dur >= durata_tot - 1e-3
            # la clip con transizione si allunga sotto la successiva: il
            # segmento dopo parte comunque al suo posto, la durata totale
            # resta quella della narrazione
            con_trans = bool(transizione) and not ultimo and (i + 1) % ogni == 0
            coda = d_trans if con_trans else 0.0
            pezzi = _pezzi(durate[src], inizio, dur + coda)
            if len(pezzi) > 1:
                con_trans, pezzi = False, _pezzi(durate[src], inizio, dur)
            t = inizio
            for attacco, d in pezzi:
                c = st.add_clip(per_path[src], "V1", start=t,
                                in_=attacco, duration=d)
                st.set_clip(c.id, fit="cover")
                st.set_audio(c.id, mute=True)
                if kenburns:
                    a, b = (1.0, KENBURNS_ZOOM) if i % 2 == 0 else (KENBURNS_ZOOM, 1.0)
                    st.set_transform(c.id, scale={"kf": [{"t": 0, "v": a},
                                                         {"t": round(d, 3), "v": b}]})
                if effetti_look:
                    st.add_effects(c.id, effetti_look)
                clip_video.append(c)
                t += d
            if con_trans:
                st.set_transition(clip_video[-1].id, transizione, d_trans)

        # narrazione: e' lei a dettare la durata del video
        voce = st.import_media([audio_path])[0]
        st.add_clip(voce.id, "A1", start=0.0, duration=durata_tot)

        if musica:
            print(f"[vedit] musica: {musica} (mood={mood})", flush=True)
            brano = st.import_media([musica])[0]
            traccia = st.add_track("audio", name="musica")
            vol = max(m._bg_volume(), 1e-3)
            gain = round(20 * math.log10(vol), 2)
            t, ultima = 0.0, None
            while t < durata_tot - 1e-3 and brano.duration > 0:
                d = min(brano.duration, durata_tot - t)
                ultima = st.add_clip(brano.id, traccia.id, start=t, duration=d)
                st.set_audio(ultima.id, gain_db=gain)
                t += d
            if ultima is not None:
                st.set_fades(ultima.id, fade_out=min(2.0, ultima.duration))
            # ducking: la musica si abbassa da sola quando parla la voce
            st.set_sidechain(traccia.id, "A1", threshold=-30.0, ratio=3.0)

        if captions:
            testi = st.add_track("video", name="testi")
            for idx, frase in sorted(captions.items()):
                inizio = idx * m.SEGMENT_DURATION
                dur = min(m.SEGMENT_DURATION, durata_tot - inizio)
                if dur <= 0.2:
                    continue
                c = st.add_text(frase, testi.id, start=inizio, duration=dur,
                                font_size=m.CAPTION_FONT_SIZE, box=True,
                                box_opacity=0.55, box_padding=18)
                st.set_transform(c.id, y=round(m.OUTPUT_H * 0.32))
                st.set_fades(c.id, fade_in=0.25, fade_out=0.25, audio=False)

        st.set_loudnorm(True, target_lufs=m.LOUDNESS_TARGET_LUFS, true_peak=-1.5, lra=11.0)
    return st


def monta_video_vedit(audio_path: str, keywords: list, clip_paths: dict, output_path: str,
                      mood: str = None, on_progress=None, custom_music: str = None,
                      captions_text: str = None) -> None:
    """Stessa firma di `montaggio.monta_video`. Solleva in caso di errore:
    il ripiego sul montaggio interno e' compito del chiamante."""
    if not disponibile():
        raise VeditNonDisponibile(
            "vedit non installato: " + INSTALLA)
    from vedit import render

    from moduli import montaggio as m

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    st = costruisci_progetto(audio_path, keywords, clip_paths, mood, custom_music, captions_text)
    try:
        st.save(os.path.join(os.path.dirname(output_path) or ".", os.path.basename(PROGETTO_PATH)))
    except Exception as e:
        # il progetto salvato e' una comodita' per ritoccare a mano, non il video
        print(f"[vedit] progetto non salvato: {e}", flush=True)

    ultimo = [-1]

    def _progresso(p: dict) -> None:
        pct = p.get("percent")
        if pct is None or not on_progress:
            return
        pct = int(min(99, pct))
        if pct >= ultimo[0] + 10:
            ultimo[0] = pct
            trascorso = p.get("elapsed") or 0.0
            eta = trascorso / pct * (100 - pct) if pct > 0 else 0.0
            eta_str = f"{int(eta // 60)}m {int(eta % 60)}s"
            on_progress(pct, eta_str)

    print(f"[vedit] render — {m.OUTPUT_W}x{m.OUTPUT_H}, {m.OUTPUT_FPS}fps", flush=True)
    opts = render.RenderOptions(
        output=output_path, quality=os.environ.get("VEDIT_QUALITA", "high"),
        audio_bitrate=m.AUDIO_BITRATE,
        width=m.OUTPUT_W, height=m.OUTPUT_H, fps=float(m.OUTPUT_FPS),
    )
    res = render.render(st.project, opts, _progresso)
    for avviso in res.warnings:
        print(f"[vedit] {avviso}", flush=True)
    print(f"[vedit] fatto in {res.seconds:.0f}s — encoder {res.encoder}", flush=True)
    if on_progress:
        on_progress(100, "fatto")
