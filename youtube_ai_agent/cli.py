"""
TubeAssistant — CLI entry point.

Usage:
    tube-assistant              # interactive TUI menu
    tube-assistant onboard      # first-time setup wizard
    tube-assistant start        # run the daemon
    tube-assistant run          # one-shot pipeline
    tube-assistant dry-run      # generate locally without upload
    tube-assistant clips        # clip source: Pexels stock or AI video
    tube-assistant preflight    # check local runtime configuration
    tube-assistant status       # workspace summary
    tube-assistant workspace    # print workspace path
"""

import os
import sys
import subprocess
import json
from pathlib import Path

from youtube_ai_agent._workspace import get as get_workspace, scaffold


# ── cross-platform key reader ─────────────────────────────────────────────────

def _console_interattiva() -> bool:
    """True solo se possiamo davvero leggere i tasti freccia.

    Senza questo controllo il menu si disegnava e poi restava appeso per
    sempre: `msvcrt.getwch()` (e `sys.stdin.read`) bloccano anche quando
    stdin non e' un terminale — output rediretto, pipe, Git Bash/mintty,
    CI — e l'utente non aveva modo di uscire se non chiudendo la finestra.
    """
    try:
        if not sys.stdin.isatty():
            return False
    except Exception:
        return False
    if sys.platform == "win32":
        # mintty (Git Bash) espone una pipe, non una console Windows:
        # msvcrt legge dalla console reale e non vedrebbe mai un tasto.
        try:
            import ctypes
            handle = ctypes.windll.kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
            mode = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
        except Exception:
            return False
    return True


def _read_key() -> str:
    """Return 'UP', 'DOWN', 'ENTER', 'ESC' or 'EOF' (stdin chiuso)."""
    if sys.platform == "win32":
        import msvcrt
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            ch2 = msvcrt.getwch()
            if ch2 == "H": return "UP"
            if ch2 == "P": return "DOWN"
            return ""
        if ch == "\r":  return "ENTER"
        if ch == "\x1b": return "ESC"
        # in raw mode Ctrl+C arriva come carattere, non come segnale
        if ch in ("\x03", "\x04"): raise KeyboardInterrupt
        if ch == "": return "EOF"
        return ""
    else:
        import tty, termios
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == "":  # EOF: stdin chiuso, altrimenti loop infinito
                return "EOF"
            if ch == "\x1b":
                ch2 = sys.stdin.read(1)
                if ch2 == "[":
                    ch3 = sys.stdin.read(1)
                    if ch3 == "A": return "UP"
                    if ch3 == "B": return "DOWN"
                return "ESC"
            if ch in ("\r", "\n"): return "ENTER"
            if ch in ("\x03", "\x04"): raise KeyboardInterrupt
            return ""
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ── TUI menu ──────────────────────────────────────────────────────────────────

_MENU_ITEMS = [
    ("Start daemon",   "start"),
    ("One-shot video", "run"),
    ("Dry run",        "dry-run"),
    ("Setup wizard",   "onboard"),
    ("Video clips",    "clips"),
    ("Preflight",      "preflight"),
    ("Update",         "update"),
    ("Status",         "status"),
    ("Quit",           None),
]


