"""Pet pack fixtures exercise decoding, atomic installation and hostile ZIPs."""

from copy import deepcopy
from io import BytesIO
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

from PIL import Image

from poketask.petpacks import (PACK_STATES, MAX_ARCHIVE_BYTES, PackError,
                               discover_pack_summaries, discover_packs,
                               import_pack, load_pack)


def gif_bytes(size=(8, 6), frames=2, *, transparent=True, magenta=False):
    pictures = []
    palette = [0, 0, 0, *( [255, 0, 255] if magenta else [85, 145, 205]), 120, 190, 230] + [0] * 759
    for index in range(frames):
        image = Image.new("P", size, 0)
        image.putpalette(palette)
        image.putpixel((1 + index % max(1, size[0] - 2), 1), 1)
        pictures.append(image)
    output = BytesIO()
    options = {"transparency": 0} if transparent else {}
    pictures[0].save(output, format="GIF", save_all=True, append_images=pictures[1:],
                     duration=[40 if index % 2 == 0 else 90 for index in range(frames)],
                     disposal=2, optimize=False, **options)
    return output.getvalue()


def png_bytes(size=(6, 5), color=(70, 130, 200, 255), *, transparent=True):
    image = Image.new("RGBA", size, (0, 0, 0, 0) if transparent else color)
    image.putpixel((1, 1), color)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class PetPackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="poketask-packs-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pets = self.root / "private-data" / "pets"
        self.metadata = {
            "schema_version": 1, "id": "fixture-metagross", "name": "巨金怪 · 测试包",
            "species_id": 376, "signature_move": "彗星拳", "sources": ["Synthetic test pixels; no downloaded art"],
            "portrait": "portrait.png", "canvas": [8, 6],
            "animations": {state: f"animations/{state}.gif" for state in PACK_STATES},
        }
        self.images = {"portrait.png": png_bytes(), **{f"animations/{state}.gif": gif_bytes() for state in PACK_STATES}}

    def archive(self, metadata=None, replacements=None, extra=(), compression=zipfile.ZIP_DEFLATED):
        path = self.root / "fixture.zip"
        entries = {"pet.json": json.dumps(metadata or self.metadata, ensure_ascii=False).encode("utf-8"),
                   **self.images, **(replacements or {})}
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(path, "w", compression=compression) as archive:
                for name, content in entries.items():
                    archive.writestr(name, content)
                for name, content in extra:
                    archive.writestr(name, content)
        return path

    def directory(self, metadata=None, replacements=None):
        directory = self.root / "unpacked"
        directory.mkdir(exist_ok=True)
        files = {"pet.json": json.dumps(metadata or self.metadata).encode(), **self.images, **(replacements or {})}
        for name, content in files.items():
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        return directory

    def installed_entries(self):
        return sorted(path.name for path in self.pets.iterdir() if not path.name.startswith(".install-"))

    def test_import_complete_pack_and_independent_portrait_dimensions(self):
        pack = import_pack(self.archive(), self.pets)
        self.assertEqual((pack.id, pack.species_id, pack.signature_move), ("fixture-metagross", 376, "彗星拳"))
        self.assertEqual(pack.portrait.size, (6, 5))
        self.assertEqual(set(pack.animations), set(PACK_STATES))
        for state in PACK_STATES:
            self.assertEqual(len(pack.animations[state]), 2)
            self.assertEqual(pack.animations[state][1].mode, "RGBA")
            self.assertEqual(pack.animations[state][1].size, (8, 6))
            self.assertEqual(pack.durations[state], [40, 90])
        self.assertEqual(self.installed_entries(), [pack.id])
        reloaded = load_pack(self.pets / pack.id)
        self.assertEqual(reloaded.name, pack.name)

    def test_summaries_do_not_decode_images_and_bad_pack_does_not_hide_good(self):
        import_pack(self.archive(), self.pets)
        with patch("poketask.petpacks.Image.open", side_effect=AssertionError("summary decoded an image")):
            summaries, errors = discover_pack_summaries(self.pets)
        self.assertEqual(errors, [])
        self.assertEqual(summaries[0]["directory"], self.pets / self.metadata["id"])
        invalid = self.pets / "broken"
        invalid.mkdir()
        (invalid / "pet.json").write_text("{}")
        packs, errors = discover_packs(self.pets)
        self.assertEqual(len(packs), 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("broken", errors[0])

    def test_duplicate_id_never_overwrites_and_case_collisions_are_rejected(self):
        archive = self.archive()
        import_pack(archive, self.pets)
        existing = self.pets / self.metadata["id"] / "portrait.png"
        original = existing.read_bytes()
        with self.assertRaisesRegex(PackError, "相同 id"):
            import_pack(archive, self.pets)
        self.assertEqual(existing.read_bytes(), original)
        metadata = {**self.metadata, "id": self.metadata["id"].upper()}
        with self.assertRaisesRegex(PackError, "相同 id"):
            import_pack(self.archive(metadata), self.pets)
        self.assertEqual(self.installed_entries(), [self.metadata["id"]])

    def test_invalid_animation_cleans_staging_and_never_installs(self):
        path = self.archive(replacements={"animations/unknown.gif": b"not an image"})
        with self.assertRaises(PackError):
            import_pack(path, self.pets)
        self.assertEqual(self.installed_entries(), [])

    def test_path_traversal_absolute_windows_and_reserved_paths_are_rejected(self):
        for name in ("../escape.png", "/absolute.png", "C:/drive.png", "dir\\escape.png",
                     "dir/../escape.png", "dir//escape.png", "CON.png", "dir/file.gif."):
            with self.subTest(name=name), self.assertRaises(PackError):
                import_pack(self.archive(extra=[(name, b"bad")]), self.pets)
        self.assertFalse((self.root / "escape.png").exists())
        self.assertFalse(self.pets.exists())

    def test_duplicate_and_case_colliding_zip_entries_are_rejected(self):
        for name in ("pet.json", "PET.JSON", "Portrait.png", "animations/IDLE.gif"):
            with self.subTest(name=name), self.assertRaisesRegex(PackError, "重复|冲突"):
                import_pack(self.archive(extra=[(name, b"bad")]), self.pets)

    def test_parent_directory_case_collision_is_rejected(self):
        metadata = deepcopy(self.metadata)
        metadata["animations"]["working"] = "Animations/working.gif"
        with self.assertRaisesRegex(PackError, "父目录"):
            import_pack(self.archive(metadata), self.pets)

    def test_symlink_and_unknown_script_entries_are_rejected(self):
        symlink = zipfile.ZipInfo("shortcut")
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaisesRegex(PackError, "符号链接"):
            import_pack(self.archive(extra=[(symlink, b"../outside")]), self.pets)
        for name in ("run.py", "launch.ps1", "README.txt", "unnecessary/"):
            with self.subTest(name=name), self.assertRaisesRegex(PackError, "仅包含"):
                import_pack(self.archive(extra=[(name, b"")]), self.pets)

    def test_directory_links_are_not_followed(self):
        directory = self.directory()
        link = self.root / "linked"
        try:
            link.symlink_to(directory, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                self.skipTest("This account cannot create directory links")
            # Junctions exercise the Windows reparse-point check without
            # requiring the privilege needed for symbolic links.
            process = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(directory)],
                                     capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            if process.returncode:
                self.skipTest("This Windows account cannot create directory links")
        with self.assertRaisesRegex(PackError, "符号链接"):
            load_pack(link)
        summaries, errors = discover_pack_summaries(link)
        self.assertEqual(summaries, [])
        self.assertEqual(len(errors), 1)

    def test_required_states_sources_schema_and_builtin_id_are_checked(self):
        variants = [{**self.metadata, "sources": []}, {**self.metadata, "schema_version": True},
                    {**self.metadata, "id": "builtin-metagross"}, {**self.metadata, "species_id": True},
                    {**self.metadata, "canvas": [193, 112]}, {**self.metadata, "extra_script": "run.py"}]
        missing = deepcopy(self.metadata)
        del missing["animations"]["asking"]
        variants.append(missing)
        for metadata in variants:
            with self.subTest(metadata=metadata), self.assertRaises(PackError):
                import_pack(self.archive(metadata), self.pets)

    def test_duplicate_json_fields_and_large_metadata_are_rejected(self):
        for raw in (b'{"id":"one","id":"two"}', b" " * (64 * 1024 + 1)):
            with self.subTest(length=len(raw)), self.assertRaises(PackError):
                import_pack(self.archive(replacements={"pet.json": raw}), self.pets)

    def test_frame_count_size_and_transparency_requirements(self):
        cases = [gif_bytes(frames=1), gif_bytes(size=(9, 6)), gif_bytes(transparent=False),
                 gif_bytes(magenta=True), gif_bytes(frames=301)]
        for index, invalid in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(PackError):
                import_pack(self.archive(replacements={"animations/idle.gif": invalid}), self.pets)
        self.assertEqual(self.installed_entries(), [])

    def test_portrait_rejects_half_alpha_opaque_background_magenta_and_animation(self):
        for portrait in (png_bytes(color=(70, 130, 200, 128)), png_bytes(transparent=False),
                         png_bytes(color=(255, 0, 255, 255)), gif_bytes()):
            with self.subTest(length=len(portrait)), self.assertRaises(PackError):
                import_pack(self.archive(replacements={"portrait.png": portrait}), self.pets)

    def test_decoded_pixel_archive_and_extraction_budgets(self):
        path = self.archive()
        with patch("poketask.petpacks.MAX_DECODED_PIXELS", 100), self.assertRaisesRegex(PackError, "解码像素"):
            import_pack(path, self.pets)
        with patch("poketask.petpacks.MAX_EXTRACTED_BYTES", 100), self.assertRaisesRegex(PackError, "解压后"):
            import_pack(path, self.pets)
        large = self.root / "large.zip"
        with large.open("wb") as stream:
            stream.truncate(MAX_ARCHIVE_BYTES + 1)
        with self.assertRaisesRegex(PackError, "20 MiB"):
            import_pack(large, self.pets)

    def test_non_deflate_compression_and_foreign_install_lock_are_rejected(self):
        with self.assertRaisesRegex(PackError, "Store 或 Deflate"):
            import_pack(self.archive(compression=zipfile.ZIP_BZIP2), self.pets)
        self.pets.mkdir(parents=True)
        lock = self.pets / (".install-" + self.metadata["id"] + ".lock")
        lock.write_text("another importer")
        with self.assertRaisesRegex(PackError, "不属于本程序"):
            import_pack(self.archive(), self.pets)
        self.assertEqual(lock.read_text(), "another importer")

    def test_process_crash_releases_lock_and_same_id_can_be_imported(self):
        self.pets.mkdir(parents=True)
        command = (
            "import sys; from pathlib import Path; from poketask.petpacks import _ImportLock; "
            "lock = _ImportLock(Path(sys.argv[1]), sys.argv[2]); lock.acquire(); "
            "print('locked', flush=True); sys.stdin.read()"
        )
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        process = subprocess.Popen([sys.executable, "-c", command, str(self.pets), self.metadata["id"]],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, env=environment,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            self.assertEqual(process.stdout.readline().strip(), "locked")
            archive = self.archive()
            with self.assertRaisesRegex(PackError, "正在导入"):
                import_pack(archive, self.pets)
            process.kill()
            process.communicate(timeout=10)
            pack = import_pack(archive, self.pets)
            self.assertEqual(pack.id, self.metadata["id"])
            self.assertEqual(discover_pack_summaries(self.pets)[1], [])
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)

    def test_foreign_empty_or_same_length_lock_is_not_reclaimed(self):
        from poketask.petpacks import _LOCK_MARKER
        self.pets.mkdir(parents=True)
        path = self.pets / (".install-" + self.metadata["id"] + ".lock")
        for content in (b"", b"x" * len(_LOCK_MARKER)):
            path.write_bytes(content)
            with self.assertRaisesRegex(PackError, "不属于本程序"):
                import_pack(self.archive(), self.pets)
            self.assertEqual(path.read_bytes(), content)


if __name__ == "__main__":
    unittest.main()
