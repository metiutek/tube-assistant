"""Schermata di configurazione della sorgente clip (stock Pexels / AI video).

Vive qui e non dentro `wizard.py` perche' la usano in due: il wizard al primo
avvio e la voce "Settings" della TUI. Duplicarla avrebbe voluto dire due elenchi
di provider da tenere allineati a mano, ed e' esattamente cosi' che una delle
due finisce per proporre un modello che l'altra non sa configurare.

Scrive solo nel `.env` del workspace: le chiavi API non stanno in
`preferenze_video.json`, che finisce nei backup e nei log.
"""

import os
from pathlib import Path

from moduli.video_ai import PROVIDER_DEFAULTS

# Le tre modalita', nell'ordine in cui vanno proposte.
SORGENTI = {
    "1": {
        "id": "pexels",
        "nome": "Pexels (stock, gratis)",
        "desc": "Clip reali scaricate da Pexels. Nessun costo, nessuna chiave in piu'.",
    },
    "2": {
        "id": "ibrido",
        "nome": "Ibrido (consigliato)",
        "desc": "L'AI genera le prime clip — l'aggancio del video — Pexels riempie il resto.",
    },
    "3": {
        "id": "ai",
        "nome": "Solo AI",
        "desc": "Ogni clip generata. Massima aderenza allo script, costo piu' alto.",
    },
}


def _env_file(workspace: str | Path = ".") -> Path:
    return Path(workspace) / ".env"


def leggi_env(workspace: str | Path = ".") -> dict:
    from dotenv import dotenv_values
    percorso = _env_file(workspace)
    return dict(dotenv_values(percorso)) if percorso.exists() else {}


def scrivi_env(workspace: str | Path, chiave: str, valore: str) -> None:
    from dotenv import set_key
    percorso = _env_file(workspace)
    percorso.touch(exist_ok=True)
    set_key(str(percorso), chiave, valore, quote_mode="never")
    # il processo corrente deve vedere subito la modifica, altrimenti un test
    # di generazione lanciato qui userebbe ancora la configurazione vecchia
    os.environ[chiave] = valore


def stato(workspace: str | Path = ".") -> str:
    """Riga di riepilogo leggibile, usata anche da `tube-assistant status`."""
    env = leggi_env(workspace)
    sorgente = (env.get("VIDEO_SOURCE") or "pexels").strip().lower()
    if sorgente not in {s["id"] for s in SORGENTI.values()}:
        sorgente = "pexels"
    if sorgente == "pexels":
        return "Pexels (stock)"
    prov = (env.get("VIDEO_AI_PROVIDER") or "replicate").strip().lower()
    conf = PROVIDER_DEFAULTS.get(prov, PROVIDER_DEFAULTS["replicate"])
    modello = (env.get("VIDEO_AI_MODEL") or "").strip() or conf["modello"]
    chiave = "✓" if (env.get(conf["env_key"]) or "").strip() else "✗ chiave mancante"
    etichetta = "solo AI" if sorgente == "ai" else f"ibrido (max {env.get('VIDEO_AI_MAX_CLIPS', '6')} clip AI)"
    return f"{etichetta} — {conf['etichetta']} / {modello} [{chiave}]"


def _prova_generazione(console) -> None:
    """Genera una clip vera per verificare chiave e modello.

    Costa: per questo si chiede prima, e si dice quanto dura l'attesa. Un
    provider mal configurato scoperto qui vale molto di piu' che scoperto a
    meta' pipeline, tre ore dopo, con il video gia' senza b-roll.
    """
    from moduli import video_ai

    console.print()
    console.print("  [dim]Il test genera UNA clip vera: consuma credito sul provider "
                  "e puo' richiedere 1-3 minuti.[/]")
    from rich.prompt import Confirm
    if not Confirm.ask("  Vuoi provare adesso?", default=False):
        return
    console.print()
    try:
        from rich.progress import Progress, SpinnerColumn, TextColumn
        with Progress(SpinnerColumn(), TextColumn("[cyan]Generazione in corso..."),
                      transient=True) as p:
            p.add_task("")
            percorso = video_ai.genera_clip("futuristic laboratory close up", durata=5)
        dimensione = os.path.getsize(percorso) / (1024 ** 2)
        console.print(f"  [bold green]✓[/]  Clip generata: {percorso} ({dimensione:.1f} MB)")
    except Exception as e:
        console.print(f"  [bold red]✗[/]  {e}")
        console.print("  [dim]Controlla chiave e nome del modello. La pipeline "
                      "ripiegherebbe comunque su Pexels.[/]")


