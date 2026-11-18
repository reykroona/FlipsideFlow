"""Desktop-only recording routes. Disabled in Docker and remote server mode."""
from __future__ import annotations

import asyncio
import ipaddress
import os
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from recording import Recorder, RecordingError, request_microphone_permission
from recognition import AudDProvider, DiscogsResolver, RecognitionError, RecognitionService, RecognitionToken

router = APIRouter(prefix="/api/recording")
recorder: Recorder | None = None
recognition: RecognitionService | None = None
recognition_token: RecognitionToken | None = None
_registration_lock = asyncio.Lock()


def enable_recording(root: Path, uploads: Path):
    global recorder, recognition, recognition_token
    if sys.platform != "darwin":
        return
    root, uploads = root.resolve(), uploads.resolve()
    if root == uploads or root in uploads.parents or uploads in root.parents:
        raise ValueError("Recordings must be stored outside the temporary uploads folder")
    recorder = Recorder(root, permission=request_microphone_permission, protect_sleep=True)
    config_dir = Path(os.environ.get("VINYLFLOW_CONFIG_DIR", str(Path(__file__).parent.parent / "config")))
    recognition_token = RecognitionToken(config_dir / "recognition-token.json")
    # Late binding: the desktop launcher configures this before importing backend.api.
    def handler():
        from backend import api
        return api.metadata_handler
    def serialize(release):
        from backend.api import _serialize_release
        return _serialize_release(release)
    recognition = RecognitionService(recorder, AudDProvider(recognition_token.get), DiscogsResolver(handler, serialize))


def local_recorder(request: Request) -> Recorder:
    # The existing app can opt into LAN access, but recording control cannot.
    try:
        local = ipaddress.ip_address(request.client.host).is_loopback
    except (ValueError, AttributeError):
        local = False
    if not local:
        raise HTTPException(403, "Recording is available only on the local Mac")
    if recorder is None:
        raise HTTPException(404, "Recording is available in the macOS desktop app")
    return recorder


async def call(method, *args, **kwargs):
    try:
        return await asyncio.to_thread(method, *args, **kwargs)
    except (RecordingError, RecognitionError) as exc:
        raise HTTPException(409, str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(503, f"Audio input error: {exc}") from exc


class InputSettings(BaseModel):
    device: int = Field(ge=0)
    device_name: str = Field(min_length=1, max_length=256)
    left: int = Field(ge=1, le=128)
    right: int = Field(ge=1, le=128)
    rate: int


class StartRecording(BaseModel):
    title: str = Field(default="", max_length=120)
    identify_album: bool = True
    auto_flip: bool = False               # pause at each run-out groove, resume at the needle drop
    side_lengths: list[float] = Field(default_factory=list, max_length=26)  # expected seconds per side


class PauseRecording(BaseModel):
    side_flip: bool = False


@router.get("/status")
async def status(request: Request):
    if recorder is None:
        return {"enabled": False}
    rec = local_recorder(request)
    return {"enabled": True, **await call(rec.status),
            "recognition": recognition.status() if recognition is not None else None}


@router.get("/devices")
async def devices(request: Request):
    rec = local_recorder(request)
    return {"devices": await call(rec.devices)}


@router.post("/monitor")
async def monitor(settings: InputSettings, request: Request):
    rec = local_recorder(request)
    return await call(rec.monitor, **settings.model_dump())


@router.post("/start")
async def start(body: StartRecording, request: Request):
    result = await call(local_recorder(request).start, body.title, body.auto_flip, body.side_lengths)
    if recognition is not None:
        await call(recognition.begin, result["id"], body.identify_album)
        result["recognition"] = recognition.status()
    return result


@router.post("/pause")
async def pause(body: PauseRecording, request: Request):
    return await call(local_recorder(request).pause, body.side_flip)


@router.post("/resume")
async def resume(request: Request):
    return await call(local_recorder(request).resume)


async def register_saved(rec, ident):
    from backend import api
    async with _registration_lock:
        source, manifest = await call(rec.open_saved, ident)
        session = api.session_store.get(ident)
        if session is None:
            await api._register_source(ident, source, manifest["title"] + ".wav", source.stat().st_size, in_place=True)
            session = api.session_store.get(ident)
        frames = manifest.get("frames") or 0
        # Recordings made before trailing flips were dropped at stop: ignore a flip at the very end.
        session.recording_side_markers = [frame / manifest["rate"] for frame in manifest["side_markers"]
                                          if not frames or frame < frames]
        if recognition is not None:
            session.recognition = recognition.saved(ident)
        return api._serialize_uploaded(session)


@router.post("/stop")
async def stop(request: Request):
    rec = local_recorder(request)
    result = await call(rec.stop)
    if result["id"] and result["seconds"] > 0:
        # A failed registration must leave a stopped recorder and a durable original.
        try:
            result["file"] = await register_saved(rec, result["id"])
        except HTTPException as exc:
            result["error"] = f"Audio saved. Open it from Saved recordings to retry: {exc.detail}"
    result["recognition"] = recognition.status() if recognition is not None else None
    return result


def require_recognition(request):
    local_recorder(request)
    if recognition is None:
        raise HTTPException(404, "Recognition is unavailable in this build")
    return recognition


@router.post("/recognition/token")
async def save_recognition_token(request: Request):
    require_recognition(request)
    if recognition_token is None:
        raise HTTPException(404, "Recognition settings are unavailable")
    # FastAPI's default validation errors echo invalid inputs, including secrets.
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(422, "Provide an AudD token as a string of at most 512 characters") from None
    token = body.get("token") if isinstance(body, dict) else None
    if not isinstance(token, str) or len(token) > 512:
        raise HTTPException(422, "Provide an AudD token as a string of at most 512 characters")
    await call(recognition_token.set, token)
    return {"configured": bool(recognition_token.get())}


@router.post("/recognition/identify")
async def identify(request: Request):
    return await call(require_recognition(request).request)


class RecognitionSelection(BaseModel):
    recording_id: str
    release_id: int | None = Field(default=None, ge=1)


@router.post("/recognition/select")
async def select_recognized_album(body: RecognitionSelection, request: Request):
    return await call(require_recognition(request).choose, body.recording_id, body.release_id)


@router.get("/saved")
async def saved(request: Request):
    return {"recordings": await call(local_recorder(request).saved)}


@router.post("/saved/{ident}/open")
async def open_saved(ident: str, request: Request):
    return {"file": await register_saved(local_recorder(request), ident)}


def shutdown_recording():
    if recognition is not None:
        recognition.shutdown()
    if recorder is not None:
        recorder.shutdown()
