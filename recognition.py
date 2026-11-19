"""Bounded, optional song recognition. Never called from the audio callback/writer.

The worker reads a short excerpt from the durable PCM file. Its state and metadata
are separate from the recorder manifest, so network failures cannot stop capture.
"""
from __future__ import annotations

import copy
import io
import json
import os
import re
import threading
import time
import unicodedata
import uuid
import wave
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np
import requests

from recording import FRAME_BYTES, HEADER_BYTES

AUTO_START_SECONDS = 120
RETRY_SECONDS = 60
MAX_AUTO_ATTEMPTS = 2
CLIP_SECONDS = 10


class RecognitionError(RuntimeError):
    def __init__(self, message, *, retryable=True):
        super().__init__(message)
        self.retryable = retryable


class Cancelled(Exception):
    pass


def _atomic_json(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, mode)
    os.fchmod(fd, mode)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    temp.replace(path)


class RecognitionToken:
    """Write-only through the API; saved locally with owner-only permissions."""
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def get(self):
        with self._lock:
            if self.path.exists():
                try:
                    return str(json.loads(self.path.read_text()).get("token", ""))
                except (OSError, ValueError):
                    return ""
            return os.environ.get("AUDD_API_TOKEN", "")

    def set(self, token):
        with self._lock:
            _atomic_json(self.path, {"token": token.strip()})


