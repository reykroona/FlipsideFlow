"""The Library: every album exported into the output folder, read from its tags.

Pure file logic (no FastAPI): scanning, tag reading and id <-> path resolution. Albums are
folders holding audio files; files exported earlier, or by other tools, show up too. Tags
are read once per file and cached by (size, mtime), so a rescan of a large library only
reads what changed.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

AUDIO_EXTENSIONS = {".flac": "flac", ".mp3": "mp3", ".aiff": "aiff", ".aif": "aiff"}
COVER_NAMES = ("folder.jpg", "cover.jpg", "folder.png", "cover.png", "front.jpg")
MANIFEST = ".vinylflow.json"  # written next to an export: where it came from
MAX_DEPTH = 3
MAX_FILES = 20000


def _id(kind: str, relative: str) -> str:
    return hashlib.sha1(f"{kind}:{relative}".encode("utf-8", "surrogateescape")).hexdigest()[:20]


def _first(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    text = getattr(value, "text", None)
    if text is not None:
        value = text[0] if text else ""
    return str(value).strip()


def _number(value: str) -> Optional[int]:
    head = (value or "").split("/")[0].strip()
    return int(head) if head.isdigit() else None


def read_tags(path: Path) -> dict:
    """Title, artist, album, year, track/disc numbers, vinyl position and length."""
    import mutagen

    info = {"title": "", "artist": "", "album": "", "album_artist": "", "year": "", "track": None,
            "disc": None, "position": "", "duration": 0.0, "has_art": False, "release_id": ""}
    try:
        audio = mutagen.File(path)
    except Exception:
        audio = None
    if audio is None:
        return info
    info["duration"] = float(getattr(getattr(audio, "info", None), "length", 0) or 0)
    tags = audio.tags
    if tags is None:
        return info
    if hasattr(tags, "getall"):  # ID3 (MP3, AIFF)
        get = lambda frame: _first(tags.get(frame))
        info.update(title=get("TIT2"), artist=get("TPE1"), album=get("TALB"), album_artist=get("TPE2"),
                    year=get("TDRC")[:4], track=_number(get("TRCK")), disc=_number(get("TPOS")),
                    position=get("TXXX:VINYLPOSITION"), release_id=get("TXXX:DISCOGS_RELEASE_ID"),
                    has_art=bool(tags.getall("APIC")))
        if not info["position"] and not info["track"]:
            info["position"] = get("TRCK")
    else:  # Vorbis comments (FLAC)
        get = lambda key: _first(tags.get(key))
        raw_track = get("tracknumber")
        info.update(title=get("title"), artist=get("artist"), album=get("album"), album_artist=get("albumartist"),
                    year=get("date")[:4], track=_number(raw_track), disc=_number(get("discnumber")),
                    position=get("vinylposition") or ("" if _number(raw_track) else raw_track),
                    release_id=get("discogs_release_id"),
                    has_art=bool(getattr(audio, "pictures", None)))
    return info


def embedded_art(path: Path) -> Optional[tuple[bytes, str]]:
    """The front cover stored in an audio file, as (bytes, mime)."""
    import mutagen

    try:
        audio = mutagen.File(path)
    except Exception:
        return None
    if audio is None:
        return None
    pictures = getattr(audio, "pictures", None)
    if pictures:
        return pictures[0].data, pictures[0].mime or "image/jpeg"
    tags = getattr(audio, "tags", None)
    if tags is not None and hasattr(tags, "getall"):
        frames = tags.getall("APIC")
        if frames:
            return frames[0].data, frames[0].mime or "image/jpeg"
    return None


def write_manifest(album_folder: Path, session_id: str, source: Path) -> None:
    """Remember which recording an export came from, so the library can reopen it."""
    path = Path(album_folder) / MANIFEST
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        data = {}
    sources = [s for s in data.get("sources", []) if isinstance(s, dict) and s.get("session_id") != session_id]
    sources.append({"session_id": session_id, "source": str(source), "exported_at": datetime.now().isoformat()})
    data["sources"] = sources[-8:]
    try:
        temp = path.with_name(MANIFEST + ".tmp")
        temp.write_text(json.dumps(data, indent=2))
        temp.replace(path)
    except OSError:
        pass  # the export itself succeeded; only "re-split" will be unavailable


class Library:
    def __init__(self, root: Callable[[], str], skip: Callable[[], Iterable[Path]] = lambda: ()):
        self._root = root
        self._skip = skip
        self._lock = threading.Lock()
        self._tag_cache: dict[str, tuple[int, int, dict]] = {}
        self._tracks: dict[str, Path] = {}
        self._albums: dict[str, Path] = {}
        self._album_art: dict[str, Path] = {}

    def root(self) -> Optional[Path]:
        raw = self._root()
        if not raw:
            return None
        try:
            return Path(raw).expanduser().resolve()
        except OSError:
            return None

    def _walk(self, root: Path, skip: set[Path]):
        count = 0
        stack = [(root, 0)]
        while stack:
            folder, depth = stack.pop()
            try:
                entries = list(os.scandir(folder))
            except OSError:
                continue
            for entry in entries:
                if entry.name.startswith(".") or entry.is_symlink():
                    continue
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    if depth + 1 < MAX_DEPTH and path.resolve() not in skip:
                        stack.append((path, depth + 1))
                elif path.suffix.lower() in AUDIO_EXTENSIONS and not entry.name.startswith("temp_"):
                    count += 1
                    if count > MAX_FILES:
                        return
                    yield path, entry.stat(follow_symlinks=False)

    def scan(self) -> dict:
        root = self.root()
        if root is None or not root.is_dir():
            return {"root": str(root or ""), "albums": [], "error": "The library folder does not exist yet. Export a record to create it."}
        skip = set()
        for path in self._skip():
            try:
                skip.add(Path(path).resolve())
            except OSError:
                pass
        folders: dict[Path, list] = {}
        seen: set[str] = set()
        for path, stat in self._walk(root, skip):
            key = str(path)
            seen.add(key)
            with self._lock:
                cached = self._tag_cache.get(key)
            if cached and cached[0] == stat.st_size and cached[1] == stat.st_mtime_ns:
                tags = cached[2]
            else:
                tags = read_tags(path)
                with self._lock:
                    self._tag_cache[key] = (stat.st_size, stat.st_mtime_ns, tags)
            folders.setdefault(path.parent, []).append((path, stat, tags))

        tracks_index: dict[str, Path] = {}
        albums_index: dict[str, Path] = {}
        art_index: dict[str, Path] = {}
        albums = []
        for folder, files in folders.items():
            relative = folder.relative_to(root).as_posix() or "."
            album_id = _id("album", relative)
            albums_index[album_id] = folder
            files.sort(key=lambda f: (f[2]["disc"] or 0, f[2]["track"] or 0, f[2]["position"], f[0].name))
            first = files[0][2]
            tracks = []
            for path, stat, tags in files:
                track_id = _id("track", path.relative_to(root).as_posix())
                tracks_index[track_id] = path
                tracks.append({
                    "id": track_id, "file": path.name, "format": AUDIO_EXTENSIONS[path.suffix.lower()],
                    "title": tags["title"] or path.stem, "artist": tags["artist"],
                    "position": tags["position"], "track": tags["track"], "disc": tags["disc"],
                    "duration": round(tags["duration"], 2), "size": stat.st_size,
                })
            cover = next((folder / name for name in COVER_NAMES if (folder / name).is_file()), None)
            if cover is None:
                art = next((path for path, _, tags in files if tags["has_art"]), None)
            else:
                art = cover
            if art is not None:
                art_index[album_id] = art
            artist = first["album_artist"] or first["artist"]
            if not artist and " - " in folder.name:
                artist = folder.name.split(" - ", 1)[0]
            album_name = first["album"] or (folder.name.split(" - ", 1)[1] if " - " in folder.name else folder.name)
            albums.append({
                "id": album_id, "folder": relative, "artist": artist or "Unknown artist", "title": album_name,
                "year": first["year"], "release_id": first["release_id"], "has_cover": art is not None,
                "formats": sorted({t["format"] for t in tracks}),
                "duration": round(sum(t["duration"] for t in tracks), 2),
                "added": max(stat.st_mtime for _, stat, _ in files),
                "tracks": tracks, "sources": self._sources(folder),
            })
        albums.sort(key=lambda a: a["added"], reverse=True)
        with self._lock:
            self._tracks, self._albums, self._album_art = tracks_index, albums_index, art_index
            for key in [k for k in self._tag_cache if k not in seen]:
                del self._tag_cache[key]
        return {"root": str(root), "albums": albums, "error": None}

    @staticmethod
    def _sources(folder: Path) -> list:
        try:
            data = json.loads((folder / MANIFEST).read_text())
        except (OSError, ValueError):
            return []
        out = []
        for item in data.get("sources", []):
            if isinstance(item, dict) and isinstance(item.get("session_id"), str):
                out.append({"session_id": item["session_id"], "exported_at": item.get("exported_at"),
                            "available": Path(str(item.get("source", ""))).is_file()})
        return out

    def _inside(self, path: Optional[Path]) -> Optional[Path]:
        root = self.root()
        if path is None or root is None:
            return None
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            return None
        if root != resolved and root not in resolved.parents:
            return None
        return resolved

    def track_path(self, track_id: str) -> Optional[Path]:
        with self._lock:
            path = self._tracks.get(track_id)
        return self._inside(path)

    def album_path(self, album_id: str) -> Optional[Path]:
        with self._lock:
            path = self._albums.get(album_id)
        return self._inside(path)

    def art_path(self, album_id: str) -> Optional[Path]:
        with self._lock:
            path = self._album_art.get(album_id)
        return self._inside(path)
