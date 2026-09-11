"""Preferenze stilistiche persistenti per i video."""
import html
import json
import os
import re

PREF_FILE = "preferenze_video.json"

DEFAULT_PREF = {
    "ritmo": "medio",              # lento / medio / veloce
    "tono_voce": "confident",      # confident / casual / dramatic / educational
    "lingua": "english",           # english / italian
    "stile_clip": "cinematic",     # cinematic / fast cuts / minimal / documentary
    "stile_thumbnail": "cinematic dramatic lighting, dark moody",
    "argomenti_preferiti": ["AI", "technology", "future tech"],
    "argomenti_evitare": [],
    "durata_target_minuti": 8,
    "musica_volume": 0.1,
    "note_libere": "",
    "thumbnail_testo_mostra": True,          # mostra titolo sulla copertina
    "thumbnail_testo_colore": "255,255,255", # R,G,B oppure nome (bianco/rosso/giallo...)
    "thumbnail_testo_posizione": "basso",    # alto / basso
    "thumbnail_testo_scala": 1.0,            # grandezza testo: 0.3-1.0 (1.0 = massimo leggibile)
    "thumbnail_font_size": "",               # preset A/B/C/D — vuoto = decide l'LLM
    "tts_voce": "en-US-GuyNeural",          # voce Edge TTS (es. it-IT-DiegoNeural)
}


def carica() -> dict:
    if os.path.exists(PREF_FILE):
        try:
            with open(PREF_FILE, encoding="utf-8") as f:
                data = json.load(f)
            return {**DEFAULT_PREF, **data}
        except Exception:
            pass
    return DEFAULT_PREF.copy()


def salva(pref: dict) -> None:
    with open(PREF_FILE, "w", encoding="utf-8") as f:
        json.dump(pref, f, indent=2, ensure_ascii=False)


def _lista_da_testo(valore: str) -> list[str]:
    valore = re.sub(r"\s+(?:e|and)\s+", ",", valore, flags=re.IGNORECASE)
    return [v.strip(" .;:-\"'") for v in valore.split(",") if v.strip(" .;:-\"'")]


def _converti_valore(chiave: str, valore):
    default_val = DEFAULT_PREF[chiave]
    if isinstance(default_val, bool):
        if isinstance(valore, bool):
            return valore
        return str(valore).strip().lower() in ("true", "1", "si", "sì", "yes", "vero")
    if isinstance(default_val, list):
        if isinstance(valore, list):
            return [str(v).strip() for v in valore if str(v).strip()]
        return _lista_da_testo(str(valore))
    if isinstance(default_val, int):
        match = re.search(r"\d+", str(valore))
        if not match:
            raise ValueError(f"Valore non valido per {chiave}")
        return int(match.group(0))
    if isinstance(default_val, float):
        raw = str(valore).replace(",", ".")
        match = re.search(r"\d+(?:\.\d+)?", raw)
        if not match:
            raise ValueError(f"Valore non valido per {chiave}")
        return float(match.group(0))
    return str(valore).strip().strip('"').strip("'")


def aggiorna(chiave: str, valore) -> dict:
    pref = carica()
    pref[chiave] = _converti_valore(chiave, valore)
    salva(pref)
    return pref


# Una domanda non e' mai un comando di impostazione. Senza questo filtro
# "che lingua parla il canale?" salvava lingua="parla il canale?" e il
# messaggio non arrivava nemmeno all'AI (handle_message fa early-return).
_INTERROGATIVI = (
    "che ", "cosa ", "quale", "quali", "come ", "quando", "perche", "perché",
    "chi ", "dove", "quanto", "quanti", "mi dici", "dimmi",
    "what ", "which", "how ", "when ", "why ", "who ", "where ",
)
# verbi che indicano davvero la volonta' di cambiare un'impostazione
_VERBI_IMPOSTAZIONE = re.compile(
    r"\b(?:imposta|setta|metti|cambia|modifica|usa|adotta|voglio|vorrei|preferisco|"
    r"d'ora\s+in\s+poi|da\s+ora|sempre|set|change|update|use|switch)\b",
    re.IGNORECASE,
)
# verbi di generazione one-shot: "genera una copertina di prova" non deve
# scrivere stile_thumbnail="di prova"
_VERBI_GENERAZIONE = re.compile(
    r"\b(?:genera|generami|crea|creami|fai|fammi|mostrami|mostra|scrivi|produci|"
    r"inviami|mandami|generate|create|make|show|send)\b",
    re.IGNORECASE,
)


def _e_domanda(testo: str) -> bool:
    t = testo.strip().lower()
    return t.endswith("?") or t.startswith(_INTERROGATIVI)


def aggiorna_da_testo(testo: str) -> dict:
    """Aggiorna preferenze da frasi naturali e restituisce solo le modifiche.

    Il parsing e' volutamente conservativo: nel dubbio non tocca nulla e lascia
    decidere all'LLM, che ha comunque il tag [SETPREF: chiave=valore]. Un falso
    positivo qui e' peggio di un mancato match, perche' salva una preferenza
    sbagliata E impedisce alla domanda di raggiungere l'AI.
    """
    pref = carica()
    original = testo.strip()
    t = original.lower()
    modifiche = {}

    if _e_domanda(original):
        return modifiche
    _vuole_impostare = bool(_VERBI_IMPOSTAZIONE.search(original))
    _vuole_generare = bool(_VERBI_GENERAZIONE.search(original))

    def set_pref(chiave: str, valore, merge_list: bool = False) -> None:
        nuovo = _converti_valore(chiave, valore)
        if merge_list and isinstance(DEFAULT_PREF[chiave], list):
            esistenti = pref.get(chiave, [])
            nuovo = [*esistenti, *[v for v in nuovo if v not in esistenti]]
        pref[chiave] = nuovo
        modifiche[chiave] = nuovo

    alias_espliciti = {
        "ritmo": ("ritmo",),
        "tono_voce": ("tono_voce", "tono voce"),
        "lingua": ("lingua", "language"),
        "stile_clip": ("stile_clip", "stile clip"),
        "stile_thumbnail": ("stile_thumbnail", "stile thumbnail"),
        "argomenti_preferiti": ("argomenti_preferiti", "argomenti preferiti"),
        "argomenti_evitare": ("argomenti_evitare", "argomenti evitare"),
        "durata_target_minuti": ("durata_target_minuti", "durata target minuti"),
        "musica_volume": ("musica_volume", "musica volume", "volume musica"),
        "note_libere": ("note_libere", "note libere"),
    }
    # separatore `=` o `:` OBBLIGATORIO: senza, qualsiasi frase che contenesse
    # la parola chiave ("...la lingua parla...") finiva salvata come valore.
    # Le forme in linguaggio naturale sono coperte dai pattern specifici sotto.
    for chiave, aliases in alias_espliciti.items():
        for alias in aliases:
            match = re.search(rf"\b{re.escape(alias)}\b\s*[=:]\s*(.+)", original, re.IGNORECASE)
            if match and match.group(1).strip():
                set_pref(chiave, match.group(1))
                break

    if re.search(r"\b(lento|medio|veloce|fast)\b", t) and re.search(r"\b(ritmo|pace|taglio|montaggio|video)\b", t):
        if "veloce" in t or "fast" in t:
            set_pref("ritmo", "veloce")
        elif "lento" in t:
            set_pref("ritmo", "lento")
        elif "medio" in t:
            set_pref("ritmo", "medio")

    tono_match = re.search(r"\b(?:tono(?:_voce|\s+voce)?|voce)\b\s*(?:=|:|a|in|piu|piu'|più)?\s*(confident|casual|dramatic|educational|drammatico|educativo)", t)
    if tono_match:
        value = tono_match.group(1)
        value = {"drammatico": "dramatic", "educativo": "educational"}.get(value, value)
        set_pref("tono_voce", value)

    lingua_match = re.search(r"\b(?:lingua|language)\b\s*(?:=|:|in|a)?\s*(english|italian|inglese|italiano)", t)
    if lingua_match:
        value = lingua_match.group(1)
        value = {"inglese": "english", "italiano": "italian"}.get(value, value)
        set_pref("lingua", value)

    clip_match = re.search(r"\b(?:stile\s+clip|clip|montaggio)\b\s*(?:=|:|in|a)?\s*(cinematic|fast cuts|minimal|documentary|documentario)", t)
    if clip_match:
        value = clip_match.group(1)
        value = {"documentario": "documentary"}.get(value, value)
        set_pref("stile_clip", value)

    durata_match = re.search(r"\b(?:durata|lunghezza|video\s+da)\b[^\d]{0,20}(\d+)\s*(?:min|minuti|minutes)?", t)
    if durata_match:
        set_pref("durata_target_minuti", durata_match.group(1))

    volume_match = re.search(r"\b(?:volume\s+musica|musica\s+volume)\b[^\d]{0,20}(\d+(?:[\.,]\d+)?)", t)
    if volume_match:
        set_pref("musica_volume", volume_match.group(1))

    evita_match = re.search(
        r"\b(?:evita|argomenti\s+da\s+evitare|non\s+(?:parlare|trattare)\s+di|non\s+voglio\s+(?:video|argomenti|contenuti)?\s*(?:su|di|che\s+parlino\s+di))\b\s*(?:parlare\s+di\s*)?(.+)",
        original,
        re.IGNORECASE,
    )
    if evita_match:
        valore = re.sub(r"^(?:argomenti\s+)?(?:come\s+)?(?:di\s+|su\s+|sul\s+|sulla\s+|about\s+)?",
                        "", evita_match.group(1).strip(), flags=re.IGNORECASE)
        set_pref("argomenti_evitare", valore, merge_list=True)

    preferiti_match = re.search(r"\b(?:argomenti\s+preferiti|parla\s+di|voglio\s+argomenti\s+su)\b\s*(.+)", original, re.IGNORECASE)
    if preferiti_match and "non " not in t and not _vuole_generare:
        set_pref("argomenti_preferiti", preferiti_match.group(1), merge_list=True)

    # "stile thumbnail X" e' esplicito; il solo "thumbnail/copertina" richiede
    # un verbo di impostazione e nessun verbo di generazione, altrimenti
    # "genera una copertina di prova" salvava stile_thumbnail="di prova"
    thumb_match = re.search(r"\bstile\s+(?:thumbnail|copertina)\b\s*(?:=|:|con|in)?\s*(.+)", original, re.IGNORECASE)
    if not thumb_match and _vuole_impostare and not _vuole_generare:
        thumb_match = re.search(r"\b(?:thumbnail|copertina)\b\s*(?:=|:|con|in|stile)?\s*(.+)", original, re.IGNORECASE)
    if thumb_match:
        value = thumb_match.group(1).strip(" .")
        # non confondere comandi sul TESTO (colore/grandezza/posizione) con lo stile immagine
        _is_text_cmd = re.search(
            r"\b(?:testo|titolo|scritte?|font|carattere|colore?|color|piu'|più|piu\s|grand|piccol|alto|basso|grandezza|scala|%)\b",
            value, re.IGNORECASE,
        )
        if value and not _is_text_cmd:
            set_pref("stile_thumbnail", value)

    note_match = re.search(r"\b(?:nota|ricorda\s+per\s+i\s+video|preferenza\s+generale)\b\s*[:\-]\s*(.+)", original, re.IGNORECASE)
    if note_match and not _vuole_generare:
        set_pref("note_libere", note_match.group(1))

    _COLORI_IT = {
        "bianco": "255,255,255", "nero": "0,0,0",
        "rosso": "255,50,50", "giallo": "255,220,0",
        "verde": "50,255,100", "blu": "50,150,255",
        "arancione": "255,140,0", "viola": "180,50,255",
        "cyan": "0,220,255", "rosa": "255,100,180",
        "white": "255,255,255", "red": "255,50,50",
        "yellow": "255,220,0", "blue": "50,150,255",
        "green": "50,255,100", "orange": "255,140,0",
    }
    testo_col_match = re.search(
        r"\b(?:testo|titolo|scritte?)\b.{0,30}(?:colore?|color)\b[^\w]*(\w+)|"
        r"\b(?:colore?|color)\b[^\w]*(?:del\s+)?(?:testo|titolo)\b[^\w]*(\w+)",
        t, re.IGNORECASE
    )
    if not testo_col_match:
        # pattern "testo rosso", "titolo giallo", "scritte rosse"
        testo_col_match2 = re.search(
            r"\b(?:testo|titolo|scritte?)\b\s+(?:in\s+|di\s+|colore\s+)?(" + "|".join(_COLORI_IT.keys()) + r")\b",
            t, re.IGNORECASE
        )
        if testo_col_match2:
            nome = testo_col_match2.group(1).lower()
            set_pref("thumbnail_testo_colore", _COLORI_IT.get(nome, nome))
    else:
        nome = (testo_col_match.group(1) or testo_col_match.group(2) or "").lower()
        if nome in _COLORI_IT:
            set_pref("thumbnail_testo_colore", _COLORI_IT[nome])

    if re.search(r"\b(?:testo|titolo|scritte?)\b.{0,20}\b(?:in\s+)?alto\b", t):
        set_pref("thumbnail_testo_posizione", "alto")
    elif re.search(r"\b(?:testo|titolo|scritte?)\b.{0,20}\b(?:in\s+)?basso\b", t):
        set_pref("thumbnail_testo_posizione", "basso")

    if re.search(r"\b(?:no\s+testo|senza\s+testo|niente\s+testo|rimuovi\s+testo|togli\s+testo|no\s+titolo|senza\s+titolo|no\s+scritte?|senza\s+scritte?)\b", t):
        set_pref("thumbnail_testo_mostra", False)
    elif re.search(r"\b(?:aggiungi\s+testo|metti\s+testo|con\s+testo|mostra\s+testo|mostra\s+titolo|testo\s+sulla\s+copertina|titolo\s+sulla\s+copertina)\b", t):
        set_pref("thumbnail_testo_mostra", True)

    # grandezza testo copertina
    if re.search(r"\b(?:testo|titolo|scritte?|font|carattere)\b.{0,30}\b(?:piu'|più|piu)\s+grand", t) or \
       re.search(r"\b(?:ingrandisci|aumenta)\b.{0,20}\b(?:testo|titolo|scritte?|font)\b", t) or \
       re.search(r"\b(?:testo|titolo|scritte?)\b.{0,15}\b(?:enorme|gigante|grandissim)", t):
        set_pref("thumbnail_testo_scala", 1.0)
    elif re.search(r"\b(?:testo|titolo|scritte?|font|carattere)\b.{0,30}\b(?:piu'|più|piu)\s+piccol", t) or \
         re.search(r"\b(?:rimpicciolisci|riduci|diminuisci)\b.{0,20}\b(?:testo|titolo|scritte?|font)\b", t):
        set_pref("thumbnail_testo_scala", 0.6)
    else:
        scala_match = re.search(r"\b(?:testo|titolo|scritte?|font|carattere|grandezza|scala)\b.{0,20}?(\d{1,3})\s*%", t)
        if scala_match:
            set_pref("thumbnail_testo_scala", max(0.3, min(1.0, int(scala_match.group(1)) / 100)))

    _VOCI_PRESET = {
        # italiano
        "italiano": "it-IT-DiegoNeural", "italiana": "it-IT-DiegoNeural",
        "italian": "it-IT-DiegoNeural",
        "italiano femmina": "it-IT-ElsaNeural", "voce femminile italiana": "it-IT-ElsaNeural",
        "italiana femmina": "it-IT-ElsaNeural",
        # inglese
        "inglese": "en-US-GuyNeural", "english": "en-US-GuyNeural",
        "inglese femmina": "en-US-AriaNeural", "voce femminile inglese": "en-US-AriaNeural",
        "british": "en-GB-SoniaNeural", "inglese british": "en-GB-SoniaNeural",
        # genere
        "femmina": "en-US-AriaNeural", "femminile": "en-US-AriaNeural",
        "donna": "en-US-AriaNeural", "maschio": "en-US-GuyNeural",
        "maschile": "en-US-GuyNeural", "uomo": "en-US-GuyNeural",
    }
    voce_match = re.search(
        r"\b(?:voce|voice|tts)\b[^\w]{0,10}(.{3,40}?)(?:\s*$|[,.])",
        original, re.IGNORECASE
    )
    if voce_match:
        raw_voce = voce_match.group(1).strip().lower()
        if raw_voce in _VOCI_PRESET:
            set_pref("tts_voce", _VOCI_PRESET[raw_voce])
        elif re.match(r"[a-z]{2}-[A-Z]{2}-\w+Neural", voce_match.group(1).strip()):
            set_pref("tts_voce", voce_match.group(1).strip())
    else:
        for pattern, voce in _VOCI_PRESET.items():
            if re.search(r"\b" + re.escape(pattern) + r"\b", t) and "voce" in t:
                set_pref("tts_voce", voce)
                break

    if modifiche:
        salva(pref)
    return modifiche


