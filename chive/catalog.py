"""The library: what's in the download folder, browsable and deletable.

Two sources of truth, merged:
  * the **index** (`<state dir>/library.json`) — rich metadata recorded when
    Chive finishes a download (title, album/show, source, id, duration…);
  * a **scan** of the download folder — so files from before the index, or
    added by hand, still show up (described from their names/folders).

All paths handed to/from the browser are relative to the download folder and
are re-validated on every use, so nothing outside it can be read or deleted.
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any

from .library import POD_TAG_RE, VIDEO_ID_RE
from .util import TTLCache, atomic_write_json, load_json

VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".m4v", ".mov", ".avi"}
AUDIO_EXTS = {".mp3", ".m4a", ".m4b", ".opus", ".ogg", ".flac", ".wav", ".aac"}
MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
COVER_NAMES = ("cover.jpg", "folder.jpg", "cover.png")
_SIDECAR_EXTS = {".srt", ".vtt", ".ass", ".lrc", ".json", ".description", ".jpg", ".jpeg",
                 ".png", ".webp", ".nfo"}
_ID_SUFFIX = re.compile(r"\s*\[[^\[\]]+\]$")
SORTS = ("recent", "title", "collection", "size")
SOURCES = ("youtube", "podcast", "music", "web", "other")

_SCAN_CACHE = TTLCache(ttl=10, maxsize=4)


# ----- paths -----------------------------------------------------------------


def resolve_rel(root: Path, rel: str, kinds: set[str] = MEDIA_EXTS | IMAGE_EXTS) -> Path:
    """The file `rel` inside `root`. Raises ValueError for anything that
    escapes the folder or isn't an allowed kind, FileNotFoundError if absent."""
    if not isinstance(rel, str) or not rel or "\x00" in rel:
        raise ValueError("invalid path")
    base = root.resolve()
    p = (base / rel).resolve()
    if p == base or not p.is_relative_to(base):
        raise ValueError("path is outside the library")
    if p.suffix.lower() not in kinds:
        raise ValueError("not a media file")
    if not p.is_file():
        raise FileNotFoundError(rel)
    return p


def rel_of(root: Path, path: str | Path) -> str | None:
    try:
        return Path(path).resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return None


def guess_source(rel: str) -> str:
    parts = rel.split("/")
    stem = Path(rel).stem
    if parts[0] == "Music":
        return "music"
    if parts[0] == "Web":
        return "web"
    if POD_TAG_RE.search(stem):
        return "podcast"
    if VIDEO_ID_RE.search(stem):
        return "youtube"
    return "other"


def _title_from_name(stem: str) -> str:
    return _ID_SUFFIX.sub("", stem).strip() or stem


# ----- the index -------------------------------------------------------------


class LibraryIndex:
    """rel path → metadata, persisted as JSON. Thread-safe."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        raw = load_json(path).get("entries")
        self._entries: dict[str, dict[str, Any]] = raw if isinstance(raw, dict) else {}

    def _save(self) -> None:
        atomic_write_json(self.path, {"version": 1, "entries": self._entries})

    def record(self, root: Path, files: list[dict[str, Any]], meta: dict[str, Any]) -> list[str]:
        """Record finished files with the item's metadata; returns rel paths."""
        added = []
        with self._lock:
            for f in files:
                rel = rel_of(root, f.get("path") or "")
                if not rel:
                    continue
                entry = {**meta, "added": time.time()}
                for key in ("title", "track", "duration"):
                    if f.get(key) not in (None, ""):
                        entry[key] = f[key]
                self._entries[rel] = {k: v for k, v in entry.items() if v not in (None, "")}
                added.append(rel)
            if added:
                self._save()
        _SCAN_CACHE.clear()
        return added

    def forget(self, rel: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._entries.pop(rel, None)
            if entry is not None:
                self._save()
        return entry

    def get(self, rel: str) -> dict[str, Any] | None:
        with self._lock:
            e = self._entries.get(rel)
            return dict(e) if e else None

    def ids(self) -> set[str]:
        with self._lock:
            return {e["video_id"] for e in self._entries.values() if e.get("video_id")}

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._entries.items()}


# ----- listing ---------------------------------------------------------------


def scan(root: Path) -> list[dict[str, Any]]:
    """Every media file under `root`: rel, size, mtime, kind (cached briefly)."""
    key = str(root.resolve()) if root.exists() else str(root)
    cached = _SCAN_CACHE.get(key)
    if cached is not None:
        return cached
    files: list[dict[str, Any]] = []
    base = root.resolve() if root.exists() else root
    try:
        for p in base.rglob("*"):
            ext = p.suffix.lower()
            if ext not in MEDIA_EXTS or p.name.startswith("."):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if not p.is_file():
                continue
            files.append({"rel": p.relative_to(base).as_posix(), "size": st.st_size, "mtime": st.st_mtime,
                          "kind": "video" if ext in VIDEO_EXTS else "audio"})
    except OSError:
        pass
    _SCAN_CACHE.set(key, files)
    return files


