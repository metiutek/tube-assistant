"""
Setup wizard entry point for the package launcher.

`wizard.py` is installed as a top-level module by pyproject (`py-modules`), so
the same file serves the git clone and the `uv tool install` deployment. The
old `_wizard_standalone.py` copy drifted (different step count, older
HuggingFace check) and was never reached; it is gone.
"""

import importlib.util
import sys
from pathlib import Path


def main():
    root_wizard = Path(__file__).parent.parent / "wizard.py"
    if root_wizard.exists():
        spec = importlib.util.spec_from_file_location("_setup_wizard", root_wizard)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.main()
        return
    try:
        import wizard
    except ImportError:
        print("[!] Setup wizard not found. Reinstall with: uv tool install --force git+https://github.com/metiu1/tube-assistant.git")
        sys.exit(1)
    wizard.main()
