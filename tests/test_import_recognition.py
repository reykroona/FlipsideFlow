import asyncio
import hashlib
import io
import threading
import wave
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

from audio_processor import run_ffmpeg
from backend import api, import_recognition_api as routes
from backend.sessions import SessionStore, SessionState
from imported_audio import choose_file_excerpt
from recognition import RecognitionError
from tests.test_recognition import SONG, release


@pytest.mark.parametrize('suffix,codec', [('.wav', 'pcm_s24le'), ('.aiff', 'pcm_s24be')])
def test_real_wav_and_aiff_excerpt_skips_silence_preserves_original(tmp_path, suffix, codec):
    path = tmp_path / ('original' + suffix)
    rate = 22050
    t = np.arange(40 * rate) / rate
    signal = np.sin(t * 440 * 2 * np.pi) * .25
    signal[:15 * rate] = 0
    stereo = (np.stack([signal, -signal], axis=1) * 32767).astype('<i2')
    run_ffmpeg(['-v', 'error', '-f', 's16le', '-ar', str(rate), '-ac', '2', '-i', 'pipe:0',
                '-c:a', codec, str(path)], input=stereo.tobytes(), text=False, capture_output=True, check=True)
    original = hashlib.sha256(path.read_bytes()).digest()
    clip, offset = choose_file_excerpt(path, 40)
    with wave.open(io.BytesIO(clip)) as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2
        assert w.getnframes() == w.getframerate() * 10
        assert np.max(np.abs(np.frombuffer(w.readframes(w.getnframes()), dtype='<i2'))) > 1000
    assert offset >= 10
    assert len(clip) < 500_000
    assert hashlib.sha256(path.read_bytes()).digest() == original


def test_silence_and_short_files_do_not_go_to_provider(tmp_path):
    path = tmp_path / 'silent.wav'
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)
        w.writeframes(bytes(22050 * 2 * 20))
    with pytest.raises(RecognitionError, match='No clear music'):
        choose_file_excerpt(path, 20)
    with pytest.raises(RecognitionError, match='at least 10'):
        choose_file_excerpt(path, 5)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = SessionStore()
    monkeypatch.setattr(api, 'session_store', store)
    source = tmp_path / 'import.wav'; source.write_bytes(b'original audio')
    session = store.create('file-one', source, source.name, 180, source.stat().st_size,
                           in_place=True, source_mtime=source.stat().st_mtime)
    calls = []
    provider = SimpleNamespace(configured=lambda: True,
        recognize=lambda clip, cancel: calls.append(clip) or SONG)
    resolver = SimpleNamespace(resolve=lambda song, cancel: ([release(1)], release(1)))
    svc = SimpleNamespace(provider=provider, resolver=resolver)
    monkeypatch.setattr(routes, 'require_recognition', lambda req: svc)
    monkeypatch.setattr(routes, 'choose_file_excerpt', lambda *args: (b'10-second-clip', 30))
    yield session, svc, calls, store
    assert not routes._jobs


async def settle():
    await asyncio.gather(*(item[0] for item in list(routes._jobs.values())))


def test_file_recognition_loads_album_and_retry_reuses_song(setup):
    session, svc, calls, store = setup
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://testserver') as c:
            response = await c.post('/api/files/file-one/recognition')
            assert response.status_code == 200 and response.json()['state'] == 'identifying'
            await settle()
            result = (await c.get('/api/files/file-one/recognition')).json()
            assert result['song'] == SONG and result['selected_release']['id'] == 1
            assert (await c.post('/api/files/file-one/recognition')).status_code == 429
            session.recognition['_requested_at'] = -100
            svc.provider.configured = lambda: False
            assert (await c.post('/api/files/file-one/recognition')).status_code == 200
            await settle()
            assert calls == [b'10-second-clip']
            assert session.release is None and session.boundaries == []
            assert session.source_audio.read_bytes() == b'original audio'
            assert (await c.post('/api/files/file-one/recognition/select', json={'release_id': 999})).status_code == 409
            assert (await c.post('/api/files/file-one/recognition/select', json={'release_id': 1})).json()['selection'] == 'manual'
    asyncio.run(exercise())


