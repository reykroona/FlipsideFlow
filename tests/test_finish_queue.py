"""Finish jobs survive restarts and never own the next capture's mutable state."""
import asyncio
import copy
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from backend import api, recording_api, finish_queue as queue
from backend.sessions import pipeline
from recording import Recorder
from tests.test_recording import FakeAudio, monitor, emit
from tests.test_finish_quality import RELEASE, SONG, ANALYSIS


@pytest.fixture
def env(tmp_path, monkeypatch):
    rec = Recorder(tmp_path / 'originals', audio=FakeAudio())
    monkeypatch.setattr(recording_api, 'recorder', rec)
    monkeypatch.setattr(recording_api, 'recognition', None)
    monkeypatch.setattr(api, 'UPLOAD_DIR', tmp_path / 'cache')
    api.UPLOAD_DIR.mkdir()
    monkeypatch.setattr(api.config, 'default_output_dir', str(tmp_path / 'output'))
    monkeypatch.setattr(queue, '_task', None)
    monkeypatch.setattr(queue, '_wake', asyncio.Event())
    monkeypatch.setattr(queue, '_cancel', threading.Event())
    monkeypatch.setattr(queue, 'export_lock', asyncio.Lock())
    monkeypatch.setattr(queue, 'start', lambda: None)
    async def no_preview(*args): pass
    monkeypatch.setattr(api, '_prepare_source', no_preview)
    client = TestClient(api.app, client=('127.0.0.1', 12345))
    monitor(rec)
    rec.start('First record')
    emit(rec, bytes(48000 * 6))
    result = client.post('/api/recording/stop', json={'auto_export': True}).json()
    ident = result['file']['id']
    session = api.session_store.get(ident)
    job = queue.get_job(ident)
    yield rec, client, session, job
    rec.shutdown()
    for item in api.session_store.list():
        if item.source_audio.is_relative_to(tmp_path): api.session_store.remove(item.id)


def test_stop_queues_once_freezes_settings_and_next_capture_runs(env):
    rec, client, session, job = env
    original = session.source_audio.read_bytes()
    duplicate = client.post('/api/recording/stop', json={'auto_export': True, 'output_format': 'mp3'}).json()
    assert duplicate['queued_job']['id'] == job['id']
    assert len(queue.jobs()) == 1
    assert queue.get_job(job['id'])['settings']['output_format'] == 'flac'
    monitor(rec)
    rec.start('Next record')
    emit(rec, bytes(48000 * 6))
    assert rec.status()['state'] == 'recording'
    assert rec.open_saved(job['id'])[0] == session.source_audio
    assert session.source_audio.read_bytes() == original
    assert client.post(f'/api/recording/finish-queue/{job["id"]}/open').status_code == 409
    assert client.delete(f'/api/queue/{job["id"]}').status_code == 409
    client.delete('/api/temp/clear-all')
    assert api.session_store.get(job['id']) is session
    session.updated_at = datetime.now() - timedelta(days=30)
    api.reap_expired()
    assert api.session_store.get(job['id']) is session


def test_restart_marks_inflight_for_review_and_keeps_pending_job(env):
    rec, client, session, job = env
    asyncio.run(queue.recover())
    assert queue.get_job(job['id'])['state'] == 'queued'
    queue.save(job, state='exporting', tracks=ANALYSIS['tracks'])
    asyncio.run(queue.recover())
    assert queue.get_job(job['id'])['state'] == 'needs_review'
    api.session_store.remove(job['id'])
    monitor(rec)
    rec.start('Next record')
    opened = client.post(f'/api/recording/finish-queue/{job["id"]}/open')
    assert opened.status_code == 200, opened.text
    assert opened.json()['tracks'] == ANALYSIS['tracks']
    assert rec.status()['state'] == 'recording'
    assert client.post('/api/recording/finish-queue/not-an-id/open').status_code == 404


def setup_analysis(env, monkeypatch, *, valid=True):
    rec, client, session, job = env
    release = {**copy.deepcopy(RELEASE), 'id': 10}
    job['recognition'] = {'song': SONG, 'selected_release': release}
    queue.save(job)
    async def envelope(s): return np.full(42000, -12.)
    async def guided(request):
        analysis = copy.deepcopy(ANALYSIS)
        if not valid: analysis['boundaries'] = []
        return analysis
    monkeypatch.setattr(api, 'get_envelope', envelope)
    monkeypatch.setattr(api, 'analyze_guided', guided)
    monkeypatch.setattr(api.metadata_handler, 'get_release_by_id', lambda *a, **k: SimpleNamespace(id=10))
    monkeypatch.setattr(api.metadata_handler, 'create_album_folder_name', lambda r: 'Artist - Album')
    return job


