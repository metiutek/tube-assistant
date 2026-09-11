"""One-shot pipeline — imported by _launcher after os.chdir(workspace).

Delegato sottile verso `main.run`: prima questo file conteneva una TERZA
copia della pipeline (dopo `agent.py` e `main.py`) ed era rimasta indietro —
scaricava una sola clip per keyword col vecchio `scarica_clip`, non proteggeva
la cache dalla pulizia e ignorava la coda dei topic. Ed e' proprio la copia
che esegue la CLI (`tube-assistant run` / `dry-run`).
"""

from main import run


__all__ = ["run"]
