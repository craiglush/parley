const API = '';  // Same origin

// --- State ---
let selectedFile = null;
let pollTimer = null;
let currentMeetingId = null;
let titleFilterDebounce = null;
let currentView = 'week';   // week | speaker | keyword | category | linked
let groupedData = null;
let captureCollapsed = true;
let linkPickerMeetingId = null;
let allMeetingsCache = [];  // cache for link picker

// Notes & Chat state
let currentNotes = [];
let chatHistory = [];
let chatScope = null;
let chatAbortController = null;
let insightsCache = {};  // { meetingId: { list: [...], activeId: null } }

// --- DOM refs ---
const $ = id => document.getElementById(id);
let audioPlayerMeetingId = null;
const dropZone = $('dropZone');
const fileInput = $('fileInput');
const uploadFields = $('uploadFields');
const uploadBtn = $('uploadBtn');
const detailOverlay = $('detailOverlay');
const searchInput = $('searchInput');
const searchResults = $('searchResults');

// --- Recording ---
let mediaRecorder = null;
let recordedChunks = [];
let recordingStartTime = null;
let recordTimerInterval = null;
let audioContext = null;
let analyserNode = null;
let vizAnimFrame = null;
let recordSource = 'both'; // 'mic', 'screen', or 'both'

// Pause/resume state
let pausedDuration = 0;
let pauseStartTime = null;

// Extra streams to clean up for combined mode
let extraStreams = [];

// Live signal monitoring (drives the "no audio detected" safety net).
let recordingPeakLevel = 0;   // max audio level seen this session (0..255 scale)
let lastSignalTime = 0;       // timestamp of the last frame with real signal
let noAudioWarned = false;    // whether the no-audio warning is currently shown

// True when a *recording* is staged but not yet uploaded (vs a drag-dropped
// file, which still exists on disk). Drives the beforeunload / nav "are you
// sure" prompts. stagedSilent gates the pre-upload silent-recording confirm.
let stagedFromRecording = false;
let stagedSilent = false;

// --- IndexedDB Recording Backup ---
const DB_NAME = 'meeting-service';
const DB_VERSION = 1;
const STORE_NAME = 'unsaved-recordings';

