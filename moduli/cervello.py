
import json
import os
import random
import re
from datetime import datetime
from moduli.ai_client import chat_ollama

# Pool di "leve" creative: ad ogni run ne peschiamo una a caso per spingere il
# modello fuori dal solito titolo/argomento. Senza questo l'LLM converge sempre
# sullo stesso tema (es. "il futuro dell'AI") e sullo stesso titolo.
_ANGLES = [
    "a surprising real-world consequence",
    "a contrarian take most people get wrong",
    "a hidden risk nobody talks about",
    "a behind-the-scenes look at how it actually works",
    "a head-to-head comparison",
    "a beginner-friendly explainer of a complex idea",
    "a near-future prediction with concrete stakes",
    "a myth-busting deep dive",
    "an underrated tool or technique",
    "a story of a spectacular failure and its lesson",
    "a practical how-to people can use today",
    "a 'what if' thought experiment",
]
_FORMATS = [
    "listicle (top N)",
    "single big idea explained",
    "case study",
    "tutorial / walkthrough",
    "news reaction / analysis",
    "myth vs reality",
    "timeline / evolution story",
    "versus comparison",
]
_SUBTHEMES = [
    "AI models and capabilities",
    "AI tools for creators and productivity",
    "robotics and automation",
    "AI ethics, safety and regulation",
    "AI in everyday consumer tech",
    "the business and money behind AI",
    "AI hardware and chips",
    "open-source vs closed AI",
    "AI and jobs / the future of work",
    "breakthrough research and science",
]

TOPIC_PROMPT = """You are a viral YouTube content strategist for a tech/AI channel.

Today's date: {current_date}
Current strategy guidance: {strategy_notes}
Topic focus: {topic_focus}
{trending_block}
For THIS video, lean into:
- Sub-theme to explore: {subtheme}
- Creative angle: {angle}
- Content format: {fmt}

Generate ONE trending topic for a YouTube video about AI or technology.
Base your suggestion on what is relevant and trending as of {current_date}.
Use the trending news above as inspiration for a timely, high-interest angle — not a copy.
Make it clearly DIFFERENT from the recent topics below — new subject, new framing.
Avoid repeating these recent topics: {recent_topics}
Reply with ONLY the topic as a short phrase (3-7 words). No explanation, no punctuation."""

CONTENT_PROMPT = """You are a viral YouTube scriptwriter for a tech/AI channel.
Today's date: {current_date}

THE SUBJECT OF THIS VIDEO IS FIXED — it is: {topic}
Everything you write (title, script, description, tags, keywords) must be about
that exact subject. This is not negotiable.

Strategy guidance — this shapes HOW you write, never WHAT the video is about.
Adapt the title formula to the subject above; if the formula does not fit the
subject, keep the subject and drop the formula.
- Title style: {title_style}
- Tone: {tone}
- Hook strength: {hook_strength}
- Notes: {strategy_notes}

Target duration: {target_minutes} minutes = ~{target_words} words
Language: write title, description, tags and script entirely in {language}.

Reply ONLY with valid JSON, exact structure:
{{
  "title": "Compelling title, max 70 chars",
  "mood": "ONE word matching the video's emotional tone: epic | chill | mysterious | upbeat | tense",
  "thumbnail_phrase": "2-3 words MAX, ALL CAPS, bold visual hook for the thumbnail — never more than 3 words, never cut off",
  "thumbnail_font_size": "ONE letter: A (very large, best for 2 words), B (large, 2 words), C (medium, 2-3 words, default), D (smaller, 3 words)",
  "description": "SEO-optimized description, 200-300 words, include keywords naturally",
  "tags": ["tag1", "tag2", "tag3"],
  "script": "Full narration. Engaging, clear, conversational. Exactly {target_words} words. No headers or sections, flowing prose.",
  "video_keywords": [
    "AI neural network visualization",
    "futuristic city technology",
    "scientist working computer"
  ],
  "thumbnail_description": "Ultra-detailed image generation prompt for FLUX.1 diffusion model. Must be ONE paragraph, 80-120 words. Describe: exact scene composition, foreground subject with precise visual details (clothing, pose, expression, materials), background environment (architecture, lighting, atmosphere, depth), color palette (specific hues, contrast, saturation), lighting setup (direction, quality, color temperature, shadows), cinematic style, camera angle, lens feel. Professional YouTube thumbnail aesthetic, bold visual impact, 16:9 landscape orientation. NO text, logos, watermarks, or words of any kind — pure visual only. Example depth: 'Close-up photorealistic render of a humanoid robot with brushed titanium face and glowing cyan eye lenses, emerging from a dark server room filled with blue neon-lit racks, volumetric fog drifting at ankle level, dramatic rim lighting from the left casting sharp shadows, deep blacks and electric blue highlights, ultra-sharp detail, 85mm portrait lens perspective, cinematic anamorphic flare.'"
}}

Rules:
- title: follow the title_style guidance above
- mood: exactly one of epic, chill, mysterious, upbeat, tense — drives background music and thumbnail style
- thumbnail_phrase: EXACTLY 2-3 words, ALL CAPS, bold hook — never more than 3 words
- thumbnail_font_size: A if 2 short words, B if 2 longer words, C if 3 words (default), D if 3 long words
- tags: 12-15 relevant tags
- script: exactly {target_words} words, engaging pace, no bullet points
- video_keywords: 12-18 unique English phrases describing stock video scenes (2-4 words each, concrete and visual)
- thumbnail_description: ultra-detailed FLUX.1 prompt as described above, always present
"""


# Ritmo di lettura usato per convertire "durata_target_minuti" in numero di
# parole. Il valore precedente (130) sottostimava la velocita' reale di Edge
# TTS — misurata su due script veri: 147 e 159 parole/minuto — e i video
# uscivano circa il 18% piu' corti di quanto chiesto dall'utente.
# Sovrascrivibile con PAROLE_AL_MINUTO per voci piu' lente o piu' veloci.
PAROLE_AL_MINUTO_DEFAULT = 150


def _parole_al_minuto() -> float:
    try:
        return max(60.0, float(os.environ.get("PAROLE_AL_MINUTO",
                                              PAROLE_AL_MINUTO_DEFAULT)))
    except (TypeError, ValueError):
        return PAROLE_AL_MINUTO_DEFAULT


# Quanto lo script puo' scostarsi dal target prima di ritentare / arrendersi.
# La soglia buona e' il traguardo, quella minima il limite sotto cui il video
# non vale la pena di essere prodotto.
SOGLIA_SCRIPT_BUONA = 0.8
SOGLIA_SCRIPT_MINIMA = 0.5
TENTATIVI_CONTENUTO = 3
# quante volte il tema deve comparire nello script perche' un titolo che
# non lo nomina sia comunque accettabile
MIN_OCCORRENZE_TEMA = 3

# budget di token per il topic: i modelli reasoning ne spendono molti a
# ragionare prima di rispondere, e con 64 restavano senza spazio per il topic
MAX_TOKEN_TOPIC = 400
# il prompt chiede 3-7 parole: oltre questa soglia non e' un topic ma il
# monologo del modello ("We need to output a short phrase 3-7 words...")
MAX_PAROLE_TOPIC = 14
# frasi tipiche del ragionamento che non devono mai finire in un titolo
_SPIE_RAGIONAMENTO = (
    "we need to", "the user", "let me", "i should", "i need to", "okay,",
    "reply with", "no punctuation", "no explanation", "as an ai",
)


# parole troppo comuni per dimostrare che il contenuto parla del topic
_PAROLE_VUOTE = {
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "are",
    "how", "why", "what", "with", "from", "that", "this", "it", "its", "ai",
    "il", "lo", "la", "i", "gli", "le", "un", "uno", "una", "di", "del", "della",
    "da", "per", "con", "su", "che", "e", "o", "come", "perche", "perché",
    "dai", "ai", "agli", "alle", "nel", "sono", "essere", "oggi",
    "degli", "dei", "delle", "dalla", "dallo", "dagli", "sulla", "sullo",
    "nella", "nello", "negli", "nelle", "questo", "questa", "quello",
}


def _parole_chiave(testo: str) -> set:
    """Parole significative di un testo, per confrontare topic e contenuto.

    Separa anche sugli apostrofi: senza, l'articolo eliso italiano resta
    attaccato ("l'evoluzione") e non combacia mai con la stessa parola scritta
    da sola nel contenuto generato.
    """
    pezzi = re.split(r"[^0-9A-Za-zÀ-ÿ]+", (testo or "").lower())
    return {p for p in pezzi if len(p) > 3} - _PAROLE_VUOTE


