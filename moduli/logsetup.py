"""Log persistente su file: tee di stdout/stderr verso logs/agent.log.

Tutti i moduli usano print() — invece di convertirli al modulo logging,
intercettiamo i flussi: ogni riga finisce sia in console sia nel file
rotante. Con pythonw (avvia_agente.bat) stdout è None e i log andrebbero
persi del tutto: qui restano su file.
"""

import io
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "agent.log")
MAX_BYTES = 2_000_000
BACKUP_COUNT = 3

_installato = False


class _Tee(io.TextIOBase):
    def __init__(self, originale, logger: logging.Logger, level: int):
        self._orig = originale
        self._logger = logger
        self._level = level
        self._buf = ""

    def write(self, s: str) -> int:
        if self._orig is not None:
            try:
                self._orig.write(s)
            except Exception:
                pass
        self._buf += s
        while "\n" in self._buf:
            riga, self._buf = self._buf.split("\n", 1)
            if riga.strip():
                try:
                    self._logger.log(self._level, riga)
                except Exception:
                    pass
        return len(s)

    def flush(self) -> None:
        if self._orig is not None:
            try:
                self._orig.flush()
            except Exception:
                pass

    @property
    def encoding(self) -> str:
        return getattr(self._orig, "encoding", None) or "utf-8"

    def isatty(self) -> bool:
        try:
            return bool(self._orig and self._orig.isatty())
        except Exception:
            return False


def forza_utf8() -> None:
    """Porta stdout/stderr su UTF-8 con sostituzione dei caratteri illeggibili.

    Log, messaggi e nomi di clip contengono frecce ed emoji. Su Windows lo
    stream di default è cp1252 quando l'output è rediretto (pipe, file, .bat),
    e una singola freccia solleva UnicodeEncodeError: l'eccezione risale la
    pipeline e la interrompe a metà per un carattere di log.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass  # stream sostituito o non riconfigurabile (pythonw): amen


def setup() -> None:
    """Idempotente: installa il tee una sola volta."""
    global _installato
    if _installato:
        return
    forza_utf8()
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        handler = RotatingFileHandler(
            LOG_FILE, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        )
        logger = logging.getLogger("tube_assistant")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.addHandler(handler)
        sys.stdout = _Tee(sys.stdout, logger, logging.INFO)
        sys.stderr = _Tee(sys.stderr, logger, logging.ERROR)
        _installato = True
    except OSError:
        # disco/permessi: meglio senza log su file che senza agente
        pass