function _openDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = (e) => {
      const db = e.target.result;
      if (!db.objectStoreNames.contains(STORE_NAME)) {
        db.createObjectStore(STORE_NAME, { keyPath: 'id' });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function _saveRecordingBackup(blob, fileName, mimeType) {
  try {
    const db = await _openDB();
    const tx = db.transaction(STORE_NAME, 'readwrite');
    const store = tx.objectStore(STORE_NAME);
    store.put({ id: 'current', blob, fileName, mimeType, createdAt: Date.now(), duration: recordTimer.textContent });
    await new Promise((resolve, reject) => { tx.oncomplete = resolve; tx.onerror = () => reject(tx.error); });
    db.close();
  } catch (e) { console.warn('IndexedDB save failed (non-fatal):', e); }
}

async function _loadRecordingBackup() {
  try {
    const db = await _openDB();
    const tx = db.transaction(STORE_NAME, 'readonly');
    const store = tx.objectStore(STORE_NAME);
    const entry = await new Promise((resolve, reject) => {
      const req = store.get('current');
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    db.close();
    return entry;
  } catch (e) { console.warn('IndexedDB load failed:', e); return null; }
}

async function _clearRecordingBackup() {
  try {
    const db = await _openDB();
    const tx = db.transaction(STORE_NAME, 'readwrite');
    const store = tx.objectStore(STORE_NAME);
    store.delete('current');
    await new Promise((resolve, reject) => { tx.oncomplete = resolve; tx.onerror = () => reject(tx.error); });
    db.close();
  } catch (e) { console.warn('IndexedDB clear failed:', e); }
}

const recordBtn = $('recordBtn');
const recordArea = $('recordArea');
const recordLabel = $('recordLabel');
const recordTimer = $('recordTimer');
const recordingIndicator = $('recordingIndicator');
const recordingIndicatorText = $('recordingIndicatorText');
const pauseBtn = $('pauseBtn');
const audioViz = $('audioViz');

// Create visualizer bars
const VIZ_BARS = 24;
for (let i = 0; i < VIZ_BARS; i++) {
  const bar = document.createElement('div');
  bar.className = 'viz-bar';
  bar.style.height = '3px';
  audioViz.appendChild(bar);
}

// Source toggle
document.querySelectorAll('.source-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    if (mediaRecorder && (mediaRecorder.state === 'recording' || mediaRecorder.state === 'paused')) return;
    document.querySelectorAll('.source-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    recordSource = btn.dataset.source;
  });
});

recordBtn.addEventListener('click', async () => {
  if (mediaRecorder && (mediaRecorder.state === 'recording' || mediaRecorder.state === 'paused')) {
    stopRecording();
  } else if (selectedFile) {
    if (!confirm('Discard your unsaved recording and start a new one?')) return;
    _clearRecordingBackup();
    selectedFile = null;
    fileInput.value = '';
    uploadFields.classList.remove('visible');
    await startRecording();
  } else {
    await startRecording();
  }
});

pauseBtn.addEventListener('click', togglePause);

// --- Loopback Device Management ---
const LOOPBACK_PATTERNS = ['cable output', 'vb-audio', 'blackhole', 'loopback', 'virtual'];

async function getAudioInputDevices() {
  try {
    const tempStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    tempStream.getTracks().forEach(t => t.stop());
  } catch (e) { /* permission denied - labels will be empty */ }

  const devices = await navigator.mediaDevices.enumerateDevices();
  return devices.filter(d => d.kind === 'audioinput');
}

function isLikelyLoopbackDevice(device) {
  const label = (device.label || '').toLowerCase();
  return LOOPBACK_PATTERNS.some(p => label.includes(p));
}

async function populateDeviceSelectors() {
  const devices = await getAudioInputDevices();
  const micSelect = $('micDeviceSelect');
  const loopbackSelect = $('loopbackDeviceSelect');

  const savedMicId = localStorage.getItem('meeting_mic_device_id') || '';
  const savedLoopbackId = localStorage.getItem('meeting_loopback_device_id') || '';

  // Preserve current selections before clearing
  const currentMicVal = micSelect.value;
  const currentLoopbackVal = loopbackSelect.value;

  // Clear and rebuild mic selector
  micSelect.innerHTML = '<option value="">Default</option>';
  devices.forEach(d => {
    const opt = document.createElement('option');
    opt.value = d.deviceId;
    opt.textContent = d.label || `Microphone (${d.deviceId.slice(0, 8)}...)`;
    micSelect.appendChild(opt);
  });

  // Clear and rebuild loopback selector
  loopbackSelect.innerHTML = '<option value="">None (use screen share)</option>';
  let autoDetectedId = '';
  devices.forEach(d => {
    const opt = document.createElement('option');
    opt.value = d.deviceId;
    opt.textContent = d.label || `Device (${d.deviceId.slice(0, 8)}...)`;
    loopbackSelect.appendChild(opt);
    if (!autoDetectedId && isLikelyLoopbackDevice(d)) {
      autoDetectedId = d.deviceId;
    }
  });

  // Restore selection: saved > current > auto-detected
  if (savedMicId && [...micSelect.options].some(o => o.value === savedMicId)) {
    micSelect.value = savedMicId;
  } else if (currentMicVal && [...micSelect.options].some(o => o.value === currentMicVal)) {
    micSelect.value = currentMicVal;
  }

  if (savedLoopbackId && [...loopbackSelect.options].some(o => o.value === savedLoopbackId)) {
    loopbackSelect.value = savedLoopbackId;
  } else if (currentLoopbackVal && [...loopbackSelect.options].some(o => o.value === currentLoopbackVal)) {
    loopbackSelect.value = currentLoopbackVal;
  } else if (autoDetectedId && !savedLoopbackId) {
    // Auto-select likely loopback device on first visit
    loopbackSelect.value = autoDetectedId;
    localStorage.setItem('meeting_loopback_device_id', autoDetectedId);
  }

  updateLoopbackIndicator();
}

function updateLoopbackIndicator() {
  const loopbackSelect = $('loopbackDeviceSelect');
  const indicator = $('loopbackActiveIndicator');
  const hint = $('loopbackHint');

  if (loopbackSelect.value) {
    const deviceName = loopbackSelect.options[loopbackSelect.selectedIndex].textContent;
    indicator.textContent = 'Loopback active: System Audio and Mic + System will use "' + deviceName + '" instead of screen share.';
    indicator.style.display = 'block';
    hint.style.display = 'none';
  } else {
    indicator.style.display = 'none';
    hint.style.display = 'block';
  }
}

// Save device selections to localStorage
$('micDeviceSelect').addEventListener('change', () => {
  localStorage.setItem('meeting_mic_device_id', $('micDeviceSelect').value);
});

$('loopbackDeviceSelect').addEventListener('change', () => {
  localStorage.setItem('meeting_loopback_device_id', $('loopbackDeviceSelect').value);
  updateLoopbackIndicator();
});

$('refreshDevices').addEventListener('click', () => populateDeviceSelectors());

$('loopbackHelpToggle').addEventListener('click', () => {
  $('loopbackHelpContent').classList.toggle('visible');
});

// Loopback gear popover toggle
$('loopbackGearBtn').addEventListener('click', (e) => {
  e.stopPropagation();
  $('loopbackPopover').classList.toggle('visible');
});
document.addEventListener('click', (e) => {
  const pop = $('loopbackPopover');
  if (pop && pop.classList.contains('visible') && !pop.contains(e.target) && e.target !== $('loopbackGearBtn')) {
    pop.classList.remove('visible');
  }
});

// Listen for device changes (hot-plug)
if (navigator.mediaDevices && navigator.mediaDevices.ondevicechange !== undefined) {
  navigator.mediaDevices.addEventListener('devicechange', () => populateDeviceSelectors());
}

// Initialize device selectors on page load
populateDeviceSelectors();

// --- System Audio Helper (loopback-aware) ---
async function getSystemAudioStream() {
  const loopbackId = $('loopbackDeviceSelect').value;
  if (loopbackId) {
    return await navigator.mediaDevices.getUserMedia({
      audio: { deviceId: { exact: loopbackId } }
    });
  }
  return await getScreenAudioStream();
}

function getSelectedMicConstraints() {
  const micId = $('micDeviceSelect').value;
  if (micId) {
    return { audio: { deviceId: { exact: micId } } };
  }
  return { audio: true };
}

function getLoopbackDeviceName() {
  const sel = $('loopbackDeviceSelect');
  if (sel.value) {
    return sel.options[sel.selectedIndex].textContent;
  }
  return null;
}

async function getScreenAudioStream() {
  let stream = await navigator.mediaDevices.getDisplayMedia({
    video: false,
    audio: true,
  });
  if (!stream.getAudioTracks().length) {
    stream = await navigator.mediaDevices.getDisplayMedia({
      video: true,
      audio: true,
    });
    stream.getVideoTracks().forEach(t => t.stop());
  }
  return stream;
}

async function startRecording() {
  try {
    let stream;
    extraStreams = [];
    pausedDuration = 0;
    pauseStartTime = null;

    const loopbackName = getLoopbackDeviceName();
    const usingLoopback = !!$('loopbackDeviceSelect').value;

    if (recordSource === 'both') {
      let micStream, sysStream;
      try {
        micStream = await navigator.mediaDevices.getUserMedia(getSelectedMicConstraints());
      } catch (micErr) {
        recordLabel.textContent = 'Mic access denied';
        console.error('Mic error:', micErr);
        return;
      }

      try {
        sysStream = await getSystemAudioStream();
      } catch (sysErr) {
        if (usingLoopback) {
          console.error('Loopback device error:', sysErr);
          recordLabel.textContent = 'Loopback device error - recording mic only';
        } else {
          console.warn('Screen share cancelled, falling back to mic-only:', sysErr);
          recordLabel.textContent = 'Screen share cancelled - recording mic only';
        }
        stream = micStream;
        extraStreams = [];
        setupRecorderFromStream(stream);
        return;
      }

      if (!sysStream.getAudioTracks().length) {
        recordLabel.textContent = 'No system audio - recording mic only';
        sysStream.getTracks().forEach(t => t.stop());
        stream = micStream;
        setupRecorderFromStream(stream);
        return;
      }

      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      // Created AFTER awaiting getUserMedia + the screen-share/loopback prompt,
      // so Chrome starts it SUSPENDED — a suspended context pulls no samples
      // through the destination, making the mixed recording (and visualizer)
      // silent. Resume before wiring up. Root cause of empty 'Both' recordings.
      if (ctx.state === 'suspended') {
        try { await ctx.resume(); } catch (e) { console.warn('AudioContext resume failed:', e); }
      }
      const dest = ctx.createMediaStreamDestination();
      const micSource = ctx.createMediaStreamSource(micStream);
      const sysSource = ctx.createMediaStreamSource(sysStream);
      micSource.connect(dest);
      sysSource.connect(dest);

      stream = dest.stream;
      extraStreams = [micStream, sysStream];
      audioContext = ctx;

    } else if (recordSource === 'screen') {
      stream = await getSystemAudioStream();
      if (!stream.getAudioTracks().length) {
        recordLabel.textContent = 'No audio track - try Mic instead';
        return;
      }
    } else {
      // mic-only: use selected mic device
      stream = await navigator.mediaDevices.getUserMedia(getSelectedMicConstraints());
    }

    setupRecorderFromStream(stream, usingLoopback, loopbackName);

  } catch (err) {
    if (err.name === 'NotAllowedError') {
      recordLabel.textContent = 'Permission denied - allow microphone access';
    } else if (err.name === 'NotFoundError') {
      recordLabel.textContent = 'No microphone found';
    } else {
      recordLabel.textContent = 'Error: ' + err.message;
    }
    console.error('Recording error:', err);
  }
}

function setupRecorderFromStream(stream, usingLoopback, loopbackName) {
    recordedChunks = [];

    // Reset signal monitoring + clear any stale no-audio warning.
    recordingPeakLevel = 0;
    lastSignalTime = 0;
    noAudioWarned = false;
    stagedSilent = false;
    hideCaptureWarning();

    const mimeType = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4']
      .find(m => MediaRecorder.isTypeSupported(m)) || '';

    mediaRecorder = new MediaRecorder(stream, mimeType ? { mimeType } : {});

    mediaRecorder.ondataavailable = e => {
      if (e.data.size > 0) recordedChunks.push(e.data);
    };

    mediaRecorder.onstop = () => {
      stream.getTracks().forEach(t => t.stop());
      extraStreams.forEach(s => s.getTracks().forEach(t => t.stop()));
      extraStreams = [];
      onRecordingStopped();
    };

    stream.getAudioTracks().forEach(track => {
      track.addEventListener('ended', () => {
        if (mediaRecorder && (mediaRecorder.state === 'recording' || mediaRecorder.state === 'paused')) {
          stopRecording();
        }
      });
    });
    extraStreams.forEach(s => {
      s.getAudioTracks().forEach(track => {
        track.addEventListener('ended', () => {
          if (mediaRecorder && (mediaRecorder.state === 'recording' || mediaRecorder.state === 'paused')) {
            stopRecording();
          }
        });
      });
    });

    mediaRecorder.start(1000);

    recordBtn.classList.add('recording');
    recordArea.classList.add('recording');
    $('captureBanner').classList.add('recording');
    recordTimer.classList.add('visible');
    recordingIndicator.classList.add('visible');
    pauseBtn.classList.add('visible');
    pauseBtn.classList.remove('paused');
    pauseBtn.innerHTML = '&#9646;&#9646;';
    pauseBtn.title = 'Pause';
    audioViz.classList.add('visible');
    $('sourceToggle').style.display = 'none';
    $('loopbackSettings').style.display = 'none';
    $('loopbackGearBtn').style.display = 'none';

    // Set recording label based on source mode and loopback status
    if (usingLoopback && loopbackName) {
      if (recordSource === 'both') {
        recordLabel.textContent = 'Recording mic + loopback (' + loopbackName + ')';
        recordingIndicatorText.textContent = 'Mic + Loopback';
      } else if (recordSource === 'screen') {
        recordLabel.textContent = 'Recording loopback (' + loopbackName + ')';
        recordingIndicatorText.textContent = 'Loopback';
      } else {
        recordLabel.textContent = 'Click to stop';
        recordingIndicatorText.textContent = 'Recording';
      }
    } else {
      recordLabel.textContent = 'Click to stop';
      recordingIndicatorText.textContent = 'Recording';
    }

    recordingStartTime = Date.now();
    pausedDuration = 0;
    recordTimerInterval = setInterval(updateRecordTimer, 1000);
    updateRecordTimer();

    setupAudioViz(stream);
}

function stopRecording() {
  if (mediaRecorder && (mediaRecorder.state === 'recording' || mediaRecorder.state === 'paused')) {
    mediaRecorder.stop();
  }
  clearInterval(recordTimerInterval);
  cancelAnimationFrame(vizAnimFrame);
  if (audioContext) {
    audioContext.close().catch(() => {});
    audioContext = null;
  }
}

function togglePause() {
  if (!mediaRecorder) return;

  if (mediaRecorder.state === 'recording') {
    mediaRecorder.pause();
    pauseStartTime = Date.now();
    clearInterval(recordTimerInterval);
    cancelAnimationFrame(vizAnimFrame);

    pauseBtn.classList.add('paused');
    pauseBtn.innerHTML = '&#9654;';
    pauseBtn.title = 'Resume';
    recordingIndicatorText.textContent = 'Paused';
    document.querySelector('.rec-dot').style.animationPlayState = 'paused';

  } else if (mediaRecorder.state === 'paused') {
    mediaRecorder.resume();
    pausedDuration += Date.now() - pauseStartTime;
    pauseStartTime = null;
    recordTimerInterval = setInterval(updateRecordTimer, 1000);

    if (analyserNode) {
      const bars = audioViz.querySelectorAll('.viz-bar');
      const dataArray = new Uint8Array(analyserNode.frequencyBinCount);
      function draw() {
        vizAnimFrame = requestAnimationFrame(draw);
        analyserNode.getByteFrequencyData(dataArray);
        updateSignalLevel(dataArray);
        const step = Math.max(1, Math.floor(dataArray.length / VIZ_BARS));
        for (let i = 0; i < VIZ_BARS; i++) {
          const val = dataArray[Math.min(i * step, dataArray.length - 1)];
          const height = Math.max(3, (val / 255) * 40);
          bars[i].style.height = height + 'px';
        }
      }
      draw();
    }

    pauseBtn.classList.remove('paused');
    pauseBtn.innerHTML = '&#9646;&#9646;';
    pauseBtn.title = 'Pause';
    recordingIndicatorText.textContent = 'Recording';
    document.querySelector('.rec-dot').style.animationPlayState = '';
  }
}

function onRecordingStopped() {
  recordBtn.classList.remove('recording');
  recordArea.classList.remove('recording');
  $('captureBanner').classList.remove('recording');
  recordLabel.textContent = 'Click to record';
  recordTimer.classList.remove('visible');
  recordingIndicator.classList.remove('visible');
  pauseBtn.classList.remove('visible', 'paused');
  audioViz.classList.remove('visible');
  $('sourceToggle').style.display = '';
  $('loopbackSettings').style.display = '';
  $('loopbackGearBtn').style.display = '';
  document.querySelector('.rec-dot').style.animationPlayState = '';
  hideCaptureWarning();

  if (!recordedChunks.length) return;

  const mimeType = mediaRecorder.mimeType || 'audio/webm';
  const ext = mimeType.includes('ogg') ? '.ogg' : mimeType.includes('mp4') ? '.m4a' : '.webm';
  const blob = new Blob(recordedChunks, { type: mimeType });
  const elapsed = recordTimer.textContent.replace(/:/g, '');
  const fileName = `recording_${new Date().toISOString().slice(0,10)}_${elapsed}${ext}`;
  const file = new File([blob], fileName, { type: mimeType });

  selectFile(file);
  _saveRecordingBackup(blob, fileName, mimeType);
  stagedFromRecording = true;

  // Surface a silent capture before the user uploads (and later transcribes) it.
  stagedSilent = (recordingPeakLevel <= SIGNAL_THRESHOLD);
  if (stagedSilent) {
    showCaptureWarning('&#9888;&#65039; This recording appears to contain no audio (silent capture). Check your mic/system-audio source — upload anyway only if you expected silence.');
  }
}

// --- Live signal monitoring (no-audio safety net) ---
const SIGNAL_THRESHOLD = 6;   // byte-FFT bin value above the silence noise floor

function updateSignalLevel(dataArray) {
  let max = 0;
  for (let i = 0; i < dataArray.length; i++) if (dataArray[i] > max) max = dataArray[i];
  if (max > SIGNAL_THRESHOLD) {
    lastSignalTime = Date.now();
    if (max > recordingPeakLevel) recordingPeakLevel = max;
    if (noAudioWarned) hideCaptureWarning();
  }
}

function showCaptureWarning(msg) {
  noAudioWarned = true;
  const el = $('captureWarning');
  if (el) {
    // innerHTML is safe here: callers only ever pass a trusted static literal
    // (the entities below render the warning glyph); no user input reaches this.
    el.innerHTML = msg || '&#9888;&#65039; No audio detected — check your microphone / system-audio source. This recording may be silent.';
    el.style.display = 'block';
  }
}

function hideCaptureWarning() {
  noAudioWarned = false;
  const el = $('captureWarning');
  if (el) el.style.display = 'none';
}

function updateRecordTimer() {
  const elapsed = Math.floor((Date.now() - recordingStartTime - pausedDuration) / 1000);
  const h = Math.floor(elapsed / 3600);
  const m = Math.floor((elapsed % 3600) / 60);
  const s = elapsed % 60;
  recordTimer.textContent =
    `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;

  // No-audio safety net: a few seconds in with no real signal -> warn the user
  // immediately instead of letting them find out after an empty transcription.
  // (This tick is paused while recording is paused, so it won't false-fire.)
  if (mediaRecorder && mediaRecorder.state === 'recording') {
    const noSignal = lastSignalTime === 0 || (Date.now() - lastSignalTime > 3000);
    if (elapsed >= 4 && noSignal) showCaptureWarning();
  }
}

function setupAudioViz(stream) {
  const ctx = audioContext || new (window.AudioContext || window.webkitAudioContext)();
  audioContext = ctx;
  // Created after media-prompt awaits -> may start suspended; resume so the
  // analyser produces data (also powers the no-audio detector).
  if (ctx.state === 'suspended') ctx.resume().catch(() => {});
  const source = ctx.createMediaStreamSource(stream);
  analyserNode = ctx.createAnalyser();
  analyserNode.fftSize = 64;
  source.connect(analyserNode);

  const bars = audioViz.querySelectorAll('.viz-bar');
  const dataArray = new Uint8Array(analyserNode.frequencyBinCount);

  function draw() {
    vizAnimFrame = requestAnimationFrame(draw);
    analyserNode.getByteFrequencyData(dataArray);
    updateSignalLevel(dataArray);
    const step = Math.max(1, Math.floor(dataArray.length / VIZ_BARS));
    for (let i = 0; i < VIZ_BARS; i++) {
      const val = dataArray[Math.min(i * step, dataArray.length - 1)];
      const height = Math.max(3, (val / 255) * 40);
      bars[i].style.height = height + 'px';
    }
  }
  draw();
}

// --- Upload ---
dropZone.addEventListener('click', () => fileInput.click());

dropZone.addEventListener('dragover', e => {
  e.preventDefault();
  dropZone.classList.add('drag-over');
});

dropZone.addEventListener('dragleave', () => {
  dropZone.classList.remove('drag-over');
});

dropZone.addEventListener('drop', e => {
  e.preventDefault();
  dropZone.classList.remove('drag-over');
  if (e.dataTransfer.files.length) selectFile(e.dataTransfer.files[0]);
});

fileInput.addEventListener('change', () => {
  if (fileInput.files.length) selectFile(fileInput.files[0]);
});

function selectFile(file) {
  selectedFile = file;
  // A drag-dropped / picked file still exists on disk, so it's not "unsaved".
  // onRecordingStopped() and the recovery flow re-set this to true after calling us.
  stagedFromRecording = false;
  $('fileName').textContent = file.name;
  $('fileSize').textContent = formatBytes(file.size);
  uploadFields.classList.add('visible');
}

$('removeFile').addEventListener('click', () => {
  selectedFile = null;
  fileInput.value = '';
  uploadFields.classList.remove('visible');
  hideCaptureWarning();
  stagedFromRecording = false;
  stagedSilent = false;
  _clearRecordingBackup();
});

uploadBtn.addEventListener('click', async () => {
  if (!selectedFile) return;

  // Guard against accidentally queueing a silent recording for transcription.
  if (stagedFromRecording && stagedSilent) {
    if (!confirm('This recording appears to contain no audio. Upload and process it anyway?')) return;
  }

  uploadBtn.disabled = true;
  const progress = $('uploadProgress');
  progress.classList.add('visible');

  const form = new FormData();
  form.append('file', selectedFile);

  const title = $('meetingTitle').value.trim();
  if (title) form.append('title', title);

  const speakers = $('numSpeakers').value;
  if (speakers) {
    form.append('min_speakers', speakers);
    form.append('max_speakers', speakers);
  }

  const context = $('meetingContext').value.trim();
  if (context) form.append('meeting_context', context);

  try {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `${API}/meetings/upload`);

    xhr.upload.addEventListener('progress', e => {
      if (e.lengthComputable) {
        const pct = Math.round((e.loaded / e.total) * 100);
        $('progressFill').style.width = pct + '%';
        $('progressText').textContent = pct < 100 ? `Uploading... ${pct}%` : 'Processing started...';
      }
    });

    xhr.onload = () => {
      if (xhr.status === 202) {
        const data = JSON.parse(xhr.responseText);
        $('progressText').textContent = `Queued! Meeting ID: ${data.meeting_id}`;
        stagedFromRecording = false;
        stagedSilent = false;
        hideCaptureWarning();
        _clearRecordingBackup();
        setTimeout(() => {
          selectedFile = null;
          fileInput.value = '';
          uploadFields.classList.remove('visible');
          progress.classList.remove('visible');
          uploadBtn.disabled = false;
          $('meetingTitle').value = '';
          $('numSpeakers').value = '';
          $('meetingContext').value = '';
          $('progressFill').style.width = '0%';
        }, 2000);
        refreshMeetings();
        startPolling();
      } else if (xhr.status === 400 || xhr.status === 413) {
        // Validation error
        try {
          const errData = JSON.parse(xhr.responseText);
          $('progressText').textContent = errData.detail || 'Upload rejected';
        } catch (_) {
          $('progressText').textContent = `Upload rejected (${xhr.status})`;
        }
        uploadBtn.disabled = false;
      } else {
        $('progressText').textContent = 'Upload failed: ' + xhr.statusText;
        uploadBtn.disabled = false;
      }
    };

    xhr.onerror = () => {
      $('progressText').textContent = 'Upload error. Check connection.';
      uploadBtn.disabled = false;
    };

    xhr.send(form);
  } catch (err) {
    $('progressText').textContent = 'Error: ' + err.message;
    uploadBtn.disabled = false;
  }
});

// --- Sidebar Toggle & Sections ---
function toggleSidebarSection(sectionId) {
  const body = $(sectionId + 'Body');
  const toggle = $(sectionId + 'Toggle');
  if (body.classList.contains('collapsed')) {
    body.classList.remove('collapsed');
    body.style.maxHeight = body.scrollHeight + 'px';
    toggle.classList.remove('collapsed');
  } else {
    body.classList.add('collapsed');
    body.style.maxHeight = '0';
    toggle.classList.add('collapsed');
  }
}

// Hamburger for mobile
$('hamburgerBtn').addEventListener('click', () => {
  const sidebar = $('sidebar');
  const backdrop = $('sidebarBackdrop');
  sidebar.classList.toggle('open');
  backdrop.classList.toggle('visible');
});
$('sidebarBackdrop').addEventListener('click', () => {
  $('sidebar').classList.remove('open');
  $('sidebarBackdrop').classList.remove('visible');
});

// --- View Switcher ---
document.querySelectorAll('.view-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.view-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    currentView = btn.dataset.view;
    refreshGroupedView();
  });
});

// --- Grouped Meeting List ---
async function refreshGroupedView() {
  try {
    const resp = await fetch(`${API}/meetings/grouped?group_by=${currentView}`);
    groupedData = await resp.json();
    renderGroupedList(groupedData);

    // Also check for in-progress meetings for polling
    const flatResp = await fetch(`${API}/meetings`);
    const flatData = await flatResp.json();
    allMeetingsCache = flatData;
    const inProgress = flatData.some(m => !['complete', 'error'].includes(m.status));
    if (inProgress) startPolling(); else stopPolling();
  } catch (err) {
    console.error('Failed to refresh grouped view:', err);
  }
}

function renderGroupedList(data) {
  const container = $('sidebarMeetingList');
  const groups = data.groups || [];

  if (!groups.length) {
    const msg = currentView === 'linked'
      ? 'No linked meetings yet.'
      : 'No meetings match your filters.';
    container.innerHTML = `<div class="empty-state" style="padding:24px">${msg}</div>`;
    return;
  }

  // Apply client-side filters
  const statusFilter = $('meetingStatusFilter').value;
  const titleFilter = $('meetingTitleFilter').value.trim().toLowerCase();

  let html = '';
  groups.forEach((group, gi) => {
    let meetings = group.meetings || [];

    // Filter within group
    if (statusFilter) meetings = meetings.filter(m => m.status === statusFilter);
    if (titleFilter) meetings = meetings.filter(m => (m.title || '').toLowerCase().includes(titleFilter));

    if (!meetings.length) return;

    html += `<div class="group-header" onclick="toggleGroup(${gi})">
      <div style="display:flex;align-items:center;gap:8px">
        <span class="group-label">${escHtml(group.label)}</span>
        <span class="group-count">${meetings.length}</span>
      </div>
      <span class="group-chevron" id="groupChevron${gi}">&#9660;</span>
    </div>`;
    html += `<div class="group-meetings" id="groupMeetings${gi}">`;
    meetings.forEach(m => {
      const isActive = m.id === currentMeetingId;
      const statusCls = `status-${m.status}`;
      html += `<div class="sidebar-meeting-item${isActive ? ' active' : ''}" onclick="openMeeting('${m.id}')">
        <div class="smi-info">
          <div class="smi-title">${escHtml(m.title || 'Untitled')}</div>
          <div class="smi-meta">
            <span>${m.date || ''}</span>
            <span>${m.duration_formatted || ''}</span>
          </div>
        </div>
        <span class="smi-status ${statusCls}">${formatStatus(m.status)}</span>
      </div>`;
    });
    html += '</div>';
  });

  if (data.unlinked_count !== undefined) {
    html += `<div style="padding:12px 16px;font-size:12px;color:var(--text-muted)">${data.unlinked_count} unlinked meeting${data.unlinked_count !== 1 ? 's' : ''}</div>`;
  }

  container.innerHTML = html || '<div class="empty-state" style="padding:24px">No meetings match your filters.</div>';
}

function toggleGroup(index) {
  const el = $('groupMeetings' + index);
  const chevron = $('groupChevron' + index);
  if (el.classList.contains('collapsed')) {
    el.classList.remove('collapsed');
    chevron.classList.remove('collapsed');
  } else {
    el.classList.add('collapsed');
    chevron.classList.add('collapsed');
  }
}

// Keep old refreshMeetings as alias for polling compatibility
async function refreshMeetings() { return refreshGroupedView(); }

function formatStatus(s) {
  if (s === 'preprocessing') return 'Pre-processing';
  if (s === 'transcribing') return 'Transcribing...';
  if (s === 'cleaning_transcript') return 'Cleaning...';
  if (s === 'identifying_speakers') return 'Speakers...';
  if (s === 'summarizing') return 'Summarizing...';
  if (s === 'tagging') return 'Tagging...';
  if (s === 'storing') return 'Storing...';
  return s;
}

// Meeting list filter events
$('meetingStatusFilter').addEventListener('change', refreshGroupedView);
$('meetingTitleFilter').addEventListener('input', () => {
  clearTimeout(titleFilterDebounce);
  titleFilterDebounce = setTimeout(() => {
    // Re-render from cached data (client-side filter)
    if (groupedData) renderGroupedList(groupedData);
  }, 300);
});

// --- Polling ---
function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(refreshGroupedView, 3000);
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

// --- Detail View ---
function isMobile() { return window.innerWidth < 768; }

async function openMeeting(id) {
  currentMeetingId = id;
  if (typeof updateFloatingChatScope === 'function') updateFloatingChatScope();

  // Highlight in sidebar
  document.querySelectorAll('.sidebar-meeting-item').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.sidebar-meeting-item').forEach(el => {
    if (el.onclick && el.onclick.toString().includes(id)) el.classList.add('active');
  });
  // Better: re-render grouped list to update active state
  if (groupedData) renderGroupedList(groupedData);

  // Close mobile sidebar if open
  $('sidebar').classList.remove('open');
  $('sidebarBackdrop').classList.remove('visible');

  if (isMobile()) {
    // Use overlay on mobile
    detailOverlay.classList.add('visible');
    document.body.style.overflow = 'hidden';
    await populateDetail(id, true);
  } else {
    // Inline in main content
    $('mainEmptyState').style.display = 'none';
    $('inlineDetail').style.display = 'block';
    await populateDetail(id, false);
  }
}

async function populateDetail(id, mobile) {
  const prefix = mobile ? 'Mobile' : '';
  const titleEl = $('detailTitle' + prefix);
  const dateEl = $('detailDate' + prefix);
  const durationEl = $('detailDuration' + prefix);
  const statusEl = $('detailStatus' + prefix);
  const transcriptEl = $('transcriptContent' + prefix);
  const summaryEl = $('summaryContent' + prefix);
  const notesEl = $('notesContent' + prefix);
  const chatEl = $('chatContent' + prefix);
  const tagsEl = $('tagsContent' + prefix);
  const relatedEl = $('relatedContent' + prefix);
  const downloadsEl = $('downloadsBar' + prefix);
  const actionBarEl = $('actionButtonsBar' + prefix);
  const tabContainer = mobile ? $('tabsMobile') : $('inlineDetail');

  // Reset tabs to transcript
  const tabBtns = (mobile ? $('tabsMobile') : $('inlineDetail')).querySelectorAll('.tab-btn');
  tabBtns.forEach(b => b.classList.remove('active'));
  tabBtns[0].classList.add('active');

  const tabIds = ['transcript', 'summary', 'notes', 'chat', 'tags', 'related'];
  const suffix = mobile ? '-mobile' : '';
  tabIds.forEach((t, i) => {
    const el = $('tab-' + t + suffix);
    if (el) el.classList.toggle('active', i === 0);
  });

  // Reset chat state for new meeting
  chatHistory = [];
  chatScope = null;
  if (chatAbortController) { chatAbortController.abort(); chatAbortController = null; }

  const loading = '<div style="text-align:center;padding:40px"><div class="spinner"></div> Loading...</div>';
  if (transcriptEl) transcriptEl.innerHTML = loading;
  if (summaryEl) summaryEl.innerHTML = loading;
  if (notesEl) notesEl.innerHTML = '';
  if (chatEl) chatEl.innerHTML = '';
  if (tagsEl) tagsEl.innerHTML = loading;
  if (relatedEl) relatedEl.innerHTML = loading;
  if (downloadsEl) downloadsEl.style.display = 'none';
  if (actionBarEl) { actionBarEl.classList.remove('visible'); actionBarEl.innerHTML = ''; }

  try {
    const statusResp = await fetch(`${API}/meetings/${id}/status`);
    const status = await statusResp.json();
    if (titleEl) titleEl.textContent = status.title || 'Meeting';
    if (dateEl) dateEl.textContent = status.date || '';
    if (durationEl) durationEl.textContent = status.duration_formatted || '';
    if (statusEl) statusEl.innerHTML = `<span class="status-badge status-${status.status}">${formatStatus(status.status)}</span>`;

    if (status.status === 'error') {
      const msg = `<div style="text-align:center;padding:40px;color:var(--red)"><p>Error: ${escHtml(status.error || 'Unknown error')}</p></div>`;
      if (transcriptEl) transcriptEl.innerHTML = msg;
      if (summaryEl) summaryEl.innerHTML = msg;
      if (actionBarEl) {
        actionBarEl.innerHTML = `<button class="action-btn retry-btn" onclick="retryMeeting('${id}')">Retry Processing</button>`;
        actionBarEl.classList.add('visible');
      }
      return;
    }

    if (status.status !== 'complete') {
      const progressInfo = status.progress_detail ? ` - ${escHtml(status.progress_detail)}` : '';
      const pctBar = status.progress_percent > 0
        ? `<div style="margin-top:12px;max-width:300px;margin-left:auto;margin-right:auto">
            <div class="progress-bar"><div class="progress-bar-fill" style="width:${status.progress_percent}%"></div></div>
            <div style="font-size:12px;color:var(--text-muted);margin-top:4px">${status.progress_percent}%${progressInfo}</div>
          </div>` : '';
      const msg = `<div style="text-align:center;padding:40px"><div class="spinner"></div> ${formatStatus(status.status)}${pctBar}</div>`;
      if (transcriptEl) transcriptEl.innerHTML = msg;
      if (summaryEl) summaryEl.innerHTML = msg;
      return;
    }

    if (actionBarEl) {
      actionBarEl.innerHTML = `
        <button class="action-btn" onclick="reprocessStep('${id}', 'cleanup')">Re-cleanup</button>
        <button class="action-btn" onclick="reprocessStep('${id}', 'identify_speakers')">Re-identify Speakers</button>
        <button class="action-btn" onclick="reprocessStep('${id}', 'summarize')">Re-summarize</button>
        <button class="action-btn" onclick="reprocessStep('${id}', 'tagging')">Re-tag</button>
      `;
      actionBarEl.classList.add('visible');
    }
  } catch (err) {
    if (transcriptEl) transcriptEl.innerHTML = `<div style="color:var(--red)">Failed to load: ${escHtml(err.message)}</div>`;
    return;
  }

  // Load content in parallel
  loadTranscript(id);
  loadSummary(id);
  loadNotes(id);
  initChat(id);
  loadTags(id);
  loadRelated(id);
  initAudioPlayer(id, mobile);

  // Download links
  const dlMap = {
    'dlTranscript': 'transcript.json',
    'dlRawTranscript': 'raw_transcript.json',
    'dlSrt': 'transcript.srt',
    'dlTranscriptMd': 'transcript.md',
    'dlSummary': 'summary.md',
  };
  for (const [elId, file] of Object.entries(dlMap)) {
    const el = $(elId + prefix);
    if (el) el.href = `${API}/meetings/${id}/files/${file}`;
  }
  if (downloadsEl) downloadsEl.style.display = 'flex';
}

async function loadTranscript(id) {
  try {
    const resp = await fetch(`${API}/meetings/${id}/transcript`);
    const data = await resp.json();
    const segments = data.segments || [];

    currentOriginalSegments = segments;
    currentSpeakerMap = {};
    currentSpeakerInfo = {};
    currentSpeakerInfoByName = {};

    try {
      const speakerResp = await fetch(`${API}/meetings/${id}/speakers`);
      if (speakerResp.ok) {
        const speakerData = await speakerResp.json();
        currentSpeakerInfo = speakerData.speaker_info || {};
        for (const [label, info] of Object.entries(currentSpeakerInfo)) {
          if (info.name) {
            currentSpeakerMap[label] = info.name;
            // Build reverse lookup so we can find info by display name too
            currentSpeakerInfoByName[info.name] = info;
          }
        }
      }
    } catch (_) {}

    renderTranscriptWithMap(segments, id);
  } catch (err) {
    const errHtml = `<div style="color:var(--red)">Failed to load transcript: ${escHtml(err.message)}</div>`;
    [$('transcriptContent'), $('transcriptContentMobile')].filter(Boolean).forEach(el => el.innerHTML = errHtml);
  }
}

async function loadSummary(id) {
  try {
    const resp = await fetch(`${API}/meetings/${id}/summary`);
    const data = await resp.json();
    renderSummary(data);
  } catch (err) {
    const errHtml = `<div style="color:var(--red)">Failed to load summary: ${escHtml(err.message)}</div>`;
    [$('summaryContent'), $('summaryContentMobile')].filter(Boolean).forEach(el => el.innerHTML = errHtml);
  }
}

async function loadTags(id) {
  try {
    const resp = await fetch(`${API}/meetings/${id}/tags`);
    const data = await resp.json();
    renderTags(data, id);
  } catch (err) {
    const errHtml = `<div style="color:var(--red)">Failed to load tags: ${escHtml(err.message)}</div>`;
    [$('tagsContent'), $('tagsContentMobile')].filter(Boolean).forEach(el => el.innerHTML = errHtml);
  }
}

let currentEditTags = {};  // current tags state for editing

function renderTags(tags, meetingId) {
  currentEditTags = JSON.parse(JSON.stringify(tags || {}));
  let html = '';

  const CATEGORIES = ['standup','planning','sprint_review','retrospective','sales','brainstorm','interview','training','one_on_one','all_hands','workshop','demo','other'];

  // Category — dropdown
  const category = tags.category || 'other';
  const options = CATEGORIES.map(c =>
    `<option value="${c}" ${c === category ? 'selected' : ''}>${c.replace(/_/g, ' ')}</option>`
  ).join('');
  html += `<div class="tags-section">
    <h3>Category</h3>
    <select class="category-select" onchange="saveTagCategory('${meetingId}', this.value)">${options}</select>
  </div>`;

  // Keywords — deletable badges + add input
  const keywords = tags.keywords || [];
  const kwBadges = keywords.map(k =>
    `<span class="tag-badge tag-badge-keyword tag-badge-delete" onclick="deleteTag('${meetingId}', 'keyword', '${escHtml(k)}')" title="Click to remove">${escHtml(k)} &times;</span>`
  ).join(' ');
  html += `<div class="tags-section">
    <h3>Keywords</h3>
    <div>${kwBadges || '<span style="color:var(--text-muted)">No keywords</span>'} <button class="tag-add-btn" onclick="showTagInput(this, '${meetingId}', 'keyword')">+ add</button></div>
  </div>`;

  // Entities — deletable badges + add input per type
  const entities = tags.entities || {};
  const entityTypes = ['people', 'companies', 'projects', 'technologies', 'dates'];

  html += `<div class="tags-section"><h3>Entities</h3>`;
  for (const etype of entityTypes) {
    const items = entities[etype] || [];
    const badges = items.map(e =>
      `<span class="tag-badge tag-badge-entity tag-badge-delete" onclick="deleteTag('${meetingId}', '${etype}', '${escHtml(e)}')" title="Click to remove">${escHtml(e)} &times;</span>`
    ).join(' ');
    html += `<div class="tags-entity-group">
      <div class="tags-entity-label">${etype}</div>
      <div>${badges || '<span style="color:var(--text-muted)">none</span>'} <button class="tag-add-btn" onclick="showTagInput(this, '${meetingId}', '${etype}')">+ add</button></div>
    </div>`;
  }
  html += '</div>';

  // Actions
  html += `<div class="tags-actions">
    <button class="action-btn" onclick="reprocessStep('${meetingId}', 'tagging')">Re-generate Tags</button>
  </div>`;

  // Related meetings
  html += `<div class="tags-section" style="margin-top:24px">
    <h3>Related Meetings</h3>
    <div id="relatedMeetings"><div class="spinner"></div> Loading...</div>
  </div>`;

  [$('tagsContent'), $('tagsContentMobile')].filter(Boolean).forEach(el => el.innerHTML = html);

  // Load related meetings async
  loadRelatedMeetings(meetingId);
}

async function loadRelatedMeetings(meetingId) {
  const relContainers = document.querySelectorAll('#relatedMeetings');
  try {
    const resp = await fetch(`${API}/meetings/${meetingId}/related?limit=5`);
    const data = await resp.json();

    if (!data.length) {
      relContainers.forEach(c => c.innerHTML = '<div style="color:var(--text-muted)">No related meetings found.</div>');
      return;
    }

    const html = data.map(r => {
      const sharedBadges = [
        ...r.shared_keywords.map(k => `<span class="tag-badge tag-badge-keyword">${escHtml(k)}</span>`),
        ...r.shared_entities.slice(0, 3).map(e => `<span class="tag-badge tag-badge-entity">${escHtml(e)}</span>`),
      ].join(' ');

      return `<div class="related-meeting-item" onclick="openMeeting('${r.meeting_id}')">
        <span class="meeting-date">${r.date || ''}</span>
        <span class="meeting-title" style="flex:1">${escHtml(r.title || 'Untitled')}</span>
        <span class="meeting-tags">${sharedBadges}</span>
        <span class="related-score">Score: ${r.score}</span>
      </div>`;
    }).join('');
    relContainers.forEach(c => c.innerHTML = html);
  } catch (err) {
    relContainers.forEach(c => c.innerHTML = `<div style="color:var(--red)">Failed to load: ${escHtml(err.message)}</div>`);
  }
}

// --- Related Tab (Phase 6) ---
async function loadRelated(meetingId) {
  const containers = [$('relatedContent'), $('relatedContentMobile')].filter(Boolean);
  if (!containers.length) return;
  containers.forEach(el => el.innerHTML = '<div style="text-align:center;padding:20px"><div class="spinner"></div> Loading links...</div>');

  try {
    const resp = await fetch(`${API}/meetings/${meetingId}/links`);
    const data = await resp.json();
    let html = '';

    // Manual links
    html += '<div class="link-section"><h3>Linked Meetings</h3>';
    if (data.manual && data.manual.length) {
      data.manual.forEach(link => {
        html += `<div class="link-item">
          <div class="link-item-info" onclick="openMeeting('${link.meeting_id}')">
            <div class="link-title">${escHtml(link.title || 'Untitled')}</div>
            <div class="link-meta">${link.date || ''} &middot; ${link.duration_formatted || ''}</div>
          </div>
          <button class="link-action-btn danger" onclick="unlinkMeeting('${meetingId}', '${link.meeting_id}')">Unlink</button>
        </div>`;
      });
    } else {
      html += '<div style="color:var(--text-muted);font-size:13px;margin-bottom:8px">No linked meetings yet.</div>';
    }
    html += `<button class="link-add-btn" onclick="showLinkPicker('${meetingId}')">+ Link a Meeting</button>`;
    if (data.manual && data.manual.length) {
      html += `<div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
        <button class="action-btn" onclick="generateInsights('${meetingId}')">New Insights</button>
        <button class="action-btn" onclick="generateInsightsWithPrompt('${meetingId}')">Insights with Custom Focus...</button>
      </div>`;
      html += `<div id="insightsGenerating" class="insights-generating" style="display:none">
        <div class="spinner"></div> Generating cross-meeting insights...
      </div>`;
      html += `<div id="insightsHistoryBar" class="insights-history-bar"></div>`;
      html += `<div id="insightsResult" style="margin-top:8px"></div>`;
    }
    html += '</div>';

    // Suggestions
    if (data.suggestions && data.suggestions.length) {
      html += '<div class="link-section"><h3>Suggested Links</h3>';
      data.suggestions.forEach(s => {
        const sharedBadges = [
          ...(s.shared_keywords || []).map(k => `<span class="tag-badge tag-badge-keyword">${escHtml(k)}</span>`),
          ...(s.shared_entities || []).slice(0, 3).map(e => `<span class="tag-badge tag-badge-entity">${escHtml(e)}</span>`),
        ].join(' ');

        // Look up title from cache
        const cached = allMeetingsCache.find(m => m.id === s.meeting_id);
        const title = cached ? cached.title : 'Meeting';
        const date = cached ? cached.date : '';

        html += `<div class="link-item">
          <div class="link-item-info" onclick="openMeeting('${s.meeting_id}')">
            <div class="link-title">${escHtml(title)}</div>
            <div class="link-meta">${date} &middot; Score: ${s.score}</div>
            <div class="link-shared-tags">${sharedBadges}</div>
          </div>
          <div style="display:flex;gap:4px;flex-shrink:0">
            <button class="link-action-btn accept" onclick="acceptSuggestion('${meetingId}', '${s.meeting_id}')">Accept</button>
            <button class="link-action-btn danger" onclick="dismissSuggestion('${meetingId}', '${s.meeting_id}')">Dismiss</button>
          </div>
        </div>`;
      });
      html += '</div>';
    }

    containers.forEach(el => el.innerHTML = html);

    // Load insights history
    if (data.manual && data.manual.length) {
      await loadInsightsHistory(meetingId);
    }

    // Append related notes block (safe: all user data run through escHtml before insertion)
    try {
      const notesResp = await fetch(`${API}/meetings/${meetingId}/related-notes`);
      const notesData = await notesResp.json();
      const relNotes = (notesData && notesData.related) || [];
      if (relNotes.length) {
        let notesHtml = '<div class="link-section"><h3>Related Notes</h3>';
        relNotes.forEach(n => {
          const noteId = escHtml(n.note_id); // escaped for safe use in onclick attribute string
          notesHtml += '<div class="link-item">'
            + '<div class="link-item-info" onclick="window.openNoteFromMeeting(\'' + noteId + '\')">'
            + '<div class="link-title">' + escHtml(n.title || n.note_id) + '</div>'
            + '<div class="link-meta">' + escHtml(n.folder || '') + (n.score != null ? ' &middot; Score: ' + n.score : '') + '</div>'
            + '</div>'
            + '</div>';
        });
        notesHtml += '</div>';
        // safe: notesHtml built entirely with escHtml-escaped values
        containers.forEach(el => el.insertAdjacentHTML('beforeend', notesHtml));
      }
    } catch (e) { /* related notes are best-effort */ }
  } catch (err) {
    const errHtml = '<div style="color:var(--red)">Failed to load links: ' + escHtml(err.message) + '</div>';
    containers.forEach(el => el.innerHTML = errHtml);
  }
}

async function acceptSuggestion(meetingId, targetId) {
  try {
    await fetch(`${API}/meetings/${meetingId}/links`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target_meeting_id: targetId }),
    });
    loadRelated(meetingId);
    pollForNewInsight(meetingId);
  } catch (err) { console.error('Accept suggestion failed:', err); }
}

async function dismissSuggestion(meetingId, targetId) {
  try {
    await fetch(`${API}/meetings/${meetingId}/links/suggestions/${targetId}/dismiss`, { method: 'POST' });
    loadRelated(meetingId);
  } catch (err) { console.error('Dismiss suggestion failed:', err); }
}

async function unlinkMeeting(meetingId, targetId) {
  try {
    await fetch(`${API}/meetings/${meetingId}/links/${targetId}`, { method: 'DELETE' });
    loadRelated(meetingId);
  } catch (err) { console.error('Unlink failed:', err); }
}

// --- Cross-Meeting Insights ---
async function generateInsights(meetingId, customPrompt) {
  const genEl = document.getElementById('insightsGenerating');
  if (genEl) genEl.style.display = 'flex';
  const containers = document.querySelectorAll('#insightsResult');

  try {
    const body = {};
    if (customPrompt) body.custom_prompt = customPrompt;

    const resp = await fetch(`${API}/meetings/${meetingId}/insights`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    if (genEl) genEl.style.display = 'none';

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      containers.forEach(el => el.innerHTML = `<div style="color:var(--red)">Failed: ${escHtml(err.detail || resp.statusText)}</div>`);
      return;
    }

    await loadInsightsHistory(meetingId);
  } catch (err) {
    if (genEl) genEl.style.display = 'none';
    containers.forEach(el => el.innerHTML = `<div style="color:var(--red)">Error: ${escHtml(err.message)}</div>`);
  }
}

function generateInsightsWithPrompt(meetingId) {
  const customPrompt = prompt(
    'What should the insights focus on? (e.g., "track progress on SOC2 audit", "summarize all action items and their owners", "identify recurring blockers")\n\nLeave empty for general insights:'
  );
  if (customPrompt === null) return;
  generateInsights(meetingId, customPrompt || undefined);
}

function renderInsights(data, meetingId) {
  const insights = data.insights || {};
  const count = data.meetings_analyzed || 0;
  let html = `<div class="insights-panel">`;
  const label = data.label || 'General';
  const ts = data.timestamp ? new Date(data.timestamp).toLocaleString() : '';
  const triggerBadge = data.trigger === 'auto_link' ? ' <span style="font-size:11px;color:var(--green)">(auto)</span>' : '';
  html += `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
    <div>
      <h3 style="color:var(--accent);margin:0">${escHtml(label)}${triggerBadge}</h3>
      <div style="font-size:11px;color:var(--text-muted)">${ts} — ${count} meetings analyzed</div>
    </div>
    ${data.id ? `<button class="link-action-btn danger" onclick="deleteInsight('${meetingId}', '${data.id}')" style="flex-shrink:0">Delete</button>` : ''}
  </div>`;

  if (insights.executive_summary) {
    html += `<div class="insights-section">
      <h4>Executive Summary</h4>
      <p>${escHtml(insights.executive_summary)}</p>
    </div>`;
  }

  if (insights.recurring_themes && insights.recurring_themes.length) {
    html += `<div class="insights-section"><h4>Recurring Themes</h4>`;
    for (const t of insights.recurring_themes) {
      html += `<div class="insights-item">
        <strong>${escHtml(t.theme || '')}</strong>
        <p>${escHtml(t.details || '')}</p>
        ${t.meetings ? `<div class="insights-meetings">${t.meetings.map(m => `<span class="tag-badge tag-badge-keyword">${escHtml(m)}</span>`).join(' ')}</div>` : ''}
      </div>`;
    }
    html += `</div>`;
  }

  if (insights.progress_tracking && insights.progress_tracking.length) {
    html += `<div class="insights-section"><h4>Progress Tracking</h4>`;
    for (const p of insights.progress_tracking) {
      const statusColor = p.status === 'completed' ? 'var(--green)' : p.status === 'stalled' ? 'var(--red)' : 'var(--yellow)';
      html += `<div class="insights-item">
        <strong>${escHtml(p.item || '')}</strong> <span style="color:${statusColor};font-size:12px">[${escHtml(p.status || '')}]</span>
        <p>${escHtml(p.history || '')}</p>
      </div>`;
    }
    html += `</div>`;
  }

  if (insights.unresolved_items && insights.unresolved_items.length) {
    html += `<div class="insights-section"><h4>Unresolved Items</h4>`;
    for (const u of insights.unresolved_items) {
      html += `<div class="insights-item">
        <strong>${escHtml(u.item || '')}</strong>
        <p>First raised: ${escHtml(u.first_raised || 'unknown')} — Status: ${escHtml(u.current_status || 'unknown')}</p>
      </div>`;
    }
    html += `</div>`;
  }

  if (insights.key_relationships && insights.key_relationships.length) {
    html += `<div class="insights-section"><h4>Key Relationships</h4>`;
    for (const r of insights.key_relationships) {
      html += `<div class="insights-item"><p>${escHtml(r.description || '')}</p></div>`;
    }
    html += `</div>`;
  }

  if (insights.recommendations && insights.recommendations.length) {
    html += `<div class="insights-section"><h4>Recommendations</h4>`;
    for (const r of insights.recommendations) {
      html += `<div class="insights-item"><p>${escHtml(r.recommendation || '')}</p></div>`;
    }
    html += `</div>`;
  }

  html += `</div>`;

  document.querySelectorAll('#insightsResult').forEach(el => el.innerHTML = html);
}

async function loadInsightsHistory(meetingId) {
  try {
    const resp = await fetch(`${API}/meetings/${meetingId}/insights`);
    if (!resp.ok) return;
    const data = await resp.json();
    const list = data.insights || [];
    insightsCache[meetingId] = { list, activeId: list.length ? list[0].id : null };
    renderInsightsChips(meetingId);
    if (list.length) {
      await loadInsightDetail(meetingId, list[0].id);
    } else {
      document.querySelectorAll('#insightsResult').forEach(el => el.innerHTML = '');
    }
  } catch (_) {}
}

function renderInsightsChips(meetingId) {
  const bar = document.getElementById('insightsHistoryBar');
  if (!bar) return;
  const cache = insightsCache[meetingId];
  if (!cache || !cache.list.length) {
    bar.innerHTML = '<div style="color:var(--text-muted);font-size:12px;padding:8px 0">No insights generated yet.</div>';
    return;
  }
  bar.innerHTML = cache.list.map(ins => {
    const active = ins.id === cache.activeId ? ' active' : '';
    const date = ins.timestamp ? new Date(ins.timestamp).toLocaleDateString() : '';
    const time = ins.timestamp ? new Date(ins.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
    const autoIcon = ins.trigger === 'auto_link' ? '<span class="chip-auto"></span>' : '';
    return `<button class="insights-chip${active}" onclick="loadInsightDetail('${meetingId}', '${ins.id}')">
      ${autoIcon}<span class="chip-label">${escHtml(ins.label)}</span>
      <span class="chip-date">${date} ${time}</span>
    </button>`;
  }).join('');
}

async function loadInsightDetail(meetingId, insightId) {
  if (insightsCache[meetingId]) {
    insightsCache[meetingId].activeId = insightId;
    renderInsightsChips(meetingId);
  }
  const containers = document.querySelectorAll('#insightsResult');
  containers.forEach(el => el.innerHTML = '<div style="text-align:center;padding:20px"><div class="spinner"></div></div>');
  try {
    const resp = await fetch(`${API}/meetings/${meetingId}/insights?insight_id=${insightId}`);
    if (!resp.ok) {
      containers.forEach(el => el.innerHTML = '<div style="color:var(--red)">Failed to load insight</div>');
      return;
    }
    const entry = await resp.json();
    renderInsights(entry, meetingId);
  } catch (err) {
    containers.forEach(el => el.innerHTML = `<div style="color:var(--red)">Error: ${escHtml(err.message)}</div>`);
  }
}

async function deleteInsight(meetingId, insightId) {
  try {
    const resp = await fetch(`${API}/meetings/${meetingId}/insights/${insightId}`, { method: 'DELETE' });
    if (!resp.ok) return;
    await loadInsightsHistory(meetingId);
  } catch (err) {
    console.error('Delete insight failed:', err);
  }
}

function pollForNewInsight(meetingId) {
  const genEl = document.getElementById('insightsGenerating');
  if (genEl) genEl.style.display = 'flex';
  let attempts = 0;
  const maxAttempts = 60;
  const existingCount = insightsCache[meetingId]?.list?.length || 0;
  const timer = setInterval(async () => {
    attempts++;
    try {
      const resp = await fetch(`${API}/meetings/${meetingId}/insights`);
      if (resp.ok) {
        const data = await resp.json();
        const newCount = (data.insights || []).length;
        if (newCount > existingCount) {
          clearInterval(timer);
          if (genEl) genEl.style.display = 'none';
          await loadInsightsHistory(meetingId);
          return;
        }
      }
    } catch (_) {}
    if (attempts >= maxAttempts) {
      clearInterval(timer);
      if (genEl) genEl.style.display = 'none';
    }
  }, 5000);
}

// --- Link Picker ---
async function showLinkPicker(meetingId) {
  linkPickerMeetingId = meetingId;
  $('linkPickerOverlay').classList.add('visible');
  $('linkPickerSearch').value = '';
  renderLinkPickerList('');
  $('linkPickerSearch').focus();

  // Refresh cache so picker always has current meetings
  try {
    const resp = await fetch(`${API}/meetings`);
    allMeetingsCache = await resp.json();
    renderLinkPickerList($('linkPickerSearch').value.trim().toLowerCase());
  } catch (_) {}
}

function closeLinkPicker() {
  $('linkPickerOverlay').classList.remove('visible');
  linkPickerMeetingId = null;
}

$('linkPickerSearch').addEventListener('input', (e) => {
  renderLinkPickerList(e.target.value.trim().toLowerCase());
});

function renderLinkPickerList(filter) {
  const list = $('linkPickerList');
  const completeMeetings = allMeetingsCache.filter(m =>
    m.status === 'complete' && m.id !== linkPickerMeetingId
  );

  const filtered = filter
    ? completeMeetings.filter(m => (m.title || '').toLowerCase().includes(filter) || (m.date || '').includes(filter))
    : completeMeetings;

  if (!filtered.length) {
    list.innerHTML = '<div style="padding:16px;color:var(--text-muted);text-align:center">No meetings found.</div>';
    return;
  }

  list.innerHTML = filtered.slice(0, 20).map(m => `
    <div class="link-picker-item" onclick="pickLink('${m.id}')">
      <span class="lpi-date">${m.date || ''}</span>
      <span class="lpi-title">${escHtml(m.title || 'Untitled')}</span>
    </div>
  `).join('');
}

async function pickLink(targetId) {
  if (!linkPickerMeetingId) return;
  try {
    await fetch(`${API}/meetings/${linkPickerMeetingId}/links`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target_meeting_id: targetId }),
    });
    closeLinkPicker();
    loadRelated(linkPickerMeetingId);
    pollForNewInsight(linkPickerMeetingId);
  } catch (err) { console.error('Link failed:', err); }
}

$('linkPickerOverlay').addEventListener('click', (e) => {
  if (e.target === $('linkPickerOverlay')) closeLinkPicker();
});

async function saveTagUpdate(meetingId, body) {
  try {
    const resp = await fetch(`${API}/meetings/${meetingId}/tags`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (resp.ok) {
      const data = await resp.json();
      renderTags(data.tags, meetingId);
      refreshMeetings();
    } else {
      const data = await resp.json().catch(() => ({}));
      console.error('Tag update failed:', data.detail);
    }
  } catch (err) {
    console.error('Tag update failed:', err);
  }
}

function saveTagCategory(meetingId, category) {
  saveTagUpdate(meetingId, { category });
}

function deleteTag(meetingId, type, value) {
  if (type === 'keyword') {
    const keywords = (currentEditTags.keywords || []).filter(k => k !== value);
    saveTagUpdate(meetingId, { keywords });
  } else {
    // Entity type
    const entities = currentEditTags.entities || {};
    const items = (entities[type] || []).filter(e => e !== value);
    saveTagUpdate(meetingId, { entities: { [type]: items } });
  }
}

function showTagInput(btn, meetingId, type) {
  if (btn.nextElementSibling && btn.nextElementSibling.classList.contains('tag-add-input')) return;
  const input = document.createElement('input');
  input.className = 'tag-add-input';
  input.placeholder = type === 'keyword' ? 'new keyword' : `new ${type.slice(0, -1)}`;
  btn.after(input);
  input.focus();

  const add = () => {
    const val = input.value.trim();
    input.remove();
    if (!val) return;
    if (type === 'keyword') {
      const keywords = [...(currentEditTags.keywords || []), val];
      saveTagUpdate(meetingId, { keywords });
    } else {
      const entities = currentEditTags.entities || {};
      const items = [...(entities[type] || []), val];
      saveTagUpdate(meetingId, { entities: { [type]: items } });
    }
  };
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); add(); }
    if (e.key === 'Escape') input.remove();
  });
  input.addEventListener('blur', add);
}

function applySpeakerMapToText(text) {
  let result = text;
  for (const [original, renamed] of Object.entries(currentSpeakerMap)) {
    if (renamed) result = result.replaceAll(original, renamed);
  }
  return result;
}

function renderSummary(s) {
  let html = '';

  // Summary (new) or Executive Summary (legacy)
  const summaryText = s.summary || s.executive_summary;
  if (summaryText) {
    html += `<div class="summary-section">
      <h3>Summary</h3>
      <p>${escHtml(applySpeakerMapToText(summaryText))}</p>
    </div>`;
  }

  // Key Topics (new: topics with outcome) or legacy key_topics
  const topics = s.topics || s.key_topics;
  if (topics && topics.length) {
    html += `<div class="summary-section"><h3>Key Topics</h3><ul>`;
    for (const t of topics) {
      const outcome = t.outcome ? ` <span style="color:var(--text-muted)">→ ${escHtml(t.outcome)}</span>` : '';
      html += `<li><strong>${escHtml(t.topic || '')}</strong>: ${escHtml(applySpeakerMapToText(t.summary || ''))}${outcome}</li>`;
    }
    html += `</ul></div>`;
  }

  // Action Items (new: task/who/deadline or legacy: description/assigned_to)
  if (s.action_items && s.action_items.length) {
    html += `<div class="summary-section"><h3>Action Items</h3><ul>`;
    for (const a of s.action_items) {
      const priorityClass = `priority-${a.priority || 'medium'}`;
      const task = a.task || a.description || '';
      const who = applySpeakerMapToText(a.who || a.assigned_to || 'Unassigned');
      const deadline = a.deadline ? ` &middot; Deadline: ${escHtml(a.deadline)}` : '';
      html += `<li><div class="action-item">
        <input type="checkbox">
        <div>
          <div>${escHtml(task)}</div>
          <div class="action-meta">
            Assigned: ${escHtml(who)}
            &middot; Priority: <span class="${priorityClass}">${a.priority || 'medium'}</span>${deadline}
          </div>
        </div>
      </div></li>`;
    }
    html += `</ul></div>`;
  }

  // Decisions
  if (s.decisions && s.decisions.length) {
    html += `<div class="summary-section"><h3>Decisions</h3><ul>`;
    for (const d of s.decisions) {
      html += `<li>
        <strong>${escHtml(applySpeakerMapToText(d.decision || ''))}</strong>
        <div class="decision-context">${escHtml(applySpeakerMapToText(d.context || ''))}</div>
      </li>`;
    }
    html += `</ul></div>`;
  }

  // Open Questions (new) or Questions Raised (legacy)
  const questions = s.open_questions || s.questions_raised;
  if (questions && questions.length) {
    html += `<div class="summary-section"><h3>Open Questions</h3><ul>`;
    for (const q of questions) {
      const badge = q.answered
        ? '<span style="color:var(--green)">[Answered]</span>'
        : '<span style="color:var(--yellow)">[Open]</span>';
      const askedBy = q.asked_by ? ` <span style="color:var(--text-muted)">(${escHtml(applySpeakerMapToText(q.asked_by))})</span>` : '';
      html += `<li>${escHtml(applySpeakerMapToText(q.question || ''))}${askedBy} ${badge}</li>`;
    }
    html += `</ul></div>`;
  }

  // Concerns & Risks (new from Pass D)
  if (s.concerns && s.concerns.length) {
    html += `<div class="summary-section"><h3>Concerns & Risks</h3><ul>`;
    for (const c of s.concerns) {
      const raisedBy = c.raised_by ? ` <span style="color:var(--text-muted)">(${escHtml(applySpeakerMapToText(c.raised_by))})</span>` : '';
      const resolvedBadge = c.resolved
        ? '<span style="color:var(--green)">[Resolved]</span>'
        : '<span style="color:var(--yellow)">[Open]</span>';
      const notes = c.notes ? `<div style="color:var(--text-secondary);font-size:13px;margin-top:2px">${escHtml(applySpeakerMapToText(c.notes))}</div>` : '';
      html += `<li>${escHtml(applySpeakerMapToText(c.concern || ''))}${raisedBy} ${resolvedBadge}${notes}</li>`;
    }
    html += `</ul></div>`;
  }

  // Key Figures & Dates (new from Pass E)
  if (s.figures && s.figures.length) {
    html += `<div class="summary-section"><h3>Key Figures & Dates</h3><ul>`;
    for (const f of s.figures) {
      const saidBy = f.said_by ? ` <span style="color:var(--text-muted)">(${escHtml(applySpeakerMapToText(f.said_by))})</span>` : '';
      html += `<li><strong>${escHtml(f.figure || '')}</strong>: ${escHtml(applySpeakerMapToText(f.context || ''))}${saidBy}</li>`;
    }
    html += `</ul></div>`;
  }

  // Sentiment (new) or sentiment_overview (legacy)
  const sentiment = s.sentiment || s.sentiment_overview;
  if (sentiment) {
    const sentClass = `sentiment-${sentiment.overall || 'neutral'}`;
    html += `<div class="summary-section"><h3>Sentiment</h3>
      <p><span class="sentiment-badge ${sentClass}">${sentiment.overall || 'N/A'}</span></p>`;
    if (sentiment.notable_moments && sentiment.notable_moments.length) {
      html += '<ul>';
      for (const m of sentiment.notable_moments) {
        if (typeof m === 'object' && m.moment) {
          html += `<li>${escHtml(applySpeakerMapToText(m.moment))} <span style="color:var(--text-muted)">— ${escHtml(m.tone || '')}</span></li>`;
        } else {
          html += `<li>${escHtml(applySpeakerMapToText(typeof m === 'string' ? m : ''))}</li>`;
        }
      }
      html += '</ul>';
    }
    html += '</div>';
  }

  const summaryHtml = html || '<div class="empty-state">No summary data.</div>';
  [$('summaryContent'), $('summaryContentMobile')].filter(Boolean).forEach(el => el.innerHTML = summaryHtml);
}

// Close detail
$('closeDetail').addEventListener('click', closeDetail);
detailOverlay.addEventListener('click', e => {
  if (e.target === detailOverlay) closeDetail();
});

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    // Close overlays in z-index order: link picker > settings > detail
    const lp = document.getElementById('linkPickerOverlay');
    if (lp && lp.classList.contains('visible')) {
      closeLinkPicker();
    } else {
      const so = document.getElementById('settingsOverlay');
      if (so && so.classList.contains('visible')) {
        if (typeof closeSettings === 'function') closeSettings();
      } else if (detailOverlay.classList.contains('visible')) {
        closeDetail();
      }
    }
  }
});

function closeDetail() {
  ['', 'Mobile'].forEach(p => {
    const a = $('meetingAudio' + p);
    if (a) { a.pause(); a.src = ''; }
    const b = $('audioPlayerBar' + p);
    if (b) b.classList.remove('visible');
  });
  audioPlayerMeetingId = null;

  // Mobile overlay
  detailOverlay.classList.remove('visible');
  document.body.style.overflow = '';

  // Desktop inline
  $('inlineDetail').style.display = 'none';
  $('mainEmptyState').style.display = 'flex';

  currentMeetingId = null;
  if (typeof updateFloatingChatScope === 'function') updateFloatingChatScope();
  // Update sidebar active state
  document.querySelectorAll('.sidebar-meeting-item').forEach(el => el.classList.remove('active'));
}

// Tabs - use event delegation for both inline and mobile tabs
document.addEventListener('click', (e) => {
  const btn = e.target.closest('.tab-btn');
  if (!btn) return;
  const tabContainer = btn.closest('.main-content-inner') || btn.closest('.detail-panel');
  if (!tabContainer) return;
  tabContainer.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  tabContainer.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
  const tabEl = tabContainer.querySelector('#tab-' + btn.dataset.tab) ||
                tabContainer.querySelector('#tab-' + btn.dataset.tab + '-mobile');
  if (tabEl) tabEl.classList.add('active');

  // Load related tab lazily
  if (btn.dataset.tab === 'related' && currentMeetingId) {
    loadRelated(currentMeetingId);
  }
  // Load notes tab lazily
  if (btn.dataset.tab === 'notes' && currentMeetingId) {
    loadNotes(currentMeetingId);
  }
});

// Clicking a transcript timestamp seeks the audio to that point
document.addEventListener('click', (e) => {
  const timeEl = e.target.closest('.seg-time');
  if (!timeEl) return;
  const seg = timeEl.closest('.transcript-segment');
  if (!seg) return;
  const t = parseFloat(seg.dataset.segStart);
  if (isNaN(t)) return;
  const isMobile = !!timeEl.closest('.detail-panel');
  const audio = $('meetingAudio' + (isMobile ? 'Mobile' : ''));
  if (!audio || !audio.src) return;
  audio.currentTime = t;
  audio.play();
  e.stopPropagation();
});

// --- Search ---
$('searchBtn').addEventListener('click', doSearch);
searchInput.addEventListener('keydown', e => { if (e.key === 'Enter') doSearch(); });
searchInput.addEventListener('focus', () => {
  $('searchFilters').classList.add('visible');
});

async function doSearch() {
  const q = searchInput.value.trim();
  if (!q) {
    searchResults.classList.remove('visible');
    return;
  }

  // Build query params with filters
  const params = new URLSearchParams({ q, limit: '10' });
  const speaker = $('searchSpeaker').value.trim();
  const dateFrom = $('searchDateFrom').value;
  const dateTo = $('searchDateTo').value;
  const chunkType = $('searchChunkType').value;
  const showContext = $('searchShowContext').checked;

  if (speaker) params.set('speaker', speaker);
  if (dateFrom) params.set('date_from', dateFrom);
  if (dateTo) params.set('date_to', dateTo);
  if (chunkType) params.set('chunk_type', chunkType);
  if (showContext) params.set('include_context', 'true');

  searchResults.classList.add('visible');
  searchResults.innerHTML = '<div style="padding:12px;color:var(--text-muted)"><div class="spinner"></div> Searching...</div>';

  try {
    const resp = await fetch(`${API}/meetings/search?${params.toString()}`);
    const data = await resp.json();

    if (resp.ok && data.length) {
      searchResults.innerHTML = data.map(r => {
        let contextHtml = '';
        if (r.context && r.context.length) {
          contextHtml = `<div class="search-context">${
            r.context.map(seg => {
              const ts = formatTimestamp(seg.start || 0);
              const sp = seg.speaker || 'UNKNOWN';
              return `<div class="search-context-seg"><strong>${ts} ${escHtml(sp)}:</strong> ${escHtml(seg.text || '')}</div>`;
            }).join('')
          }</div>`;
        }
        return `<div class="search-result-item" style="cursor:pointer" onclick="openMeeting('${r.meeting_id}')">
          <div class="search-result-meta">
            <span>${r.date || ''}</span>
            <span>${escHtml(r.title || '')}</span>
            <span>${r.chunk_type || ''}</span>
            ${r.speaker ? `<span>${escHtml(r.speaker)}</span>` : ''}
            <span>Score: ${(r.score || 0).toFixed(3)}</span>
          </div>
          <div class="search-result-text">${escHtml(r.text || '')}</div>
          ${contextHtml}
        </div>`;
      }).join('');
    } else if (resp.ok) {
      searchResults.innerHTML = '<div style="padding:16px;color:var(--text-muted)">No results found.</div>';
    } else {
      searchResults.innerHTML = `<div style="padding:16px;color:var(--red)">Search error: ${escHtml(data.detail || 'Unknown error')}</div>`;
    }
  } catch (err) {
    searchResults.innerHTML = `<div style="padding:16px;color:var(--red)">Search failed: ${escHtml(err.message)}</div>`;
  }
}

// --- Delete Meeting ---
async function deleteMeeting(id) {
  if (!confirm('Are you sure you want to delete this meeting? This cannot be undone.')) return;

  try {
    const resp = await fetch(`${API}/meetings/${id}`, { method: 'DELETE' });
    if (resp.ok) {
      if (currentMeetingId === id) closeDetail();
      refreshMeetings();
    } else {
      const data = await resp.json().catch(() => ({}));
      alert('Delete failed: ' + (data.detail || resp.statusText));
    }
  } catch (err) {
    alert('Delete failed: ' + err.message);
  }
}

$('detailDeleteBtn').addEventListener('click', () => {
  if (currentMeetingId) deleteMeeting(currentMeetingId);
});

// --- Retry & Reprocess ---
async function retryMeeting(id) {
  try {
    const resp = await fetch(`${API}/meetings/${id}/retry`, { method: 'POST' });
    if (resp.ok) {
      closeDetail();
      refreshMeetings();
      startPolling();
    } else {
      const data = await resp.json().catch(() => ({}));
      alert('Retry failed: ' + (data.detail || resp.statusText));
    }
  } catch (err) {
    alert('Retry failed: ' + err.message);
  }
}

async function reprocessStep(id, step) {
  try {
    const resp = await fetch(`${API}/meetings/${id}/reprocess`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ step }),
    });
    if (resp.ok) {
      // Refresh after a short delay to show progress
      startPolling();
      setTimeout(() => openMeeting(id), 1000);
    } else {
      const data = await resp.json().catch(() => ({}));
      alert('Reprocess failed: ' + (data.detail || resp.statusText));
    }
  } catch (err) {
    alert('Reprocess failed: ' + err.message);
  }
}

// --- Speaker Name Mapping ---
let currentSpeakerMap = {};
let currentSpeakerInfo = {};
let currentSpeakerInfoByName = {};  // reverse lookup: display name -> speaker info entry
let currentOriginalSegments = [];

function renderSpeakerMapBar(segments, meetingId) {
  const speakers = [];
  const seen = new Set();
  for (const seg of segments) {
    const sp = seg.speaker || 'UNKNOWN';
    if (!seen.has(sp)) {
      seen.add(sp);
      speakers.push(sp);
    }
  }

  if (!speakers.length) return '';

  const chips = speakers.map((sp, idx) => {
    const colorClass = `speaker-${idx % 8}`;
    const info = currentSpeakerInfo[sp] || currentSpeakerInfoByName[sp];
    const isAutoDetected = info && info.auto_detected;
    const autoStar = isAutoDetected ? '<span class="speaker-chip-auto" title="Auto-detected by AI">&#9733;</span>' : '';
    const detailParts = [];
    if (info && info.title) detailParts.push(info.title);
    if (info && info.company) detailParts.push(info.company);
    const detailText = detailParts.length ? `<span class="speaker-chip-detail">(${escHtml(detailParts.join(', '))})</span>` : '';
    const nameText = info ? info.name : (currentSpeakerMap[sp] || '');
    return `<span class="speaker-chip ${colorClass}" data-original="${escHtml(sp)}" onclick="editSpeakerName(this, '${escHtml(sp)}', '${meetingId}')">
      <span class="speaker-chip-label">${escHtml(sp)}:</span>
      <span class="speaker-chip-name">${escHtml(nameText || 'click to rename')}${detailText}${autoStar}</span>
    </span>`;
  }).join('');

  const mergeBtn = speakers.length >= 2
    ? `<button class="action-btn" style="font-size:0.75rem;padding:2px 8px;margin-left:8px" onclick="showMergeSpeakers('${meetingId}')" title="Merge two speakers into one">Merge Speakers</button>`
    : '';
  const reassignBtn = `<button class="action-btn" style="font-size:0.75rem;padding:2px 8px;margin-left:4px" onclick="toggleReassignMode('${meetingId}')" title="Click segments to reassign them to a different speaker">Reassign Segments</button>`;

  return `<div class="speaker-map-bar">${chips}${mergeBtn}${reassignBtn}</div>`;
}

function editSpeakerName(chipEl, originalName, meetingId) {
  if (chipEl.querySelector('.speaker-chip-input')) return;

  const info = currentSpeakerInfo[originalName] || currentSpeakerInfoByName[originalName];
  const currentName = currentSpeakerMap[originalName] || (info ? info.name : '') || '';
  const nameSpan = chipEl.querySelector('.speaker-chip-name');
  const oldText = nameSpan.textContent;

  const input = document.createElement('input');
  input.className = 'speaker-chip-input';
  input.value = currentName;
  input.placeholder = 'Enter name';

  nameSpan.textContent = '';
  nameSpan.appendChild(input);
  input.focus();
  input.select();

  const save = async () => {
    const newName = input.value.trim();
    if (newName) {
      currentSpeakerMap[originalName] = newName;
    } else {
      delete currentSpeakerMap[originalName];
    }

    try {
      const resp = await fetch(`${API}/meetings/${meetingId}/speakers`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ speaker_map: currentSpeakerMap }),
      });
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        console.error('Failed to save speaker names:', data.detail);
      }
    } catch (err) {
      console.error('Failed to save speaker names:', err);
    }

    renderTranscriptWithMap(currentOriginalSegments, meetingId);
    if (currentMeetingId === meetingId) {
      loadSummary(meetingId);
    }
  };

  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); save(); }
    if (e.key === 'Escape') {
      nameSpan.textContent = oldText;
    }
  });

  input.addEventListener('blur', save);
}

let reassignMode = false;
let reassignMeetingId = null;
let selectedSegmentIndices = new Set();

function showMergeSpeakers(meetingId) {
  // Collect current speakers from transcript
  const speakers = [];
  const seen = new Set();
  for (const seg of currentOriginalSegments) {
    const sp = currentSpeakerMap[seg.speaker] || seg.speaker || 'UNKNOWN';
    if (!seen.has(sp)) { seen.add(sp); speakers.push(sp); }
  }
  if (speakers.length < 2) { alert('Need at least 2 speakers to merge.'); return; }

  const srcLabel = 'Select speaker to REMOVE (will be merged into target):';
  const src = prompt(srcLabel + '\n\nSpeakers:\n' + speakers.map((s, i) => `${i + 1}. ${s}`).join('\n') + '\n\nEnter number:');
  if (!src) return;
  const srcIdx = parseInt(src) - 1;
  if (isNaN(srcIdx) || srcIdx < 0 || srcIdx >= speakers.length) return;

  const remaining = speakers.filter((_, i) => i !== srcIdx);
  const tgtLabel = 'Select TARGET speaker (keeps this name):';
  const tgt = prompt(tgtLabel + '\n\n' + remaining.map((s, i) => `${i + 1}. ${s}`).join('\n') + '\n\nEnter number:');
  if (!tgt) return;
  const tgtIdx = parseInt(tgt) - 1;
  if (isNaN(tgtIdx) || tgtIdx < 0 || tgtIdx >= remaining.length) return;

  const source = speakers[srcIdx];
  const target = remaining[tgtIdx];

  fetch(`${API}/meetings/${meetingId}/speakers/merge`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ speakers: [source, target], target }),
  }).then(r => r.json()).then(data => {
    if (data.detail) {
      // Reload the meeting to reflect changes
      loadMeeting(meetingId);
    }
  }).catch(err => console.error('Merge failed:', err));
}

function toggleReassignMode(meetingId) {
  reassignMode = !reassignMode;
  reassignMeetingId = reassignMode ? meetingId : null;
  selectedSegmentIndices.clear();

  // Update button style
  document.querySelectorAll('.transcript-segment').forEach(el => {
    el.classList.remove('seg-selected');
  });

  if (reassignMode) {
    // Add reassign toolbar
    const bar = document.querySelector('.speaker-map-bar');
    if (bar && !document.getElementById('reassign-toolbar')) {
      const toolbar = document.createElement('div');
      toolbar.id = 'reassign-toolbar';
      toolbar.style.cssText = 'display:flex;align-items:center;gap:8px;margin-top:8px;padding:8px;background:var(--surface);border-radius:8px;border:1px solid var(--accent)';
      toolbar.innerHTML = `
        <span style="font-size:0.8rem;color:var(--accent)">Click segments to select, then:</span>
        <button class="action-btn" style="font-size:0.75rem;padding:2px 8px" onclick="applyReassign()">Assign to Speaker...</button>
        <button class="action-btn" style="font-size:0.75rem;padding:2px 8px" onclick="toggleReassignMode()">Cancel</button>
        <span id="reassign-count" style="font-size:0.75rem;color:var(--text-secondary)">0 selected</span>
      `;
      bar.after(toolbar);
    }

    // Make segments clickable for selection
    document.querySelectorAll('.transcript-segment').forEach((el, idx) => {
      el.style.cursor = 'pointer';
      el.onclick = (e) => {
        e.stopPropagation();
        if (selectedSegmentIndices.has(idx)) {
          selectedSegmentIndices.delete(idx);
          el.classList.remove('seg-selected');
        } else {
          selectedSegmentIndices.add(idx);
          el.classList.add('seg-selected');
        }
        const countEl = document.getElementById('reassign-count');
        if (countEl) countEl.textContent = `${selectedSegmentIndices.size} selected`;
      };
    });
  } else {
    const toolbar = document.getElementById('reassign-toolbar');
    if (toolbar) toolbar.remove();
    document.querySelectorAll('.transcript-segment').forEach(el => {
      el.style.cursor = '';
      el.onclick = null;
    });
  }
}

function applyReassign() {
  if (!selectedSegmentIndices.size) return;

  const speakers = [];
  const seen = new Set();
  for (const seg of currentOriginalSegments) {
    const sp = currentSpeakerMap[seg.speaker] || seg.speaker || 'UNKNOWN';
    if (!seen.has(sp)) { seen.add(sp); speakers.push(sp); }
  }

  const input = prompt(
    'Assign selected segments to which speaker?\n\n' +
    speakers.map((s, i) => `${i + 1}. ${s}`).join('\n') +
    '\n\nEnter number, or type a new speaker name:'
  );
  if (!input) return;

  const num = parseInt(input);
  const newSpeaker = (!isNaN(num) && num >= 1 && num <= speakers.length)
    ? speakers[num - 1]
    : input.trim();

  if (!newSpeaker) return;

  fetch(`${API}/meetings/${reassignMeetingId}/speakers/reassign`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ segment_indices: [...selectedSegmentIndices], new_speaker: newSpeaker }),
  }).then(r => r.json()).then(data => {
    if (data.detail) {
      toggleReassignMode();
      loadMeeting(reassignMeetingId);
    }
  }).catch(err => console.error('Reassign failed:', err));
}

function applyMapToSegments(segments) {
  return segments.map(seg => {
    const sp = seg.speaker || 'UNKNOWN';
    return {
      ...seg,
      speaker: currentSpeakerMap[sp] || sp,
    };
  });
}

// --- Virtual scroll for large transcripts ---
const VIRTUAL_SCROLL_THRESHOLD = 200;
const VIRTUAL_SCROLL_BATCH = 50;

function renderTranscriptWithMap(originalSegments, meetingId) {
  const containers = [$('transcriptContent'), $('transcriptContentMobile')].filter(Boolean);
  if (!originalSegments.length) {
    containers.forEach(el => el.innerHTML = '<div class="empty-state">No transcript segments.</div>');
    return;
  }

  const mapped = applyMapToSegments(originalSegments);
  const speakerMapBarHtml = renderSpeakerMapBar(originalSegments, meetingId);

  const speakerColorMap = {};
  let speakerIdx = 0;

  // Pre-compute color map
  for (const seg of originalSegments) {
    const sp = seg.speaker || 'UNKNOWN';
    if (!(sp in speakerColorMap)) {
      speakerColorMap[sp] = speakerIdx++;
    }
  }

  function renderSegmentHtml(seg, origSeg, segIdx) {
    const speaker = seg.speaker || 'UNKNOWN';
    const origSp = origSeg.speaker || 'UNKNOWN';
    const colorClass = `speaker-${(speakerColorMap[origSp] || 0) % 8}`;
    const time = formatTimestamp(seg.start);
    const anns = getSegmentAnnotations(seg.start);
    const badge = anns.length > 0
      ? `<span class="seg-annotation-badge" onclick="event.stopPropagation();switchToNotesTab()" title="${anns.length} annotation${anns.length > 1 ? 's' : ''}">${anns.length}</span>`
      : '';
    return `<div class="transcript-segment" data-seg-start="${seg.start}">
      <span class="seg-time">${time}</span>
      <span class="seg-speaker ${colorClass}">${escHtml(speaker)}${badge}</span>
      <span class="seg-text">${escHtml(seg.text)}</span>
      <button class="seg-annotate-btn" onclick="event.stopPropagation();showAnnotationForm(${segIdx}, ${seg.start}, '${meetingId}')" title="Annotate">&#9998;</button>
    </div>`;
  }

  // Full HTML for mobile (no virtual scroll) and small transcripts
  const fullSegmentsHtml = speakerMapBarHtml + mapped.map((seg, i) =>
    renderSegmentHtml(seg, originalSegments[i], i)
  ).join('');

  // Mobile container always gets full render
  const mobileEl = $('transcriptContentMobile');
  if (mobileEl) mobileEl.innerHTML = fullSegmentsHtml;

  // Desktop: use virtual scroll for large transcripts
  const desktopEl = $('transcriptContent');
  if (!desktopEl) return;

  if (mapped.length > VIRTUAL_SCROLL_THRESHOLD) {
    desktopEl.innerHTML = speakerMapBarHtml;

    const segContainer = document.createElement('div');
    desktopEl.appendChild(segContainer);

    let renderedCount = 0;

    function renderBatch() {
      const end = Math.min(renderedCount + VIRTUAL_SCROLL_BATCH, mapped.length);
      let batchHtml = '';
      for (let i = renderedCount; i < end; i++) {
        batchHtml += renderSegmentHtml(mapped[i], originalSegments[i], i);
      }
      segContainer.insertAdjacentHTML('beforeend', batchHtml);
      renderedCount = end;

      // Remove old sentinel if any
      const oldSentinel = segContainer.querySelector('.scroll-sentinel');
      if (oldSentinel) oldSentinel.remove();

      // Add sentinel if more segments remain
      if (renderedCount < mapped.length) {
        const sentinel = document.createElement('div');
        sentinel.className = 'scroll-sentinel';
        segContainer.appendChild(sentinel);

        const observer = new IntersectionObserver((entries) => {
          if (entries[0].isIntersecting) {
            observer.disconnect();
            renderBatch();
          }
        }, { root: null, rootMargin: '200px' });
        observer.observe(sentinel);
      }
    }

    renderBatch();
  } else {
    desktopEl.innerHTML = fullSegmentsHtml;
  }
}

// --- Utilities ---
function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}

function formatBytes(b) {
  if (b < 1024) return b + ' B';
  if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
  return (b / 1048576).toFixed(1) + ' MB';
}

function formatTimestamp(sec) {
  if (!isFinite(sec) || isNaN(sec) || sec < 0) return '00:00:00';
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = Math.floor(sec % 60);
  return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
}

// ── Audio Player ──────────────────────────────────────────────────────────

async function initAudioPlayer(meetingId, mobile) {
  const p = mobile ? 'Mobile' : '';
  const bar   = $('audioPlayerBar' + p);
  const audio = $('meetingAudio' + p);
  if (!bar || !audio) return;

  // Reset
  audio.pause();
  audio.src = '';
  bar.classList.remove('visible');

  // Check whether audio exists for this meeting
  const url = `${API}/meetings/${meetingId}/audio`;
  try {
    const ac = new AbortController();
    const probe = await fetch(url, { headers: { 'Range': 'bytes=0-0' }, signal: ac.signal });
    ac.abort();
    if (!probe.ok && probe.status !== 206) return;
  } catch (_) { return; }

  audio.src = url;
  bar.classList.add('visible');
  audioPlayerMeetingId = meetingId;

  const playBtn    = $('audioPlayBtn'      + p);
  const scrubber   = $('audioScrubber'     + p);
  const curTimeEl  = $('audioCurrentTime'  + p);
  const totTimeEl  = $('audioTotalTime'    + p);
  const skipBack   = $('audioSkipBack'     + p);
  const skipFwd    = $('audioSkipFwd'      + p);
  const speedSel   = $('audioSpeedSelect'  + p);

  speedSel.value = '1';
  audio.playbackRate = 1;

  const updateDuration = () => {
    if (isFinite(audio.duration) && audio.duration > 0) {
      scrubber.max = audio.duration;
      totTimeEl.textContent = formatTimestamp(Math.floor(audio.duration));
    }
  };
  audio.onloadedmetadata = updateDuration;
  audio.ondurationchange = updateDuration;

  audio.ontimeupdate = () => {
    if (!audio.seeking) scrubber.value = audio.currentTime;
    curTimeEl.textContent = formatTimestamp(Math.floor(audio.currentTime));
    syncTranscriptToAudio(audio.currentTime, mobile);
  };

  audio.onplay  = () => { playBtn.innerHTML = '&#9646;&#9646;'; };
  audio.onpause = () => { playBtn.innerHTML = '&#9654;'; };
  audio.onended = () => { playBtn.innerHTML = '&#9654;'; };

  playBtn.onclick  = () => { audio.paused ? audio.play() : audio.pause(); };
  skipBack.onclick = () => { audio.currentTime = Math.max(0, audio.currentTime - 10); };
  skipFwd.onclick  = () => { audio.currentTime = Math.min(audio.duration || Infinity, audio.currentTime + 10); };
  scrubber.oninput = () => { audio.currentTime = parseFloat(scrubber.value); };
  speedSel.onchange = () => { audio.playbackRate = parseFloat(speedSel.value); };
}

function syncTranscriptToAudio(currentTime, mobile) {
  // Determine if Transcript tab is currently active
  const container = mobile
    ? document.querySelector('.detail-panel')
    : document.querySelector('.main-content-inner');
  const activeBtn = container && container.querySelector('.tab-btn.active');
  const transcriptActive = activeBtn && activeBtn.dataset.tab === 'transcript';

  // Re-query each call: virtual scroll adds segments progressively
  const segs = document.querySelectorAll('.transcript-segment');
  if (!segs.length) return;

  let activeEl = null;
  for (let i = segs.length - 1; i >= 0; i--) {
    const t = parseFloat(segs[i].dataset.segStart);
    if (!isNaN(t) && t <= currentTime) { activeEl = segs[i]; break; }
  }

  segs.forEach(s => s.classList.remove('audio-active'));
  if (!activeEl) return;
  activeEl.classList.add('audio-active');

  if (transcriptActive) {
    const r = activeEl.getBoundingClientRect();
    if (r.top < 80 || r.bottom > window.innerHeight - 40) {
      activeEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  }
}

// --- Notes ---
async function loadNotes(meetingId) {
  try {
    const resp = await fetch(`${API}/meetings/${meetingId}/notes`);
    const data = await resp.json();
    currentNotes = data.notes || [];
    renderNotes(meetingId);
  } catch (err) {
    currentNotes = [];
    renderNotes(meetingId);
  }
}

function renderNotes(meetingId) {
  const containers = [$('notesContent'), $('notesContentMobile')].filter(Boolean);
  const freeNotes = currentNotes.filter(n => n.type === 'free');
  const annotations = currentNotes.filter(n => n.type === 'annotation');

  const html = `
    <div class="notes-section">
      <div class="notes-section-header">
        <h3>Notes</h3>
      </div>
      <div id="noteFormSlot"></div>
      <button class="add-note-btn" onclick="showNoteForm('${meetingId}')">+ Add Note</button>
      ${freeNotes.length === 0 ? '<div class="empty-notes">No notes yet</div>' :
        freeNotes.map(n => renderNoteCard(n, meetingId)).join('')}
    </div>
    <div class="notes-section">
      <div class="notes-section-header">
        <h3>Transcript Annotations</h3>
      </div>
      ${annotations.length === 0 ? '<div class="empty-notes">No annotations yet. Click the pencil icon on transcript segments to add annotations.</div>' :
        annotations.sort((a, b) => (a.segment_start || 0) - (b.segment_start || 0))
          .map(n => renderAnnotationCard(n, meetingId)).join('')}
    </div>
  `;
  containers.forEach(c => c.innerHTML = html);
}

function renderNoteCard(note, meetingId) {
  const date = new Date(note.created_at).toLocaleString();
  return `<div class="note-card" id="note-${note.id}">
    <div class="note-card-header">
      <span class="note-meta">${date}</span>
      <div class="note-actions">
        <button onclick="editNote('${meetingId}', '${note.id}')" title="Edit">Edit</button>
        <button onclick="deleteNote('${meetingId}', '${note.id}')" title="Delete">Del</button>
      </div>
    </div>
    <div class="note-content" id="note-content-${note.id}">${escHtml(note.content)}</div>
  </div>`;
}

function renderAnnotationCard(note, meetingId) {
  const date = new Date(note.created_at).toLocaleString();
  const time = note.segment_start != null ? formatTimestamp(note.segment_start) : '';
  return `<div class="note-card" id="note-${note.id}">
    <div class="note-card-header">
      <span class="note-meta">${date}</span>
      <div class="note-actions">
        <button onclick="editNote('${meetingId}', '${note.id}')" title="Edit">Edit</button>
        <button onclick="deleteNote('${meetingId}', '${note.id}')" title="Delete">Del</button>
      </div>
    </div>
    <div class="note-content" id="note-content-${note.id}">${escHtml(note.content)}</div>
    ${time ? `<div class="note-segment-ref" onclick="jumpToTranscriptTime(${note.segment_start})">@ ${time}</div>` : ''}
  </div>`;
}

function showNoteForm(meetingId, existingNoteId) {
  const existing = existingNoteId ? currentNotes.find(n => n.id === existingNoteId) : null;
  const slot = document.getElementById('noteFormSlot');
  if (!slot) return;
  slot.innerHTML = `<div class="note-form">
    <textarea id="noteFormText" placeholder="Type your note...">${existing ? escHtml(existing.content) : ''}</textarea>
    <div class="note-form-actions">
      <button class="note-save-btn" onclick="${existing ? `saveEditNote('${meetingId}', '${existingNoteId}')` : `createNote('${meetingId}')`}">${existing ? 'Save' : 'Add'}</button>
      <button class="note-cancel-btn" onclick="document.getElementById('noteFormSlot').innerHTML=''">Cancel</button>
    </div>
  </div>`;
  document.getElementById('noteFormText').focus();
}

async function createNote(meetingId) {
  const text = document.getElementById('noteFormText');
  if (!text || !text.value.trim()) return;
  try {
    await fetch(`${API}/meetings/${meetingId}/notes`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type: 'free', content: text.value.trim() }),
    });
    loadNotes(meetingId);
  } catch (err) { console.error('Failed to create note:', err); }
}

function editNote(meetingId, noteId) {
  showNoteForm(meetingId, noteId);
}

async function saveEditNote(meetingId, noteId) {
  const text = document.getElementById('noteFormText');
  if (!text || !text.value.trim()) return;
  try {
    await fetch(`${API}/meetings/${meetingId}/notes/${noteId}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: text.value.trim() }),
    });
    loadNotes(meetingId);
  } catch (err) { console.error('Failed to update note:', err); }
}

async function deleteNote(meetingId, noteId) {
  if (!confirm('Delete this note?')) return;
  try {
    await fetch(`${API}/meetings/${meetingId}/notes/${noteId}`, { method: 'DELETE' });
    loadNotes(meetingId);
  } catch (err) { console.error('Failed to delete note:', err); }
}

function getSegmentAnnotations(segStart) {
  return currentNotes.filter(n => n.type === 'annotation' && Math.abs((n.segment_start || 0) - segStart) < 0.5);
}

function showAnnotationForm(segIndex, segStart, meetingId) {
  // Remove any existing annotation form
  document.querySelectorAll('.annotation-inline-form').forEach(el => el.remove());
  const segments = document.querySelectorAll('.transcript-segment');
  const seg = segments[segIndex];
  if (!seg) return;
  const form = document.createElement('div');
  form.className = 'annotation-inline-form';
  form.innerHTML = `<textarea id="annotationFormText" placeholder="Add annotation for this segment..."></textarea>
    <div class="note-form-actions">
      <button class="note-save-btn" onclick="createAnnotation('${meetingId}', ${segStart}, ${segIndex})">Add</button>
      <button class="note-cancel-btn" onclick="this.closest('.annotation-inline-form').remove()">Cancel</button>
    </div>`;
  seg.after(form);
  form.querySelector('textarea').focus();
}

async function createAnnotation(meetingId, segStart, segIndex) {
  const text = document.getElementById('annotationFormText');
  if (!text || !text.value.trim()) return;
  try {
    await fetch(`${API}/meetings/${meetingId}/notes`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type: 'annotation', content: text.value.trim(), segment_start: segStart, segment_index: segIndex }),
    });
    document.querySelectorAll('.annotation-inline-form').forEach(el => el.remove());
    loadNotes(meetingId);
    // Reload transcript to show badges
    loadTranscript(meetingId);
  } catch (err) { console.error('Failed to create annotation:', err); }
}

function switchToNotesTab() {
  const tabContainer = document.querySelector('.main-content-inner') || document.querySelector('.detail-panel');
  if (!tabContainer) return;
  tabContainer.querySelectorAll('.tab-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === 'notes');
  });
  tabContainer.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  const notesTab = tabContainer.querySelector('#tab-notes') || tabContainer.querySelector('#tab-notes-mobile');
  if (notesTab) notesTab.classList.add('active');
  if (currentMeetingId) loadNotes(currentMeetingId);
}

function jumpToTranscriptTime(segStart) {
  // Switch to transcript tab
  const tabContainer = document.querySelector('.main-content-inner') || document.querySelector('.detail-panel');
  if (!tabContainer) return;
  tabContainer.querySelectorAll('.tab-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === 'transcript');
  });
  tabContainer.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  const transcriptTab = tabContainer.querySelector('#tab-transcript') || tabContainer.querySelector('#tab-transcript-mobile');
  if (transcriptTab) transcriptTab.classList.add('active');

  // Find and scroll to the segment
  const segments = document.querySelectorAll('.transcript-segment');
  for (const seg of segments) {
    const timeEl = seg.querySelector('.seg-time');
    if (!timeEl) continue;
    // Parse time string back to seconds for approximate match
    const parts = timeEl.textContent.split(':').map(Number);
    const t = (parts[0] || 0) * 3600 + (parts[1] || 0) * 60 + (parts[2] || 0);
    if (Math.abs(t - segStart) < 1) {
      seg.scrollIntoView({ behavior: 'smooth', block: 'center' });
      seg.style.background = 'var(--accent-dim)';
      setTimeout(() => seg.style.background = '', 2000);
      return;
    }
  }
}