def _ancore(testo: str) -> set:
    """Token del topic che NON cambiano traducendo: sigle (LLM, GPT), nomi
    propri (Nvidia) e numeri.

    Il confronto non puo' essere sulle parole comuni: qui il topic e' spesso
    scritto in italiano mentre il video viene generato in inglese, quindi
    "evoluzione" non comparirebbe mai in "The Evolution of...". Le sigle e i
    nomi propri invece restano identici in entrambe le lingue.
    """
    ancore = set()
    for pezzo in re.split(r"[^0-9A-Za-zÀ-ÿ\-]+", testo or ""):
        nudo = pezzo.strip("-")
        if len(nudo) < 3:
            continue
        parte_alfabetica = re.sub(r"[^A-Za-zÀ-ÿ]", "", nudo)
        ha_cifre = any(c.isdigit() for c in nudo)
        e_sigla = parte_alfabetica.isupper() and len(parte_alfabetica) >= 2
        e_nome_proprio = parte_alfabetica[:1].isupper() and not parte_alfabetica.isupper()
        if ha_cifre or e_sigla or e_nome_proprio:
            ancore.add(parte_alfabetica.lower() or nudo.lower())
    return {a for a in ancore if a and a not in _PAROLE_VUOTE}


def _prompt_senza_strategia(prompt: str) -> str:
    """Sostituisce le righe di strategia con istruzioni neutre."""
    neutro = {
        "- Title style:": "- Title style: clear and descriptive, name the subject explicitly",
        "- Tone:": "- Tone: confident and informative",
        "- Hook strength:": "- Hook strength: medium",
        "- Notes:": "- Notes: ignore any previous formula, write about the fixed subject above",
    }
    righe = []
    for riga in prompt.splitlines():
        sostituita = next((v for k, v in neutro.items() if riga.startswith(k)), None)
        righe.append(sostituita if sostituita else riga)
    return "\n".join(righe)


def _contenuto_fuori_tema(topic: str, content: dict) -> bool:
    """Il contenuto generato non parla affatto del topic richiesto.

    Con una strategia auto-appresa aggressiva (title_style come formula
    letterale ricavata dai video passati) il modello riscriveva il video su
    un altro argomento: topic "L'evoluzione degli LLM" -> titolo "He Struck a
    110mph Tennis Serve Blind". Il topic scelto dall'utente deve vincere.

    Controllo volutamente permissivo: scatta solo se il topic ha ancore
    riconoscibili e NESSUNA compare nel contenuto. Senza ancore non si giudica,
    per non bloccare la pipeline su un falso positivo.
    """
    ancore = _ancore(topic)
    if not ancore:
        return False
    # Contano titolo e script: sono loro a dire di cosa parla il video. Tag e
    # keyword non bastano — il modello ci infila le parole del topic anche
    # quando ha scritto tutt'altro (visto in produzione: titolo sul baseball
    # con "LLM" nei tag).
    titolo = (content.get("title") or "").lower()
    script = (content.get("script") or "").lower()
    if any(a in titolo for a in ancore):
        return False
    # titolo che non nomina il tema: si accetta solo se lo script ne parla
    # davvero, non se lo cita di sfuggita
    occorrenze = sum(script.count(a) for a in ancore)
    return occorrenze < MIN_OCCORRENZE_TEMA


def _topic_non_valido(topic: str, recent_norm: set) -> str:
    """Ritorna il motivo dello scarto, stringa vuota se il topic va bene."""
    t = (topic or "").strip()
    if not t:
        return "vuoto"
    if len(t.split()) > MAX_PAROLE_TOPIC:
        return f"{len(t.split())} parole, sembra il ragionamento del modello"
    basso = t.lower()
    if any(s in basso for s in _SPIE_RAGIONAMENTO):
        return "contiene frasi di ragionamento"
    if basso in recent_norm:
        return "gia' usato di recente"
    return ""


