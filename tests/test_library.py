"""Library scan: albums from tags, ids that can't escape the folder, covers, manifests."""
import json
import struct

import pytest
from fastapi.testclient import TestClient
from mutagen.aiff import AIFF
from mutagen.id3 import APIC, TALB, TIT2, TPE1, TPE2, TRCK, TXXX

from library import MANIFEST, Library, write_manifest


def _ieee_extended(rate):
    # 80-bit float for the AIFF COMM chunk (only exact for integer rates)
    exponent, mantissa = 16383 + 63, int(rate)
    while mantissa and not mantissa & (1 << 63):
        mantissa <<= 1
        exponent -= 1
    return struct.pack(">HQ", exponent, mantissa)


def make_aiff(path, seconds=1, rate=8000, **tags):
    frames = seconds * rate
    data = b"\0\0" * frames
    comm = struct.pack(">hIh", 1, frames, 16) + _ieee_extended(rate)
    ssnd = struct.pack(">II", 0, 0) + data
    body = b"AIFF" + b"COMM" + struct.pack(">I", len(comm)) + comm + b"SSND" + struct.pack(">I", len(ssnd)) + ssnd
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"FORM" + struct.pack(">I", len(body)) + body)
    audio = AIFF(path)
    audio.add_tags()
    frames_map = {"title": TIT2, "artist": TPE1, "album": TALB, "album_artist": TPE2, "track": TRCK}
    for key, frame in frames_map.items():
        if key in tags:
            audio.tags.add(frame(encoding=3, text=str(tags[key])))
    if "position" in tags:
        audio.tags.add(TXXX(encoding=3, desc="VINYLPOSITION", text=tags["position"]))
    if tags.get("art"):
        audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=b"\xff\xd8fakejpeg"))
    audio.save()
    return path


@pytest.fixture
def lib_root(tmp_path):
    album = tmp_path / "out" / "Fleetwood Mac - Rumours"
    make_aiff(album / "A2-Dreams.aiff", title="Dreams", artist="Fleetwood Mac", album="Rumours", track=2, position="A2", art=True)
    make_aiff(album / "A1-Second Hand News.aiff", title="Second Hand News", artist="Fleetwood Mac", album="Rumours", track=1, position="A1")
    make_aiff(album / "temp_A3.aiff", title="half written")
    make_aiff(tmp_path / "out" / "Recordings" / "abc" / "audio.aiff", title="raw")
    make_aiff(tmp_path / "out" / "Untagged Artist - Some Album" / "01.aiff")
    return tmp_path / "out"


def test_scan_groups_albums_sorts_tracks_and_skips(lib_root):
    lib = Library(lambda: str(lib_root), lambda: [lib_root / "Recordings"])
    result = lib.scan()
    assert result["error"] is None
    albums = {a["title"]: a for a in result["albums"]}
    assert set(albums) == {"Rumours", "Some Album"}  # raw recordings and temp files are not albums
    rumours = albums["Rumours"]
    assert rumours["artist"] == "Fleetwood Mac"
    assert [t["title"] for t in rumours["tracks"]] == ["Second Hand News", "Dreams"]
    assert [t["position"] for t in rumours["tracks"]] == ["A1", "A2"]
    assert rumours["has_cover"] is True  # embedded art counts
    assert rumours["tracks"][0]["duration"] == pytest.approx(1, abs=0.01)
    untagged = albums["Some Album"]
    assert untagged["artist"] == "Untagged Artist"  # from the folder name
    assert untagged["tracks"][0]["title"] == "01"


def test_ids_resolve_only_inside_the_library(lib_root, tmp_path):
    lib = Library(lambda: str(lib_root))
    album = next(a for a in lib.scan()["albums"] if a["title"] == "Rumours")
    track_id = album["tracks"][0]["id"]
    assert lib.track_path(track_id).name == "A1-Second Hand News.aiff"
    assert lib.track_path("nope") is None
    # The output folder moved elsewhere: old ids must not reach the old files.
    other = tmp_path / "elsewhere"
    other.mkdir()
    moved = Library(lambda: str(other))
    moved._tracks = dict(lib._tracks)
    assert moved.track_path(track_id) is None


def test_missing_root_is_reported(tmp_path):
    result = Library(lambda: str(tmp_path / "missing")).scan()
    assert result["albums"] == [] and result["error"]


def test_manifest_records_sources(tmp_path):
    folder = tmp_path / "A - B"
    folder.mkdir()
    source = tmp_path / "rec.wav"
    source.write_bytes(b"x")
    write_manifest(folder, "s1", source)
    write_manifest(folder, "s1", source)  # re-export replaces, not duplicates
    write_manifest(folder, "s2", tmp_path / "gone.wav")
    data = json.loads((folder / MANIFEST).read_text())
    assert [s["session_id"] for s in data["sources"]] == ["s1", "s2"]
    assert [s["available"] for s in Library._sources(folder)] == [True, False]


def test_library_routes(lib_root, monkeypatch):
    import backend.api as api
    import backend.library_api as library_api

    monkeypatch.setattr(library_api, "library", Library(lambda: str(lib_root), lambda: [lib_root / "Recordings"]))
    client = TestClient(api.app)
    body = client.get("/api/library").json()
    album = next(a for a in body["albums"] if a["title"] == "Rumours")
    audio = client.get(f"/api/library/audio/{album['tracks'][0]['id']}", headers={"Range": "bytes=0-9"})
    assert audio.status_code == 206 and audio.content[:4] == b"FORM"
    cover = client.get(f"/api/library/cover/{album['id']}")
    assert cover.status_code == 200 and cover.content.startswith(b"\xff\xd8")
    assert client.get("/api/library/audio/unknown").status_code == 404
