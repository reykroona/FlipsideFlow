# Third-party software

FlipsideFlow is based on VinylFlow. Its original MIT copyright and license notice
is preserved in `LICENSE`, also included in the app bundle.

The desktop app bundles Python and third-party runtime libraries, including
PyInstaller, pywebview, PyObjC, PortAudio/sounddevice, NumPy, FastAPI, and their
dependencies. Their respective licenses continue to apply. Python dependencies
are listed in `requirements.txt`; bundled browser library versions and licenses
are documented in `backend/static/vendor/README.md`.

## FFmpeg

The first Apple Silicon beta bundles FFmpeg 9.0, copyright 2000–2026 the FFmpeg
developers, supplied by the `imageio-ffmpeg` Python wheel. FFmpeg is run as a
separate executable to decode and encode audio.

This binary was built with `--enable-gpl` and identifies its license as GNU GPL
version 2 or, at your option, any later version. The license text is included in
`licenses/FFmpeg-COPYING.GPLv2`. FFmpeg and its included codec libraries are not
covered by FlipsideFlow's MIT license.

- FFmpeg project and source: https://ffmpeg.org/download.html
- FFmpeg 9.0 source tree: https://github.com/FFmpeg/FFmpeg/tree/n9.0
- Binary distribution project: https://github.com/imageio/imageio-ffmpeg
- Binary provenance/build resources: https://github.com/imageio/imageio-binaries

The exact configuration and license of the bundled executable can be inspected
with `FlipsideFlow.app/Contents/Frameworks/ffmpeg_bin/ffmpeg -version` and `-L`.
FFmpeg is distributed without warranty, including any implied warranty of
merchantability or fitness for a particular purpose.