def _parse_json(text: str) -> dict:
    decoder = json.JSONDecoder()
    start = text.find("{")
    if start < 0:
        raise ValueError("No JSON found in model response")
    try:
        data, _ = decoder.raw_decode(text[start:])
    except json.JSONDecodeError as e:
        snippet = text[:500].replace("\n", "\\n")
        # JSON che finisce a metà = risposta tagliata dal limite di output del
        # provider, non un modello che sbaglia formato: dillo esplicitamente
        if e.pos >= len(text[start:]) - 2:
            raise ValueError(
                "Risposta del modello troncata (JSON incompleto): lo script "
                "richiesto non entra nel limite di output. Riduci "
                "durata_target_minuti oppure alza AI_MAX_TOKENS nel .env. "
                f"Dettaglio: {e}"
            ) from e
        raise ValueError(f"Invalid JSON from model: {e}; response starts with: {snippet}") from e
    required = {"title", "description", "tags", "script", "video_keywords"}
    missing = required - set(data)
    if missing:
        raise ValueError(f"Model JSON missing required fields: {', '.join(sorted(missing))}")
    return data


def _fetch_trending() -> str:
    try:
        from moduli.web_search import cerca_notizie
        results = cerca_notizie("AI technology breakthrough news 2026", max_results=5)
        if results:
            return f"\nTRENDING NOW (use as inspiration for a timely topic angle):\n{results}\n"
    except Exception:
        pass
    return ""


def genera_topic(strategy: dict = None, recent_topics: list = None) -> str:
    strategy = strategy or {}
    trending = _fetch_trending()
    angle = random.choice(_ANGLES)
    fmt = random.choice(_FORMATS)
    subtheme = random.choice(_SUBTHEMES)
    print(f"[cervello] Leve creative — tema:{subtheme} | angolo:{angle} | formato:{fmt}", flush=True)
    prompt = TOPIC_PROMPT.format(
        current_date=datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z"),
        strategy_notes=strategy.get("notes", "Standard approach"),
        topic_focus=strategy.get("topic_focus", "AI and technology trends"),
        recent_topics=", ".join(recent_topics or []) or "none",
        trending_block=trending,
        angle=angle,
        fmt=fmt,
        subtheme=subtheme,
    )
    recent_norm = {t.strip().lower() for t in (recent_topics or [])}

    def _clean(t: str) -> str:
        # il modello a volte avvolge il topic in virgolette nonostante il prompt
        return t.strip().strip('"“”‘’\' ')

    def _genera(p: str) -> str:
        # 64 token bastavano ai modelli normali ma non a quelli "reasoning",
        # che li spendevano tutti a ragionare
        return _clean(chat_ollama(p, max_tokens=MAX_TOKEN_TOPIC))

    topic = _genera(prompt)
    for _ in range(2):
        motivo = _topic_non_valido(topic, recent_norm)
        if not motivo:
            break
        print(f"[cervello] Topic scartato ({motivo}): {topic[:60]!r} — ritento", flush=True)
        prompt = (prompt.replace(angle, random.choice(_ANGLES))
                        .replace(subtheme, random.choice(_SUBTHEMES)))
        topic = _genera(prompt)
    if _topic_non_valido(topic, recent_norm):
        raise ValueError(
            f"Topic non utilizzabile dopo 3 tentativi: {topic[:120]!r}. "
            "Il modello non rispetta il formato richiesto — prova un altro "
            "modello (OLLAMA_CLOUD_MODEL / OPENROUTER_MODEL nel .env)."
        )
    return topic


def _strategia_neutra(strategy: dict) -> dict:
    """Tiene tono e ritmo, scarta cio' che detta l'ARGOMENTO.

    La strategia viene appresa dalle performance passate e su un canale con
    pochi dati degenera in una formula letterale (`title_style` del tipo
    "[Human] Achieved [Metric] — AI Gave Them [Ability]"). Il modello la
    segue alla lettera e riscrive il video su quell'argomento, ignorando il
    topic. Quando il topic lo ha scelto l'utente, deve vincere lui.
    """
    neutra = dict(strategy or {})
    neutra["title_style"] = ("clear and descriptive — the title must name the "
                             "subject of this video explicitly")
    neutra["notes"] = ("Write about the fixed subject above. Ignore any formula "
                       "or topic pattern learned from previous videos.")
    neutra.pop("topic_focus", None)
    neutra.pop("avoid_patterns", None)
    return neutra


