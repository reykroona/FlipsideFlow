"""Song recognition for registered, finalized audio files in the desktop workbench.

Jobs are scoped to a Session, never the current recorder. Only excerpts leave the
Mac; source files and existing edit/export state are never modified by a worker.
"""
import asyncio
import copy
import threading
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from backend.recording_api import require_recognition
from backend.sessions import SessionState
from imported_audio import choose_file_excerpt
from recognition import Cancelled, RecognitionError

router = APIRouter(prefix='/api/files')
_jobs = {}


def _source(file_id, expected=None):
    from backend import api
    session = api.session_store.get(file_id)
    if session is None or (expected is not None and session is not expected):
        raise HTTPException(404, 'This file is no longer in the queue.')
    api._require_source(session)
    if not session.source_audio.is_file():
        raise HTTPException(410, 'The source audio is no longer available.')
    return session


def _status(session):
    return copy.deepcopy(session.recognition or {'file_id': session.id, 'source': 'import',
        'state': 'idle', 'song': None, 'candidates': [], 'selected_release': None, 'message': ''})


def _publish(session, **updates):
    try:
        _source(session.id, session)
    except HTTPException:
        session.recognition = {**_status(session), 'state': 'error', 'selected_release': None,
                               'message': 'The source changed or was removed. Import it again before identifying.'}
        return False
    state = _status(session)
    if state.get('selection') == 'manual':
        updates.pop('selected_release', None)
    session.recognition = {**state, **updates}
    return True


async def _identify(session, service, cancel):
    try:
        song = (session.recognition or {}).get('song')
        if not song:
            clip, offset = await asyncio.to_thread(choose_file_excerpt, session.source_audio, session.source_duration)
            _source(session.id, session)  # do not upload a changed or removed original
            song = await asyncio.to_thread(service.provider.recognize, clip, cancel)
            if not song:
                _publish(session, state='no_match', message='No song match. Try again or search Discogs manually.')
                return
            if not _publish(session, song=song, excerpt_offset=offset, state='looking_up',
                            message='Song identified. Looking for the album on Discogs…'):
                return
        candidates, selected = await asyncio.to_thread(service.resolver.resolve, song, cancel)
        _publish(session, state='matched' if candidates else 'no_album', candidates=candidates,
                 selected_release=selected, message='Likely album found. Check the pressing and sides before exporting.' if selected else
                 'Several albums contain this song. Choose an album below.' if candidates else
                 'Song identified, but no matching vinyl tracklist was found. Search Discogs manually.')
    except RecognitionError as exc:
        _publish(session, state='error', message=str(exc))
    except (Cancelled, asyncio.CancelledError):
        cancel.set()
    except HTTPException:
        # The source disappeared/changed. Do not attach a stale result to it.
        if session.recognition:
            session.recognition = {**session.recognition, 'state': 'error', 'selected_release': None,
                                   'message': 'The source changed or was removed. Import it again before identifying.'}
    except Exception:
        _publish(session, state='error', message='Recognition could not finish. Try again or search Discogs manually.')
    finally:
        job = _jobs.get(session.id)
        if job and job[0] is asyncio.current_task():
            _jobs.pop(session.id, None)


@router.get('/{file_id}/recognition')
async def status(file_id: str, request: Request):
    require_recognition(request)
    return _status(_source(file_id))


@router.post('/{file_id}/recognition')
async def identify(file_id: str, request: Request):
    service = require_recognition(request)
    session = _source(file_id)
    if session.state == SessionState.PROCESSING:
        raise HTTPException(409, 'Wait for export to finish before identifying this file.')
    if file_id in _jobs:
        return _status(session)  # repeated clicks never spend another recognition request
    state = _status(session)
    if time.monotonic() - state.get('_requested_at', -100) < 10:
        raise HTTPException(429, 'Please wait a few seconds before retrying.')
    if len(_jobs) >= 2:
        raise HTTPException(429, 'Two files are already being identified. Wait for one to finish.')
    if not state.get('song') and not service.provider.configured():
        raise HTTPException(409, 'Add an AudD API token in Settings to identify imported audio.')
    session.recognition = {**state, 'file_id': file_id, 'source': 'import',
        'state': 'looking_up' if state.get('song') else 'identifying', '_requested_at': time.monotonic(),
        'message': 'Looking up the identified song on Discogs…' if state.get('song') else 'Identifying a 10-second excerpt with AudD…'}
    cancel = threading.Event()
    task = asyncio.create_task(_identify(session, service, cancel))
    _jobs[file_id] = task, cancel
    return _status(session)


class Selection(BaseModel):
    release_id: int | None = Field(default=None, ge=1)


@router.post('/{file_id}/recognition/select')
async def select(file_id: str, body: Selection, request: Request):
    require_recognition(request)
    session = _source(file_id)
    if session.state == SessionState.PROCESSING:
        raise HTTPException(409, 'Wait for export to finish before choosing another album.')
    state = _status(session)
    chosen = next((c for c in state.get('candidates', []) if c['id'] == body.release_id), None)
    if body.release_id is not None and chosen is None:
        raise HTTPException(409, 'Choose one of the suggested albums.')
    session.recognition = {**state, 'selection': 'manual', 'selected_release': chosen,
        'message': 'Album selected. Check the pressing and sides before exporting.' if chosen else 'Automatic album selection dismissed.'}
    return _status(session)


async def shutdown():
    tasks = list(_jobs.values())
    for task, cancel in tasks:
        cancel.set()
        task.cancel()
    await asyncio.gather(*(task for task, _ in tasks), return_exceptions=True)
    _jobs.clear()