def configura(console, workspace: str | Path = ".", intestazione=None) -> str:
    """Menu interattivo. Ritorna la sorgente scelta ('pexels'|'ibrido'|'ai')."""
    from rich.prompt import Prompt

    env = leggi_env(workspace)
    if intestazione:
        intestazione()

    attuale = (env.get("VIDEO_SOURCE") or "pexels").strip().lower()
    console.print("  [bold]Da dove arrivano le clip del video:[/]\n")
    for num, s in SORGENTI.items():
        segno = "[bold green]•[/]" if s["id"] == attuale else " "
        console.print(f"  {segno} [cyan]{num}[/]  [bold]{s['nome']}[/]")
        console.print(f"       [dim]{s['desc']}[/]")
    console.print()

    default = next((n for n, s in SORGENTI.items() if s["id"] == attuale), "1")
    scelta = Prompt.ask("  Scegli", choices=list(SORGENTI), default=default)
    sorgente = SORGENTI[scelta]["id"]
    scrivi_env(workspace, "VIDEO_SOURCE", sorgente)

    if sorgente == "pexels":
        console.print()
        console.print("  [bold green]✓[/]  Clip stock da Pexels — nessuna chiave in piu'.")
        return sorgente

    # ── provider ─────────────────────────────────────────────────────────────
    console.print()
    console.print("  [bold]Servizio di generazione video:[/]\n")
    elenco = list(PROVIDER_DEFAULTS.items())
    for i, (pid, conf) in enumerate(elenco, start=1):
        console.print(f"  [cyan]{i}[/]  [bold]{conf['etichetta']}[/]  "
                      f"[dim]— modello di default: {conf['modello']}[/]")
        if conf.get("nota"):
            console.print(f"       [dim]{conf['nota']}[/]")
        gia = "  [green](chiave gia' configurata)[/]" if (env.get(conf["env_key"]) or "").strip() else ""
        console.print(f"       [dim]chiave: {conf['env_key']} — {conf['hint']}[/]{gia}")
    console.print()

    attuale_prov = (env.get("VIDEO_AI_PROVIDER") or "replicate").strip().lower()
    default_prov = next((str(i) for i, (pid, _) in enumerate(elenco, start=1)
                         if pid == attuale_prov), "1")
    idx = Prompt.ask("  Scegli", choices=[str(i) for i in range(1, len(elenco) + 1)],
                     default=default_prov)
    provider_id, conf = elenco[int(idx) - 1]
    scrivi_env(workspace, "VIDEO_AI_PROVIDER", provider_id)

    # ── chiave ───────────────────────────────────────────────────────────────
    console.print()
    esistente = (env.get(conf["env_key"]) or "").strip()
    if esistente:
        console.print(f"  [dim]Chiave {conf['env_key']} gia' presente "
                      f"(...{esistente[-4:]}). Invio per tenerla.[/]")
    else:
        console.print(f"  [dim cyan]→[/]  Crea una chiave su: {conf['hint']}")
    chiave = Prompt.ask(f"  [bold]{conf['env_key']}[/]", default=esistente or "",
                        show_default=False)
    if chiave.strip():
        scrivi_env(workspace, conf["env_key"], chiave.strip())

    # ── modello ──────────────────────────────────────────────────────────────
    console.print()
    console.print(f"  [dim]Modello: invio per il default ({conf['modello']}). "
                  f"Su questi servizi i modelli cambiano spesso: se ne esce uno "
                  f"migliore basta scriverlo qui.[/]")
    modello = Prompt.ask("  [bold]Modello[/]",
                         default=(env.get("VIDEO_AI_MODEL") or "").strip() or conf["modello"])
    scrivi_env(workspace, "VIDEO_AI_MODEL", modello.strip() or conf["modello"])

    # ── tetto di spesa ───────────────────────────────────────────────────────
    console.print()
    if sorgente == "ibrido":
        console.print("  [dim]Quante clip generare per video. E' il tetto di spesa: "
                      "il resto del montaggio usa Pexels.[/]")
        default_max = (env.get("VIDEO_AI_MAX_CLIPS") or "6").strip()
    else:
        console.print("  [bold yellow]⚠[/]  In modalita' 'solo AI' un video da 8 minuti "
                      "sono ~96 clip da generare.")
        console.print("  [dim]Sotto questo numero il montaggio riusa le clip generate "
                      "invece di scaricarne altre.[/]")
        default_max = (env.get("VIDEO_AI_MAX_CLIPS") or "12").strip()
    massimo = Prompt.ask("  [bold]Massimo clip AI per video[/]", default=default_max)
    try:
        massimo = str(max(1, int("".join(c for c in massimo if c.isdigit()) or default_max)))
    except ValueError:
        massimo = default_max
    scrivi_env(workspace, "VIDEO_AI_MAX_CLIPS", massimo)

    console.print()
    console.print(f"  [bold green]✓[/]  {stato(workspace)}")
    _prova_generazione(console)
    return sorgente