// --- Chat ---
async function initChat(meetingId) {
  chatHistory = [];
  chatScope = { scope: 'meeting', meeting_id: meetingId };
  if (chatAbortController) { chatAbortController.abort(); chatAbortController = null; }

  const containers = [$('chatContent'), $('chatContentMobile')].filter(Boolean);
  if (!containers.length) return;

  // Build scope options based on meeting data
  let scopeOptions = [{ label: 'This Meeting', scope: 'meeting', meeting_id: meetingId }];

  try {
    const statusResp = await fetch(`${API}/meetings/${meetingId}/status`);
    const status = await statusResp.json();
    const meeting = status;

    // Check for linked meetings
    try {
      const linksResp = await fetch(`${API}/meetings/${meetingId}/links`);
      const linksData = await linksResp.json();
      const manualCount = (linksData.manual || []).length;
      if (manualCount > 0) {
        scopeOptions.push({ label: `Linked (${manualCount + 1})`, scope: 'linked', meeting_id: meetingId });
      }
    } catch (e) {}

    // Check for category
    try {
      const tagsResp = await fetch(`${API}/meetings/${meetingId}/tags`);
      const tags = await tagsResp.json();
      if (tags.category && tags.category !== 'other') {
        scopeOptions.push({ label: `Category: ${tags.category}`, scope: 'category', category: tags.category, meeting_id: meetingId });
      }
    } catch (e) {}

    scopeOptions.push({ label: 'All Meetings', scope: 'global' });
  } catch (e) {
    scopeOptions.push({ label: 'All Meetings', scope: 'global' });
  }

  const scopeBarHtml = `<div class="chat-scope-bar">${scopeOptions.map((opt, i) =>
    `<button class="chat-scope-btn${i === 0 ? ' active' : ''}" data-scope-idx="${i}" onclick="setChatScope(${i})">${escHtml(opt.label)}</button>`
  ).join('')}</div>`;

  const chatHtml = `<div class="chat-container">
    ${scopeBarHtml}
    <div class="chat-messages" id="chatMessages">
      <div class="chat-empty">Ask a question about this meeting's content</div>
    </div>
    <div class="chat-input-area">
      <textarea id="chatInput" placeholder="Ask about this meeting..." rows="1"
        onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendChatMessage();}"></textarea>
      <button class="chat-send-btn" id="chatSendBtn" onclick="sendChatMessage()">Send</button>
    </div>
  </div>`;

  containers.forEach(c => c.innerHTML = chatHtml);

  // Store scope options globally
  window._chatScopeOptions = scopeOptions;
}

