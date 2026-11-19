"""Durable, serial finish queue. Originals remain in the recorder folder.

Capture never waits for this worker. A job owns its recognition, split result and
export settings; restarting the app preserves pending/review work. Interrupted
work requires review instead of silently repeating an export or paid lookup.
"""
import asyncio
import copy
import json
import logging
import threading
import time
import uuid
from contextvars import ContextVar
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from recognition import _atomic_json, RecognitionError, Cancelled
from imported_audio import choose_file_excerpt
from finish_quality import choose_scope, quality
from audio_processor import background_audio

router = APIRouter(prefix='/api/recording/finish-queue')
logger = logging.getLogger(__name__)
export_lock = asyncio.Lock()
_task = None
_wake = None
_cancel = threading.Event()
worker_id = ContextVar('finish_queue_worker_id', default=None)
ACTIVE = {'queued', 'identifying', 'detecting', 'exporting'}
manual_exports = set()


def _rec():
    from backend.recording_api import recorder
    return recorder


def _path(ident):
    ident = str(uuid.UUID(ident))
    root = _rec().root
    folder = root / ident
    if folder.is_symlink(): raise ValueError('Linked recordings are not supported')
    return folder / 'finish-job.json'


def get_job(ident):
    if _rec() is None: return None
    try:
        path = _path(ident)
        if path.is_symlink(): return None
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) and data.get('id') == ident and data.get('state') in ACTIVE | {'needs_review', 'complete'} and isinstance(data.get('created_at'), (int, float)) else None
    except (OSError, ValueError): return None


def jobs():
    if _rec() is None or not _rec().root.exists(): return []
    result = []
    for folder in _rec().root.iterdir():
        job = get_job(folder.name)
        if job: result.append(job)
    return sorted(result, key=lambda j: j['created_at'], reverse=True)


def busy(ident):
    return ident in manual_exports or (get_job(ident) or {}).get('state') in ACTIVE


def protected_ids():
    return manual_exports | {j['id'] for j in jobs() if j['state'] in ACTIVE}


def save(job, **updates):
    job.update(updates)
    _atomic_json(_path(job['id']), job)


def enqueue(session, options):
    from backend import api
    existing = get_job(session.id)
    if existing: return existing
    job = {'id': session.id, 'title': session.source_filename, 'created_at': time.time(),
           'state': 'queued', 'message': 'Queued. You can start the next record.', 'score': None,
           'recognition': copy.deepcopy(session.recognition), 'tracks': [], 'sides': [],
           'manual_release_id': options.release_id,
           'settings': {'output_format': options.output_format, 'output_dir': str(api.config.default_output_dir),
                        'flac_compression': api.config.default_flac_compression,
                        'track_numbering': api.config.track_numbering,
                        'restoration_level': options.restoration_level, 'hum_freq': 50}}
    save(job)
    start()
    _wake.set()
    return job


def start():
    global _task, _wake, _cancel
    if _rec() is None or (_task is not None and not _task.done()): return
    _cancel = threading.Event()
    _wake = asyncio.Event()
    _wake.set()
    _task = asyncio.create_task(_loop())


async def recover():
    # Never auto-overwrite a partial export after a crash/restart.
    for job in jobs():
        if job['state'] in ACTIVE - {'queued'}:
            save(job, state='needs_review', message='Processing was interrupted. Review this recording before exporting.')
    start()


async def _loop():
    while not _cancel.is_set():
        await _wake.wait()
        _wake.clear()
        for job in reversed(jobs()):
            if _cancel.is_set(): return
            if job['state'] == 'queued': await run_job(job)


async def _session(job):
    from backend import api
    from backend.recording_api import register_saved
    if api.session_store.get(job['id']) is None:
        await register_saved(_rec(), job['id'])
    session = api.session_store.get(job['id'])
    api._require_source(session)
    session.recognition = copy.deepcopy(job.get('recognition'))
    return session