def test_clear_match_exports_while_next_recording_continues(env, monkeypatch):
    rec, client, session, _ = env
    job = setup_analysis(env, monkeypatch)
    monitor(rec)
    rec.start('Second record')
    second_id = rec.status()['id']
    calls = []
    def export(old, spec, *args):
        assert old.id == job['id'] and old.mappings == {1: 'A1', 2: 'A2'}
        calls.append(spec)
        emit(rec, bytes(48000 * 6))
        old.last_run = SimpleNamespace(output_folder=spec.output_dir / 'Artist - Album')
    monkeypatch.setattr(pipeline, 'run', export)
    asyncio.run(queue.run_job(job))
    result = queue.get_job(job['id'])
    assert result['state'] == 'complete', result
    assert result['score'] == 100
    assert len(calls) == 1
    assert rec.status()['id'] == second_id and rec.status()['state'] == 'recording'
    assert rec.status()['seconds'] == 1


def test_no_silence_evidence_needs_review_even_at_90(env, monkeypatch):
    job = setup_analysis(env, monkeypatch, valid=False)
    monkeypatch.setattr(pipeline, 'run', lambda *args: pytest.fail('Unsafe export'))
    asyncio.run(queue.run_job(job))
    result = queue.get_job(job['id'])
    assert result['state'] == 'needs_review'
    assert result['score'] == 90
    assert result['tracks']


def test_existing_output_folder_is_never_auto_overwritten(env, monkeypatch):
    job = setup_analysis(env, monkeypatch)
    from pathlib import Path
    folder = Path(job['settings']['output_dir']) / 'Artist - Album'
    folder.mkdir(parents=True)
    (folder / 'keep.flac').write_bytes(b'existing audio')
    monkeypatch.setattr(pipeline, 'run', lambda *args: pytest.fail('Overwrite'))
    asyncio.run(queue.run_job(job))
    assert queue.get_job(job['id'])['state'] == 'needs_review'
    assert (folder / 'keep.flac').read_bytes() == b'existing audio'


def test_missing_provider_stays_reviewable_and_explicit_export_resolves_job(env):
    rec, client, session, job = env
    asyncio.run(queue.run_job(job))
    assert queue.get_job(job['id'])['state'] == 'needs_review'
    assert 'no AudD token' in queue.get_job(job['id'])['message']  # says why, not "could not finish"
    assert client.post(f'/api/recording/finish-queue/{job["id"]}/open').status_code == 200
    queue.reviewed_export(job['id'], '/output/reviewed')
    assert queue.get_job(job['id'])['state'] == 'complete'


def test_manual_reexport_waiting_for_slot_is_processing_and_protected(env, monkeypatch):
    from backend.sessions import SessionState
    rec, client, session, job = env
    queue.save(job, state='needs_review')
    session.state = SessionState.COMPLETE
    session.last_run = SimpleNamespace(output_folder='/previous', final_fraction=1., message='Complete', output_files=[])
    def export(*args):
        pass
    monkeypatch.setattr(pipeline, 'run', export)
    request = api.ProcessRequest(file_id=session.id, release_id=10,
        track_mapping=[api.TrackMapping(detected=1, discogs='A1')],
        track_boundaries=[api.TrackBoundary(number=1,start=0,end=1,duration=1)])
    async def scenario():
        await queue.export_lock.acquire()
        try:
            await api.process_file(request)
            await asyncio.sleep(.01)
            result = await api.get_process_job(session.id)
            assert result['status'] == 'processing'
            assert 'output_path' not in result  # never announce the previous export as this one
            assert not api.cleanup_session(session.id)
            with pytest.raises(api.HTTPException) as error:
                await api.process_file(request)
            assert error.value.status_code == 409
        finally:
            queue.export_lock.release()
        for _ in range(100):
            if session.id not in queue.manual_exports: break
            await asyncio.sleep(.01)
        assert session.id not in queue.manual_exports
    asyncio.run(scenario())
