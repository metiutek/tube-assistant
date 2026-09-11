"""
Shared state.json I/O with threading lock.
Imported by both agent.py and telegram_handler.py so concurrent
reads/writes between pipeline thread and bot thread don't corrupt the file.
"""
import json
import os
import shutil
import threading

_LOCK = threading.Lock()
STATE_FILE = "state.json"


def load_state() -> dict:
    with _LOCK:
        return _load()


def save_state(state: dict) -> None:
    with _LOCK:
        _save(state)


def _leggi(percorso: str) -> dict | None:
    """Legge un file di stato e lo accetta solo se e' un oggetto JSON.

    `state.json` e' una superficie di controllo modificabile a mano: un edit
    sbagliato puo' produrre `[...]` o `null`. Restituire quel valore fa
    esplodere il primo `state.get(...)` con AttributeError, cioe' il daemon
    non parte piu' e l'errore non dice qual e' il problema.
    """
    try:
        with open(percorso, encoding="utf-8") as f:
            data = f.read().strip()
    except OSError:
        return None
    if not data:
        return {}
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        print(f"[state] {percorso} non contiene un oggetto JSON "
              f"(trovato {type(parsed).__name__}): ignorato", flush=True)
        return None
    return parsed


def _load() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    stato = _leggi(STATE_FILE)
    if stato is not None:
        return stato
    backup = f"{STATE_FILE}.bak"
    if os.path.exists(backup):
        stato = _leggi(backup)
        if stato is not None:
            print(f"[state] ripristinato da {backup}", flush=True)
            return stato
    return {}


def _save(state: dict) -> None:
    backup = f"{STATE_FILE}.bak"
    tmp = f"{STATE_FILE}.tmp"
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                json.load(f)
            shutil.copy2(STATE_FILE, backup)
        except Exception:
            pass
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)