CONTINUA_PROMPT = """You are continuing the narration script of a YouTube video.

Video subject: {topic}
Language: {language}

The script so far ends with these words:
...{coda}

Write ONLY the CONTINUATION — about {mancanti} more words — so the finished
script reaches roughly {target_words} words ({target_minutes} minutes of
narration).

Rules:
- Start exactly where the text above stops. Do not repeat it, do not summarise it.
- Do not restart the video, do not greet the viewer again.
- Flowing spoken prose, same voice and tone. No headings, no bullet points,
  no stage directions, no JSON, no quotes around the text.
- Add real substance: concrete examples, numbers, causes and consequences,
  objections and answers, then a proper closing with a call to action.
- Reply with the continuation text and nothing else."""

# quante parole della fine dello script si mostrano al modello per fargli
# riprendere il discorso senza rileggerlo tutto
_CODA_PAROLE = 90
MAX_CONTINUAZIONI = 2


def _ripulisci_continuazione(testo: str) -> str:
    """Toglie dalla continuazione tutto cio' che non e' narrazione.

    Il modello a volte incornicia la risposta ("Here is the continuation:",
    blocchi ```), e quel testo finirebbe letto ad alta voce dal TTS.
    """
    testo = (testo or "").strip()
    testo = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", testo).strip()
    righe = []
    for i, riga in enumerate(testo.splitlines()):
        pulita = riga.strip()
        if pulita.startswith("#"):
            continue
        # riga introduttiva del tipo "Continuation:" solo se e' la prima
        if i == 0 and pulita.endswith(":") and len(pulita.split()) <= 8:
            continue
        righe.append(pulita)
    return " ".join(r for r in righe if r).strip()


def _allunga_script(content: dict, topic: str, target_words: int,
                    soglia: int, language: str, target_minutes) -> dict:
    """Chiede al modello di CONTINUARE lo script finche' non arriva a target.

    Rigenerare l'intero JSON per uno script corto costa una chiamata piena e
    di solito restituisce un testo altrettanto corto: il modello "sente" quella
    lunghezza come completa. Continuare invece funziona, perche' il compito
    cambia — non "scrivi 1200 parole" ma "aggiungine altre 400".
    """
    script = (content.get("script") or "").strip()
    if not script:
        return content
    for _ in range(MAX_CONTINUAZIONI):
        parole = len(script.split())
        if parole >= soglia:
            break
        mancanti = max(target_words - parole, 80)
        prompt = CONTINUA_PROMPT.format(
            topic=topic,
            language=language,
            coda=" ".join(script.split()[-_CODA_PAROLE:]),
            mancanti=mancanti,
            target_words=target_words,
            target_minutes=target_minutes,
        )
        try:
            # ~2 token per parola, piu' margine per i modelli che ragionano
            aggiunta = _ripulisci_continuazione(
                chat_ollama(prompt, max_tokens=min(8192, mancanti * 2 + 600)))
        except Exception as e:
            print(f"[cervello] Continuazione fallita: {e}", flush=True)
            break
        nuove = len(aggiunta.split())
        # un modello che ignora "solo testo" e rispedisce il JSON del video
        # farebbe leggere al TTS le graffe e i nomi dei campi
        if aggiunta.lstrip().startswith("{") or '"script"' in aggiunta:
            print("[cervello] Continuazione scartata: il modello ha "
                  "risposto in JSON invece che in prosa", flush=True)
            break
        if nuove < 40:
            print(f"[cervello] Continuazione inutilizzabile ({nuove} parole)", flush=True)
            break
        script = f"{script} {aggiunta}".strip()
        print(f"[cervello] Script allungato: +{nuove} parole -> "
              f"{len(script.split())} (soglia {soglia})", flush=True)
    esteso = dict(content)
    esteso["script"] = script
    return esteso


