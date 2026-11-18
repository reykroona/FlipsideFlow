import io
import json
import threading
import uuid
import wave
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import numpy as np
import pytest
import requests

from recording import wav_header
from recognition import (
    AudDProvider, Cancelled, DiscogsResolver, RecognitionError, RecognitionService,
    RecognitionToken, choose_excerpt, rank_releases,
)


SONG = {"artist": "Test Artist", "title": "First Song", "album": "The Album"}


def release(ident=1, album="The Album", master=10, **extra):
    return dict(id=ident, master_id=master, title=album, artist="Test Artist", format="Vinyl",
                tracks=[dict(position="A1", title="First Song", duration="3:00"),
                        dict(position="B1", title="Second Song", duration="4:00")], **extra)


def pcm_file(tmp_path, rate=48000, seconds=16, *, silence=False, clipped=False):
    t = np.arange(rate * seconds) / rate
    signal = np.zeros(len(t)) if silence else np.sin(t * 2 * np.pi * 440) * 0.3
    if clipped:
        signal[:] = 0.99999
    # Opposite-polarity channels should not cancel out the recognition copy.
    values = (np.stack([signal, -signal], axis=1) * 8388607).astype(np.int32)
    packed = np.stack([values & 255, values >> 8 & 255, values >> 16 & 255], axis=2).astype(np.uint8)
    path = tmp_path / "audio.wav"
    data = packed.tobytes()
    path.write_bytes(wav_header(len(data), rate) + data)
    return dict(path=path, rate=rate, frames=len(t), pause_markers=[])


@pytest.mark.parametrize("rate", [44100, 48000, 96000])
def test_excerpt_is_short_and_original_is_unchanged(tmp_path, rate):
    snapshot = pcm_file(tmp_path, rate)
    original = snapshot["path"].read_bytes()
    clip, offset = choose_excerpt(snapshot)
    with wave.open(io.BytesIO(clip), "rb") as f:
        assert f.getnchannels() == 1 and f.getsampwidth() == 2
        assert f.getnframes() == rate * 10
        assert f.getframerate() == rate
    assert offset >= 0 and offset + 10 <= 16
    assert snapshot["path"].read_bytes() == original


@pytest.mark.parametrize("kind", ["silence", "clipped", "short", "pause"])
def test_no_unusable_or_discontinuous_excerpt(tmp_path, kind):
    snapshot = pcm_file(tmp_path, rate=8000, seconds=8 if kind == "short" else 16,
                        silence=kind == "silence", clipped=kind == "clipped")
    if kind == "pause":
        snapshot["pause_markers"] = [8 * 8000]
    with pytest.raises(RecognitionError):
        choose_excerpt(snapshot)


def test_excerpt_does_not_trust_live_wav_header_or_unwritten_frames(tmp_path):
    snapshot = pcm_file(tmp_path, rate=8000)
    with snapshot["path"].open("r+b") as f:
        f.write(wav_header(0, 8000))
    snapshot["frames"] += 8000 * 60
    clip, offset = choose_excerpt(snapshot)
    assert offset + 10 <= 16


def response(payload, status=200):
    return SimpleNamespace(status_code=status, json=lambda: payload)


def test_audd_sends_only_excerpt_with_bounded_request():
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return response({"status": "success", "result": SONG})
    provider = AudDProvider(lambda: "private-token", post)
    assert provider.recognize(b"wav bytes", threading.Event()) == SONG
    url, kwargs = calls[0]
    assert url == "https://api.audd.io/"
    assert kwargs["files"]["file"][1] == b"wav bytes"
    assert kwargs["data"] == {"api_token": "private-token"}
    assert kwargs["timeout"] == (5, 20) and kwargs["allow_redirects"] is False


def test_audd_no_match_errors_and_cancellation_do_not_leak_tokens():
    assert AudDProvider(lambda: "key", lambda *a, **k: response({"status": "success", "result": None})).recognize(b"", threading.Event()) is None
    for status in (401, 403, 429):
        with pytest.raises(RecognitionError) as error:
            AudDProvider(lambda: "key", lambda *a, **k: response({}, status)).recognize(b"", threading.Event())
        assert not error.value.retryable
    def offline(*a, **k):
        raise requests.Timeout("sensitive-token-should-not-appear")
    with pytest.raises(RecognitionError) as error:
        AudDProvider(lambda: "key", offline).recognize(b"", threading.Event())
    assert "sensitive" not in str(error.value)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(Cancelled):
        AudDProvider(lambda: "key", offline).recognize(b"", cancel)


def test_token_file_is_private_and_clear_overrides_environment(tmp_path, monkeypatch):
    token = RecognitionToken(tmp_path / "token.json")
    monkeypatch.setenv("AUDD_API_TOKEN", "environment-token")
    assert token.get() == "environment-token"
    token.set(" my-key ")
    assert token.get() == "my-key" and token.path.stat().st_mode & 0o777 == 0o600
    token.set("")
    assert token.get() == ""