def invalidate() -> None:
    _SCAN_CACHE.clear()


def _cover_for(root: Path, rel: str, covers: dict[str, str | None]) -> str | None:
    folder = rel.rsplit("/", 1)[0] if "/" in rel else ""
    if folder not in covers:
        covers[folder] = None
        for name in COVER_NAMES:
            if (root / folder / name).is_file():
                covers[folder] = f"{folder}/{name}" if folder else name
                break
    return covers[folder]


def describe(root: Path, f: dict[str, Any], entry: dict[str, Any] | None,
             covers: dict[str, str | None]) -> dict[str, Any]:
    rel = f["rel"]
    e = entry or {}
    parts = rel.split("/")
    folder = parts[-2] if len(parts) > 1 else None
    return {
        "path": rel, "size": f["size"], "mtime": f["mtime"], "kind": f["kind"],
        "title": e.get("title") or _title_from_name(Path(rel).stem),
        "collection": e.get("collection") or folder,
        "artist": e.get("artist"),
        "source": e.get("source") or guess_source(rel),
        "video_id": e.get("video_id"),
        "duration": e.get("duration"),
        "track": e.get("track"),
        "date": e.get("date"),
        "thumbnail": e.get("thumbnail"),
        "cover": _cover_for(root, rel, covers),
    }


def list_library(root: Path, index: LibraryIndex, query: str = "", source: str = "",
                 sort: str = "recent", offset: int = 0, limit: int = 60) -> dict[str, Any]:
    entries = index.snapshot()
    covers: dict[str, str | None] = {}
    items = [describe(root, f, entries.get(f["rel"]), covers) for f in scan(root)]
    counts = {s: 0 for s in SOURCES}
    for it in items:
        counts[it["source"]] = counts.get(it["source"], 0) + 1
    if source:
        items = [it for it in items if it["source"] == source]
    q = (query or "").strip().lower()
    if q:
        items = [it for it in items if q in " ".join(
            str(it.get(k) or "") for k in ("title", "collection", "artist", "path")).lower()]
    if sort == "title":
        items.sort(key=lambda it: it["title"].lower())
    elif sort == "collection":
        items.sort(key=lambda it: ((it["collection"] or "").lower(), it.get("track") or 0,
                                   it["title"].lower()))
    elif sort == "size":
        items.sort(key=lambda it: it["size"], reverse=True)
    else:
        items.sort(key=lambda it: it["mtime"], reverse=True)
    total = len(items)
    return {"items": items[offset: offset + limit], "total": total, "offset": offset,
            "limit": limit, "counts": counts,
            "total_size": sum(it["size"] for it in items)}


# ----- deleting --------------------------------------------------------------


def _sidecars(p: Path) -> list[Path]:
    """Subtitles, info JSON, thumbnails… written next to `p` with its stem."""
    out = []
    try:
        for q in p.parent.iterdir():
            if q == p or not q.is_file():
                continue
            if (q.stem == p.stem or q.name.startswith(p.stem + ".")) and q.suffix.lower() in _SIDECAR_EXTS:
                out.append(q)
    except OSError:
        pass
    return out


def _clear_archive(root: Path, video_id: str) -> bool:
    path = root / "archive.txt"
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return False
    keep = [ln for ln in lines if not (ln.split() and ln.split()[-1] == video_id)]
    if len(keep) == len(lines):
        return False
    path.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
    return True


def _prune(root: Path, folder: Path) -> None:
    """Remove folders the delete left empty (or holding only a cover)."""
    base = root.resolve()
    while folder != base and folder.is_relative_to(base):
        try:
            rest = list(folder.iterdir())
            if any(q.name not in COVER_NAMES for q in rest):
                return
            for q in rest:
                q.unlink()
            folder.rmdir()
        except OSError:
            return
        folder = folder.parent


def delete(root: Path, rel: str, index: LibraryIndex) -> dict[str, Any]:
    """Delete a media file with its sidecars and index entry. Its id is also
    removed from the download archive, so it can be downloaded again."""
    p = resolve_rel(root, rel, MEDIA_EXTS)
    entry = index.forget(rel) or {}
    removed = [p.name]
    p.unlink()
    for side in _sidecars(p):
        try:
            side.unlink()
            removed.append(side.name)
        except OSError:
            pass
    vid = entry.get("video_id")
    if not vid:
        m = VIDEO_ID_RE.search(p.stem)
        vid = m.group(1) if m else None
    archive_cleared = bool(vid) and _clear_archive(root, vid)
    _prune(root, p.parent)
    invalidate()
    return {"removed": removed, "archive_cleared": archive_cleared}