def genera_contenuto(topic: str, strategy: dict = None,
                     topic_esplicito: bool = False) -> dict:
    strategy = strategy or {}
    if topic_esplicito:
        # topic scelto dall'utente: la strategia non puo' cambiargli argomento
        strategy = _strategia_neutra(strategy)
    try:
        from moduli.preferenze import carica
        pref = carica()
    except Exception:
        pref = {}
    target_minutes = pref.get("durata_target_minuti", 8)
    language = pref.get("lingua", "english") or "english"
    target_words = int(target_minutes * _parole_al_minuto())
    prompt = CONTENT_PROMPT.format(
        current_date=datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z"),
        topic=topic,
        title_style=strategy.get("title_style", "curiosity-driven"),
        tone=strategy.get("tone", "confident and informative"),
        hook_strength=strategy.get("hook_strength", "medium"),
        strategy_notes=strategy.get("notes", "Standard approach"),
        target_minutes=target_minutes,
        target_words=target_words,
        language=language,
    )
    # uno script troppo corto produce un video di pochi secondi: meglio
    # ritentare una volta e poi fallire con un errore chiaro che pubblicarlo
    # Gli LLM contano male le parole e tendono a stare sotto il target: con la
    # soglia al 50% un video "da 8 minuti" ne durava tranquillamente 4
    # (misurato: 433 parole invece di 600, video 2.7 min invece di 4).
    # Si ritenta puntando alla soglia buona; solo se tutti i tentativi
    # falliscono si tiene lo script piu' lungo ottenuto — meglio un video
    # corto che nessun video.
    soglia_buona = max(150, int(target_words * SOGLIA_SCRIPT_BUONA))
    soglia_minima = max(120, int(target_words * SOGLIA_SCRIPT_MINIMA))
    migliore = None
    last_err = None
    correzione = ""  # feedback sul tentativo precedente, vuoto al primo giro
    for attempt in range(TENTATIVI_CONTENUTO):
        try:
            content = _parse_json(chat_ollama(prompt + correzione, max_tokens=8192))
            if _contenuto_fuori_tema(topic, content):
                # La strategia auto-appresa sta imponendo il suo argomento:
                # per i tentativi successivi la si neutralizza, perche' un
                # topic scelto dall'utente vale piu' di una formula ricavata
                # dalle performance passate.
                prompt = _prompt_senza_strategia(prompt)
                raise ValueError(
                    f"Il contenuto non parla del topic richiesto: titolo "
                    f"{content.get('title', '')!r} per topic {topic!r}"
                )
            words = len(content.get("script", "").split())
            if words < soglia_buona:
                # Prima di buttare via tutto e rigenerare l'intero JSON, si
                # chiede al modello di CONTINUARE lo script: rigenerare da capo
                # riproduce quasi sempre la stessa lunghezza (misurato: 742 →
                # 646 → 696 su target 1200), mentre una continuazione parte da
                # cio' che c'e' gia' e aggiunge solo quello che manca.
                content = _allunga_script(content, topic, target_words,
                                          soglia_buona, language, target_minutes)
                words = len(content.get("script", "").split())
            if words >= soglia_buona:
                return content
            if migliore is None or words > len(migliore.get("script", "").split()):
                migliore = content
            # Ritentare con lo STESSO prompt non serviva a niente: il modello
            # riproduceva la stessa lunghezza (misurato: 742 → 646 → 696 parole
            # su un target di 1200). Ora il tentativo successivo sa quanto è
            # stato corto e di quanto deve allungare.
            mancanti = max(soglia_buona - words, 0)
            correzione = (
                f"\n\nRETRY — YOUR PREVIOUS ATTEMPT FAILED.\n"
                f"The \"script\" field contained only {words} words. That is far "
                f"too short for a {target_minutes}-minute video.\n"
                f"This attempt MUST contain AT LEAST {soglia_buona} words in "
                f"\"script\" (about {mancanti} words more than last time), "
                f"ideally {target_words}.\n"
                f"Do not summarise. Expand with concrete detail: real examples, "
                f"numbers, causes, consequences, objections and answers, a longer "
                f"opening hook and a proper closing. Keep it flowing prose, same "
                f"topic, same JSON structure."
            )
            last_err = ValueError(
                f"Script corto: {words} parole invece di ~{target_words} "
                f"per un video da {target_minutes} minuti"
            )
            print(f"[cervello] {last_err} (tentativo {attempt + 1}/"
                  f"{TENTATIVI_CONTENUTO})", flush=True)
        except ValueError as e:
            last_err = e
            print(f"[cervello] Contenuto non valido (tentativo {attempt + 1}/"
                  f"{TENTATIVI_CONTENUTO}): {e}", flush=True)

    if migliore is not None:
        parole = len(migliore.get("script", "").split())
        if parole >= soglia_minima:
            print(f"[cervello] Nessun tentativo ha raggiunto {soglia_buona} parole: "
                  f"uso il piu' lungo ({parole}, ~{parole / _parole_al_minuto():.1f} "
                  f"min invece di {target_minutes})", flush=True)
            return migliore
    raise last_err