function setChatScope(idx) {
  const options = window._chatScopeOptions || [];
  if (!options[idx]) return;
  chatScope = options[idx];
  document.querySelectorAll('.chat-scope-btn').forEach((btn, i) => {
    btn.classList.toggle('active', i === idx);
  });
}

async function sendChatMessage() {
  const input = document.getElementById('chatInput');
  const sendBtn = document.getElementById('chatSendBtn');
  if (!input || !input.value.trim()) return;

  const message = input.value.trim();
  input.value = '';
  input.style.height = 'auto';

  // Add user message
  chatHistory.push({ role: 'user', content: message });
  appendChatMessage('user', message);

  // Clear empty state
  const empty = document.querySelector('.chat-empty');
  if (empty) empty.remove();

  // Disable input during streaming
  if (sendBtn) sendBtn.disabled = true;
  input.disabled = true;

  // Create assistant bubble
  const assistantEl = appendChatMessage('assistant', '');
  const contentEl = assistantEl.querySelector('.chat-msg-text');
  const badgeEl = assistantEl.querySelector('.chat-context-badge');

  // Start streaming
  chatAbortController = new AbortController();
  let fullResponse = '';

  try {
    const resp = await fetch(`${API}/meetings/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message,
        context: chatScope || { scope: 'meeting', meeting_id: currentMeetingId },
        history: chatHistory.slice(0, -1).slice(-20), // last 20 messages, excluding the just-added user msg
      }),
      signal: chatAbortController.signal,
    });

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try {
          const event = JSON.parse(line.slice(6));
          if (event.type === 'context') {
            if (badgeEl) badgeEl.textContent = `${event.chunks_used} chunks from ${event.meetings_searched} meeting${event.meetings_searched !== 1 ? 's' : ''}`;
          } else if (event.type === 'token') {
            fullResponse += event.content;
            if (contentEl) contentEl.textContent = fullResponse;
            // Auto-scroll
            const msgs = document.getElementById('chatMessages');
            if (msgs) msgs.scrollTop = msgs.scrollHeight;
          } else if (event.type === 'error') {
            if (contentEl) contentEl.textContent = `Error: ${event.content}`;
          } else if (event.type === 'done') {
            break;
          }
        } catch (e) {}
      }
    }
  } catch (err) {
    if (err.name !== 'AbortError') {
      if (contentEl) contentEl.textContent = `Error: ${err.message}`;
    }
  }

  if (fullResponse) {
    chatHistory.push({ role: 'assistant', content: fullResponse });
  }

  chatAbortController = null;
  if (sendBtn) sendBtn.disabled = false;
  input.disabled = false;
  input.focus();
}

function appendChatMessage(role, content) {
  const msgs = document.getElementById('chatMessages');
  if (!msgs) return null;
  const div = document.createElement('div');
  div.className = `chat-msg ${role}`;
  if (role === 'assistant') {
    div.innerHTML = `<div class="chat-msg-text">${escHtml(content)}</div><div class="chat-context-badge"></div>`;
  } else {
    div.innerHTML = `<div class="chat-msg-text">${escHtml(content)}</div>`;
  }
  msgs.appendChild(div);
  msgs.scrollTop = msgs.scrollHeight;
  return div;
}


// --- Settings ---
let settingsData = null;   // loaded from server
let settingsDefaults = null;
let settingsDirty = false;

const settingsOverlay = $('settingsOverlay');
const promptFields = {
  cleanup_system: $('promptCleanup'),
  speaker_id: $('promptSpeakerId'),
  analysis_pass_a: $('promptPassA'),
  analysis_pass_b: $('promptPassB'),
  analysis_pass_c: $('promptPassC'),
  analysis_pass_d: $('promptPassD'),
  analysis_pass_e: $('promptPassE'),
  analysis_pass_f: $('promptPassF'),
  analysis_pass_g: $('promptPassG'),
  chunk_summary: $('promptChunkSummary'),
};

$('settingsBtn').addEventListener('click', openSettings);
$('closeSettings').addEventListener('click', closeSettings);
$('settingsCancel').addEventListener('click', closeSettings);
settingsOverlay.addEventListener('click', e => {
  if (e.target === settingsOverlay) closeSettings();
});

// Track unsaved changes
function markSettingsDirty() {
  settingsDirty = true;
  $('settingsUnsaved').classList.add('visible');
}

Object.values(promptFields).forEach(ta => ta.addEventListener('input', markSettingsDirty));
$('settingsModel').addEventListener('input', markSettingsDirty);
$('settingsTemp').addEventListener('input', () => {
  $('tempDisplay').textContent = parseFloat($('settingsTemp').value).toFixed(2);
  markSettingsDirty();
});

// Chat settings listeners
function toggleChatCustomFields() {
  const custom = $('chatCustomFields');
  if (custom) custom.classList.toggle('visible', $('chatEndpoint').value === 'custom');
}
$('chatEndpoint').addEventListener('change', () => { toggleChatCustomFields(); markSettingsDirty(); });
$('chatModel').addEventListener('input', markSettingsDirty);
$('chatCustomUrl').addEventListener('input', markSettingsDirty);
$('chatCustomKey').addEventListener('input', markSettingsDirty);
$('chatTemp').addEventListener('input', () => {
  $('chatTempDisplay').textContent = parseFloat($('chatTemp').value).toFixed(2);
  markSettingsDirty();
});
$('chatMaxChunks').addEventListener('input', markSettingsDirty);
$('chatSystemPrompt').addEventListener('input', markSettingsDirty);
$('chatResetSystemPrompt').addEventListener('click', () => {
  if (settingsDefaults && settingsDefaults.chat) {
    $('chatSystemPrompt').value = settingsDefaults.chat.system_prompt || '';
    markSettingsDirty();
  }
});

// SMTP / email settings listeners
function toggleSmtpFields() {
  const fields = $('smtpFields');
  if (fields) fields.classList.toggle('visible', $('smtpEnabled').checked);
}
$('smtpEnabled').addEventListener('change', () => { toggleSmtpFields(); markSettingsDirty(); });
['smtpHost', 'smtpPort', 'smtpUsername', 'smtpPassword', 'smtpFromEmail',
 'smtpFromName', 'smtpReplyTo', 'smtpRecipients'].forEach(id => {
  $(id).addEventListener('input', markSettingsDirty);
});
$('smtpSecure').addEventListener('change', markSettingsDirty);
$('smtpTestBtn').addEventListener('click', async () => {
  const status = $('smtpTestStatus');
  if (settingsDirty) {
    status.style.color = 'var(--yellow, #b8860b)';
    status.textContent = 'Save your settings first, then send a test.';
    return;
  }
  status.style.color = 'var(--text-secondary)';
  status.textContent = 'Sending…';
  try {
    const resp = await fetch(`${API}/api/settings/test-email`, { method: 'POST' });
    const data = await resp.json().catch(() => ({}));
    if (resp.ok) {
      status.style.color = 'var(--green, #2e7d32)';
      status.textContent = data.detail || 'Test email sent.';
    } else {
      status.style.color = 'var(--red, #c62828)';
      status.textContent = data.detail || 'Failed to send test email.';
    }
  } catch (err) {
    status.style.color = 'var(--red, #c62828)';
    status.textContent = 'Failed: ' + err.message;
  }
});

// Per-prompt reset buttons
document.querySelectorAll('.settings-reset-prompt-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const key = btn.dataset.prompt;
    if (settingsDefaults && settingsDefaults.prompts[key] !== undefined) {
      promptFields[key].value = settingsDefaults.prompts[key];
      markSettingsDirty();
    }
  });
});

// Reset all
$('settingsResetAll').addEventListener('click', async () => {
  if (!confirm('Reset all LLM settings to defaults?')) return;
  try {
    const resp = await fetch(`${API}/api/settings/reset`, { method: 'POST' });
    if (resp.ok) {
      const data = await resp.json();
      populateSettingsForm(data.settings, settingsDefaults);
      settingsDirty = false;
      $('settingsUnsaved').classList.remove('visible');
    }
  } catch (err) {
    alert('Reset failed: ' + err.message);
  }
});

// Save
$('settingsSave').addEventListener('click', async () => {
  const body = {
    prompts: {},
    ollama_model: $('settingsModel').value.trim(),
    temperature: parseFloat($('settingsTemp').value),
    chat: {
      endpoint: $('chatEndpoint').value,
      model: $('chatModel').value.trim(),
      custom_url: $('chatCustomUrl').value.trim(),
      custom_api_key: $('chatCustomKey').value.trim(),
      temperature: parseFloat($('chatTemp').value),
      max_context_chunks: parseInt($('chatMaxChunks').value) || 15,
      system_prompt: $('chatSystemPrompt').value,
    },
    smtp: {
      enabled: $('smtpEnabled').checked,
      host: $('smtpHost').value.trim(),
      port: parseInt($('smtpPort').value) || 587,
      secure: $('smtpSecure').checked,
      username: $('smtpUsername').value.trim(),
      password: $('smtpPassword').value,
      from_email: $('smtpFromEmail').value.trim(),
      from_name: $('smtpFromName').value.trim(),
      reply_to: $('smtpReplyTo').value.trim(),
      recipients: $('smtpRecipients').value.trim(),
    },
  };
  for (const [key, ta] of Object.entries(promptFields)) {
    body.prompts[key] = ta.value;
  }

  try {
    const resp = await fetch(`${API}/api/settings`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (resp.ok) {
      settingsDirty = false;
      $('settingsUnsaved').classList.remove('visible');
      closeSettings();
    } else {
      const data = await resp.json().catch(() => ({}));
      alert('Save failed: ' + (data.detail || resp.statusText));
    }
  } catch (err) {
    alert('Save failed: ' + err.message);
  }
});

async function openSettings() {
  settingsOverlay.classList.add('visible');
  document.body.style.overflow = 'hidden';
  settingsDirty = false;
  $('settingsUnsaved').classList.remove('visible');

  // Load current settings from server
  try {
    const resp = await fetch(`${API}/api/settings`);
    const data = await resp.json();
    settingsData = data.settings;
    settingsDefaults = data.defaults;
    populateSettingsForm(settingsData, settingsDefaults);
  } catch (err) {
    $('settingsBody').innerHTML = `<div style="color:var(--red);padding:20px">Failed to load settings: ${escHtml(err.message)}</div>`;
  }
}

function populateSettingsForm(settings, defaults) {
  $('settingsModel').value = settings.ollama_model || '';
  $('settingsTemp').value = settings.temperature || 0.3;
  $('tempDisplay').textContent = parseFloat(settings.temperature || 0.3).toFixed(2);

  for (const [key, ta] of Object.entries(promptFields)) {
    ta.value = (settings.prompts && settings.prompts[key]) || '';
  }

  // Chat settings
  const chat = settings.chat || {};
  $('chatEndpoint').value = chat.endpoint || 'ollama';
  $('chatModel').value = chat.model || '';
  $('chatCustomUrl').value = chat.custom_url || '';
  $('chatCustomKey').value = chat.custom_api_key || '';
  $('chatTemp').value = chat.temperature || 0.5;
  $('chatTempDisplay').textContent = parseFloat(chat.temperature || 0.5).toFixed(2);
  $('chatMaxChunks').value = chat.max_context_chunks || 15;
  $('chatSystemPrompt').value = chat.system_prompt || '';
  toggleChatCustomFields();

  // SMTP / email settings
  const smtp = settings.smtp || {};
  $('smtpEnabled').checked = smtp.enabled === true;
  $('smtpHost').value = smtp.host || '';
  $('smtpPort').value = smtp.port || 587;
  $('smtpSecure').checked = smtp.secure === true;
  $('smtpUsername').value = smtp.username || '';
  $('smtpPassword').value = smtp.password || '';
  $('smtpFromEmail').value = smtp.from_email || '';
  $('smtpFromName').value = smtp.from_name || 'Meeting Service';
  $('smtpReplyTo').value = smtp.reply_to || '';
  $('smtpRecipients').value = smtp.recipients || '';
  $('smtpTestStatus').textContent = '';
  toggleSmtpFields();
}

function closeSettings() {
  if (settingsDirty) {
    if (!confirm('You have unsaved changes. Discard?')) return;
  }
  settingsOverlay.classList.remove('visible');
  document.body.style.overflow = '';
}

// --- Unsaved recording recovery on page load ---
(async function checkRecovery() {
  const entry = await _loadRecordingBackup();
  if (!entry) return;
  const banner = $('recoveryBanner');
  const loadBtn = $('recoveryLoadBtn');
  const dismissBtn = $('recoveryDismissBtn');
  banner.hidden = false;

  loadBtn.addEventListener('click', () => {
    const file = new File([entry.blob], entry.fileName, { type: entry.mimeType });
    selectFile(file);
    stagedFromRecording = true;   // keep it protected until uploaded
    banner.hidden = true;
  });

  dismissBtn.addEventListener('click', () => {
    _clearRecordingBackup();
    banner.hidden = true;
  });
})();

// ---------------------------------------------------------------------------
// Unsaved-recording guards: warn before leaving / navigating away while a
// recording is in progress or staged-but-not-uploaded. The recording is also
// autosaved (IndexedDB), so it's recoverable even if they proceed.
// ---------------------------------------------------------------------------
function isCapturing() {
  return !!(mediaRecorder && (mediaRecorder.state === 'recording' || mediaRecorder.state === 'paused'));
}

// Returns true if it's OK to navigate away (no risk, or user confirmed).
// Exposed for the pillar nav (notes-tasks.js) and any other in-app navigation.
window.captureGuardConfirm = function () {
  if (isCapturing()) {
    return confirm('A recording is still in progress. It keeps running in the background and is auto-saved — switch anyway?');
  }
  if (stagedFromRecording) {
    return confirm('You have a recording that hasn’t been uploaded yet. It’s saved and can be recovered later — leave it for now?');
  }
  return true;
};
window.isCapturing = isCapturing;

window.addEventListener('beforeunload', (e) => {
  if (isCapturing() || stagedFromRecording) {
    e.preventDefault();
    e.returnValue = '';   // triggers the browser's native "leave site?" prompt
    return '';
  }
});

// --- Init ---
refreshGroupedView();
startPolling();
setTimeout(() => {
  fetch(`${API}/meetings`).then(r => r.json()).then(data => {
    allMeetingsCache = data;
    const inProgress = data.some(m => !['complete', 'error'].includes(m.status));
    if (!inProgress) stopPolling();
  }).catch(() => {});
}, 6000);