def _enable_ansi() -> None:
    """Enable ANSI escape code processing on Windows 10+."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass


def _tui_menu() -> None:
    if not _console_interattiva():
        print(_HELP)
        print("  (nessun terminale interattivo: usa un comando esplicito, "
              "es. `tube-assistant status`)\n")
        return

    _enable_ansi()
    try:
        from rich.console import Console
    except ImportError:
        print(_HELP)
        return

    console = Console()
    selected = 0

    # number of lines the menu occupies (used to move cursor back up on redraw)
    # blank + title + blank + N items + blank + hint = N + 5
    _MENU_HEIGHT = len(_MENU_ITEMS) + 5

    def _render(sel: int, first: bool = False) -> None:
        lines = []
        lines.append("")
        lines.append("  [bold red]TubeAssistant[/]")
        lines.append("")
        for i, (label, _) in enumerate(_MENU_ITEMS):
            if i == sel:
                lines.append(f"  [bold cyan]> {label}[/]")
            else:
                lines.append(f"    [dim]{label}[/]")
        lines.append("")
        lines.append("  [dim]↑/↓  enter  esc[/]")

        if not first:
            # move cursor up to overwrite previous render
            sys.stdout.write(f"\033[{_MENU_HEIGHT}A")
            sys.stdout.flush()

        for line in lines:
            # clear the line then print
            sys.stdout.write("\033[2K")
            sys.stdout.flush()
            console.print(line)

    _render(selected, first=True)

    while True:
        key = _read_key()
        if key == "UP":
            selected = (selected - 1) % len(_MENU_ITEMS)
            _render(selected)
        elif key == "DOWN":
            selected = (selected + 1) % len(_MENU_ITEMS)
            _render(selected)
        elif key == "ENTER":
            _, cmd = _MENU_ITEMS[selected]
            print()
            if cmd is None:
                sys.exit(0)
            _COMMANDS[cmd]()
            return
        elif key in ("ESC", "EOF"):
            print()
            sys.exit(0)


# ── helpers ───────────────────────────────────────────────────────────────────

def _python() -> str:
    return sys.executable


def _launch(workspace: Path, command: str) -> int:
    # Con l'output rediretto (log, pipe) lo stdout del padre e' bufferizzato:
    # senza questo flush le righe "Starting/Running ... in: <workspace>"
    # comparivano DOPO tutto l'output del figlio, cioe' a pipeline finita.
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        return subprocess.run(
            [_python(), "-m", "youtube_ai_agent._launcher", str(workspace), command],
            cwd=str(workspace),
        ).returncode
    except KeyboardInterrupt:
        # Ctrl+C arriva a tutto il gruppo di processi: il figlio si sta gia'
        # fermando da solo, qui serve solo non stampare un traceback.
        print("\n  Interrotto.\n")
        return 130


def _gia_configurato(workspace: Path) -> bool:
    """Workspace di fatto pronto, anche senza il marker `.setup_done`.

    Il marker lo scrive solo il wizard: chi ha configurato a mano (procedura
    documentata nel README) o ha perso il file veniva rispedito al wizard a
    ogni comando, con il rischio di sovrascrivere una configurazione che
    funziona. Serve un .env con la chiave AI e Pexels, piu' le credenziali
    Google gia' autorizzate.
    """
    from dotenv import dotenv_values

    if not (workspace / "credentials.json").exists() or not (workspace / "token.json").exists():
        return False
    try:
        env = dotenv_values(workspace / ".env")
    except Exception:
        return False

    # i segnaposto del template (`your_pexels_key_here`) non sono
    # configurazione: contarli come tale saltava il wizard su un workspace
    # in realta' vuoto
    from moduli.preflight import AI_KEY_BY_SERVICE, _e_placeholder

    def _valorizzata(nome: str) -> bool:
        valore = (env.get(nome) or "").strip()
        return bool(valore) and not _e_placeholder(valore)

    if not _valorizzata("PEXELS_API_KEY"):
        return False
    servizio = (env.get("AI_SERVICE") or "").strip()
    if not servizio:
        return False
    chiave = AI_KEY_BY_SERVICE.get(servizio)
    # ollama_local non richiede chiave
    return chiave is None or _valorizzata(chiave)


def _check_setup(workspace: Path) -> bool:
    if (workspace / ".setup_done").exists():
        return True
    if _gia_configurato(workspace):
        # niente wizard: la configurazione c'e' gia', segna il marker e prosegui
        try:
            (workspace / ".setup_done").touch()
        except OSError:
            pass
        return True
    print("\n  Setup required — launching wizard...\n")
    cmd_onboard()
    return False


def _print_status(workspace: Path) -> None:
    env_file   = workspace / ".env"
    state_file = workspace / "state.json"

    print(f"\n  Workspace : {workspace}")
    print(f"  .env      : {'✓' if env_file.exists() else '✗ missing'}")
    print(f"  creds     : {'✓' if (workspace / 'credentials.json').exists() else '✗ missing'}")
    if (workspace / ".setup_done").exists():
        stato_setup = "✓ done"
    elif _gia_configurato(workspace):
        stato_setup = "✓ configurato a mano"
    else:
        stato_setup = "✗ run onboard"
    print(f"  setup     : {stato_setup}")
    try:
        from dotenv import load_dotenv
        from moduli.impostazioni_video import stato as _stato_clip
        load_dotenv(env_file)
        print(f"  clip      : {_stato_clip(workspace)}")
    except Exception:
        pass

    if state_file.exists():
        try:
            state  = json.loads(state_file.read_text(encoding="utf-8"))
            queue  = state.get("topic_queue", [])
            videos = state.get("video_ids", [])
            vpd    = state.get("videos_per_day", 1)
            print(f"  topics    : {len(queue)} in queue")
            print(f"  videos    : {len(videos)} published")
            print(f"  vpd       : {vpd}")
            if videos:
                print(f"  last      : https://youtu.be/{videos[0]}")
        except Exception:
            pass
    print()


# ── commands ──────────────────────────────────────────────────────────────────

def cmd_onboard() -> None:
    workspace = get_workspace()
    scaffold(workspace)
    sys.exit(_launch(workspace, "wizard"))


def cmd_start() -> None:
    workspace = get_workspace()
    if not _check_setup(workspace):
        sys.exit(1)
    scaffold(workspace)
    print(f"\n  Starting TubeAssistant in: {workspace}")
    print("  Press Ctrl+C to stop.\n")
    sys.exit(_launch(workspace, "agent"))


def cmd_run() -> None:
    workspace = get_workspace()
    if not _check_setup(workspace):
        sys.exit(1)
    scaffold(workspace)
    print(f"\n  Running one-shot pipeline in: {workspace}\n")
    sys.exit(_launch(workspace, "main"))


def cmd_dry_run() -> None:
    workspace = get_workspace()
    if not _check_setup(workspace):
        sys.exit(1)
    scaffold(workspace)
    print(f"\n  Running dry-run pipeline in: {workspace}\n")
    sys.exit(_launch(workspace, "dry-run"))


def cmd_update() -> None:
    import subprocess
    print("\n  Aggiornamento in corso...\n")
    try:
        result = subprocess.run(
            ["uv", "tool", "install", "--force",
             "git+https://github.com/metiu1/tube-assistant.git"],
            text=True,
        )
    except (FileNotFoundError, OSError):
        # installazione da sorgente (pip / installa.bat): `uv` puo' non esserci
        print("\n  ✗ `uv` non trovato in PATH.\n")
        print("    Da sorgente aggiorna con:  git pull && pip install -e .")
        print("    Oppure installa uv:        pip install uv\n")
        sys.exit(1)
    if result.returncode == 0:
        print("\n  ✓ Aggiornamento completato. Riavvia con: tube-assistant\n")
    else:
        print("\n  ✗ Aggiornamento fallito. Riprova manualmente:\n")
        print("    uv tool install --force git+https://github.com/metiu1/tube-assistant.git\n")
    sys.exit(0)


def cmd_status() -> None:
    workspace = get_workspace()
    _print_status(workspace)


def cmd_preflight() -> None:
    workspace = get_workspace()
    scaffold(workspace)
    from dotenv import load_dotenv
    from moduli.preflight import format_checks, run_checks
    load_dotenv(workspace / ".env")
    checks = run_checks(workspace)
    print()
    print(format_checks(checks))
    print()
    sys.exit(0 if all(c["ok"] for c in checks) else 1)


def cmd_clips() -> None:
    """Sorgente delle clip: stock Pexels, AI video, o le due insieme.

    Sta nel menu e non solo nel wizard perche' e' l'impostazione che si cambia
    piu' spesso: si prova l'AI per qualche video, si guarda la fattura, si
    torna all'ibrido. Rifare tutto l'onboarding per questo — e rischiare di
    sovrascrivere token e credenziali che funzionano — non ha senso.
    """
    workspace = get_workspace()
    scaffold(workspace)
    try:
        from rich.console import Console
    except ImportError:
        print("\n  Serve 'rich' per questa schermata:  pip install rich\n")
        sys.exit(1)
    from dotenv import load_dotenv
    load_dotenv(workspace / ".env")
    # le cache (`cache/ai_video/`) sono percorsi relativi: senza questo, una
    # clip di prova generata da qui finirebbe nella directory da cui si e'
    # lanciato il comando invece che nel workspace
    os.chdir(workspace)
    from moduli.impostazioni_video import configura

    console = Console()

    def _intestazione() -> None:
        console.print()
        console.print("  [bold cyan]Sorgente delle clip video[/]")
        console.print(f"  [dim]{workspace}[/]")
        console.print()

    configura(console, workspace=workspace, intestazione=_intestazione)
    console.print()
    sys.exit(0)


def cmd_workspace() -> None:
    print(get_workspace())


# ── dispatch ──────────────────────────────────────────────────────────────────

_COMMANDS = {
    "onboard":   cmd_onboard,
    "start":     cmd_start,
    "run":       cmd_run,
    "dry-run":   cmd_dry_run,
    "clips":     cmd_clips,
    "preflight": cmd_preflight,
    "update":    cmd_update,
    "status":    cmd_status,
    "workspace": cmd_workspace,
}

_HELP = """\
TubeAssistant

