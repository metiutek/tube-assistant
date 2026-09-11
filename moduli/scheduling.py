"""Calcolo unico degli orari di produzione e pubblicazione.

Prima esistevano tre implementazioni scollegate (`agent._get_trigger_hours`,
`telegram_handler._publish_hours` e `pubblica.calcola_publish_slots`) con
priorita' diverse: con l'auto-scheduling attivo il daemon produceva su
`best_hours_utc` mentre `/status` e `/prossimi` mostravano `publish_hours_utc`,
cioe' un orario che non corrispondeva a nulla. Qui la priorita' e' una sola e
tutti i chiamanti la condividono.

Priorita' orari di PUBBLICAZIONE:
  1. auto_scheduling + best_hours_utc   (orari scelti dalle analytics)
  2. publish_hours_utc                  (impostati a mano con /setpubblica)
  3. spread di default per videos_per_day

Gli orari di PRODUZIONE sono quelli di pubblicazione meno TRIGGER_LEAD_HOURS,
salvo `trigger_hours_utc` esplicito in state.json che vince su tutto.
"""

from datetime import datetime, timedelta, timezone

# la produzione parte 3 ore prima dell'orario di pubblicazione
TRIGGER_LEAD_HOURS = 3
DEFAULT_VIDEOS_PER_DAY = 1
# default storico usato solo quando non e' configurato NULLA
DEFAULT_TRIGGER_HOURS = [14]
DEFAULT_PUBLISH_HOURS = [20]

# spread consigliati per numero di video al giorno (mattina/pomeriggio/sera)
PUBLISH_SPREADS = {
    1: [20],
    2: [14, 20],
    3: [10, 15, 20],
    4: [9, 13, 17, 21],
    5: [8, 11, 14, 17, 20],
}


def videos_per_day(state: dict) -> int:
    try:
        return max(1, int(state.get("videos_per_day", DEFAULT_VIDEOS_PER_DAY)))
    except (TypeError, ValueError):
        return DEFAULT_VIDEOS_PER_DAY


def _ore_valide(hours) -> list[int]:
    """Scarta valori non numerici o fuori range finiti in state.json a mano."""
    valide = []
    for h in hours or ():
        try:
            h = int(h)
        except (TypeError, ValueError):
            continue
        if 0 <= h <= 23:
            valide.append(h)
    return valide


def publish_hours(state: dict) -> list[int]:
    """Orari di pubblicazione UTC, ordinati e limitati a videos_per_day."""
    n = videos_per_day(state)
    hours = []
    if state.get("auto_scheduling"):
        hours = _ore_valide(state.get("best_hours_utc"))
    if not hours:
        hours = _ore_valide(state.get("publish_hours_utc"))
    if not hours:
        hours = PUBLISH_SPREADS.get(n, DEFAULT_PUBLISH_HOURS)
    return sorted(dict.fromkeys(hours))[:n]


def trigger_hours(state: dict) -> list[int]:
    """Orari UTC in cui la pipeline deve partire, ordinati e limitati a
    videos_per_day. `trigger_hours_utc` esplicito vince su tutto."""
    n = videos_per_day(state)
    espliciti = _ore_valide(state.get("trigger_hours_utc"))
    if espliciti:
        return sorted(dict.fromkeys(espliciti))[:n]
    non_configurato = (
        (not state.get("auto_scheduling") or not _ore_valide(state.get("best_hours_utc")))
        and not _ore_valide(state.get("publish_hours_utc"))
    )
    if non_configurato and n == 1:
        # nessuna configurazione, un video al giorno: default storico
        return sorted(DEFAULT_TRIGGER_HOURS)[:n]
    # con piu' video al giorno il default storico dava un solo trigger per N
    # video: gli altri non partivano mai. Deriva sempre dallo spread.
    return sorted((h - TRIGGER_LEAD_HOURS) % 24 for h in publish_hours(state))[:n]


def prossima_pubblicazione(state: dict, run_index: int = 0,
                           now: datetime | None = None) -> datetime:
    """Datetime di pubblicazione per il run_index-esimo video di oggi."""
    now = now or datetime.now(timezone.utc)
    hours = publish_hours(state)
    hour = hours[run_index % len(hours)]
    candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate
