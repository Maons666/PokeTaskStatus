"""Run the supported automated checks from a fresh checkout."""
import os
from pathlib import Path
import subprocess
import sys

root=Path(__file__).resolve().parents[1]
environment=os.environ.copy()
environment["PYTHONPATH"]=str(root/"src")
raise SystemExit(subprocess.call([sys.executable,"-m","unittest","discover","-s","tests","-v"],cwd=root,env=environment))
