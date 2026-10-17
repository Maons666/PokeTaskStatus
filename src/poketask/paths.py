from pathlib import Path
import os
import sys


def assets_dir():
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    return base / "assets" / "vendor"


def data_dir():
    override = os.environ.get("POKETASK_DATA_DIR")
    if override:
        return Path(override)
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local/share"))) / "PokeTaskStatus"