async def run_job(job):
    from backend import api, recording_api
    from backend.sessions import OutputSpec, pipeline
    token = worker_id.set(job['id'])
    audio_token = background_audio.set(True)
    try:
        session = await _session(job)
        service = recording_api.recognition
        recognition = job.get('recognition') or {}
        song = recognition.get('song')
        release = recognition.get('selected_release')
        manual = bool(job.get('manual_release_id'))
        if manual:
            release = await api.get_release(job['manual_release_id'])
        elif recognition.get('selection') == 'manual' and not release:
            save(job, state='needs_review', message='Choose an album and review the splits before exporting.')
            return
        elif not release and not song and (service is None or not service.provider.configured()):
            # Nothing can identify it: say so, and still prepare silence-based splits when possible.
            try:
                tracks = (await api.analyze_file(api.AnalyzeRequest(file_id=session.id)))['tracks']
            except Exception:
                tracks = []
            save(job, tracks=tracks, state='needs_review',
                 message='No album was picked and no AudD token is set. Pick the album, check the tracks, then export.')
            return
        elif not release:
            save(job, state='identifying', message='Identifying the song and album…')
            if not song:
                clip, offset = await asyncio.to_thread(choose_file_excerpt, session.source_audio, session.source_duration)
                song = await asyncio.to_thread(service.provider.recognize, clip, _cancel)
                recognition = {**recognition, 'song': song, 'excerpt_offset': offset}
                save(job, recognition=recognition)
            if song:
                candidates, release = await asyncio.to_thread(service.resolver.resolve, song, _cancel)
                recognition = {**recognition, 'song': song, 'candidates': candidates, 'selected_release': release}
                save(job, recognition=recognition)
        if _cancel.is_set(): raise Cancelled()
        if not release:
            # Still provide a waveform and silence-based splits for later review.
            analysis = await api.analyze_file(api.AnalyzeRequest(file_id=session.id))
            save(job, tracks=analysis['tracks'], state='needs_review', message='No clear album match. Choose an album and review the splits.')
            return
        recognition = {**recognition, 'selected_release': release, 'song': song, 'source': 'queue'}
        session.recognition = recognition
        save(job, recognition=recognition, state='detecting', message='Checking the sides, track lengths and split points…')
        db = await api.get_envelope(session)
        lo, hi = api.music_span(db, api.ENVELOPE_FRAME_S)
        scope = choose_scope(release, song, len(session.recording_side_markers) + 1, hi - lo)
        if scope is None:
            analysis = await api.analyze_file(api.AnalyzeRequest(file_id=session.id))
            save(job, tracks=analysis['tracks'], state='needs_review', message='Recorded sides or track lengths are uncertain. Review before exporting.')
            return
        analysis = await api.analyze_guided(api.GuidedAnalyzeRequest(file_id=session.id,
            expected_tracks=len(scope['tracks']), durations=scope['lengths'], side_breaks=scope['breaks']))
        check = quality(song, release, scope, analysis, manual=manual)
        save(job, tracks=analysis['tracks'], sides=scope['sides'], score=check['score'], checks=check)
        if not check['eligible']:
            save(job, state='needs_review', message=' '.join(check['reasons']) or 'Match quality is below 90. Review before exporting.')
            return
        # Both manual and queued exports share this slot; capture has its own worker.
        async with export_lock:
            if _cancel.is_set(): raise Cancelled()
            spec = OutputSpec(**{**job['settings'], 'output_dir': Path(job['settings']['output_dir'])})
            full_release = await asyncio.to_thread(api.metadata_handler.get_release_by_id, release['id'], raise_errors=True)
            if full_release is None: raise RecognitionError('Discogs album could not be loaded. Retry after reviewing.')
            folder = spec.output_dir / api.metadata_handler.create_album_folder_name(full_release)
            if folder.exists():
                save(job, state='needs_review', message='This album already has an output folder. Review to avoid overwriting existing files.')
                return
            session.set_mappings({i + 1: t['position'] for i, t in enumerate(scope['tracks'])})
            save(job, state='exporting', message='Exporting in the background…')
            await asyncio.to_thread(pipeline.run, session, spec, release['id'], api.audio_processor, api.metadata_handler,
                                    lambda event: None, full_release)
            save(job, state='complete', message='Exported to your library.', output_path=str(session.last_run.output_folder))
    except (Cancelled, asyncio.CancelledError):
        save(job, state='needs_review', message='Processing was interrupted. Review before exporting.')
    except Exception as exc:
        # Provider errors and paths may contain credentials: log the type and where, never the message.
        import traceback
        where = traceback.extract_tb(exc.__traceback__)[-1] if exc.__traceback__ else None
        logger.warning('Finish job %s failed: %s at %s:%s', job['id'], type(exc).__name__,
                       where.filename.rsplit('/', 1)[-1] if where else '?', where.lineno if where else '?')
        save(job, state='needs_review', message='Automatic processing could not finish. Open this recording to identify it or review and export.')
    finally:
        worker_id.reset(token)
        background_audio.reset(audio_token)


@router.get('')
async def list_jobs(request: Request):
    from backend.recording_api import local_recorder
    local_recorder(request)
    # Do not send full tracklists/settings with every background status poll.
    return {'jobs': [{k: j.get(k) for k in ('id', 'title', 'state', 'message', 'score', 'output_path')} for j in jobs()]}


@router.post('/{ident}/open')
async def open_review(ident: str, request: Request):
    from backend import api
    from backend.recording_api import local_recorder
    local_recorder(request)
    job = get_job(ident)
    if not job: raise HTTPException(404, 'Recording job not found.')
    if job['state'] in ACTIVE: raise HTTPException(409, 'This recording is still processing in the background.')
    session = await _session(job)
    return {'file': api._serialize_uploaded(session), 'tracks': job.get('tracks', []),
            'release': (job.get('recognition') or {}).get('selected_release'), 'sides': job.get('sides', [])}


def reviewed_export(ident, output_path):
    job = get_job(ident)
    if job and job['state'] == 'needs_review':
        save(job, state='complete', message='Reviewed and exported to your library.', output_path=str(output_path))


async def shutdown():
    _cancel.set()
    if _task and not _task.done():
        _task.cancel()
        await asyncio.gather(_task, return_exceptions=True)