def test_album_ranking_groups_pressings_and_verifies_song():
    wrong = release(4, "Wrong", 40)
    wrong["tracks"][0]["title"] = "Completely different song"
    candidates, chosen = rank_releases(SONG, [release(1), release(2), release(3, "Compilation", 30), wrong])
    assert len(candidates) == 2 and chosen["title"] == "The Album"
    assert all(c["id"] != 4 for c in candidates)
    tied_song = {**SONG, "album": ""}
    candidates, chosen = rank_releases(tied_song, [release(1), release(2, "Another Album", 20)])
    assert len(candidates) == 2 and chosen is None
    cd = release()
    cd["format"] = "CD"
    assert rank_releases(SONG, [cd]) == ([], None)
    unrelated = release()
    unrelated["artist"] = "Someone Else"
    assert rank_releases(SONG, [unrelated]) == ([], None)


class FakeRecorder:
    def __init__(self, root):
        self.root = root
        self.snapshot = None
    def start(self, seconds=0):
        ident = str(uuid.uuid4())
        (self.root / ident).mkdir(parents=True)
        self.snapshot = dict(id=ident, state="recording", rate=48000, frames=seconds * 48000)
        return ident
    def capture_snapshot(self):
        return dict(self.snapshot) if self.snapshot else None
    def seconds(self, seconds):
        self.snapshot["frames"] = seconds * 48000


class FakeProvider:
    def __init__(self):
        self.calls = 0
        self.results = [SONG]
        self.has_token = True
    def configured(self):
        return self.has_token
    def recognize(self, clip, cancel):
        self.calls += 1
        value = self.results[min(self.calls - 1, len(self.results) - 1)]
        if isinstance(value, Exception):
            raise value
        return value


class FakeResolver:
    def __init__(self):
        self.calls = 0
        self.error = None
    def resolve(self, song, cancel):
        self.calls += 1
        if self.error:
            raise self.error
        return [release()], release()


@pytest.fixture
def service(tmp_path, monkeypatch):
    rec, provider, resolver = FakeRecorder(tmp_path), FakeProvider(), FakeResolver()
    monkeypatch.setattr("recognition.choose_excerpt", lambda snapshot: (b"excerpt", 100))
    svc = RecognitionService(rec, provider, resolver, autostart=False)
    ident = rec.start()
    svc.begin(ident)
    return svc, rec, provider, resolver


def test_triggers_on_recorded_time_not_pause_time_and_caps_retries(service):
    svc, rec, provider, resolver = service
    provider.results = [None]
    rec.seconds(119)
    svc.tick()
    assert provider.calls == 0
    rec.seconds(120)
    rec.snapshot["state"] = "paused"
    svc.tick()
    assert provider.calls == 0
    rec.snapshot["state"] = "recording"
    svc.tick()
    assert provider.calls == 1 and svc.status()["next_at"] == 180
    svc.tick()
    rec.seconds(179)
    svc.tick()
    assert provider.calls == 1
    rec.seconds(180)
    svc.tick()
    rec.seconds(3600)
    svc.tick()
    assert provider.calls == 2 and resolver.calls == 0


def test_success_persists_album_and_does_not_repeat_paid_recognition(service):
    svc, rec, provider, resolver = service
    rec.seconds(120)
    svc.tick()
    status = svc.status()
    assert status["state"] == "matched" and status["selected_release"]["id"] == 1
    saved = json.loads((rec.root / status["recording_id"] / "recognition.json").read_text())
    assert saved["song"] == SONG and saved["selected_release"]["id"] == 1
    rec.seconds(180)
    svc.tick()
    assert provider.calls == 1
    svc.request()  # retry album lookup reuses the song
    svc.tick()
    assert provider.calls == 1 and resolver.calls == 2


def test_missing_token_disabled_and_fatal_errors_do_not_spend_requests(service):
    svc, rec, provider, resolver = service
    provider.has_token = False
    rec.seconds(200)
    svc.tick()
    assert svc.status()["state"] == "needs_setup" and provider.calls == 0
    provider.has_token = True
    svc.tick()
    assert provider.calls == 1
    svc.begin(rec.snapshot["id"], enabled=False)
    svc.tick()
    assert provider.calls == 1
    svc.begin(rec.snapshot["id"])
    provider.results = [RecognitionError("Quota", retryable=False)]
    svc.tick()
    rec.seconds(400)
    svc.tick()
    assert provider.calls == 2


def test_discogs_failure_preserves_song_and_recording(service):
    svc, rec, provider, resolver = service
    resolver.error = RecognitionError("Discogs token missing", retryable=False)
    rec.seconds(120)
    svc.tick()
    assert svc.status()["song"] == SONG
    assert rec.snapshot["state"] == "recording"
    resolver.error = None
    svc.request()
    svc.tick()
    assert provider.calls == 1 and svc.status()["state"] == "matched"


