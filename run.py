"""Source and PyInstaller entry point."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from poketask.app import main

if __name__ == "__main__":
    main()
