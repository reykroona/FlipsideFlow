const fs = require('fs');
const path = require('path');
const assert = require('assert');
const source = fs.readFileSync(path.join(__dirname, '../../backend/static/app.js'), 'utf8');
const vinylApp = new Function(source + ';return vinylApp;')();
function setup() {
    const a = vinylApp();
    a.notify = () => {};
    a.recordingEnabled = true;
    a.discogsConfigured = false;
    a.setupRequired = true;
    return a;
}
(async () => {
    // The launcher prompts only while Discogs is missing, and never interrupts live capture.
    const a = setup();
    a.updateSetupPrompt();
    assert.strictEqual(a.setupRequired, true, 'no Discogs token yet: show the welcome dialog');
    a.discogsConfigured = true;
    a.updateSetupPrompt();
    assert.strictEqual(a.setupRequired, false, 'AudD is optional: no dialog at every launch without it');
    a.songRecognition.configured = true;
    a.updateSetupPrompt();
    assert.strictEqual(a.setupRequired, false);
    a.songRecognition.configured = false;
    a.recordingEnabled = false;
    a.updateSetupPrompt();
    assert.strictEqual(a.setupRequired, false, 'web/server mode does not ask for unavailable AudD');
    a.discogsConfigured = false;
    a.recorder.state = 'recording';
    a.updateSetupPrompt();
    assert.strictEqual(a.setupRequired, false);

    // Both tokens save independently. Neither secret remains in the form after success.
    const both = setup();
    both.setupToken = ' discogs-test-key ';
    both.recognitionTokenInput = 'audd-test-key';
    const calls = [];
    global.fetch = async (url, options) => {
        calls.push([url, JSON.parse(options.body)]);
        return {ok: true, json: async () => ({username: 'Test user'})};
    };
    both.recordingRequest = async (route, body) => {
        calls.push([route, body]);
        return {configured: true};
    };
    await both.submitWelcomeSetup();
    assert.deepStrictEqual(calls.map(c => c[0]), ['/api/setup/discogs-token', 'recognition/token']);
    assert.strictEqual(calls[0][1].token, 'discogs-test-key');
    assert.strictEqual(both.setupToken, '');
    assert.strictEqual(both.recognitionTokenInput, '');
    assert.strictEqual(both.setupRequired, false);
    assert.ok(both.discogsConfigured && both.songRecognition.configured);

    // AudD alone is allowed; blank fields never clear an existing saved credential.
    const audd = setup();
    audd.recognitionTokenInput = 'audd';
    global.fetch = async () => { throw new Error('Discogs must not be called'); };
    audd.recordingRequest = async () => ({configured: true});
    await audd.submitWelcomeSetup();
    assert.strictEqual(audd.setupRequired, false);
    assert.strictEqual(audd.discogsConfigured, false);
    assert.strictEqual(audd.songRecognition.configured, true);

    // A failed Discogs validation still saves AudD and leaves the welcome screen open.
    const failure = setup();
    failure.setupToken = 'invalid';
    failure.recognitionTokenInput = 'audd';
    global.fetch = async () => ({ok: false, json: async () => ({detail: 'Invalid token'})});
    failure.recordingRequest = async () => ({configured: true});
    await failure.submitWelcomeSetup();
    assert.strictEqual(failure.setupRequired, true);
    assert.match(failure.setupError, /Discogs: Invalid token/);
    assert.strictEqual(failure.setupToken, 'invalid');
    assert.strictEqual(failure.recognitionTokenInput, '');
    assert.strictEqual(failure.songRecognition.configured, true);
    assert.strictEqual(failure.setupLoading, false);
    assert.strictEqual(failure.recognitionBusy, false);

    // Conversely, an AudD save error keeps the successful Discogs setup for the retry.
    const retry = setup();
    retry.setupToken = 'discogs';
    retry.recognitionTokenInput = 'audd';
    let discogsCalls = 0;
    global.fetch = async () => { discogsCalls++; return {ok: true, json: async () => ({username: 'Test'})}; };
    retry.recordingRequest = async () => { throw new Error('Could not save token'); };
    await retry.submitWelcomeSetup();
    assert.strictEqual(retry.setupRequired, true);
    assert.strictEqual(retry.discogsConfigured, true);
    assert.match(retry.setupError, /AudD: Could not save token/);
    retry.recordingRequest = async () => ({configured: true});
    await retry.submitWelcomeSetup();
    assert.strictEqual(discogsCalls, 1);
    assert.strictEqual(retry.setupRequired, false);

    // The Discogs-only Settings action remains independent of AudD.
    const settings = setup();
    settings.setupToken = 'discogs';
    settings.recordingRequest = async () => { throw new Error('AudD must not be called'); };
    await settings.submitSetup();
    assert.strictEqual(settings.discogsConfigured, true);
})().catch(error => { console.error(error); process.exit(1); });