def test_stale_result_never_attaches_to_new_recording(service):
    svc, rec, provider, resolver = service
    entered, resume = threading.Event(), threading.Event()
    def recognize(*args):
        entered.set()
        assert resume.wait(2)
        return SONG
    provider.recognize = recognize
    rec.seconds(120)
    worker = threading.Thread(target=svc.tick)
    worker.start()
    assert entered.wait(2)
    new_id = rec.start()
    svc.begin(new_id)
    resume.set()
    worker.join(2)
    assert not worker.is_alive()
    assert svc.status()["recording_id"] == new_id and svc.status()["song"] is None
    assert resolver.calls == 0


def test_user_selection_is_not_overwritten_by_later_lookup(service):
    svc, rec, provider, resolver = service
    rec.seconds(120)
    svc.tick()
    svc.choose(rec.snapshot["id"], None)
    svc.request()
    svc.tick()
    assert svc.status()["selection"] == "manual" and svc.status()["selected_release"] is None


def test_discogs_uses_structured_album_search_and_timed_token_header(monkeypatch):
    from metadata_handler import MetadataHandler
    parent = MetadataHandler("private-discogs-token", "VinylFlow tests")
    parent.min_request_interval = 0
    calls = []
    def request(method, url, **kwargs):
        calls.append((url, kwargs))
        body = {"results": []}
        return SimpleNamespace(status_code=200, content=json.dumps(body).encode())
    monkeypatch.setattr("recognition.requests.request", request)
    resolver = DiscogsResolver(lambda: parent, lambda r: r)
    assert resolver.resolve(SONG, threading.Event()) == ([], None)
    assert len(calls) == 2  # album first, song fallback
    assert parse_qs(urlparse(calls[0][0]).query)["release_title"] == ["The Album"]
    assert parse_qs(urlparse(calls[0][0]).query)["format"] == ["Vinyl"]
    assert parse_qs(urlparse(calls[1][0]).query)["track"] == ["First Song"]
    assert all("private-discogs-token" not in url for url, _ in calls)
    assert calls[0][1]["headers"]["Authorization"] == "Discogs token=private-discogs-token"
    assert calls[0][1]["timeout"][0] == 5


def test_discogs_verifies_full_tracks_falls_back_and_reuses_cache(monkeypatch):
    from backend.api import _serialize_release
    from metadata_handler import MetadataHandler
    parent = MetadataHandler("fake-token", "VinylFlow tests")
    parent.min_request_interval = 0
    calls = []
    def request(method, url, **kwargs):
        calls.append(url)
        if '/database/search' in url:
            ident = 1 if 'release_title=' in url else 2
            body = {'results': [dict(id=ident, type='release', title='Test Artist - The Album', format=['Vinyl'], master_id=10)]}
        else:
            ident = int(url.rsplit('/', 1)[-1])
            body = dict(id=ident, title='The Album', year=1977, artists=[{'id': 42, 'name': 'Test Artist', 'join': '', 'anv': ''}],
                        labels=[], formats=[{'name': 'Vinyl'}], images=[], genres=[], extraartists=[],
                        tracklist=[dict(position='A1', title='Wrong Song' if ident == 1 else 'First Song', duration='3:00', type_='track')])
        return SimpleNamespace(status_code=200, content=json.dumps(body).encode())
    monkeypatch.setattr('recognition.requests.request', request)
    resolver = DiscogsResolver(lambda: parent, _serialize_release)
    candidates, chosen = resolver.resolve(SONG, threading.Event())
    assert chosen['id'] == 2 and chosen['tracks'][0]['title'] == 'First Song'
    assert {c['id'] for c in candidates} == {2}
    detail_calls = len([url for url in calls if '/releases/' in url])
    assert detail_calls == 2
    resolver.resolve(SONG, threading.Event())
    assert len([url for url in calls if '/releases/' in url]) == detail_calls


def test_discogs_detail_failure_is_reported_as_error_not_no_album(monkeypatch):
    from metadata_handler import MetadataHandler
    parent = MetadataHandler('fake-token', 'VinylFlow tests')
    parent.min_request_interval = 0
    def request(method, url, **kwargs):
        if '/database/search' in url:
            return SimpleNamespace(status_code=200, content=json.dumps({'results': [dict(id=1, type='release', title='Test Artist - The Album', format=['Vinyl'])]}).encode())
        raise requests.Timeout('private request details')
    monkeypatch.setattr('recognition.requests.request', request)
    with pytest.raises(RecognitionError, match='Could not reach Discogs'):
        DiscogsResolver(lambda: parent, lambda r: r).resolve(SONG, threading.Event())