Usage:
  tube-assistant              interactive menu
  tube-assistant onboard      first-time setup
  tube-assistant start        run the daemon
  tube-assistant run          one-shot pipeline
  tube-assistant dry-run      generate locally without upload
  tube-assistant clips        clip source: Pexels stock or AI video generation
  tube-assistant preflight    check local runtime configuration
  tube-assistant status       workspace summary
  tube-assistant workspace    print workspace path

Environment:
  YOUTUBE_AI_WORKSPACE   override workspace directory
"""


def main() -> None:
    # I messaggi della CLI usano ✓/✗ e box-drawing: su Windows con stdout
    # cp1252 (output rediretto, pipe, file di log) un solo carattere fa
    # crashare il comando con UnicodeEncodeError a meta' stampa.
    try:
        from moduli.logsetup import forza_utf8
        forza_utf8()
    except Exception:
        pass

    args = sys.argv[1:]

    try:
        if not args:
            _tui_menu()
            return

        if args[0] in ("-h", "--help", "help"):
            print(_HELP)
            sys.exit(0)

        cmd = args[0].lower()
        if cmd not in _COMMANDS:
            print(f"\n[!] Unknown command: {cmd}")
            print(f"    Run: tube-assistant\n")
            sys.exit(1)

        _COMMANDS[cmd]()
    except KeyboardInterrupt:
        # la CLI dice "Press Ctrl+C to stop": deve uscire pulita, non con un
        # traceback di 20 righe
        print("\n  Interrotto.\n")
        sys.exit(130)


if __name__ == "__main__":  # `python -m youtube_ai_agent.cli <comando>`
    main()
