"""Declarative, local-only pet packs. No package code is ever executed.

ZIP imports are decoded and validated in a private staging directory before a
single directory rename installs them. The lightweight discovery API is meant
for the box UI; it does not decode every installed animation into memory.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import unicodedata
import zipfile

from PIL import Image, UnidentifiedImageError


PACK_STATES = ("idle", "working", "asking", "completed", "waiting", "failed",
               "interrupted", "disconnected", "unknown")
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_EXTRACTED_BYTES = 40 * 1024 * 1024
MAX_METADATA_BYTES = 64 * 1024
MAX_DECODED_PIXELS = 24_000_000
MAX_FRAMES = 300
MAX_ENTRIES = 64
MAX_CANVAS = (192, 112)
_REQUIRED = {"schema_version", "id", "name", "species_id", "signature_move",
             "sources", "portrait", "animations", "canvas"}
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in "123456789¹²³"),
             *(f"LPT{i}" for i in "123456789¹²³")}
_LOCK_MARKER = b"PokeTaskStatus pet-pack import lock v1\n"


class PackError(ValueError):
    """A safe, readable pack validation or installation error."""


@dataclass
class PetPack:
    id: str
    name: str
    species_id: int
    signature_move: str
    sources: list[str]
    portrait: Image.Image
    animations: dict[str, list[Image.Image]]
    durations: dict[str, list[int]]


def _collision_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _relative_path(value, *, directory: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value) > 240:
        raise PackError("包内路径必须是长度不超过 240 的相对路径")
    if directory and value.endswith("/"):
        value = value[:-1]
    if (not value or value.startswith("/") or "\\" in value or ":" in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise PackError("包内路径不可包含绝对路径、驱动器、反斜杠或控制字符")
    parts = value.split("/")
    if len(parts) > 8 or any(part in ("", ".", "..") for part in parts):
        raise PackError("包内路径不可越级、包含空段，或超过 8 层")
    for part in parts:
        if (part.endswith((".", " ")) or any(char in part for char in '<>"|?*')
                or part.split(".", 1)[0].upper() in _RESERVED):
            raise PackError("包内路径包含 Windows 不支持的名称")
    return "/".join(parts)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PackError("pet.json 含重复字段")
        result[key] = value
    return result


def _text(value, label: str, maximum: int) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise PackError(f"{label} 必须是非空单行文本，最多 {maximum} 字符")
    return value


def _metadata(raw: bytes) -> dict:
    if len(raw) > MAX_METADATA_BYTES:
        raise PackError("pet.json 不可超过 64 KiB")
    try:
        data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_object)
    except (ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, PackError):
            raise
        raise PackError("pet.json 不是有效的 UTF-8 JSON") from exc
    if not isinstance(data, dict) or set(data) != _REQUIRED:
        raise PackError("pet.json 必须包含约定的全部字段，且不能添加未知字段")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise PackError("仅支持 schema_version=1 的形象包")
    identity = data["id"]
    if not isinstance(identity, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identity) is None:
        raise PackError("id 必须由 1–64 个英文字母、数字、短横线或下划线组成")
    if identity.casefold() == "builtin-metagross" or identity.upper() in _RESERVED:
        raise PackError("此 id 为内置形象或系统保留名称，请使用其他 id")
    _text(data["name"], "name", 80)
    _text(data["signature_move"], "signature_move", 80)
    if type(data["species_id"]) is not int or not 1 <= data["species_id"] <= 99999:
        raise PackError("species_id 必须是 1–99999 的整数")
    sources = data["sources"]
    if not isinstance(sources, list) or not 1 <= len(sources) <= 16:
        raise PackError("sources 必须是包含 1–16 项来源的非空字符串列表")
    for source in sources:
        _text(source, "sources 来源", 1000)
    canvas = data["canvas"]
    if (not isinstance(canvas, list) or len(canvas) != 2
            or any(type(value) is not int or value < 1 or value > bound
                   for value, bound in zip(canvas, MAX_CANVAS))):
        raise PackError("canvas 必须是 [宽, 高]，最大为 [192, 112]")
    portrait = _relative_path(data["portrait"])
    if PurePosixPath(portrait).suffix.lower() != ".png":
        raise PackError("portrait 必须指向静态 PNG 文件")
    animations = data["animations"]
    if not isinstance(animations, dict) or set(animations) != set(PACK_STATES):
        raise PackError("animations 必须且只能声明全部九种状态")
    for state, path in animations.items():
        _relative_path(path)
        if PurePosixPath(path).suffix.lower() != ".gif":
            raise PackError(f"{state} 必须指向 GIF 文件")
    files = {"pet.json", portrait, *animations.values()}
    normalized = [_collision_key(path) for path in files]
    if len(set(normalized)) != len(normalized):
        raise PackError("声明的文件路径存在大小写或 Unicode 名称冲突")
    # A file cannot also be the parent directory of another declared file.
    parents = _parent_paths(files)
    if set(normalized) & {_collision_key(path) for path in parents}:
        raise PackError("声明的文件与目录路径冲突")
    if len({_collision_key(path) for path in parents}) != len(parents):
        raise PackError("声明的父目录存在大小写或 Unicode 名称冲突")
    return data


def _files(data: dict) -> set[str]:
    return {"pet.json", data["portrait"], *data["animations"].values()}


def _parent_paths(files: set[str]) -> set[str]:
    return {str(parent) for filename in files for parent in PurePosixPath(filename).parents
            if str(parent) != "."}


def _is_link(path: Path) -> bool:
    status = path.lstat()
    return (stat.S_ISLNK(status.st_mode)
            or bool(getattr(status, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)))


def _directory_manifest(directory: Path) -> dict:
    if _is_link(directory) or not directory.is_dir():
        raise PackError("形象包目录必须是普通目录，不能是符号链接或联接")
    manifest = directory / "pet.json"
    if _is_link(manifest) or not manifest.is_file():
        raise PackError("包根目录必须包含普通文件 pet.json")
    with manifest.open("rb") as stream:
        data = _metadata(stream.read(MAX_METADATA_BYTES + 1))
    allowed = _files(data)
    allowed_dirs = _parent_paths(allowed)
    found = set()
    seen = set()
    total = 0
    entries = 0
    pending = [directory]
    while pending:
        parent = pending.pop()
        for path in parent.iterdir():
            entries += 1
            if entries > MAX_ENTRIES:
                raise PackError("包内文件与目录条目总数不可超过 64")
            if _is_link(path):
                raise PackError("包内不可包含符号链接或联接")
            relative = _relative_path(path.relative_to(directory).as_posix())
            key = _collision_key(relative)
            if key in seen:
                raise PackError("包内存在大小写或 Unicode 名称冲突")
            seen.add(key)
            if path.is_dir():
                if relative not in allowed_dirs:
                    raise PackError("包内包含未声明的目录")
                pending.append(path)
            elif path.is_file():
                if relative not in allowed:
                    raise PackError("包内包含未声明的文件；不允许脚本或其他附加文件")
                total += path.stat().st_size
                if total > MAX_EXTRACTED_BYTES:
                    raise PackError("包内容解压后不可超过 40 MiB")
                found.add(relative)
            else:
                raise PackError("包内只能包含普通文件与目录")
    if found != allowed:
        raise PackError("包内缺少 pet.json 声明的图像文件")
    return data


def _rgba(frame: Image.Image, label: str) -> Image.Image:
    result = frame.convert("RGBA")
    alpha = result.getchannel("A").histogram()
    if not alpha[0] or any(alpha[1:255]):
        result.close()
        raise PackError(f"{label} 每一帧都必须有透明像素，且不能使用半透明像素")
    if any(color == (255, 0, 255, 255) for _, color in result.getcolors(result.width * result.height) or []):
        result.close()
        raise PackError(f"{label} 不可使用不透明洋红色 #FF00FF（系统透明色键）")
    return result


def _close_images(portrait: Image.Image | None, animations: dict) -> None:
    if portrait is not None:
        portrait.close()
    for frames in animations.values():
        for frame in frames:
            frame.close()


def load_pack(directory: Path) -> PetPack:
    """Fully validate and decode a directory into bounded RGBA frame lists."""
    directory = Path(directory)
    portrait = None
    animations: dict[str, list[Image.Image]] = {}
    durations: dict[str, list[int]] = {}
    try:
        data = _directory_manifest(directory)
        canvas = tuple(data["canvas"])
        with Image.open(directory / data["portrait"]) as source:
            if (source.format != "PNG" or getattr(source, "is_animated", False)
                    or not 1 <= source.width <= MAX_CANVAS[0] or not 1 <= source.height <= MAX_CANVAS[1]):
                raise PackError("portrait 必须是最大 192×112 的静态 PNG")
            portrait = _rgba(source, "portrait")
        pixels = portrait.width * portrait.height
        for state in PACK_STATES:
            frames: list[Image.Image] = []
            times: list[int] = []
            animations[state], durations[state] = frames, times
            with Image.open(directory / data["animations"][state]) as source:
                if source.format != "GIF":
                    raise PackError(f"{state} 文件内容不是 GIF")
                # Avoid n_frames: it can scan an unbounded number of GIF frames.
                for index in range(MAX_FRAMES + 1):
                    try:
                        source.seek(index)
                    except EOFError:
                        break
                    if index >= MAX_FRAMES:
                        raise PackError(f"{state} 动画不可超过 300 帧")
                    if source.size != canvas:
                        raise PackError(f"{state} 动画每帧尺寸必须与 canvas 完全一致")
                    pixels += canvas[0] * canvas[1]
                    if pixels > MAX_DECODED_PIXELS:
                        raise PackError("全包解码像素总数不可超过 24,000,000")
                    frames.append(_rgba(source, state))
                    duration = source.info.get("duration", 100)
                    times.append(max(20, min(5000, int(duration or 100))))
                if len(frames) < 2:
                    raise PackError(f"{state} 动画必须至少包含 2 帧")
        return PetPack(data["id"], data["name"], data["species_id"], data["signature_move"],
                       list(data["sources"]), portrait, animations, durations)
    except PackError:
        _close_images(portrait, animations)
        raise
    except (OSError, ValueError, EOFError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        _close_images(portrait, animations)
        raise PackError("无法读取形象包：目录、JSON 或图像文件损坏") from exc


def _zip_manifest(archive: zipfile.ZipFile) -> tuple[dict, list[zipfile.ZipInfo]]:
    entries = archive.infolist()
    if len(entries) > MAX_ENTRIES:
        raise PackError("ZIP 文件与目录条目总数不可超过 64")
    seen = set()
    files = {}
    directories = set()
    total = 0
    for info in entries:
        if info.orig_filename != info.filename:
            raise PackError("ZIP 路径包含被截断的非法字符")
        name = _relative_path(info.filename, directory=info.is_dir())
        key = _collision_key(name)
        if key in seen:
            raise PackError("ZIP 含重复、大小写或 Unicode 名称冲突的条目")
        seen.add(key)
        mode = stat.S_IFMT(info.external_attr >> 16)
        if (mode not in (0, stat.S_IFREG, stat.S_IFDIR)
                or (mode == stat.S_IFDIR and not info.is_dir())
                or (mode == stat.S_IFREG and info.is_dir())):
            raise PackError("ZIP 不可包含符号链接或特殊文件")
        if info.flag_bits & 1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise PackError("ZIP 不可加密，压缩方式仅支持 Store 或 Deflate")
        if info.is_dir():
            if info.file_size:
                raise PackError("ZIP 目录条目不可携带数据")
            directories.add(name)
        else:
            total += info.file_size
            if total > MAX_EXTRACTED_BYTES:
                raise PackError("ZIP 解压后不可超过 40 MiB")
            files[name] = info
    if "pet.json" not in files or files["pet.json"].file_size > MAX_METADATA_BYTES:
        raise PackError("ZIP 根目录必须包含不超过 64 KiB 的 pet.json")
    with archive.open(files["pet.json"]) as stream:
        data = _metadata(stream.read(MAX_METADATA_BYTES + 1))
    allowed = _files(data)
    if set(files) != allowed or not directories <= _parent_paths(allowed):
        raise PackError("ZIP 必须仅包含 pet.json 与它声明的全部 PNG/GIF，不允许脚本或其他文件")
    return data, list(files.values())


def _remove_stage(stage: Path | None, pets_dir: Path) -> None:
    if stage is None or not stage.exists():
        return
    # Never recursively remove a path outside the exact staging directory root.
    if (_is_link(stage) or stage.resolve().parent != pets_dir.resolve()
            or not stage.name.startswith(".import-")):
        raise PackError("临时目录路径异常，已停止清理")
    shutil.rmtree(stage)


class _ImportLock:
    """OS-owned lock: process death releases it, while its marked file remains.

    Keeping the file avoids an unlink/recreate race between waiting importers.
    Publish the complete marker atomically so even a crash during initialization
    cannot leave an ambiguous empty lock at the destination.
    """

    def __init__(self, pets_dir: Path, identity: str):
        self.path = pets_dir / (".install-" + identity.casefold() + ".lock")
        self.stream = None
        self.locked = False

    def acquire(self) -> None:
        if not self.path.exists():
            descriptor, temporary = tempfile.mkstemp(prefix=".install-init-", dir=self.path.parent)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(_LOCK_MARKER)
                    output.flush()
                    os.fsync(output.fileno())
                try:
                    if os.name == "nt":
                        os.rename(temporary, self.path)  # Windows rename never replaces a target.
                    else:
                        os.link(temporary, self.path)  # Atomic no-replace publication on POSIX.
                except FileExistsError:
                    pass
            finally:
                Path(temporary).unlink(missing_ok=True)
        if _is_link(self.path) or not self.path.is_file():
            raise PackError("导入锁不是本程序的普通锁文件，未修改该文件")
        descriptor = os.open(self.path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        self.stream = os.fdopen(descriptor, "r+b")
        try:
            if os.fstat(self.stream.fileno()).st_size != len(_LOCK_MARKER):
                raise PackError("导入锁文件格式不属于本程序，未修改该文件")
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise PackError("这个 id 的形象包正在导入，请稍后重试") from exc
            self.locked = True
            if self.stream.read(len(_LOCK_MARKER) + 1) != _LOCK_MARKER:
                raise PackError("导入锁文件格式不属于本程序，未修改该文件")
        except BaseException:
            self.release()
            raise

    def release(self) -> None:
        if self.stream is None:
            return
        try:
            if self.locked:
                self.stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        finally:
            self.stream.close()
            self.stream = None
            self.locked = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *unused):
        self.release()


def import_pack(zip_path: Path, pets_dir: Path) -> PetPack:
    """Validate, stage and atomically install a ZIP without replacing an id."""
    zip_path, pets_dir = Path(zip_path), Path(pets_dir).absolute()
    stage = None
    lock = None
    pack = None
    try:
        if zip_path.stat().st_size > MAX_ARCHIVE_BYTES:
            raise PackError("形象包 ZIP 不可超过 20 MiB")
        with zipfile.ZipFile(zip_path) as archive:
            data, files = _zip_manifest(archive)
            if pets_dir.exists() and (_is_link(pets_dir) or not pets_dir.is_dir()):
                raise PackError("形象包安装位置必须是普通目录")
            pets_dir.mkdir(parents=True, exist_ok=True)
            lock = _ImportLock(pets_dir, data["id"])
            lock.acquire()
            if any(_collision_key(path.name) == _collision_key(data["id"]) for path in pets_dir.iterdir()):
                raise PackError("已有相同 id 的形象包，未覆盖；请为新包使用其他 id")
            stage = Path(tempfile.mkdtemp(prefix=".import-", dir=pets_dir))
            extracted = 0
            for info in files:
                target = stage.joinpath(*PurePosixPath(info.filename).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("xb") as output:
                    while chunk := source.read(64 * 1024):
                        extracted += len(chunk)
                        if extracted > MAX_EXTRACTED_BYTES:
                            raise PackError("ZIP 实际解压内容超过 40 MiB")
                        output.write(chunk)
            pack = load_pack(stage)
            destination = pets_dir / pack.id
            if destination.exists():
                raise PackError("已有相同 id 的形象包，未覆盖")
            os.rename(stage, destination)
            stage = None
            return pack
    except PackError:
        if pack is not None:
            _close_images(pack.portrait, pack.animations)
        raise
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
        if pack is not None:
            _close_images(pack.portrait, pack.animations)
        raise PackError("无法导入 ZIP；请确认文件完整、安装目录可写且没有同名包") from exc
    finally:
        try:
            _remove_stage(stage, pets_dir)
        finally:
            if lock is not None:
                lock.release()


def discover_pack_summaries(pets_dir: Path) -> tuple[list[dict], list[str]]:
    """Read bounded metadata and declared paths, without decoding any image."""
    pets_dir = Path(pets_dir)
    summaries, errors = [], []
    if not pets_dir.exists():
        return summaries, errors
    try:
        if _is_link(pets_dir) or not pets_dir.is_dir():
            raise PackError("形象包安装目录不能是符号链接或联接")
        directories = sorted(pets_dir.iterdir(), key=lambda path: path.name.casefold())
    except OSError:
        return [], ["无法读取形象包目录"]
    except PackError as exc:
        return [], [str(exc)]
    ids = set()
    for directory in directories:
        if directory.name.startswith((".import-", ".install-")):
            continue
        try:
            data = _directory_manifest(directory)
            if directory.name != data["id"]:
                raise PackError("安装目录名必须与包 id 一致")
            key = _collision_key(data["id"])
            if key in ids:
                raise PackError("存在重复 id 的形象包")
            ids.add(key)
            summaries.append({key: data[key] for key in ("id", "name", "species_id", "signature_move")}
                             | {"directory": directory})
        except (PackError, OSError) as exc:
            reason = str(exc) if isinstance(exc, PackError) else "目录或 pet.json 无法读取"
            errors.append(f"{directory.name[:80]}：{reason}")
    return summaries, errors


def discover_packs(pets_dir: Path) -> tuple[list[PetPack], list[str]]:
    """Decode all discovered packs; prefer summaries for a UI with many packs."""
    summaries, errors = discover_pack_summaries(pets_dir)
    packs = []
    for summary in summaries:
        try:
            packs.append(load_pack(summary["directory"]))
        except PackError as exc:
            errors.append(f"{summary['id']}：{exc}")
    return packs, errors
