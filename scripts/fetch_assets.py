"""Fetch pinned original game sprites and the OFL pixel font; no AI artwork."""
import hashlib
import io
import json
from pathlib import Path
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "vendor"


def download(url):
    req = urllib.request.Request(url, headers={"User-Agent": "PokeTaskStatus/0.1"})
    with urllib.request.urlopen(req, timeout=60) as response:
        data = response.read(200 * 1024 * 1024)
        if len(data) >= 200 * 1024 * 1024:
            raise RuntimeError("Asset archive unexpectedly exceeds 200 MiB")
        return data


def main():
    manifest = json.loads((ROOT / "assets/manifest.json").read_text(encoding="utf-8"))
    OUT.mkdir(parents=True, exist_ok=True)
    hashes = {}
    def verify(name, content):
        expected=manifest.get("sha256",{}).get(name)
        if expected and hashlib.sha256(content).hexdigest()!=expected:
            raise RuntimeError(f"Checksum mismatch: {name}; refusing unverified artwork/font")
        return content
    for name, path in manifest["sprites"].items():
        url = f'https://raw.githubusercontent.com/PokeAPI/sprites/{manifest["sprite_revision"]}/sprites/{path}'
        target = OUT / name
        if not target.exists():
            target.write_bytes(verify(name,download(url)))
        verify(name,target.read_bytes())
        hashes[name] = hashlib.sha256(target.read_bytes()).hexdigest()
        print(name, target.stat().st_size)
    if not (OUT / "pixel.ttf").exists():
        with zipfile.ZipFile(io.BytesIO(download(manifest["font_url"]))) as archive:
            candidates = [n for n in archive.namelist() if n.endswith("zh_hans.ttf")]
            if len(candidates) != 1:
                raise RuntimeError("Expected one Simplified Chinese pixel font")
            (OUT / "pixel.ttf").write_bytes(verify("pixel.ttf",archive.read(candidates[0])))
            for name in archive.namelist():
                if "LICENSE" in name.upper() and not name.endswith("/"):
                    (OUT / Path(name).name).write_bytes(archive.read(name))
    verify("pixel.ttf",(OUT / "pixel.ttf").read_bytes())
    hashes["pixel.ttf"] = hashlib.sha256((OUT / "pixel.ttf").read_bytes()).hexdigest()
    (OUT / "checksums.json").write_text(json.dumps(hashes, indent=2), encoding="utf-8")
    print("Assets ready:", OUT)


if __name__ == "__main__":
    main()
