"""Bounded local noise-floor calibration; keeps levels, never audio."""
import threading
import time
import numpy as np


class SilenceLearning:
    SECONDS = 10
    FRAME_S = .01

    def __init__(self):
        self.lock = threading.Lock()
        self.cancel()

    def cancel(self):
        with self.lock:
            self.state = 'idle'
            self.levels = []
            self.pending = np.empty((0, 2))
            self.result = None
            self.started = 0

    def start(self):
        self.cancel()
        with self.lock:
            self.state = 'listening'
            self.started = time.monotonic()

    def feed(self, values, rate):
        with self.lock:
            if self.state != 'listening': return
            width = round(rate * self.FRAME_S)
            samples = np.concatenate((self.pending, values / 8388608.0))
            count = min(len(samples) // width, round(self.SECONDS / self.FRAME_S) - len(self.levels))
            if count:
                frames = samples[:count * width].reshape(count, width, 2)
                rms = np.sqrt(np.mean(frames ** 2, axis=1)).max(axis=1)
                self.levels.extend((20 * np.log10(np.maximum(rms, 1e-9))).tolist())
            self.pending = samples[count * width:] if len(self.levels) < 1000 else np.empty((0, 2))
            if len(self.levels) >= 1000:
                self.result = suggest(self.levels)
                self.state = 'ready' if self.result['usable'] else 'rejected'

    def applied(self):
        with self.lock:
            self.state = 'applied'
            self.result['message'] = 'Threshold applied and saved. Relearn if you change input gain or turntables.'
        return self.status()

    def status(self):
        with self.lock:
            if self.state == 'listening' and time.monotonic() - self.started > 20:
                self.state = 'rejected'
                self.result = {'usable': False, 'message': 'Input stopped. Check your input and try again.'}
            return {'state': self.state, 'seconds': round(len(self.levels) * self.FRAME_S, 1), **(self.result or {})}


def suggest(levels):
    db = np.asarray(levels)
    floor = float(np.percentile(db, 80))
    threshold = round(floor + 6, 1)
    if floor < -85:
        return {'usable': False, 'message': 'Almost no input signal. Lower the needle into a spinning silent groove and check the input.'}
    if threshold > -30 or np.percentile(db, 95) - np.percentile(db, 20) > 18:
        return {'usable': False, 'message': 'The sample is too loud or uneven. Try a quieter groove with no music or repeating loud clicks.'}
    return {'usable': True, 'noise_floor': round(floor, 1), 'threshold': max(-75, threshold),
            'message': 'Measured locally. Apply this threshold, then check a few track gaps. Relearn after changing input gain.'}