def choose_excerpt(snapshot):
    """Return a ten-second mono PCM WAV; never crosses a pause/flip seam.

Choose among the last 45 seconds of complete disk frames, favoring music over
silence/clipping. Header checkpoints can lag: we read PCM by offset, not the live
WAV header. Only the recognition copy becomes mono/16-bit; the master is untouched.
"""
    rate = snapshot["rate"]
    path = snapshot["path"]
    available = min(snapshot["frames"], max(0, path.stat().st_size - HEADER_BYTES) // FRAME_BYTES)
    count = CLIP_SECONDS * rate
    if available < count:
        raise RecognitionError("Record at least 10 seconds of music before identifying.")
    best = None
    seams = snapshot.get("pause_markers", [])
    with path.open("rb") as f:
        for end in range(available, max(count - 1, available - 45 * rate), -5 * rate):
            start = end - count
            if any(start < seam < end for seam in seams):
                continue
            f.seek(HEADER_BYTES + start * FRAME_BYTES)
            data = f.read(count * FRAME_BYTES)
            if len(data) != count * FRAME_BYTES:
                continue
            p = np.frombuffer(data, dtype=np.uint8).reshape(-1, 2, 3).astype(np.int32)
            values = p[:, :, 0] | p[:, :, 1] << 8 | p[:, :, 2] << 16
            values = (values ^ 0x800000) - 0x800000
            rms = np.sqrt(np.mean((values.astype(np.float64) / 8388608.0) ** 2, axis=0))
            channel = int(np.argmax(rms))  # avoids cancelling out-of-phase stereo
            mono = values[:, channel]
            clipping = np.mean(np.abs(mono) >= 8380000)
            if rms[channel] < 10 ** (-50 / 20) or clipping > 0.005:
                continue
            active = np.mean(np.sqrt(np.mean((mono.reshape(CLIP_SECONDS, rate).astype(np.float64)
                                             / 8388608.0) ** 2, axis=1)) > 10 ** (-50 / 20))
            if active < 0.8:
                continue
            score = active + min(float(rms[channel]), 0.25) - clipping * 10
            if best is None or score > best[0]:
                best = (score, (mono >> 8).astype("<i2").tobytes(), start)
    if best is None:
        raise RecognitionError("No clear music excerpt yet. Check the input levels; recognition will retry.")
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(best[1])
    return out.getvalue(), best[2] / rate


class AudDProvider:
    name = "AudD"

    def __init__(self, token_getter, post=requests.post):
        self.token_getter = token_getter
        self.post = post

    def configured(self):
        return bool(self.token_getter())

    def recognize(self, clip, cancel):
        token = self.token_getter()
        if not token:
            raise RecognitionError("Add an AudD API token to enable song recognition.", retryable=False)
        if cancel.is_set():
            raise Cancelled()
        try:
            response = self.post("https://api.audd.io/", data={"api_token": token},
                                 files={"file": ("excerpt.wav", clip, "audio/wav")},
                                 timeout=(5, 20), allow_redirects=False)
            if response.status_code in (401, 403):
                raise RecognitionError("AudD rejected the token. Check your API token and account.", retryable=False)
            if response.status_code == 429:
                raise RecognitionError("AudD request limit reached. Check your account before retrying.", retryable=False)
            if response.status_code != 200:
                raise RecognitionError("AudD is temporarily unavailable. Try again later.")
            payload = response.json()
        except (requests.RequestException, ValueError):
            # Never reflect request bodies, URLs containing credentials, or provider error text.
            raise RecognitionError("Could not reach AudD. Check your connection and try again.") from None
        if cancel.is_set():
            raise Cancelled()
        if not isinstance(payload, dict):
            raise RecognitionError("AudD returned an invalid response. Try again later.")
        if payload.get("status") != "success":
            error = payload.get("error")
            code = error.get("error_code") if isinstance(error, dict) else None
            if code in (900, 901, 902):
                raise RecognitionError("AudD account/token or quota needs attention. Check the AudD dashboard.", retryable=False)
            raise RecognitionError("AudD could not complete recognition. Check your account or try again.", retryable=False)
        result = payload.get("result")
        if not result:
            return None
        if not isinstance(result, dict) or not result.get("title") or not result.get("artist"):
            raise RecognitionError("AudD returned an incomplete song match.")
        return {key: str(result.get(key) or "")[:500] for key in ("title", "artist", "album")}


def normalized(value):
    value = re.sub(r"\s*\(\d+\)$", "", str(value or ""))
    value = unicodedata.normalize("NFKD", value).casefold()
    return " ".join(re.findall(r"[^\W_]+", "".join(c for c in value if not unicodedata.combining(c))))


def rank_releases(song, candidates):
    """Only verified tracklists qualify; one representative per Discogs master."""
    title, artist, album = (normalized(song.get(k)) for k in ("title", "artist", "album"))
    groups = {}
    for candidate in candidates:
        tracks = candidate.get("tracks") or []
        titles = [normalized(t.get("title")) for t in tracks]
        match = max((SequenceMatcher(None, title, t).ratio() for t in titles), default=0)
        if match < 0.92 or normalized(candidate.get("format")) != "vinyl":
            continue
        same_artist = artist == normalized(candidate.get("artist"))
        same_album = bool(album) and album == normalized(candidate.get("title"))
        # A common song title alone is not evidence that this is the same recording.
        artist_match = SequenceMatcher(None, artist, normalized(candidate.get("artist"))).ratio()
        if artist_match < 0.85 and not (same_album and normalized(candidate.get("artist")) == "various"):
            continue
        score = match * 4 + same_artist * 2 + same_album * 4
        if len(tracks) >= 4:
            score += 0.25
        # Complete lengths make a representative pressing useful for guided splitting.
        completeness = sum(bool(t.get("duration")) for t in tracks) / max(1, len(tracks))
        item = {**candidate, "score": round(score, 3), "reason": "Song and album match" if same_album else "Song appears in this tracklist"}
        group = candidate.get("master_id") or (normalized(candidate.get("artist")), normalized(candidate.get("title")))
        previous = groups.get(group)
        if previous is None or (score, completeness) > previous[:2]:
            groups[group] = (score, completeness, item)
    ranked = [v[2] for v in sorted(groups.values(), key=lambda v: (v[0], v[1]), reverse=True)]
    # If different albums tie, show choices instead of silently selecting the first hit.
    selected = ranked[0] if ranked and (len(ranked) == 1 or ranked[0]["score"] - ranked[1]["score"] >= 1) else None
    return ranked[:5], selected


class DiscogsResolver:
    def __init__(self, handler_getter, serialize):
        self.handler_getter = handler_getter
        self.serialize = serialize

    def resolve(self, song, cancel):
        from metadata_handler import MetadataHandler
        parent = self.handler_getter()
        if not parent.discogs_token:
            raise RecognitionError("Song identified. Add your Discogs token in Settings, then retry the album lookup.", retryable=False)
        deadline = time.monotonic() + 35
        class Fetcher:
            def fetch(self, client, method, url, data=None, headers=None, json=True):
                if cancel.is_set():
                    raise Cancelled()
                if time.monotonic() >= deadline:
                    raise RecognitionError("Discogs lookup timed out. Retry the album lookup.")
                parent._rate_limit()
                if cancel.is_set():
                    raise Cancelled()
                if time.monotonic() >= deadline:
                    raise RecognitionError("Discogs lookup timed out. Retry the album lookup.")
                try:
                    response = requests.request(method, url, data=data,
                        headers={**(headers or {}), "Authorization": "Discogs token=" + parent.discogs_token},
                        timeout=(5, min(10, max(1, deadline - time.monotonic()))), allow_redirects=False)
                    if response.status_code != 200:
                        raise RecognitionError("Discogs lookup failed. Check your Discogs token or try again later.")
                    return response.content, response.status_code
                except requests.RequestException:
                    raise RecognitionError("Could not reach Discogs. Retry the album lookup.") from None
        handler = MetadataHandler(parent.discogs_token, parent.discogs_user_agent)
        handler.client._fetcher = Fetcher()
        handler._rate_limit = lambda: None  # shared limiter is applied by the bounded fetcher
        handler._release_cache = parent._release_cache
        handler._release_cache_lock = parent._release_cache_lock
        filters = {"artist": song["artist"], "format": "Vinyl"}
        if song.get("album"):
            filters["release_title"] = song["album"]
        else:
            filters["track"] = song["title"]
        candidates = []
        seen = set()
        searches = [filters]
        if song.get("album"):
            searches.append({"artist": song["artist"], "track": song["title"], "format": "Vinyl"})
        for query in searches:
            for card in handler.search_basic("", 8, filters=query):
                if cancel.is_set():
                    raise Cancelled()
                if time.monotonic() >= deadline or len(seen) >= 12:
                    break
                if card["id"] in seen:
                    continue
                seen.add(card["id"])
                release = handler.get_release_by_id(card["id"], raise_errors=True)
                if release is not None:
                    candidates.append({**card, **self.serialize(release)})
            if rank_releases(song, candidates)[0] or time.monotonic() >= deadline or len(seen) >= 12:
                break
        return rank_releases(song, candidates)


class RecognitionService:
    """One bounded worker, generation-scoped results, persisted metadata per capture."""
    def __init__(self, recorder, provider, resolver, *, autostart=True):
        self.recorder, self.provider, self.resolver = recorder, provider, resolver
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._state = self._initial(None, False)
        self._manual = False
        self._last_manual = 0
        self._thread = None
        if autostart:
            self._thread = threading.Thread(target=self._loop, name="vinylflow-recognition", daemon=True)
            self._thread.start()

    @staticmethod
    def _initial(ident, enabled):
        return dict(recording_id=ident, enabled=enabled, provider="AudD", state="waiting" if enabled else "disabled",
                    attempts=0, next_at=AUTO_START_SECONDS, song=None, candidates=[], selected_release=None,
                    selection="auto", message="Identifies after 2 minutes of recorded audio." if enabled else "Automatic identification is off.")

    def begin(self, ident, enabled=True):
        with self._lock:
            self._cancel.set()
            self._cancel = threading.Event()
            self._state = self._initial(ident, enabled)
            self._manual = False
            self._last_manual = 0
            self._persist()
        self._wake.set()

    def status(self):
        with self._lock:
            return {**copy.deepcopy(self._state), "configured": self.provider.configured()}

    def _persist(self):
        ident = self._state["recording_id"]
        if ident:
            try:
                _atomic_json(self.recorder.root / ident / "recognition.json", self._state)
            except OSError:
                self._state["persistence_warning"] = "Recognition details could not be saved; the audio recording is unaffected."

    def saved(self, ident):
        ident = str(uuid.UUID(ident))
        with self._lock:
            if ident == self._state["recording_id"]:
                return copy.deepcopy(self._state)
        try:
            data = json.loads((self.recorder.root / ident / "recognition.json").read_text())
            return data if data.get("recording_id") == ident else None
        except (OSError, ValueError):
            return None

    def request(self):
        snapshot = self.recorder.capture_snapshot()
        with self._lock:
            if not snapshot or snapshot["id"] != self._state["recording_id"]:
                raise RecognitionError("Start a recording before identifying a song.")
            if self._state["state"] in ("identifying", "looking_up"):
                return self.status()
            if time.monotonic() - self._last_manual < 10:
                raise RecognitionError("Please wait a few seconds before retrying.")
            self._last_manual = time.monotonic()
            self._manual = True
        self._wake.set()
        return self.status()

    def choose(self, ident, release_id):
        with self._lock:
            if ident != self._state["recording_id"]:
                raise RecognitionError("This recording is no longer active.")
            chosen = next((c for c in self._state["candidates"] if c["id"] == release_id), None)
            if release_id is not None and chosen is None:
                raise RecognitionError("Choose one of the suggested releases.")
            self._state.update(selection="manual", selected_release=chosen,
                               message="Album selected. Check the pressing before exporting." if chosen else
                               "Automatic album selection dismissed. Use the manual Discogs search after saving.")
            self._persist()
            return self.status()

    def _publish(self, cancel, **updates):
        with self._lock:
            if cancel.is_set() or cancel is not self._cancel:
                return
            if self._state["selection"] == "manual":
                updates.pop("selected_release", None)
            if all(self._state.get(key) == value for key, value in updates.items()):
                return
            self._state.update(updates)
            self._persist()

    def tick(self):
        snapshot = self.recorder.capture_snapshot()
        with self._lock:
            state = self._state
            if not snapshot or snapshot["id"] != state["recording_id"]:
                return
            automatic = (state["enabled"] and snapshot["state"] == "recording" and not state["song"]
                         and state["attempts"] < MAX_AUTO_ATTEMPTS and state["next_at"] is not None
                         and snapshot["frames"] / snapshot["rate"] >= state["next_at"])
            if not self._manual and not automatic:
                return
            self._manual = False
            cancel = self._cancel
            song = copy.deepcopy(state["song"])
            if not song and not self.provider.configured():
                self._publish(cancel, state="needs_setup", message="Add an AudD API token to enable recognition.")
                return
            attempts = state["attempts"] + (0 if song else 1)
            next_at = max(180, snapshot["frames"] / snapshot["rate"] + RETRY_SECONDS) if attempts < MAX_AUTO_ATTEMPTS else None
            self._publish(cancel, state="looking_up" if song else "identifying", attempts=attempts, next_at=next_at,
                          message="Looking for the album on Discogs…" if song else "Identifying a short excerpt with AudD…")
        try:
            if not song:
                clip, offset = choose_excerpt(snapshot)
                song = self.provider.recognize(clip, cancel)
                if cancel.is_set():
                    raise Cancelled()
                if song is None:
                    self._publish(cancel, state="no_match", message="No song match. Will retry after another minute of recording." if next_at else "No song match. You can identify again or search Discogs manually.")
                    return
                self._publish(cancel, song=song, excerpt_offset=offset, state="looking_up", message="Song identified. Looking for the album on Discogs…")
            candidates, selected = self.resolver.resolve(song, cancel)
            self._publish(cancel, state="matched" if candidates else "no_album", candidates=candidates, selected_release=selected,
                          message="Likely album loaded. The exact pressing is unconfirmed." if selected else
                          "Several albums contain this song. Choose the one you are recording." if candidates else
                          "Song identified, but no matching vinyl tracklist was found. Try a manual Discogs search.")
        except Cancelled:
            pass
        except RecognitionError as exc:
            self._publish(cancel, state="error", message=str(exc), next_at=next_at if exc.retryable else None)
        except Exception:
            self._publish(cancel, state="error", message="Recognition could not finish. Recording is continuing; you can retry.")

    def _loop(self):
        while not self._stop.is_set():
            self._wake.wait(0.5)
            self._wake.clear()
            try:
                self.tick()
            except Exception:
                # Recorder availability and metadata errors must not kill the scheduler.
                pass

    def shutdown(self):
        self._cancel.set()
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=0.5)  # network work is bounded and daemonized; never delay saving audio
