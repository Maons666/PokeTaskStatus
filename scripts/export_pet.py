"""Export a complete importable example with the built-in Metagross animations."""
import argparse
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=ROOT/"release/metagross.pokepet.zip")
    destination=parser.parse_args().output
    from poketask.packexport import export_builtin_pack
    print(export_builtin_pack(destination))


if __name__=="__main__":
    main()