def formato_html() -> str:
    pref = carica()
    lines = ["⚙️ <b>Preferenze video attuali:</b>\n"]
    for k, v in pref.items():
        val = ", ".join(v) if isinstance(v, list) else str(v)
        lines.append(f"<b>{html.escape(k)}</b>: {html.escape(val or '-')}")
    lines.append("\nPuoi scrivere in modo naturale, per esempio:")
    lines.append("<code>metti ritmo veloce</code>")
    lines.append("<code>evita crypto e web3</code>")
    lines.append("<code>thumbnail con colori vivi e stile energico</code>")
    return "\n".join(lines)


def come_contesto() -> str:
    pref = carica()
    return (
        "[PREFERENZE UTENTE DEL CANALE]\n"
        f"- Ritmo: {pref['ritmo']}\n"
        f"- Tono voce: {pref['tono_voce']}\n"
        f"- Lingua: {pref['lingua']}\n"
        f"- Stile clip: {pref['stile_clip']}\n"
        f"- Stile thumbnail: {pref['stile_thumbnail']}\n"
        f"- Testo sulla copertina: {'sì' if pref.get('thumbnail_testo_mostra', True) else 'no'}\n"
        f"- Colore testo copertina: {pref.get('thumbnail_testo_colore', '255,255,255')}\n"
        f"- Posizione testo copertina: {pref.get('thumbnail_testo_posizione', 'basso')}\n"
        f"- Grandezza testo copertina: {pref.get('thumbnail_testo_scala', 1.0)} (0.3-1.0)\n"
        f"- Voce TTS: {pref.get('tts_voce', 'en-US-GuyNeural')}\n"
        f"- Argomenti preferiti: {', '.join(pref['argomenti_preferiti'])}\n"
        f"- Argomenti da evitare: {', '.join(pref['argomenti_evitare']) or 'nessuno'}\n"
        f"- Durata target: {pref['durata_target_minuti']} minuti\n"
        f"- Note: {pref['note_libere'] or 'nessuna'}"
    )
