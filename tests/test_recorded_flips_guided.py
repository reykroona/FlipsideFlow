"""Side flips pressed while recording are hard side breaks for guided detection."""
import uuid

import numpy as np
import pytest
from fastapi.testclient import TestClient

import backend.api as api


@pytest.fixture
def flipped(tmp_path, monkeypatch):
    """Two sides of two 100 s songs; the flip at 205 s joins the sides with no silence at all
    (paused audio is not saved), and a long quiet passage inside side B's first song is
    the stretch silence-guessing would take for the side change."""
    sid = str(uuid.uuid4())
    source = tmp_path / 'album.wav'
    source.touch()
    session = api.session_store.create(session_id=sid, source_audio=source, source_filename='album.wav',
                                       source_duration=410, source_size=0)
    envelope = np.full(41000, -12.)
    for start, end in [(0, 2), (100, 103), (203, 205), (205, 207), (250, 262), (305, 308), (408, 410)]:
        envelope[start * 100:end * 100] = -90
    session.recording_side_markers = [205.0]

    async def cached_envelope(session):
        return envelope

    monkeypatch.setattr(api, 'get_envelope', cached_envelope)
    yield session
    api.session_store.remove(sid)


def test_recorded_flip_is_the_side_break(flipped):
    response = TestClient(api.app).post('/api/analyze-guided', json={
        'file_id': flipped.id, 'expected_tracks': 4, 'durations': [100] * 4, 'side_breaks': [2],
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['matched'] is True and len(body['tracks']) == 4
    # Side A's tracks end at or before the flip, side B's start at or after it.
    assert body['tracks'][1]['end'] <= 205 + 0.01
    assert body['tracks'][2]['start'] >= 205 - 0.01
    assert body['tracks'][2]['end'] == pytest.approx(body['tracks'][3]['start'], abs=0.01)
    assert body['tracks'][3]['start'] > 300  # not the quiet passage at 250 s


def test_flips_that_disagree_with_the_sides_are_ignored(flipped):
    flipped.recording_side_markers = [100.0, 205.0]  # two flips, but one side change chosen
    response = TestClient(api.app).post('/api/analyze-guided', json={
        'file_id': flipped.id, 'expected_tracks': 4, 'durations': [100] * 4, 'side_breaks': [2],
    })
    assert response.status_code == 200, response.text
    assert len(response.json()['tracks']) == 4
