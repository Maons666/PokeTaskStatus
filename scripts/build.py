"""Build one portable Windows executable. Generated files stay outside source."""
from pathlib import Path
import argparse
import os
import subprocess
import sys
from PIL import Image

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir",type=Path,default=ROOT/"release")
    destination=parser.parse_args().dist_dir.resolve()
    if sys.platform != "win32":
        raise SystemExit("Build the Windows artifact on Windows.")
    if not (ROOT/"assets/vendor/pixel.ttf").is_file():
        raise SystemExit("Run python scripts/fetch_assets.py first.")
    local=ROOT/".local/build"
    local.mkdir(parents=True,exist_ok=True)
    with Image.open(ROOT/"assets/vendor/metagross.gif") as source:
        icon=source.convert("RGBA")
        square=Image.new("RGBA",(128,128))
        square.alpha_composite(icon,((128-icon.width)//2,(128-icon.height)//2))
        square.save(local/"poketask.ico",sizes=[(32,32),(48,48),(64,64),(128,128)])
    command=[sys.executable,"-m","PyInstaller","--noconfirm","--clean","--onefile","--windowed",
        "--name","PokeTaskStatus","--distpath",str(destination),"--workpath",str(local/"work"),
        "--specpath",str(local),"--paths",str(ROOT/"src"),"--icon",str(local/"poketask.ico"),
        "--add-data",str(ROOT/"assets")+";assets","--exclude-module","numpy",
        "--exclude-module","pandas","--exclude-module","matplotlib","--exclude-module","IPython",
        str(ROOT/"run.py")]
    # Conda keeps these DLLs outside DLLs/. Include them for a truly portable exe.
    conda_bin=Path(sys.prefix)/"Library/bin"
    for dll in ("tcl86t.dll","tk86t.dll","ffi.dll","libexpat.dll","libcrypto-3-x64.dll",
                "libssl-3-x64.dll","liblzma.dll","LIBBZ2.dll","libmpdec-4.dll"):
        if (conda_bin/dll).is_file():
            command[3:3]=["--add-binary",str(conda_bin/dll)+";."]
    environment=os.environ.copy()
    environment["PYINSTALLER_CONFIG_DIR"]=str(local/"cache")
    if conda_bin.is_dir():
        environment["PATH"]=str(conda_bin)+os.pathsep+environment.get("PATH","")
    subprocess.run(command,cwd=ROOT,env=environment,check=True)
    print("Ready:",destination/"PokeTaskStatus.exe")


if __name__=="__main__":
    main()
