# 🎵 FlipsideFlow

**Record a side, flip it, export. Open source.**

> FlipsideFlow is based on [VinylFlow](https://github.com/olimic1000/vinylflow) by
> olimic1000 and contributors (MIT license). It adds a guided record → flip → export flow
> with automatic side flips, a library with a built-in player, export without a Discogs
> match, and a dark interface. Downloads below are for this FlipsideFlow fork.

Turn your vinyl recordings into perfectly tagged, organized digital files in minutes — not hours. FlipsideFlow automates track splitting, Discogs metadata tagging, cover art embedding, and vinyl-style numbering (A1, A2, B1, B2).

![FlipsideFlow Demo](docs/demo.gif)

---

## Use FlipsideFlow

- 🖥️ **Desktop app (Apple Silicon Mac):** [Download the app ZIP](https://github.com/reykroona/FlipsideFlow/releases/download/v0.1.0-beta.3/FlipsideFlow-0.1.0-beta.3-macos-arm64.zip), or browse [releases](https://github.com/reykroona/FlipsideFlow/releases). No Docker, Python, or FFmpeg installation required. See [Desktop Apps (Beta)](#desktop-apps-beta) for installation.
- 🐳 **Docker (self-hosted):** run locally via [Quick Start (Docker)](#quick-start-docker)
- ⚙️ **Python local mode:** see [Manual Setup (Non-Docker)](#manual-setup-non-docker)

---

## The Problem

Digitizing a vinyl record manually takes **20–30 minutes per album**: record in Audacity, manually find track boundaries, split, export, look up metadata, type it all in, find cover art, embed it. Multiply that by a collection of hundreds of records and it's a weekend project that never ends.

## The Solution

FlipsideFlow does it in **3 minutes**. Upload your recording, pick the album from the Discogs results it finds for you, check the detected tracks, and hit process. Done.

---

## Features

- **Automatic silence detection** — intelligently finds track boundaries in your recording
- **Guided detection** — the Discogs track lengths are aligned to the gaps between songs, so one wrong length no longer shifts later cuts; a side picker (full album, per disc, per side) sets how many tracks to place, and **Snap to Discogs** places them again on demand
- **Discogs first** — selecting a recording searches Discogs from its file name right away; result cards show artwork, track count, whether Discogs lists track lengths, and how well the lengths fit your recording
- **Multiple output formats** — FLAC (lossless), MP3 (320kbps), or AIFF (lossless)
- **Multiple input formats** — WAV and AIFF recordings supported
- **Import from a folder** — large recordings are read in place from a read-only folder instead of being uploaded through the browser; see [Import from a folder](#import-from-a-folder-no-upload)
- **Vinyl-style numbering** — proper A1, A2, B1, B2 track notation
- **Rich tags** — multi-artist credits keep Discogs' join strings ("Simon & Garfunkel") without disambiguation suffixes ("Nirvana (2)" → "Nirvana", also in folder names), album artist is always set, compilations carry per-track artists and the compilation flag, and unknown values are left out instead of written empty
- **Classical credits** — for releases with the Discogs genre "Classical", composers (from composer credits, else a lone release artist with no performing credit, when the release credits no composer), conductor, ensembles and soloists (allowlisted performing roles only, honouring credit track ranges such as "A1 to A3", also on a suite's movements) are tagged, and ARTIST lists soloists, then ensembles, then the conductor when the credits cover every performer in the artist line (otherwise it stays as Discogs has it); production credits are never written
- **Cover art** — downloads and embeds album artwork automatically
- **Interactive waveform editor** — drag regions to fine-tune track boundaries
- **Batch queue** — process multiple records with real-time progress
- **Remote access** — control from any device on your network (phone, tablet, laptop)

---

## Quick Start (Docker)

Docker is an optional way to self-host the web app. For recording directly on your Mac, use the [desktop app](#desktop-apps-beta). If you prefer local Python, see [Manual Setup (Non-Docker)](#manual-setup-non-docker).

### Prerequisites

You'll need:
- **Docker Desktop** (free) — [Download here](https://www.docker.com/products/docker-desktop/)

That's it! Git is optional (see Step 1 below). No need to mess with configuration files — FlipsideFlow will guide you through setup in your browser.

## 1. Get FlipsideFlow

### Option A: Download ZIP (easiest — no Git required)

1. Go to [github.com/reykroona/FlipsideFlow](https://github.com/reykroona/FlipsideFlow)
2. Click the green **Code** button, then **Download ZIP**
3. Unzip the folder to a location you can find (like your Downloads or Desktop)
4. Open the unzipped `FlipsideFlow-main` folder

### Option B: Clone with Git (for terminal users)

```bash
git clone https://github.com/reykroona/FlipsideFlow.git
cd FlipsideFlow
```

## 2. Start FlipsideFlow

1. **Open your terminal:**
   - **Mac:** Open "Terminal" app (or iTerm)
   - **Windows:** Open "Command Prompt" or "PowerShell"
   - **Linux:** Open your terminal application

2. **Navigate to the FlipsideFlow folder:**
   - If you downloaded the ZIP, type `cd ` (with a space after) and drag the `vinylflow` folder into the terminal window, then press Enter
   - Or type the full path, like: `cd ~/Downloads/vinylflow` or `cd C:\Users\YourName\Downloads\vinylflow`
   - **Windows shortcut:** In File Explorer, open the vinylflow folder, click the address bar, type `cmd` and press Enter

3. **Start FlipsideFlow:**
   ```bash
   docker compose up -d
   ```

4. **Open your browser** and go to **http://localhost:8000**

### Accessing from other devices (LAN)

By default the port is published on `127.0.0.1` only, so FlipsideFlow is reachable just from the computer running it. To open it from a phone or another computer, add `BIND_ADDRESS=0.0.0.0` to your `.env` and run `docker compose up -d` again, then browse to `http://<your-computer's-IP>:8000`. To reach it by a hostname instead of an IP, also add `VINYLFLOW_ALLOWED_HOSTS=<name>` (e.g. `nas.local`) to your `.env`.

**Risk:** FlipsideFlow has no authentication, and the API can change the output folder and write files there. Anyone who can reach the port can use it, so only enable this on a trusted network.

### Import from a folder (no upload)

A full album recorded at 24-bit/96 kHz is about 1.5 GB, and uploading it through the browser copies it into the container. Instead, put your recordings in the `incoming/` folder next to `docker-compose.yml` (created on first start) and use the **Import from folder** tab: FlipsideFlow reads them where they are.

- To use a folder you already have, set `IMPORT_FOLDER=/path/to/your/recordings` in `.env` and run `docker compose up -d` again.
- The folder is mounted **read-only**: FlipsideFlow never modifies, moves or deletes your recordings. Removing a file from the queue (or the automatic temp clean-up) only deletes FlipsideFlow's own preview and analysis files.
- Subfolders are listed (up to 3 levels). A file that was changed in the last few seconds shows as "Still copying…" until the copy finishes. A file already in the queue can be imported again; it reuses the existing entry. The list marks files "In queue" or "Complete".
- Desktop app: the import folder is `~/Music/FlipsideFlow/Incoming`, and **Choose files…** opens any WAV/AIFF on your computer in place.
- Running without Docker or the desktop app: set `VINYLFLOW_IMPORT_DIR` to the folder (for example in `.env`). The import folder can't be changed from the web UI.

## 3. First-Run Setup

When you open FlipsideFlow for the first time, you'll see a welcome screen that guides you through setup in seconds:

1. **Discogs token** — Generate a free token at [discogs.com/settings/developers](https://www.discogs.com/settings/developers) for album artwork and tracklists.
2. **AudD token (macOS desktop)** — Get a token from the [AudD dashboard](https://dashboard.audd.io/) for automatic song recognition while recording. Trial and usage limits apply.
3. **Save and continue** — Enter either or both tokens. Discogs is validated immediately; the saved AudD token is checked on the next identification request.
4. **Or skip for now** — Both services are optional. Add or change tokens later in Settings.

Tokens are stored locally and persist across app restarts. If one service fails during setup, the other successful save is retained. The welcome screen appears when a service is missing, but does not interrupt a running recording. Docker/server mode asks only for Discogs.

## Desktop Apps (Beta)

The downloadable beta is for **Apple Silicon Macs (M1 or newer) running macOS 14 Sonoma or later**. Windows and Intel Mac packaging recipes are included in the source, but this release supplies only an ARM64 Mac app.

1. Download `FlipsideFlow-0.1.0-beta.3-macos-arm64.zip` from [Releases](https://github.com/reykroona/FlipsideFlow/releases).
2. Unzip it and drag **FlipsideFlow.app** to **Applications**. Quit an older running copy before opening the new one.
3. The beta is ad-hoc signed and **not notarized by Apple**. If macOS blocks it, try opening it once, then use **System Settings → Privacy & Security → Open Anyway** and confirm.
4. Allow microphone access when prompted, connect your Scarlett, and select its CoreAudio input. AudD and Discogs tokens are needed only for their online features.

Python, FFmpeg, and the audio libraries are bundled. **Docker is not required.** Real Scarlett hardware and live AudD recognition still need field testing; review side transitions and track boundaries before export.

For local development, run `python desktop_launcher.py`. Packaging scripts are in `scripts/`.

### Record directly on macOS (Apple Silicon)

The macOS desktop app can capture a stereo USB input, including a CoreAudio Scarlett,
directly to a **24-bit PCM WAV**. No Audacity export or browser upload is needed.

The app opens on **Record**, a four-step flow:

1. **Add.** Choose **Record and identify automatically** to identify the song from the
   WAV while it is being recorded. Add your AudD and Discogs tokens in **Settings**
   first. Or search for the album manually before starting. You can also switch off
   identification and name the album afterwards.
2. **Record.** The first time, pick the interface, its left/right inputs (usually 1 and 2)
   and 44.1, 48 or 96 kHz; the choice is remembered. The LED meters run straight away:
   set the interface gain so loud passages reach the amber, never the red. Press
   **Start recording** (or Space). A ruler fills in each track of the side as it plays.
3. **Flip.** With **Pause by itself at the end of each side** on (the default), FlipsideFlow
   pauses about 10 seconds into the run-out groove (never before ¾ of the side's
   Discogs length, or 4 minutes when the length is unknown), says which side to put on,
   and starts again by itself when the needle drops. The 5 seconds before the drop are
   kept, so quiet intros are not cut. **Flip side** / **Resume now** do the same by hand.
   Press **Finish record** after the last side.
4. **Export.** Each recorded flip is a hard side break, and each side is split on its own
   from the Discogs lengths. Tracks whose length matches Discogs get a ✓; any that don't
   are marked **listen**. Click ▶ to hear a whole track in the player at the bottom.
   **Fix splits…** opens the full waveform editor (the **Workbench**). Pick FLAC, MP3 or
   AIFF and press **Export**. Without Discogs, **Name it myself** lets you type the artist,
   album, year and track names; tracks are numbered by side from the recorded flips.

The **Library** shows every album in the library folder (Settings → Library folder), read
from the files' tags, including ones exported earlier or by other tools. Click a cover to
see its tracks and click a track to play it; the player at the bottom has one volume for
the whole app. **Show in Finder** opens the folder, and **Re-split** reopens the original
recording in the Workbench. Recordings that were never exported are listed at the top
with **Continue**.

The **Workbench** is the full editor: import or upload files, the queue, Discogs search,
side picker, waveform, Smart Rescan and track mapping.

Original WAVs are retained under `~/Music/FlipsideFlow/Recordings/<recording-id>/audio.wav`
(`~/Music/VinylFlow/...` on a Mac that ran it before the rename: existing folders keep being used),
alongside a manifest with the recording name, format, and flip positions. Removing
a recording from the editing queue or clearing temporary files **does not delete
the original**. Use **Saved recordings → Open** to return to it after restarting.
Set `VINYLFLOW_RECORDING_DIR` before launching to choose a different location;
it must not overlap the temporary uploads folder. Recordings larger than 4 GiB
automatically use RF64 in the same `.wav` file.

Capture runs in the desktop backend and continues across a UI reload. On input
overflow, disconnect, or low disk space, capture stops with an error and retains
the audio already written. **Saved recordings → Recover & open** repairs an
interrupted WAV header using complete frames on disk. Check interrupted audio
before exporting; abrupt power loss can still lose the last unflushed samples.
The app prevents idle system sleep during a recording, including pauses. Closing
the desktop window while recording asks whether to stop and save first.

If permission was denied, enable **FlipsideFlow** under **System Settings → Privacy
& Security → Microphone**, then reopen it. In Python development mode the permission
may be attributed to the terminal/Python launcher. If a sample rate cannot be
opened, check the interface's format in **Audio MIDI Setup**. Input rates requiring
CoreAudio conversion are rejected instead of silently resampled.

Recording is enabled only by the macOS desktop launcher and controlled from the
local Mac. Docker/server mode continues to support importing recordings (Workbench) and
the Library.

#### Identify the song and load an album

In **Settings → AudD song recognition**, save an API token from the
[AudD dashboard](https://dashboard.audd.io/). AudD offers a trial and paid plans;
check its [current pricing](https://audd.io/pricing/) before enabling it. No Apple
Developer membership is needed. Set your Discogs token in **Settings** as usual.
Keep **Identify the album while recording** checked on the main Record screen.
The **Set up AudD / AudD settings** button opens the same Settings section.

After two minutes of recorded audio (pauses do not count), FlipsideFlow sends a
10-second excerpt to AudD. It skips silence, heavily clipped audio, and pause/flip
seams. If no song is found or a temporary error occurs, it makes at most one
automatic retry a minute later. **Identify now** allows a manual attempt after
10 seconds. Account/token/quota errors require attention before retrying.
The original stereo 24-bit WAV is never altered or uploaded; only the recognition
copy is converted to mono 16-bit. Network work runs separately from capture.

The recognized artist, song, and album are used to search Discogs for vinyl
releases and verify the song against their tracklists. A clear result appears as
the likely album on the main Record screen while capture continues. The cover,
album name, sides, and tracklist appear immediately; no need to finish or import
the WAV first. Ambiguous results offer a choice, and **Identify now** can retry.
After **Finish record**, the chosen album is carried into the editor and starts
guided track detection on the completed WAV. A late match will not replace a manually selected release
or edited tracks. Check the album and pressing before exporting: recognition
cannot determine your exact vinyl pressing. Manual Discogs search remains available.

Results are saved beside the recording in `recognition.json` and restored through
**Saved recordings → Open**. If Discogs is unavailable or its token is missing,
the identified song is retained; **Retry album lookup** reuses it without another
AudD request. The AudD token is stored in `recognition-token.json` in the app's
configuration directory with owner-only file permissions and is never returned
by the API. `AUDD_API_TOKEN` can supply it in development; saving/removing a token
in the app overrides that environment variable. Removing the token disables
future AudD requests but cannot cancel a request already sent.

#### Build an Apple Silicon app locally

Use native ARM64 Python 3.11+ and FFmpeg on your PATH:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt pyinstaller
./scripts/build-app.sh
```

The app is written to `dist/FlipsideFlow.app`, with PortAudio and the Microphone usage
description bundled. `VINYLFLOW_FFMPEG_PATH` can select a standalone FFmpeg binary.
For a distribution build, set `VINYLFLOW_CODESIGN_IDENTITY` to your Developer ID
identity and notarize the resulting app. The default local build is ad-hoc signed.

### Releases

This fork uses `main` for development and version tags for releases. Until hardware
and live-service validation is complete, desktop downloads are marked as prereleases.

---

## Manual Setup (Non-Docker)

For tech-savvy users who prefer managing their own Python environment.

### Prerequisites

You'll need to install these system dependencies first:

- **Python 3.11 or later**
- **FFmpeg** (handles all audio processing and format conversion: MP3, FLAC, AIFF)
- **FLAC encoder** (optional, provides dedicated FLAC encoding tools)
- **libsndfile1** (optional, audio I/O library)

**Installation by OS:**

**macOS** (using Homebrew):
```bash
brew install python@3.11 ffmpeg flac libsndfile
```

**Ubuntu/Debian**:
```bash
sudo apt-get update
sudo apt-get install python3.11 python3.11-venv ffmpeg flac libsndfile1-dev
```

**Windows**:
- Install [Python 3.11+](https://www.python.org/downloads/) (check "Add to PATH" during installation)
- Download [FFmpeg](https://ffmpeg.org/download.html) and add it to your PATH
- FLAC and libsndfile are bundled with FFmpeg on Windows

### Installation Steps

```bash
# 1. Clone the repository
git clone https://github.com/reykroona/FlipsideFlow.git
cd FlipsideFlow

# 2. Create and activate a Python virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Start the server
python -m uvicorn backend.api:app --host 0.0.0.0 --port 8000
```

Open **http://localhost:8000** in your browser.

### First-Run Setup (Non-Docker)

Just like with Docker, FlipsideFlow will show you a welcome screen on first run:

1. Visit [discogs.com/settings/developers](https://www.discogs.com/settings/developers) and generate a free API token
2. Paste it into the FlipsideFlow setup screen
3. In the macOS desktop app, optionally add an AudD token too. Click **Save and continue**.

Your token is saved to `config/settings.json` and works immediately without restart.

**Alternative (for advanced users):** You can still use a `.env` file if you prefer:

```bash
cp .env.example .env
```

Edit `.env` and add your token:

```ini
DISCOGS_USER_TOKEN=your_token_here
DEFAULT_OUTPUT_DIR=~/Music/FlipsideFlow
```

**Notes:**
- The `output/` and `temp_uploads/` directories are created automatically
- To import recordings in place instead of uploading them, add `VINYLFLOW_IMPORT_DIR=~/Music/FlipsideFlow/Incoming` (or any folder) to `.env`
- You can adjust silence detection and other settings in `.env` (see [Configuration](#configuration) below)
- To stop the server, press `Ctrl+C` in the terminal

---

## How It Works

1. **Add** — import a WAV or AIFF recording from the import folder, or drag and drop it to upload
2. **Pick the release** — FlipsideFlow searches Discogs from the file name as soon as you select the recording. Result cards fill in their track lists as they load; releases with complete track lengths are marked, and the one whose lengths best fit your recording is flagged "Best fit". Edit the search box to look again. The chosen release stays in a card above the waveform (cover art, a link to Discogs, **Change release**)
3. **Choose the sides, detect** — if the release has several sides, FlipsideFlow suggests which one(s) your recording holds by comparing the Discogs lengths with the length of the music (side chips plus Full album / per-disc presets; change them any time). Picking a release then places exactly that many tracks, using the Discogs lengths and the silences between songs, and marks where the lengths expect each break with dashed lines on the waveform. After you have moved boundaries by hand, changing the release or sides asks before placing them again. **Snap to Discogs** places them again on demand (Undo brings your boundaries back). No Discogs token, or no good match? Use **Skip Discogs — detect tracks by silence only**; **Try Discogs again** takes you back to the search once tracks exist
4. **Map** — match detected tracks to Discogs track listings (already done for the chosen sides; change any row by hand)
5. **Choose format** — FLAC, MP3, or AIFF output
6. **Process** — tracks are split, converted, tagged, and saved with cover art

Your files appear in the `output/` folder, organized as `Artist - Album/A1-Track Name.flac`.

---

## Output Example

```
output/
└── Aril Brikha - Departure/
    ├── A1-Groove La Chord.flac
    ├── A2-Art Of Vengeance.flac
    ├── B1-Ambiogenesis.flac
    ├── B2-Deeparture In Mars.flac
    └── folder.jpg
```

Each file includes embedded metadata: artist, album, title, track number, year, label, Discogs ID, and cover art.

---

## Who Is This For?

- **DJs** digitizing crate finds for digital sets
- **Vinyl collectors** preserving and cataloguing collections
- **Record labels** archiving back catalogs
- **Music lovers** who want their vinyl in lossless digital

---

## Configuration

**Discogs Token**: Managed via the web UI (Settings ⚙️ menu)

**Audio Processing Settings**: Adjust in the app via Settings (⚙️), or for advanced users, edit `config/settings.json` or `.env`:

```ini
# Silence detection (adjust if tracks aren't splitting correctly)
DEFAULT_SILENCE_THRESHOLD=-40      # dB — increase to -35 if tracks are merging
DEFAULT_MIN_SILENCE_DURATION=1.5   # seconds — decrease to 1.0 for short gaps
DEFAULT_MIN_TRACK_LENGTH=30        # seconds — ignore segments shorter than this

# FLAC compression (0-8, higher = smaller files)
DEFAULT_FLAC_COMPRESSION=8
```

**Config Priority**: `config/settings.json` (UI-editable) → `.env` (manual) → environment variables (Docker)

**Track Numbering**: Configurable via Settings (⚙️) or by setting `TRACK_NUMBERING` in `config/settings.json`:
- `vinyl` — writes the vinyl position (e.g. `A1`, `B2`) directly as `TRACKNUMBER`. Preserves vinyl notation but some players (Jellyfin, Roon) may mis-sort tracks.
- `sequential_disc_per_lp` *(default)* — writes integer `DISCNUMBER` (A+B = disc 1, C+D = disc 2) and sequential `TRACKNUMBER` across both sides of each LP, with track/disc totals (`TRACKTOTAL`/`DISCTOTAL`, or `n/total` in ID3) and the original vinyl position preserved in a `VINYLPOSITION` tag. Numbering follows the release's tracklist, so a side exported on its own continues after side A and a skipped track leaves a gap. Single LPs appear as one disc; 2-LP sets appear as two. Recommended for correct playback order in media servers. The disc comes from the side letter, so a release whose sides skip letters on Discogs (A, B, E, F) counts as 3 discs, and tracks on other media (`CD1`, `DVD1`) keep their position as `TRACKNUMBER` with no disc or totals.

*Upgrading:* an existing `config/settings.json` without a saved scheme is set to `vinyl` on the first start, so your library keeps sorting one way; only new installs get the sequential default. An install configured only through `.env` or environment variables has no such file, so set `TRACK_NUMBERING=vinyl` there to keep the old numbering.

### Silence Detection Tips

| Problem | Fix |
|---|---|
| Tracks merging together | Increase threshold (e.g. `-35` instead of `-40`) |
| Too many splits | Decrease threshold (e.g. `-45` instead of `-40`) |
| Splitting on brief silence | Increase min silence duration (e.g. `2.0`) |

---

## Managing Docker

```bash
# View logs
docker compose logs -f

# Stop FlipsideFlow
docker compose stop

# Restart
docker compose restart

# Remove containers (keeps your files in ./output)
docker compose down
```

---

## Troubleshooting

**See the setup screen on first run?**
This is normal! FlipsideFlow guides you through adding your Discogs token via the web interface. Just follow the on-screen instructions — it takes 30 seconds.

**"command not found: git"?**
You don't need Git! Use the **Download ZIP** option in [Step 1](#1-get-flipsideflow) instead.

**"command not found: docker"?**
Make sure Docker Desktop is installed and **running**. You should see the Docker icon in your system tray (Mac menu bar or Windows taskbar).

**Can't navigate to the FlipsideFlow folder in terminal?**
- **Mac/Linux:** Type `cd ` (with a space) and drag the vinylflow folder into the terminal window, then press Enter
- **Windows:** Open the vinylflow folder in File Explorer, click the address bar, type `cmd` and press Enter — this opens a terminal already in the right folder

**Container won't start?**
Check if port 8000 is in use: `lsof -i :8000` (Mac/Linux) or `netstat -ano | findstr :8000` (Windows). Change the port in `.env` with `PORT=8080`.

**Files not appearing in output/?**
Make sure the `output/` directory exists and has write permissions: `chmod -R 755 ./output`

**Discogs search returns no results?**
Edit the search box (the file name is only a starting point) and press Enter. If it keeps failing, your API token might be invalid or revoked. Click the Settings (⚙️) button and update your token, or generate a new one at [discogs.com/settings/developers](https://www.discogs.com/settings/developers)

**Tracks not splitting correctly?**
Try adjusting silence detection in Settings (⚙️), or pick a Discogs release and use **Snap to Discogs** to place the tracks from its lengths.

---

## Technology Stack

| Component | Technology |
|---|---|
| Backend | Python, FastAPI, uvicorn |
| Audio processing | FFmpeg |
| Metadata tagging | Mutagen (FLAC, MP3, AIFF) |
| Music database | Discogs API |
| Frontend | Alpine.js, Tailwind CSS |
| Waveform display | WaveSurfer.js |
| Deployment | Docker |

---

## Roadmap

### Shipped (v1.0)
- Core digitization workflow
- Discogs integration with visual search
- Interactive waveform editor with draggable track boundaries
- Manual track splitting and deletion
- Vinyl-style track numbering (A1, A2, B1, B2)
- FLAC, MP3, and AIFF output
- WAV and AIFF input
- Guided detection from Discogs lengths, with a side picker and Snap to Discogs
- WebSocket real-time progress
- Docker one-command setup
- **Web-based first-run setup** — no more hidden `.env` files!

### Planned
- BPM and key detection
- Rekordbox / Traktor export
- Click and pop removal
- MusicBrainz integration
- Cloud-hosted option

---

## Contributing

Found a bug? Have a feature idea? [Open an issue](https://github.com/reykroona/FlipsideFlow/issues) — contributions welcome.

---

## License

[MIT](LICENSE) — free to use, modify, and distribute. FlipsideFlow keeps VinylFlow's original copyright notice in [LICENSE](LICENSE).

---

**FlipsideFlow** — based on **VinylFlow**, built with ❤️ by DJs, for DJs.
