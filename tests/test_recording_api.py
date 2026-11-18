import pytest
from fastapi.testclient import TestClient

from backend import api, recording_api
from recording import Recorder
from tests.test_recording import FakeAudio, emit


@pytest.fixture
def env(tmp_path, monkeypatch):
    rec = Recorder(tmp_path / "originals", audio=FakeAudio())
    monkeypatch.setattr(recording_api, "recorder", rec)
    monkeypatch.setattr(api, "UPLOAD_DIR", tmp_path / "uploads")
    api.UPLOAD_DIR.mkdir()
    async def no_preview(*args):
        pass
    monkeypatch.setattr(api, "_prepare_source", no_preview)
    before = {s.id for s in api.session_store.list()}
    yield rec, TestClient(api.app, client=("127.0.0.1", 12345))
    rec.shutdown()
    for session in api.session_store.list():
        if session.id not in before:
            api.session_store.remove(session.id)


def test_disabled_and_remote_controls_are_rejected(env, monkeypatch):
    rec, client = env
    remote = TestClient(api.app, client=("192.168.1.5", 5000))
    assert remote.post("/api/recording/start", json={}).status_code == 403
    assert remote.get("/api/recording/devices").status_code == 403
    assert client.post("/api/recording/start", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
    monkeypatch.setattr(recording_api, "recorder", None)
    assert client.get("/api/recording/status").json() == {"enabled": False}
    assert client.post("/api/recording/start", json={}).status_code == 404


def test_stop_registers_once_and_queue_cleanup_keeps_original(env):
    rec, client = env
    assert client.post("/api/recording/monitor", json=dict(device=0, device_name="Scarlett USB", left=1, right=2, rate=48000)).status_code == 200
    client.post("/api/recording/start", json={"title": "Test album"})
    emit(rec, bytes(48000 * 6))
    client.post("/api/recording/pause", json={"side_flip": True})
    client.post("/api/recording/resume")
    emit(rec, bytes(48000 * 6))
    result = client.post("/api/recording/stop").json()
    file = result["file"]
    assert file["in_place"] and file["duration"] == 2
    assert file["recording_side_markers"] == [1]
    source = api.session_store.get(file["id"]).source_audio
    digest = source.read_bytes()
    assert client.post("/api/recording/stop").json()["file"]["id"] == file["id"]
    api.cleanup_session(file["id"])
    api.reap_expired()
    assert source.read_bytes() == digest
    reopened = client.post(f'/api/recording/saved/{file["id"]}/open').json()["file"]
    assert reopened["id"] == file["id"] and reopened["recording_side_markers"] == [1]


def test_bad_input_and_arbitrary_path_are_rejected(env):
    rec, client = env
    assert client.post("/api/recording/monitor", json=dict(device=0, device_name="Scarlett USB", left=1, right=1, rate=48000)).status_code == 409
    assert client.post("/api/recording/saved/not-a-uuid/open").status_code == 409
