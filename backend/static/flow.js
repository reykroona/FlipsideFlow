/**
 * FlipsideFlow — the guided Record flow (Add → Record → Flip → Export), the Library and the
 * one bottom player.  Mixed into vinylApp() (see the end of app.js), so `this` is the same
 * Alpine component: the flow drives the recorder, detection and export code in app.js and
 * the old full editor stays available as the "Workbench" view ("Fix splits").
 */
function vinylFlowMixin() {
    const freshFlow = () => ({
        step: 'add',            // add | record | review | done
        query: '',
        results: [],
        searching: false,
        notice: '',
        release: null,          // Discogs release picked before recording (tracks loaded)
        loadingId: null,
        autoFlip: true,
        showInput: false,
        fileId: null,
        donePath: '',
        doneAlbumId: null,
    });

    return {
        view: 'workbench',      // record | library | workbench
        flow: freshFlow(),
        _flowSeq: 0,
        library: { albums: [], root: '', error: null, loading: false, selectedId: null, filter: '', loaded: false },
        bar: { queue: [], index: -1, playing: false, time: 0, duration: 0, triedMp3: false },

        async initFlow() {
            this.view = this.recordingEnabled ? 'record' : 'library';
            await this.loadLibrary();
            if (!this.recordingEnabled && !this.library.albums.length) this.view = 'workbench';
            // A recording still running in the backend (window reloaded): go straight to it.
            if (this.recordingActive) {
                this.view = 'record';
                this.flow.step = 'record';
            }
            window.addEventListener('keydown', (e) => this.flowKey(e));
        },

        setView(view) {
            if (this.view === view) return;
            this.view = view;
            if (view === 'library') this.loadLibrary();
            if (view === 'workbench' && this.detectedTracks.length) {
                // The waveform was laid out while hidden (zero width): draw it again now it shows.
                this.$nextTick(() => {
                    if (this.analyzing) return;
                    this.destroyWaveform();
                    this.initWaveform();
                });
            }
        },

        /** Space starts / flips / resumes in the Record view and plays / pauses in the Library. */
        flowKey(e) {
            if (e.code !== 'Space' || e.metaKey || e.ctrlKey || e.altKey) return;
            const tag = (e.target?.tagName || '').toLowerCase();
            if (['input', 'textarea', 'select', 'button', 'a', 'audio'].includes(tag) || e.target?.isContentEditable) return;
            if (this.showSettings || this.setupRequired) return;
            if (this.view === 'record' && this.flow.step === 'record') {
                e.preventDefault();
                this.flowPrimary();
            } else if (this.view === 'library' && this.bar.queue.length) {
                e.preventDefault();
                this.toggleBar();
            }
        },

        // ---------------------------------------------------------------- Add

        async flowSearch() {
            const query = this.flow.query.trim();
            if (!query) return;
            if (!this.discogsConfigured) {
                this.flow.notice = 'Add your Discogs token in Settings to look records up. You can also record now and type the names after.';
                return;
            }
            const seq = ++this._flowSeq;
            this.flow.searching = true;
            this.flow.notice = '';
            try {
                const response = await fetch('/api/search', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ query, max_results: 12 })
                });
                const data = await response.json();
                if (seq !== this._flowSeq) return;
                if (!response.ok) throw new Error(data.detail || 'Discogs search failed');
                this.flow.results = data.results || [];
                if (!this.flow.results.length) this.flow.notice = 'Nothing found. Try the artist and the album name, or record now and name it after.';
            } catch (error) {
                if (seq === this._flowSeq) this.flow.notice = `${error.message}. Check the connection, or record now and name it after.`;
            } finally {
                if (seq === this._flowSeq) this.flow.searching = false;
            }
        },

        /** Load the full tracklist of a search result (one request, cached by the backend). */
        async fetchRelease(result) {
            this.flow.loadingId = result.id;
            try {
                const response = await fetch(`/api/release/${result.id}`);
                const data = await response.json();
                if (!response.ok) throw new Error(data.detail || 'Could not load this release');
                if (!Array.isArray(data.tracks) || !data.tracks.length) throw new Error('This release has no tracklist on Discogs');
                return data;
            } catch (error) {
                this.notify(`${error.message}. Pick another, or name it yourself.`, { type: 'warning', timeout: 6000 });
                return null;
            } finally {
                this.flow.loadingId = null;
            }
        },

        async flowPick(result) {
            if (this.flow.loadingId) return;
            const release = await this.fetchRelease(result);
            if (!release) return;
            this.flow.release = release;
            this.flowGoRecord();
        },

        flowSkipLookup() {
            this.flow.release = null;
            this.flowGoRecord();
        },

        flowBackToAdd() {
            if (this.recordingActive) return;
            this.flow.step = 'add';
        },

        // ---------------------------------------------------------------- Record

        get flowSides() {
            return this.flow.release ? this.releaseSides(this.flow.release) : [];
        },

        get flowTitle() {
            const release = this.flow.release;
            if (release) return `${release.artist} — ${release.title}`;
            return this.recordingTitle.trim() || 'New record';
        },

        get flowSideIndex() {
            const letter = this.recorder.side || 'A';
            return Math.max(0, letter.charCodeAt(0) - 65);
        },

        /** Side chips: done / live / waiting, from the release's sides (or just the count so far). */
        get flowSideChips() {
            const index = this.recordingActive || this.recorder.state === 'finished' ? this.flowSideIndex : -1;
            const live = this.recorder.state === 'recording';
            const sides = this.flowSides.length
                ? this.flowSides.map(g => ({ label: g.side || 'A', seconds: g.totalSeconds, tracks: g.tracks.length }))
                : Array.from({ length: Math.max(1, index + 1) }, (_, i) => ({ label: String.fromCharCode(65 + i), seconds: null, tracks: null }));
            return sides.map((side, i) => ({
                ...side,
                state: i < index ? 'done' : i === index ? (live ? 'live' : this.recordingActive ? 'paused' : 'done') : 'next'
            }));
        },

        get flowExpectedSide() {
            return this.flowSides[this.flowSideIndex] || null;
        },

        /** The current side's tracks laid out by length, filling in as the side plays. */
        get flowRuler() {
            const side = this.flowExpectedSide;
            if (!side) return [];
            const played = this.recordingActive ? (this.recorder.side_seconds || 0) : 0;
            let at = 0;
            return side.tracks.map(track => {
                const length = this.parseTrackDuration(track.duration) || 180;
                const start = at;
                at += length;
                const fill = Math.max(0, Math.min(1, (played - start) / length));
                return { position: track.position, title: track.title, length, fill, now: fill > 0 && fill < 1 };
            });
        },

        get flowNowPlaying() {
            return this.flowRuler.find(t => t.now) || null;
        },

        /** LED segments for one channel: 30 segments over -60..0 dBFS, amber from -12, red from -3. */
        ledSegments(db) {
            const lit = Math.round(Math.max(0, Math.min(1, ((db ?? -120) + 60) / 60)) * 30);
            return Array.from({ length: 30 }, (_, i) => {
                if (i >= lit) return '';
                const level = -60 + (i + 1) * 2;
                return level > -3 ? 'r' : level > -12 ? 'y' : 'g';
            });
        },

        get flowInputLabel() {
            const input = this.recordingInput;
            if (!input.device_name) return 'No input selected';
            return `${input.device_name} · inputs ${input.left}–${input.right} · ${input.rate / 1000} kHz`;
        },

        async flowGoRecord() {
            this.flow.step = 'record';
            if (this.flow.release) this.recordingTitle = `${this.flow.release.artist} — ${this.flow.release.title}`.slice(0, 120);
            if (!this.recordingDevices.length) await this.refreshRecordingDevices();
            if (this.recordingInput.device === '') {
                this.flow.showInput = true;
                return;
            }
            // Live meters right away, so the level can be checked before the needle drops.
            if (!this.recordingActive && this.recorder.state !== 'monitoring') {
                await this.recordingAction('monitor', this.recordingInput);
            }
        },

        async flowStart() {
            if (this.recordingBusy) return;
            if (this.recorder.state !== 'monitoring') {
                await this.recordingAction('monitor', this.recordingInput);
                if (this.recorder.state !== 'monitoring') return;
            }
            await this.recordingAction('start', {
                title: this.recordingTitle,
                // A release picked up front needs no song recognition.
                identify_album: !this.flow.release && this.identifyAlbum,
                auto_flip: this.flow.autoFlip,
                side_lengths: this.flowSides.map(g => g.totalSeconds || 0),
            });
        },

        async flowFlip() {
            await this.recordingAction('pause', { side_flip: true });
        },

        async flowResume() {
            await this.recordingAction('resume');
        },

        /** The one big button: start, flip, or resume. */
        flowPrimary() {
            if (this.recordingBusy) return;
            if (this.recorder.state === 'recording') return this.flowFlip();
            if (this.recorder.state === 'paused') return this.flowResume();
            return this.flowStart();
        },

        async flowFinish() {
            if (!this.recordingActive || this.recordingBusy) return;
            await this.recordingAction('stop');
            if (this.flow.step === 'record' && !this.recordingActive && this.recorder.state !== 'recording') {
                // stop() handed the file to onRecordedFileAccepted; without one, nothing was saved.
                if (!this.flow.fileId) this.notify(this.recordingError || 'Nothing was recorded.', { type: 'warning' });
            }
        },

        async flowChangeInput() {
            if (this.recordingActive) return;
            if (this.recorder.state === 'monitoring') await this.recordingAction('stop');
            this.flow.showInput = true;
        },

        async flowApplyInput() {
            this.flow.showInput = false;
            await this.recordingAction('monitor', this.recordingInput);
        },

        /** app.js hook: a recording was stopped and opened.  Returns true when the flow handled it. */
        onRecordedFileAccepted(file) {
            if (this.view !== 'record') return false;
            this.flow.fileId = file.id;
            this.flow.step = 'review';
            const release = this.flow.release;
            if (release) {
                const flips = (file.recording_side_markers || []).length;
                const sides = this.releaseSides(release).slice(0, flips + 1).map(g => g.side);
                this.searchSeq++;              // the file-name search selectFile started is not needed
                this.searchLoading = false;
                this.selectRelease(release, { sides });
            } else {
                this.flow.query = this.recordingTitle.replace(/\s+[-–—]\s+/, ' ').trim();
                this.flow.results = [];
                if (!this.applyRecognitionToCurrentFile() && this.flow.query && this.discogsConfigured) this.flowSearch();
            }
            this.notify('Recording saved.', { type: 'success' });
            return true;
        },

        // ---------------------------------------------------------------- Review & export

        get flowFile() {
            return this.uploadedFiles.find(f => f.id === this.flow.fileId) || null;
        },

        /** Detected tracks with their names and a length check against Discogs. */
        get flowTracks() {
            const release = this.selectedRelease;
            const manual = release?.manual ? this.manualPositions(this.detectedTracks) : null;
            return this.detectedTracks.map((track, idx) => {
                const rt = release?.tracks?.[this.customMapping[idx]];
                const expected = rt && !release.manual ? this.parseTrackDuration(rt.duration) : null;
                const diff = expected ? Math.abs(track.duration - expected) : null;
                return {
                    idx, track, position: manual ? manual[idx] : (rt?.position || `${idx + 1}`),
                    title: rt?.title || `Track ${track.number}`, expected,
                    check: diff === null ? 'none' : diff <= 4 ? 'ok' : 'check',
                };
            });
        },

        get flowChecksOk() {
            const rows = this.flowTracks.filter(r => !r.track.ignored);
            return rows.length > 0 && rows.every(r => r.check !== 'check') && !this.showTrackCountMismatch;
        },

        async flowPickForFile(result) {
            if (this.flow.loadingId || this.analyzing) return;
            const release = await this.fetchRelease(result);
            if (!release) return;
            const flips = (this.currentFile?.recording_side_markers || []).length;
            const sides = flips ? this.releaseSides(release).slice(0, flips + 1).map(g => g.side) : null;
            this.selectRelease(release, { sides });
        },

        flowFixSplits() {
            this.setView('workbench');
            this.$nextTick(() => document.getElementById('release-card')?.scrollIntoView({ behavior: 'smooth', block: 'start' }));
        },

        async flowExport() {
            if (this.isProcessing || this.analyzing) return;
            await this.processFile();
        },

        /** app.js hook: an export finished. */
        async onExportComplete(fileId, outputPath) {
            await this.loadLibrary();
            if (this.view !== 'record' || fileId !== this.flow.fileId) return;
            this.flow.donePath = outputPath || '';
            const folderName = (outputPath || '').split(/[\\/]/).pop();
            const album = this.library.albums.find(a => a.folder === folderName || a.folder.endsWith('/' + folderName));
            this.flow.doneAlbumId = album?.id || null;
            this.flow.step = 'done';
        },

        flowOpenInLibrary() {
            if (this.flow.doneAlbumId) this.library.selectedId = this.flow.doneAlbumId;
            this.setView('library');
        },

        flowRecordNext() {
            if (this.recordingActive) return;
            this.resetForNextFile();
            this.flow = freshFlow();
            this.recordingTitle = '';
        },

        // ---------------------------------------------------------------- Library

        async loadLibrary() {
            this.library.loading = true;
            try {
                const response = await fetch('/api/library');
                const data = await response.json();
                if (!response.ok) throw new Error(data.detail || 'Could not read the library');
                this.library.albums = data.albums || [];
                this.library.root = data.root || '';
                this.library.error = data.error || null;
                if (this.library.selectedId && !this.library.albums.some(a => a.id === this.library.selectedId)) {
                    this.library.selectedId = null;
                }
            } catch (error) {
                this.library.error = error.message;
            } finally {
                this.library.loading = false;
                this.library.loaded = true;
            }
            if (this.recordingEnabled) this.loadSavedRecordings();
        },

        get libraryAlbums() {
            const q = this.library.filter.trim().toLowerCase();
            if (!q) return this.library.albums;
            return this.library.albums.filter(a =>
                `${a.artist} ${a.title} ${a.year} ${a.tracks.map(t => t.title).join(' ')}`.toLowerCase().includes(q));
        },

        get libraryAlbum() {
            return this.library.albums.find(a => a.id === this.library.selectedId) || null;
        },

        get libraryTrackCount() {
            return this.library.albums.reduce((n, a) => n + a.tracks.length, 0);
        },

        /** Saved recordings never exported: the library's "needs review" row. */
        get unfinishedRecordings() {
            const exported = new Set(this.library.albums.flatMap(a => (a.sources || []).map(s => s.session_id)));
            return this.savedRecordings.filter(r => !exported.has(r.id));
        },

        selectAlbum(album) {
            this.library.selectedId = this.library.selectedId === album.id ? null : album.id;
        },

        coverUrl(album) {
            return album?.has_cover ? `/api/library/cover/${album.id}?v=${Math.round(album.added)}` : '';
        },

        initials(text) {
            return (text || '?').split(/\s+/).filter(Boolean).slice(0, 2).map(w => w[0]).join('').toUpperCase();
        },

        async revealInFinder(id) {
            try {
                const response = await fetch(`/api/library/reveal/${id}`, { method: 'POST' });
                if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || 'Could not open Finder');
            } catch (error) {
                this.notify(error.message, { type: 'error' });
            }
        },

        /** Reopen the recording an album came from in the Workbench, to split it again. */
        async resplitAlbum(album) {
            const source = [...(album.sources || [])].reverse().find(s => s.available);
            const saved = source && this.savedRecordings.find(r => r.id === source.session_id);
            if (!saved) {
                this.notify('The original recording for this album is not in Saved recordings any more.', { type: 'warning' });
                return;
            }
            this.setView('workbench');
            await this.openRecordedFile(saved.id);
        },

        async continueRecording(item) {
            this.flow = freshFlow();
            this.view = 'record';
            this.flow.step = 'review';
            await this.openRecordedFile(item.id);
        },

        // ---------------------------------------------------------------- Bottom player

        get barItem() {
            return this.bar.queue[this.bar.index] || null;
        },

        albumQueue(album) {
            return album.tracks.map(t => ({
                src: `/api/library/audio/${t.id}`, mp3: `/api/library/audio/${t.id}?mp3=1`,
                title: t.title, subtitle: `${album.artist} — ${album.title}`, cover: this.coverUrl(album),
                trackId: t.id, albumId: album.id, position: t.position, start: 0, end: null,
            }));
        },

        playAlbum(album, index = 0) {
            this.bar.queue = this.albumQueue(album);
            this.playBarIndex(index);
        },

        /** Review a detected track in full: the whole recording, limited to the track's range. */
        playReviewTrack(row) {
            const file = this.currentFile;
            if (!file) return;
            const { track } = row;
            const queue = this.flowTracks.filter(r => !r.track.ignored).map(r => ({
                src: `/api/audio/${file.id}#t=${r.track.start.toFixed(2)},${r.track.end.toFixed(2)}`, mp3: null,
                title: r.title, subtitle: this.selectedRelease ? `${this.selectedRelease.artist} — ${this.selectedRelease.title}` : file.filename,
                cover: this.selectedRelease?.cover_url || '', trackId: `rev-${r.idx}`, position: r.position,
                start: r.track.start, end: r.track.end,
            }));
            this.bar.queue = queue;
            this.playBarIndex(Math.max(0, queue.findIndex(q => q.trackId === `rev-${row.idx}`)));
            void track;
        },

        playBarIndex(index) {
            const el = this.$refs.barPlayer;
            const item = this.bar.queue[index];
            if (!el || !item) return;
            this.stopWaveform();
            if (this.currentPlayingTrack) this.stopPreview();
            this.bar.index = index;
            this.bar.triedMp3 = false;
            this.bar.time = 0;
            this.bar.duration = item.end !== null ? item.end - item.start : 0;
            el.src = item.src;
            el.volume = this.playbackVolume;
            el.play().catch(err => { if (err.name !== 'AbortError') this.onBarError(); });
        },

        toggleBar() {
            const el = this.$refs.barPlayer;
            if (!el || !this.barItem) return;
            if (el.paused) {
                this.stopWaveform();
                if (this.currentPlayingTrack) this.stopPreview();
                el.play().catch(() => {});
            } else {
                el.pause();
            }
        },

        pauseBar() {
            const el = this.$refs.barPlayer;
            if (el && !el.paused) el.pause();
        },

        barNext() {
            if (this.bar.index + 1 < this.bar.queue.length) this.playBarIndex(this.bar.index + 1);
            else this.pauseBar();
        },

        barPrev() {
            const el = this.$refs.barPlayer;
            if (el && this.bar.time > 3) { el.currentTime = this.barItem?.start || 0; return; }
            if (this.bar.index > 0) this.playBarIndex(this.bar.index - 1);
        },

        onBarTime() {
            const el = this.$refs.barPlayer;
            const item = this.barItem;
            if (!el || !item) return;
            this.bar.time = Math.max(0, el.currentTime - item.start);
            if (item.end === null) this.bar.duration = Number.isFinite(el.duration) ? el.duration : 0;
            else if (el.currentTime >= item.end - 0.05) this.barNext();
        },

        seekBar(value) {
            const el = this.$refs.barPlayer;
            const item = this.barItem;
            if (!el || !item) return;
            el.currentTime = (item.start || 0) + Number(value);
        },

        /** The original can't play here (AIFF in some browsers): fall back to an MP3 copy once. */
        onBarError() {
            const el = this.$refs.barPlayer;
            const item = this.barItem;
            if (!el || !item) return;
            if (item.mp3 && !this.bar.triedMp3) {
                this.bar.triedMp3 = true;
                el.src = item.mp3;
                el.play().catch(() => {});
                return;
            }
            this.bar.playing = false;
            this.notify(`Could not play "${item.title}".`, { type: 'error' });
        },

        isBarTrack(trackId) {
            return this.barItem?.trackId === trackId;
        },
    };
}
