import json
import threading
import io
import wave

import pytest
from fastapi.testclient import TestClient

from backend import api, recording_api
from recognition import RecognitionService, RecognitionToken, choose_excerpt as real_choose_excerpt
from tests.test_recording import emit
from tests.test_recording_api import env
from tests.test_recognition import FakeProvider, FakeResolver, SONG


@pytest.fixture
def recognition_env(env, tmp_path, monkeypatch):
    rec, client = env
    provider = FakeProvider()
    svc = RecognitionService(rec, provider, FakeResolver(), autostart=False)
    token = RecognitionToken(tmp_path / 'token.json')
    monkeypatch.setattr(recording_api, 'recognition', svc)
    monkeypatch.setattr(recording_api, 'recognition_token', token)
    monkeypatch.setattr('recognition.choose_excerpt', lambda snapshot: (b'excerpt', 0))
    yield rec, client, svc, provider, token
    svc.shutdown()


def start(client, **extra):
    assert client.post('/api/recording/monitor', json=dict(device=0, device_name='Scarlett USB', left=1, right=2, rate=48000)).status_code == 200
    response = client.post('/api/recording/start', json=extra)
    assert response.status_code == 200
    return response.json()['id']


def test_key_is_write_only_including_invalid_requests(recognition_env):
    rec, client, svc, provider, token = recognition_env
    secret = 'fake-private-key'
    assert client.post('/api/recording/recognition/token', json={'token': secret}).json() == {'configured': True}
    assert token.get() == secret
    assert secret not in client.get('/api/recording/status').text
    for invalid in [secret * 100, {'token': secret}, None]:
        response = client.post('/api/recording/recognition/token', json={'token': invalid})
        assert response.status_code == 422 and secret not in response.text
    assert client.post('/api/recording/recognition/token', json={'token': ''}).json() == {'configured': False}
    remote = TestClient(api.app, client=('192.168.1.5', 5000))
    assert remote.post('/api/recording/recognition/token', json={'token': secret}).status_code == 403
    assert client.post('/api/recording/recognition/token', json={'token': secret}, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert token.get() == ''


def test_opt_out_manual_lookup_and_recovery_keep_metadata(recognition_env):
    rec, client, svc, provider, token = recognition_env
    ident = start(client, identify_album=False)
    assert svc.status()['enabled'] is False
    emit(rec, bytes(48000 * 6))
    assert client.post('/api/recording/recognition/identify').status_code == 200
    svc.tick()
    assert svc.status()['song'] == SONG
    result = client.post('/api/recording/stop').json()
    assert result['file']['recognition']['selected_release']['id'] == 1
    original = (rec.root / ident / 'audio.wav').read_bytes()
    # Recovery loads persisted metadata even after a fresh service starts.
    svc.begin(None)
    reopened = client.post(f'/api/recording/saved/{ident}/open').json()['file']
    assert reopened['recognition']['song'] == SONG
    assert (rec.root / ident / 'audio.wav').read_bytes() == original
    assert provider.calls == 1


def test_stop_and_pause_do_not_wait_for_network_lookup(recognition_env):
    rec, client, svc, provider, token = recognition_env
    ident = start(client)
    emit(rec, bytes(48000 * 6))
    entered, finish = threading.Event(), threading.Event()
    def recognize(*args):
        entered.set()
        assert finish.wait(5)
        return SONG
    provider.recognize = recognize
    client.post('/api/recording/recognition/identify')
    worker = threading.Thread(target=svc.tick)
    worker.start()
    try:
        assert entered.wait(2)
        assert client.post('/api/recording/pause', json={'side_flip': True}).status_code == 200
        assert client.get('/api/recording/status').json()['state'] == 'paused'
        assert client.post('/api/recording/resume').status_code == 200
        emit(rec, bytes(48000 * 6))
        stopped = client.post('/api/recording/stop').json()
        assert stopped['seconds'] == 2 and stopped['file']['id'] == ident
        assert not finish.is_set()
    finally:
        finish.set()
        worker.join(3)
    assert svc.status()['selected_release']['id'] == 1
    assert json.loads((rec.root / ident / 'recognition.json').read_text())['song'] == SONG
    choice = client.post('/api/recording/recognition/select', json={'recording_id': ident, 'release_id': None})
    assert choice.status_code == 200 and choice.json()['selected_release'] is None
    assert client.post('/api/recording/recognition/select', json={'recording_id': ident, 'release_id': 999}).status_code == 409


def test_identifies_actual_growing_wav_at_two_minutes_without_stopping(recognition_env, tmp_path, monkeypatch):
    from tests.test_recognition import pcm_file
    from recording import HEADER_BYTES
    rec, client, svc, provider, token = recognition_env
    # Exercise real excerpt extraction from the open capture file, not a sampler stub.
    monkeypatch.setattr('recognition.choose_excerpt', real_choose_excerpt)
    fixture = pcm_file(tmp_path, rate=48000, seconds=1)
    pcm = fixture['path'].read_bytes()[HEADER_BYTES:]
    ident = start(client, identify_album=True, auto_flip=False)
    for _ in range(119):
        emit(rec, pcm)
    svc.tick()
    assert provider.calls == 0
    def recognize(clip, cancel):
        assert rec.status()['state'] == 'recording'
        with wave.open(io.BytesIO(clip)) as audio:
            assert audio.getnframes() == 48000 * 10
            assert audio.getnchannels() == 1 and audio.getsampwidth() == 2
        provider.calls += 1
        return SONG
    provider.recognize = recognize
    emit(rec, pcm)
    svc.tick()
    live = client.get('/api/recording/status').json()
    assert live['state'] == 'recording' and live['seconds'] == 120
    assert live['recognition']['selected_release'], live['recognition']
    assert live['recognition']['selected_release']['id'] == 1
    assert api.session_store.get(ident) is None  # growing source never enters the editor
    emit(rec, pcm)
    assert client.get('/api/recording/status').json()['seconds'] == 121
    result = client.post('/api/recording/stop').json()
    assert result['file']['recognition']['selected_release']['id'] == 1
    assert provider.calls == 1
    assert (rec.root / ident / 'audio.wav').read_bytes()[HEADER_BYTES:] == pcm * 121