@pytest.mark.parametrize('mutation', ['remove', 'change', 'manual'])
def test_pending_recognition_does_not_block_or_publish_stale_results(setup, mutation):
    session, svc, calls, store = setup
    entered, finish = threading.Event(), threading.Event()
    def recognize(*args):
        entered.set()
        assert finish.wait(5)
        calls.append('request')
        return SONG
    svc.provider.recognize = recognize
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://testserver') as c:
            assert (await c.post('/api/files/file-one/recognition')).status_code == 200
            try:
                for _ in range(100):
                    if entered.is_set(): break
                    await asyncio.sleep(.01)
                assert entered.is_set()
                assert (await c.get('/api/queue')).status_code == 200
                assert (await c.post('/api/files/file-one/recognition')).status_code == 200
                if mutation == 'remove': store.remove(session.id)
                if mutation == 'change': session.source_audio.write_bytes(b'changed source')
                if mutation == 'manual':
                    assert (await c.post('/api/files/file-one/recognition/select', json={'release_id': None})).status_code == 200
            finally:
                finish.set()
            await settle()
            assert len(calls) == 1
            assert session.recognition['selected_release'] is None
            if mutation == 'manual': assert session.recognition['selection'] == 'manual'
    asyncio.run(exercise())


@pytest.mark.parametrize('outcome', ['no_match', 'no_album', 'ambiguous', 'provider_error', 'discogs_error'])
def test_failures_and_ambiguous_albums_remain_reviewable(setup, outcome):
    session, svc, calls, store = setup
    def fail(*args): raise RecognitionError('Service unavailable')
    if outcome == 'no_match': svc.provider.recognize = lambda *args: None
    if outcome == 'no_album': svc.resolver.resolve = lambda *args: ([], None)
    if outcome == 'ambiguous': svc.resolver.resolve = lambda *args: ([release(1), release(2)], None)
    if outcome == 'provider_error': svc.provider.recognize = fail
    if outcome == 'discogs_error': svc.resolver.resolve = fail
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://testserver') as c:
            assert (await c.post('/api/files/file-one/recognition')).status_code == 200
            await settle()
            result = (await c.get('/api/files/file-one/recognition')).json()
            assert result['selected_release'] is None
            if outcome == 'discogs_error': assert result['song'] == SONG
            if outcome == 'ambiguous': assert len(result['candidates']) == 2
    asyncio.run(exercise())


def test_invalid_sources_and_export_are_rejected_before_network(setup):
    session, svc, calls, store = setup
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://testserver') as c:
            assert (await c.post('/api/files/missing/recognition')).status_code == 404
            session.state = SessionState.PROCESSING
            assert (await c.post('/api/files/file-one/recognition')).status_code == 409
            session.state = SessionState.READY
            svc.provider.configured = lambda: False
            assert (await c.post('/api/files/file-one/recognition')).status_code == 409
            session.source_audio.unlink()
            assert (await c.post('/api/files/file-one/recognition')).status_code == 410
            assert calls == []
    asyncio.run(exercise())


def test_routes_keep_desktop_local_and_same_origin_guards(setup, monkeypatch):
    from backend import recording_api
    from fastapi.testclient import TestClient
    session, svc, calls, store = setup
    monkeypatch.setattr(routes, 'require_recognition', recording_api.require_recognition)
    monkeypatch.setattr(recording_api, 'recorder', object())
    monkeypatch.setattr(recording_api, 'recognition', svc)
    remote = TestClient(api.app, client=('192.168.1.5', 1234))
    assert remote.post('/api/files/file-one/recognition').status_code == 403
    local = TestClient(api.app, client=('127.0.0.1', 1234))
    assert local.post('/api/files/file-one/recognition', headers={'Origin': 'https://evil.example'}).status_code == 403
    monkeypatch.setattr(recording_api, 'recorder', None)
    assert local.post('/api/files/file-one/recognition').status_code == 404
    assert calls == []
