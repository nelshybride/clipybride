const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  tab: 'file',
  file: null,
  source: null,
  count: 5,
  length: 35,
  aspect: '9:16',
  layout: 'fill',
  camBox: null,          // zone caméra en fractions de la source : {x, y, w}
  camScale: 0.3,         // largeur de référence pour les préréglages
  camDetected: null,
  subtitles: false,
  subtitleY: 0.78,       // centre du bloc de sous-titres, 0 (haut) .. 1 (bas)
  whisper: false,
  uploading: false,
  jobId: null,
  lastJobId: null,
  autoPublish: false,
  publish: null,        // statut des plateformes (GET /api/publish/status)
};

const CORNER_LABELS = {
  'top-left': 'en haut à gauche',
  'top-right': 'en haut à droite',
  'bottom-left': 'en bas à gauche',
  'bottom-right': 'en bas à droite',
};

const SETTINGS_KEY = 'clipstudio:settings';
const VIDEO_EXT = /\.(mp4|mov|mkv|webm|m4v)$/i;

/* ------------------------------------------------------------------ */
/* Utilitaires                                                         */
/* ------------------------------------------------------------------ */

function fmtTime(value) {
  if (!Number.isFinite(value) || value < 0) return '—';
  const total = Math.round(value);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h
    ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
    : `${m}:${String(s).padStart(2, '0')}`;
}

function fmtSize(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return '—';
  if (bytes === 0) return '0 Ko';
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(2)} Go`;
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} Mo`;
  return `${Math.max(1, Math.round(bytes / 1024))} Ko`;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function toast(message, type = 'info') {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = message;
  $('#toasts').append(el);
  setTimeout(() => {
    el.classList.add('out');
    setTimeout(() => el.remove(), 250);
  }, 4200);
}

async function api(path, options) {
  let res;
  try {
    res = await fetch(path, options);
  } catch {
    throw new Error('Serveur injoignable. Relance start.bat puis réessaie.');
  }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { /* réponse non JSON */ }
  if (!res.ok) throw new Error((data && data.detail) || `Erreur serveur (${res.status}).`);
  return data;
}

/* ------------------------------------------------------------------ */
/* Progression                                                         */
/* ------------------------------------------------------------------ */

function setBar(container, bar, pct, ratio) {
  container.classList.remove('hidden');
  if (ratio == null) {
    container.classList.add('indeterminate');
    bar.style.width = '';
    pct.textContent = '…';
  } else {
    container.classList.remove('indeterminate');
    const percent = Math.max(0, Math.min(1, ratio));
    bar.style.width = `${(percent * 100).toFixed(1)}%`;
    pct.textContent = `${Math.round(percent * 100)} %`;
  }
}

const uploadUI = {
  show(label) {
    $('#uploadLabel').textContent = label;
    setBar($('#uploadProgress'), $('#uploadBar'), $('#uploadPct'), null);
  },
  progress(ratio) {
    const total = state.file?.size || 0;
    $('#uploadLabel').textContent = `Envoi de la vidéo… ${fmtSize(ratio * total)} / ${fmtSize(total)}`;
    setBar($('#uploadProgress'), $('#uploadBar'), $('#uploadPct'), ratio);
  },
  hide() {
    $('#uploadProgress').classList.add('hidden');
  },
};

const jobUI = {
  show(label, ratio) {
    $('#jobLabel').textContent = label;
    setBar($('#jobProgress'), $('#jobBar'), $('#jobPct'), ratio);
  },
  hide() {
    $('#jobProgress').classList.add('hidden');
  },
};

/* ------------------------------------------------------------------ */
/* Import                                                              */
/* ------------------------------------------------------------------ */

function uploadVideo(file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/upload', true);
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.setRequestHeader('X-Filename', encodeURIComponent(file.name));
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.loaded / event.total);
    };
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText || 'null'); } catch { /* ignore */ }
      if (xhr.status >= 200 && xhr.status < 300 && data) resolve(data);
      else reject(new Error((data && data.detail) || `Échec de l'envoi (${xhr.status}).`));
    };
    xhr.onerror = () => reject(new Error("Connexion interrompue pendant l'envoi."));
    xhr.send(file);
  });
}

function refreshImportBtn() {
  const url = $('#urlInput').value.trim();
  const ready = state.tab === 'file' ? !!state.file : url.length > 10;
  $('#importBtn').disabled = !ready || state.uploading;
  $('#importBtn').setAttribute('aria-busy', state.uploading ? 'true' : 'false');
}

function pickFile(file) {
  if (!file) return;
  if (!VIDEO_EXT.test(file.name)) {
    toast('Format non pris en charge (MP4, MOV, MKV, WebM).', 'error');
    return;
  }
  state.file = file;
  $('#dzTitle').textContent = file.name;
  $('#dzHint').textContent = `${fmtSize(file.size)} · prêt à importer`;
  $('#dropzone').classList.add('has-file');
  refreshImportBtn();
}

async function importSource() {
  if (state.uploading) return;

  if (state.tab === 'file') {
    if (!state.file) return toast('Choisis une vidéo d’abord.', 'error');
    state.uploading = true;
    refreshImportBtn();
    $('#importLabel').textContent = 'Envoi en cours…';
    uploadUI.show('Envoi de la vidéo…');
    try {
      const source = await uploadVideo(state.file, uploadUI.progress);
      onSourceReady(source);
      toast('Vidéo importée.', 'success');
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      state.uploading = false;
      $('#importLabel').textContent = 'Importer la vidéo';
      uploadUI.hide();
      refreshImportBtn();
    }
    return;
  }

  const url = $('#urlInput').value.trim();
  if (!url) return toast('Colle une URL YouTube, Twitch ou Kick.', 'error');
  state.uploading = true;
  refreshImportBtn();
  $('#importLabel').textContent = 'Téléchargement…';
  uploadUI.show('Téléchargement de la vidéo…');
  try {
    const source = await api('/api/url', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    onSourceReady(source);
    toast('Vidéo importée.', 'success');
  } catch (err) {
    toast(err.message, 'error');
  } finally {
    state.uploading = false;
    $('#importLabel').textContent = 'Importer la vidéo';
    uploadUI.hide();
    refreshImportBtn();
  }
}

function onSourceReady(source) {
  state.source = source;
  $('#importArea').classList.add('hidden');
  $('#workspace').classList.remove('hidden');
  $('#resultsCard').classList.add('hidden');
  $('#methodBadge').textContent = 'automatique';

  const video = $('#sourceVideo');
  video.src = source.video_url;
  video.poster = source.poster || '';
  video.load();

  $('#sourceName').textContent = source.filename || 'Vidéo importée';
  $('#sourceDuration').textContent = fmtTime(source.duration);
  $('#sourceSize').textContent = fmtSize(source.size);
  $('#sourceRes').textContent = source.width && source.height
    ? `${source.width}×${source.height}`
    : '—';

  state.camDetected = null;
  state.camBox = null;
  applySettings();
  if (state.layout === 'split') ensureCamBox();

  window.scrollTo({ top: 0, behavior: 'smooth' });
  refreshStorage();
}

function backToImport() {
  state.source = null;
  const video = $('#sourceVideo');
  video.pause();
  video.removeAttribute('src');
  video.load();
  $('#workspace').classList.add('hidden');
  $('#resultsCard').classList.add('hidden');
  $('#importArea').classList.remove('hidden');
  refreshImportBtn();
}

/* ------------------------------------------------------------------ */
/* Génération                                                          */
/* ------------------------------------------------------------------ */

function renderSkeletons(count) {
  $('#clipGrid').dataset.aspect = state.aspect;
  $('#clipGrid').innerHTML = Array.from({ length: count }).map(() => `
    <article class="clip skeleton">
      <div class="thumb"></div>
      <div class="clip-body"><div class="sk-line"></div><div class="sk-line short"></div></div>
    </article>`).join('');
}

function clipCard(clip) {
  return `
    <article class="clip">
      <button class="thumb" type="button" data-clip="${escapeHtml(clip.url)}" data-title="${escapeHtml(clip.name)}"
              aria-label="Lire ${escapeHtml(clip.name)}">
        ${clip.poster ? `<img src="${escapeHtml(clip.poster)}" alt="" loading="lazy" onerror="this.remove()">` : ''}
        <span class="play" aria-hidden="true">▶</span>
        <span class="dur">${clip.duration}s</span>
      </button>
      <div class="clip-body">
        <div class="clip-title">
          <b>${escapeHtml(clip.name)}</b>
          ${clip.subtitles ? '<span class="tag">ST</span>' : ''}${clip.split ? '<span class="tag">CAM</span>' : ''}
          <button class="copy" type="button" data-time="${clip.start}" title="Copier le timecode"
                  aria-label="Copier le timecode">⧉</button>
        </div>
        <div class="clip-sub">début ${fmtTime(clip.start)}</div>
        <a class="btn ghost small" href="${escapeHtml(clip.url)}" download="${escapeHtml(clip.name)}">Télécharger</a>
      </div>
    </article>`;
}

function renderClips(clips) {
  $('#clipGrid').dataset.aspect = state.aspect;
  $('#clipGrid').innerHTML = clips.map(clipCard).join('');
  $('#resultsCount').textContent = clips.length ? `· ${clips.length}` : '';
}

// Ajoute seulement les clips nouveaux : ceux déjà affichés ne sont pas rechargés.
function syncClips(clips) {
  const grid = $('#clipGrid');
  const shown = $$('.clip:not(.skeleton)', grid).length;
  if (!clips.length) return renderClips(clips);
  if (clips.length < shown) return renderClips(clips);
  const fresh = clips.slice(shown);
  if (fresh.length) {
    $$('.clip.skeleton', grid).slice(0, fresh.length).forEach((el) => el.remove());
    grid.insertAdjacentHTML('beforeend', fresh.map(clipCard).join(''));
  }
  $('#resultsCount').textContent = `· ${clips.length}`;
}

function finishJob(job) {
  const clips = job.clips || [];
  syncClips(clips);
  // La vidéo peut être trop courte pour tous les clips demandés : on retire
  // les vignettes d'attente restantes.
  $$('.clip.skeleton', $('#clipGrid')).forEach((el) => el.remove());
  $('#resultsCount').textContent = `· ${clips.length}`;
  $('#resultsNote').textContent = job.note
    ? `${job.note} Vérifie les passages avant publication.`
    : `Méthode : ${job.method}. Vérifie les passages avant publication.`;
  $('#methodBadge').textContent = job.method === 'transcription' ? 'transcription' : 'répartition';
  if (job.zip_url) {
    $('#zipBtn').href = job.zip_url;
    $('#zipBtn').classList.remove('hidden');
  }
  toast(`${clips.length} clip(s) prêt(s).`, 'success');
  $('#resultsCard').scrollIntoView({ behavior: 'smooth', block: 'start' });
  refreshStorage();
  maybeAutoPublish();
}

async function pollJob(id) {
  while (state.jobId === id) {
    const job = await api(`/api/jobs/${id}`);
    jobUI.show(job.message || 'Encodage…', job.progress || 0);
    if (job.clips?.length) syncClips(job.clips);
    if (job.status === 'done') return finishJob(job);
    if (job.status === 'error') throw new Error(job.error || 'Traitement impossible.');
    await new Promise((resolve) => setTimeout(resolve, 600));
  }
}

async function generate() {
  if (!state.source || state.jobId) return;
  const btn = $('#generateBtn');
  btn.disabled = true;
  $('#generateLabel').textContent = 'Génération…';
  $('#resultsCard').classList.remove('hidden');
  $('#resultsNote').textContent = 'Analyse de la vidéo puis encodage : chaque clip s’affiche dès qu’il est prêt.';
  $('#resultsCount').textContent = '';
  $('#zipBtn').classList.add('hidden');
  renderSkeletons(state.count);
  jobUI.show('Analyse de la vidéo…', 0);

  try {
    const { job_id: jobId } = await api('/api/process', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        source_id: state.source.source_id,
        clip_count: state.count,
        clip_length: state.length,
        aspect: state.aspect,
        subtitles: state.subtitles && state.whisper,
        subtitle_y: state.subtitleY,
        layout: state.layout,
        cam_corner: 'auto',
        cam_scale: state.camScale,
        cam_box: state.layout === 'split' && state.camBox ? roundBox(state.camBox) : null,
      }),
    });
    state.jobId = jobId;
    state.lastJobId = jobId;
    await pollJob(jobId);
  } catch (err) {
    toast(err.message, 'error');
    $('#resultsCard').classList.add('hidden');
  } finally {
    state.jobId = null;
    btn.disabled = false;
    $('#generateLabel').textContent = 'Générer mes clips';
    jobUI.hide();
  }
}

/* ------------------------------------------------------------------ */
/* Lecteur                                                             */
/* ------------------------------------------------------------------ */

function openClip(url, title) {
  const modal = $('#modal');
  const video = $('#modalVideo');
  $('#modalTitle').textContent = title || 'Clip';
  $('#modalDownload').href = url;
  $('#modalDownload').setAttribute('download', title || 'clip.mp4');
  video.src = url;
  modal.classList.remove('hidden');
  video.play().catch(() => { /* lecture auto bloquée : contrôles manuels */ });
}

function closeClip() {
  const video = $('#modalVideo');
  if (video) {
    video.pause();
    video.removeAttribute('src');
    video.load();
  }
  $('#modal').classList.add('hidden');
}

/* ------------------------------------------------------------------ */
/* Publication (YouTube / TikTok)                                      */
/* ------------------------------------------------------------------ */

async function loadPublish() {
  try {
    state.publish = await api('/api/publish/status');
    renderPublish();
  } catch { /* serveur occupé : on réessaiera */ }
}

function renderPublish() {
  const providers = state.publish?.providers || {};
  $('#pubProviders').innerHTML = Object.entries(providers).map(([key, p]) => `
    <div class="pub-row" data-provider="${key}">
      <b>${escapeHtml(p.label)}</b>
      <span class="pub-state ${p.connected ? 'ok' : p.configured ? 'warn' : ''}">${
        p.connected ? 'connecté' : p.configured ? 'à connecter' : 'identifiants manquants'}</span>
      ${p.configured
        ? `<button class="btn ghost small" type="button" data-connect="${key}">Connecter</button>`
        : `<button class="link-btn" type="button" data-setup="${key}">Configurer</button>`}
    </div>
    <div class="pub-setup hidden" data-form="${key}">
      <input class="text-input" data-cid="${key}" autocomplete="off"
             placeholder="${key === 'youtube' ? 'Client ID Google' : 'Client Key TikTok'}" />
      <input class="text-input" type="password" data-secret="${key}" autocomplete="off"
             placeholder="Client Secret" />
      <button class="btn primary small" type="button" data-save="${key}">Enregistrer</button>
      <p class="hint">URL de redirection à déclarer chez la plateforme :
        <code>${escapeHtml(`${location.origin}/api/oauth/${key}/callback`)}</code></p>
    </div>`).join('');
  const connected = firstConnected();
  $('#pubHint').textContent = connected
    ? `${state.publish.providers[connected].label} est prêt : les clips partent dès la fin de l'encodage.`
    : 'Connecte une plateforme pour publier sans repasser par le site.';
}

function firstConnected() {
  const providers = state.publish?.providers || {};
  return Object.keys(providers).find((key) => providers[key].connected) || null;
}

async function savePublishCredentials(provider) {
  const clientId = $(`[data-cid="${provider}"]`).value.trim();
  const secret = $(`[data-secret="${provider}"]`).value.trim();
  try {
    state.publish = await api(`/api/publish/credentials/${provider}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_id: clientId, client_secret: secret }),
    });
    renderPublish();
    toast('Identifiants enregistrés sur cet ordinateur.', 'success');
  } catch (err) {
    toast(err.message, 'error');
  }
}

async function startPublishConnect(provider) {
  try {
    const { url } = await api(`/api/publish/auth/${provider}`, { method: 'POST' });
    window.open(url, '_blank', 'noopener');
    toast('Termine la connexion dans l’onglet ouvert, puis reviens ici.', 'info');
    const timer = setInterval(async () => {
      await loadPublish();
      if (state.publish?.providers?.[provider]?.connected) {
        clearInterval(timer);
        toast(`${state.publish.providers[provider].label} connecté.`, 'success');
      }
    }, 3000);
    setTimeout(() => clearInterval(timer), 180000);
  } catch (err) {
    toast(err.message, 'error');
  }
}

function renderPublishJob(data) {
  const el = $('#pubStatus');
  if (!data || !data.clips) {
    el.textContent = '';
    return;
  }
  const label = state.publish?.providers?.[data.provider]?.label || data.provider;
  const parts = data.clips.map((clip) => {
    const mark = clip.state === 'publié' ? '✓' : clip.state === 'échec' ? '✗' : '…';
    const name = clip.name.replace(/\.mp4$/, '');
    return `${mark} ${name}${clip.error ? ` (${clip.error})` : ''}`;
  });
  el.textContent = `${label} · ${data.status} — ${parts.join(' · ')}`;
  el.classList.toggle('err', data.status === 'partiel');
}

async function publishJob(provider) {
  if (!state.lastJobId) return toast('Génère les clips d’abord.', 'error');
  try {
    const data = await api(`/api/publish/${state.lastJobId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider, privacy: $('#publishPrivacy').value }),
    });
    renderPublishJob(data);
    pollPublish();
  } catch (err) {
    toast(err.message, 'error');
  }
}

async function pollPublish() {
  const id = state.lastJobId;
  for (let i = 0; i < 900 && id === state.lastJobId; i += 1) {
    let data;
    try {
      data = await api(`/api/publish/${id}`);
    } catch {
      return;
    }
    renderPublishJob(data);
    if (data.status === 'terminé' || data.status === 'partiel') {
      toast(data.status === 'terminé' ? 'Clips publiés.' : 'Publication partielle : vérifie les erreurs.',
            data.status === 'terminé' ? 'success' : 'error');
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
}

function maybeAutoPublish() {
  if (!state.autoPublish) return;
  const provider = firstConnected();
  if (!provider) {
    toast('Publication auto : connecte YouTube ou TikTok d’abord.', 'error');
    return;
  }
  publishJob(provider);
}

/* ------------------------------------------------------------------ */
/* Réglages                                                            */
/* ------------------------------------------------------------------ */

function applySettings() {
  $('#countValue').textContent = state.count;
  $('#countRange').value = String(state.count);
  $('#lengthValue').textContent = `${state.length} s`;
  $('#lengthRange').value = String(state.length);
  $$('#aspects .aspect').forEach((b) => b.classList.toggle('active', b.dataset.value === state.aspect));

  $$('#layouts .aspect').forEach((b) => b.classList.toggle('active', b.dataset.value === state.layout));
  const split = state.layout === 'split';
  $('#camOptions').classList.toggle('hidden', !split);
  renderCamBox();

  $('#autoPublish').checked = state.autoPublish;

  const subs = $('#subsToggle');
  subs.disabled = !state.whisper;
  subs.checked = state.subtitles && state.whisper;
  $('#subPlace').classList.toggle('hidden', !subs.checked);
  renderSubPreview();
  if (!state.whisper) {
    $('#subsHint').textContent = 'Indisponible : lance setup.ps1 pour installer la reconnaissance vocale';
  } else if (state.subtitles) {
    $('#subsHint').textContent = 'La voix est transcrite puis incrustée dans chaque clip';
  } else {
    $('#subsHint').textContent = 'Transcription locale de la voix, sans internet';
  }
}

/* Sous-titres : position réglée en glissant le texte sur un aperçu 9:16. */

const SUB_MIN_Y = 0.08;
const SUB_MAX_Y = 0.78;   // juste au-dessus de la bande « interface TikTok »

function renderSubPreview() {
  const box = $('#subBox');
  if (box) box.style.top = `${(state.subtitleY * 100).toFixed(1)}%`;
  const pct = $('#subPct');
  if (pct) pct.textContent = `${Math.round(state.subtitleY * 100)} %`;
  const frame = $('#subFrame');
  const poster = state.source?.poster;
  if (frame && poster && frame.getAttribute('src') !== poster) frame.src = poster;
}

function setupSubBox() {
  const stage = $('#subStage');
  let dragging = false;

  const place = (event) => {
    const rect = stage.getBoundingClientRect();
    if (!rect.height) return;
    const y = (event.clientY - rect.top) / rect.height;
    state.subtitleY = Math.min(SUB_MAX_Y, Math.max(SUB_MIN_Y, y));
    renderSubPreview();
  };

  const stop = (event) => {
    if (!dragging) return;
    dragging = false;
    try {
      if (stage.hasPointerCapture(event.pointerId)) stage.releasePointerCapture(event.pointerId);
    } catch { /* rien à relâcher */ }
    saveSettings();
  };

  stage.addEventListener('pointerdown', (event) => {
    dragging = true;
    try { stage.setPointerCapture(event.pointerId); } catch { /* pointeur déjà relâché */ }
    place(event);
    event.preventDefault();
  });
  stage.addEventListener('pointermove', (event) => { if (dragging) place(event); });
  stage.addEventListener('pointerup', stop);
  stage.addEventListener('pointercancel', stop);
  stage.addEventListener('keydown', (event) => {
    const step = event.key === 'ArrowUp' ? -0.02 : event.key === 'ArrowDown' ? 0.02 : 0;
    if (!step) return;
    event.preventDefault();
    state.subtitleY = Math.min(SUB_MAX_Y, Math.max(SUB_MIN_Y, state.subtitleY + step));
    renderSubPreview();
    saveSettings();
  });
}

/* Zone caméra : cadre libre posé sur une image de la source. */

const BAND_RATIO = 1080 / 960; // largeur / hauteur de la bande caméra

function sourceRatio() {
  const width = Number(state.source?.width) || 16;
  const height = Number(state.source?.height) || 9;
  return width / height;
}

// Hauteur du cadre (fraction de la source) pour une largeur donnée.
function camBoxHeight(width) {
  return (width * sourceRatio()) / BAND_RATIO;
}

function clampCamBox(box) {
  const maxWidth = Math.min(1, BAND_RATIO / sourceRatio());
  const width = Math.min(Math.max(box.w, 0.06), maxWidth);
  const height = camBoxHeight(width);
  return {
    w: width,
    x: Math.min(Math.max(box.x, 0), 1 - width),
    y: Math.min(Math.max(box.y, 0), 1 - height),
  };
}

function cornerCamBox(corner, width = state.camScale) {
  const box = clampCamBox({ w: width, x: 0, y: 0 });
  if (corner.endsWith('right')) box.x = 1 - box.w;
  if (corner.startsWith('bottom')) box.y = 1 - camBoxHeight(box.w);
  return box;
}

function roundBox(box) {
  return { x: Number(box.x.toFixed(4)), y: Number(box.y.toFixed(4)), w: Number(box.w.toFixed(4)) };
}

function frameAt() {
  const duration = Number(state.source?.duration) || 4;
  return Math.min(Math.max(duration * 0.3, 0.4), 30).toFixed(2);
}

function renderCamBox() {
  const el = $('#camBox');
  if (!el) return;
  if (!state.camBox || state.layout !== 'split') {
    el.classList.add('hidden');
    return;
  }
  el.classList.remove('hidden');
  el.style.left = `${state.camBox.x * 100}%`;
  el.style.top = `${state.camBox.y * 100}%`;
  el.style.width = `${state.camBox.w * 100}%`;
}

function loadCamFrame() {
  if (!state.source) return;
  const url = `/api/frame/${state.source.source_id}?at=${frameAt()}`;
  const img = $('#camFrame');
  if (img.dataset.url === url) return;
  img.dataset.url = url;
  img.src = url;
}

function camPreviewUrl() {
  if (!state.source || !state.camBox) return '';
  const params = new URLSearchParams({
    layout: 'split',
    x: state.camBox.x.toFixed(4),
    y: state.camBox.y.toFixed(4),
    w: state.camBox.w.toFixed(4),
    at: frameAt(),
  });
  return `/api/preview/${state.source.source_id}?${params}`;
}

let previewTimer = null;

// Pendant le glissement l'aperçu se rafraîchit en continu (~4 fois/s),
// et pas seulement quand on relâche.
function schedulePreview() {
  renderCamBox();
  if (previewTimer) return;
  previewTimer = setTimeout(() => {
    previewTimer = null;
    refreshCamPreview();
  }, 220);
}

async function refreshCamPreview() {
  if (!state.source || state.layout !== 'split') return;
  if (!state.camBox) state.camBox = cornerCamBox('bottom-right');
  renderCamBox();
  loadCamFrame();
  const hint = $('#camPreviewHint');
  if (hint) hint.textContent = 'Aperçu du rendu final : caméra en haut, gameplay en bas.';
  $('#camPreview').src = camPreviewUrl();
}

async function detectCamera({ silent = false } = {}) {
  if (!state.source) return null;
  const hint = $('#camPreviewHint');
  if (hint) hint.textContent = 'Détection de la caméra…';
  try {
    const data = await api(`/api/cam/${state.source.source_id}`);
    const rect = data.detected?.rect || data.candidate?.rect;
    state.camDetected = data.detected;
    state.camBox = rect ? clampCamBox({ x: rect.x, y: rect.y, w: rect.w })
                        : cornerCamBox('bottom-right');
    if (!silent) {
      toast(data.detected
        ? `Caméra détectée ${CORNER_LABELS[data.detected.corner]}. Ajuste le cadre si besoin.`
        : 'Pas de caméra nette : le cadre est un point de départ, place-le à la main.',
      data.detected ? 'success' : 'info');
    }
    await refreshCamPreview();
    return data.detected;
  } catch (err) {
    state.camBox = state.camBox || cornerCamBox('bottom-right');
    if (!silent) toast(err.message, 'error');
    await refreshCamPreview();
    return null;
  }
}

async function ensureCamBox() {
  if (!state.source) return;
  loadCamFrame();
  if (!state.camBox) await detectCamera({ silent: true });
  else await refreshCamPreview();
}

function setupCamBox() {
  const stage = $('#camStage');
  const box = $('#camBox');
  if (!stage || !box) return;
  let drag = null;

  const point = (event) => {
    const rect = stage.getBoundingClientRect();
    return {
      x: (event.clientX - rect.left) / Math.max(1, rect.width),
      y: (event.clientY - rect.top) / Math.max(1, rect.height),
    };
  };

  stage.addEventListener('pointerdown', (event) => {
    if (!state.camBox || event.button !== 0) return;
    event.preventDefault();
    const p = point(event);
    const onHandle = event.target === $('#camHandle');
    if (onHandle) {
      drag = { mode: 'resize', anchorX: state.camBox.x };
    } else if (box.contains(event.target)) {
      drag = { mode: 'move', startX: p.x, startY: p.y, box: { ...state.camBox } };
    } else {
      // Clic dans la source : le cadre se centre sur le clic, puis suit le doigt.
      const width = state.camBox.w;
      const next = clampCamBox({ w: width, x: p.x - width / 2, y: p.y - camBoxHeight(width) / 2 });
      state.camBox = next;
      drag = { mode: 'move', startX: p.x, startY: p.y, box: { ...next } };
    }
    try { stage.setPointerCapture(event.pointerId); } catch { /* pointeur déjà relâché */ }
    schedulePreview();
  });

  stage.addEventListener('pointermove', (event) => {
    if (!drag) return;
    const p = point(event);
    if (drag.mode === 'move') {
      state.camBox = clampCamBox({
        w: drag.box.w,
        x: drag.box.x + (p.x - drag.startX),
        y: drag.box.y + (p.y - drag.startY),
      });
    } else {
      state.camBox = clampCamBox({ w: p.x - drag.anchorX, x: drag.anchorX, y: state.camBox.y });
    }
    schedulePreview();
  });

  const stop = (event) => {
    if (!drag) return;
    drag = null;
    clearTimeout(previewTimer);
    previewTimer = null;
    try {
      if (stage.hasPointerCapture(event.pointerId)) stage.releasePointerCapture(event.pointerId);
    } catch { /* rien à relâcher */ }
    refreshCamPreview();
  };
  stage.addEventListener('pointerup', stop);
  stage.addEventListener('pointercancel', stop);
}

function saveSettings() {
  try {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify({
      count: state.count, length: state.length, aspect: state.aspect,
      layout: state.layout, camScale: state.camScale,
      subtitles: state.subtitles, subtitleY: state.subtitleY,
      autoPublish: state.autoPublish,
    }));
  } catch { /* stockage indisponible */ }
}

function restoreSettings() {
  try {
    const saved = JSON.parse(localStorage.getItem(SETTINGS_KEY) || 'null');
    if (saved) {
      if (Number.isFinite(saved.count)) state.count = Math.max(1, Math.min(20, Math.round(saved.count)));
      if (Number.isFinite(saved.length)) state.length = saved.length;
      if (saved.aspect === '9:16' || saved.aspect === '16:9') state.aspect = saved.aspect;
      if (saved.layout === 'fill' || saved.layout === 'split') state.layout = saved.layout;
      if (Number.isFinite(saved.camScale)) state.camScale = saved.camScale;
      state.subtitles = !!saved.subtitles;
      if (Number.isFinite(saved.subtitleY)) state.subtitleY = Math.min(0.78, Math.max(0.08, saved.subtitleY));
      state.autoPublish = !!saved.autoPublish;
    }
  } catch { /* ignore */ }
  applySettings();
}

/* ------------------------------------------------------------------ */
/* Twitch                                                              */
/* ------------------------------------------------------------------ */

const twitch = { status: null, channels: [], active: null, period: 'week', clips: [] };
const fmtViews = (value) => new Intl.NumberFormat('fr-FR').format(Number(value) || 0);

async function loadTwitch(force = false) {
  try {
    const status = await api('/api/twitch/status');
    twitch.status = status;
    twitch.channels = status.channels || [];
    renderTwitchStatus();
    if (!status.configured) return;
    if (!twitch.active || !twitch.channels.some((c) => c.login === twitch.active)) {
      twitch.active = twitch.channels[0]?.login || null;
    }
    renderChannels();
    if (twitch.active && (force || !twitch.clips.length)) await loadTwitchClips();
  } catch (err) {
    toast(err.message, 'error');
  }
}

function renderTwitchStatus() {
  const configured = !!twitch.status?.configured;
  $('#twitchSetup').classList.toggle('hidden', configured);
  $('#twitchManager').classList.toggle('hidden', !configured);
  const note = $('#twitchNote');
  if (!configured) {
    note.textContent = 'Connecte ton application Twitch pour lister les clips d’une chaîne.';
  } else if (!twitch.status.valid) {
    note.textContent = twitch.status.error || 'Identifiants Twitch invalides.';
  } else {
    note.textContent = `Connecté (${twitch.status.source}). Les identifiants restent sur cet ordinateur.`;
  }
  renderChannels();
}

function renderChannels() {
  const list = $('#channelList');
  if (!twitch.channels.length) {
    list.innerHTML = '<p class="hint">Aucune chaîne pour le moment. Ajoute une chaîne pour voir ses clips.</p>';
    $('#twitchGrid').innerHTML = '';
    return;
  }
  list.innerHTML = twitch.channels.map((channel) => `
    <div class="channel-chip ${channel.login === twitch.active ? 'active' : ''}">
      ${channel.avatar ? `<img src="${escapeHtml(channel.avatar)}" alt="">` : ''}
      <button class="channel-name" type="button" data-select="${escapeHtml(channel.login)}">${escapeHtml(channel.name || channel.login)}</button>
      <button class="channel-remove" type="button" data-remove="${escapeHtml(channel.login)}"
              aria-label="Retirer ${escapeHtml(channel.name || channel.login)}">✕</button>
    </div>`).join('');
}

async function loadTwitchClips() {
  if (!twitch.active) return;
  $('#twitchGrid').innerHTML = '<p class="hint">Chargement des clips…</p>';
  try {
    const data = await api(`/api/twitch/clips/${encodeURIComponent(twitch.active)}?period=${twitch.period}&limit=24`);
    twitch.clips = data.clips || [];
    renderTwitchClips();
  } catch (err) {
    $('#twitchGrid').innerHTML = '';
    toast(err.message, 'error');
  }
}

function renderTwitchClips() {
  const grid = $('#twitchGrid');
  if (!twitch.clips.length) {
    grid.innerHTML = '<p class="hint">Aucun clip sur cette période.</p>';
    return;
  }
  grid.innerHTML = twitch.clips.map((clip) => `
    <article class="tclip">
      <div class="tclip-thumb">
        ${clip.thumbnail ? `<img src="${escapeHtml(clip.thumbnail)}" alt="" loading="lazy" onerror="this.remove()">` : ''}
        <span class="dur">${fmtTime(Number(clip.duration))}</span>
      </div>
      <div class="tclip-body">
        <b title="${escapeHtml(clip.title)}">${escapeHtml(clip.title)}</b>
        <div class="tclip-meta">${fmtViews(clip.views)} vues · ${escapeHtml(clip.creator || '—')}</div>
        <div class="tclip-actions">
          <button class="btn primary small" type="button" data-import="${escapeHtml(clip.mp4 || '')}"
                  data-title="${escapeHtml(clip.title)}" ${clip.mp4 ? '' : 'disabled'}>Découper</button>
          ${clip.page ? `<a class="btn ghost small" href="${escapeHtml(clip.page)}" target="_blank" rel="noreferrer">Twitch</a>` : ''}
        </div>
      </div>
    </article>`).join('');
}

async function addChannel() {
  const input = $('#channelInput');
  const login = input.value.trim();
  if (!login) return;
  const btn = $('#channelAdd');
  btn.disabled = true;
  try {
    const channel = await api('/api/twitch/channels', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ login }),
    });
    twitch.channels.push(channel);
    twitch.active = channel.login;
    input.value = '';
    renderChannels();
    await loadTwitchClips();
    toast(`Chaîne ${channel.name} ajoutée.`, 'success');
  } catch (err) {
    toast(err.message, 'error');
  } finally {
    btn.disabled = false;
  }
}

async function removeChannel(login) {
  try {
    const data = await api(`/api/twitch/channels/${encodeURIComponent(login)}`, { method: 'DELETE' });
    twitch.channels = data.channels || [];
    if (twitch.active === login) {
      twitch.active = twitch.channels[0]?.login || null;
      twitch.clips = [];
    }
    renderChannels();
    if (twitch.active) await loadTwitchClips();
    else $('#twitchGrid').innerHTML = '';
  } catch (err) {
    toast(err.message, 'error');
  }
}

async function saveTwitchCredentials() {
  const clientId = $('#twitchId').value.trim();
  const secret = $('#twitchSecret').value.trim();
  if (!clientId || !secret) return toast('Client ID et Client Secret requis.', 'error');
  const btn = $('#twitchSave');
  btn.disabled = true;
  btn.textContent = 'Vérification…';
  try {
    await api('/api/twitch/credentials', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_id: clientId, client_secret: secret }),
    });
    $('#twitchSecret').value = '';
    toast('Application Twitch connectée.', 'success');
    await loadTwitch(true);
  } catch (err) {
    toast(err.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Enregistrer';
  }
}

async function importTwitchClip(button) {
  const url = button.dataset.import;
  if (!url) return;
  const label = button.textContent;
  button.disabled = true;
  button.textContent = 'Téléchargement…';
  try {
    const source = await api('/api/twitch/import', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, title: button.dataset.title || 'Clip Twitch' }),
    });
    onSourceReady(source);
    toast('Clip Twitch importé.', 'success');
  } catch (err) {
    toast(err.message, 'error');
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

/* ------------------------------------------------------------------ */
/* Stockage                                                            */
/* ------------------------------------------------------------------ */

const STORAGE_LABELS = {
  outputs: 'Clips générés',
  uploads: 'Vidéos importées',
  thumbs: 'Vignettes',
};

async function refreshStorage() {
  try {
    const data = await api('/api/storage');
    const total = data.uploads.bytes + data.outputs.bytes + data.thumbs.bytes;
    $('#chipStorage').innerHTML = `<i class="dot ${total ? 'ok' : ''}"></i> Stockage ${fmtSize(total)}`;
    return data;
  } catch {
    $('#chipStorage').innerHTML = '<i class="dot warn"></i> Stockage —';
    return null;
  }
}

async function openStorage() {
  $('#storageModal').classList.remove('hidden');
  $('#storageRows').innerHTML = '<p class="hint">Calcul…</p>';
  const data = await refreshStorage();
  if (!data) {
    $('#storageRows').innerHTML = '<p class="hint">Impossible de lire le stockage.</p>';
    return;
  }
  $('#storageRows').innerHTML = Object.entries(STORAGE_LABELS).map(([key, label]) => `
    <label class="storage-row">
      <input type="checkbox" data-target="${key}" ${data[key].files ? 'checked' : 'disabled'}>
      <span class="storage-name">${label}</span>
      <span class="storage-size">${fmtSize(data[key].bytes)} · ${data[key].files} fichier(s)</span>
    </label>`).join('');
}

async function purgeStorage() {
  const targets = $$('#storageRows input[data-target]:checked').map((input) => input.dataset.target);
  if (!targets.length) return toast('Sélectionne au moins une catégorie.', 'error');
  const days = $('#olderThan').value;
  const btn = $('#storagePurge');
  btn.disabled = true;
  try {
    const data = await api('/api/storage/purge', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ targets, older_than_days: days ? Number(days) : null }),
    });
    toast(`${fmtSize(data.freed_bytes)} libérés (${data.deleted} élément(s)).`, 'success');
    if (targets.includes('uploads')) {
      backToImport();
      $('#resultsCard').classList.add('hidden');
      state.clips = [];
    }
    await openStorage();
  } catch (err) {
    toast(err.message, 'error');
  } finally {
    btn.disabled = false;
  }
}

/* ------------------------------------------------------------------ */
/* Écouteurs                                                           */
/* ------------------------------------------------------------------ */

function bind() {
  $$('.tab').forEach((tab) => tab.addEventListener('click', () => {
    state.tab = tab.dataset.tab;
    $$('.tab').forEach((t) => {
      const active = t === tab;
      t.classList.toggle('active', active);
      t.setAttribute('aria-selected', String(active));
    });
    $('#filePane').classList.toggle('hidden', state.tab !== 'file');
    $('#urlPane').classList.toggle('hidden', state.tab !== 'url');
    $('#twitchPane').classList.toggle('hidden', state.tab !== 'twitch');
    $('#importBtn').classList.toggle('hidden', state.tab === 'twitch');
    refreshImportBtn();
    if (state.tab === 'twitch') loadTwitch();
  }));

  const dz = $('#dropzone');
  const input = $('#fileInput');
  // preventDefault évite que le <label> ouvre deux fois la boîte de dialogue.
  dz.addEventListener('click', (event) => {
    event.preventDefault();
    input.click();
  });
  dz.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      input.click();
    }
  });
  input.addEventListener('change', () => pickFile(input.files[0]));
  ['dragenter', 'dragover'].forEach((type) => dz.addEventListener(type, (event) => {
    event.preventDefault();
    dz.classList.add('over');
  }));
  ['dragleave', 'drop'].forEach((type) => dz.addEventListener(type, (event) => {
    event.preventDefault();
    dz.classList.remove('over');
  }));
  dz.addEventListener('drop', (event) => pickFile(event.dataTransfer.files[0]));

  // Empêche le navigateur d'ouvrir un fichier lâché à côté de la zone.
  ['dragover', 'drop'].forEach((type) => window.addEventListener(type, (event) => {
    if (!dz.contains(event.target)) event.preventDefault();
  }));

  $('#urlInput').addEventListener('input', refreshImportBtn);
  $('#urlInput').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') importSource();
  });
  $('#importBtn').addEventListener('click', importSource);
  $('#changeBtn').addEventListener('click', backToImport);
  $('#generateBtn').addEventListener('click', generate);

  $('#countRange').addEventListener('input', (event) => {
    state.count = Math.max(1, Math.min(20, Number(event.target.value) || 1));
    applySettings();
    saveSettings();
  });

  $('#lengthRange').addEventListener('input', (event) => {
    state.length = Number(event.target.value);
    applySettings();
    saveSettings();
  });

  $('#aspects').addEventListener('click', (event) => {
    const btn = event.target.closest('.aspect');
    if (!btn) return;
    state.aspect = btn.dataset.value;
    applySettings();
    saveSettings();
  });

  $('#layouts').addEventListener('click', (event) => {
    const btn = event.target.closest('.aspect');
    if (!btn) return;
    state.layout = btn.dataset.value;
    applySettings();
    saveSettings();
    if (state.layout === 'split') ensureCamBox();
  });

  $('#camDetect').addEventListener('click', () => {
    state.camDetected = null;
    detectCamera();
  });

  setupCamBox();

  $('#pubProviders').addEventListener('click', (event) => {
    const setup = event.target.closest('[data-setup]');
    if (setup) {
      $(`[data-form="${setup.dataset.setup}"]`).classList.toggle('hidden');
      return;
    }
    const save = event.target.closest('[data-save]');
    if (save) {
      savePublishCredentials(save.dataset.save);
      return;
    }
    const connect = event.target.closest('[data-connect]');
    if (connect) startPublishConnect(connect.dataset.connect);
  });

  $('#publishNow').addEventListener('click', () => {
    const provider = firstConnected();
    if (!provider) return toast('Connecte YouTube ou TikTok d’abord.', 'error');
    publishJob(provider);
  });

  $('#autoPublish').addEventListener('change', (event) => {
    state.autoPublish = event.target.checked;
    saveSettings();
  });

  $('#subsToggle').addEventListener('change', (event) => {
    state.subtitles = event.target.checked && state.whisper;
    applySettings();
    saveSettings();
    if (state.subtitles) toast('Sous-titres activés : la voix sera transcrite avant l’encodage.', 'success');
  });

  setupSubBox();

  $('#clipGrid').addEventListener('click', (event) => {
    const thumb = event.target.closest('.thumb[data-clip]');
    if (thumb) {
      openClip(thumb.dataset.clip, thumb.dataset.title);
      return;
    }
    const copy = event.target.closest('.copy');
    if (copy) {
      const text = fmtTime(Number(copy.dataset.time));
      navigator.clipboard?.writeText(text)
        .then(() => toast(`Timecode ${text} copié.`, 'success'))
        .catch(() => toast('Copie impossible.', 'error'));
    }
  });

  $('#modalClose').addEventListener('click', closeClip);
  $('#modal').addEventListener('click', (event) => {
    if (event.target === $('#modal')) closeClip();
  });

  $('#chipStorage').addEventListener('click', openStorage);
  $('#storageClose').addEventListener('click', () => $('#storageModal').classList.add('hidden'));
  $('#storageCancel').addEventListener('click', () => $('#storageModal').classList.add('hidden'));
  $('#storagePurge').addEventListener('click', purgeStorage);
  $('#storageModal').addEventListener('click', (event) => {
    if (event.target === $('#storageModal')) $('#storageModal').classList.add('hidden');
  });

  $('#twitchSave').addEventListener('click', saveTwitchCredentials);
  $('#channelAdd').addEventListener('click', addChannel);
  $('#channelInput').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') addChannel();
  });
  $('#channelList').addEventListener('click', (event) => {
    const select = event.target.closest('[data-select]');
    if (select) {
      twitch.active = select.dataset.select;
      renderChannels();
      loadTwitchClips();
      return;
    }
    const remove = event.target.closest('[data-remove]');
    if (remove) removeChannel(remove.dataset.remove);
  });
  $('#periodSeg').addEventListener('click', (event) => {
    const btn = event.target.closest('button[data-value]');
    if (!btn) return;
    twitch.period = btn.dataset.value;
    $$('#periodSeg button').forEach((b) => b.classList.toggle('active', b === btn));
    loadTwitchClips();
  });
  $('#twitchRefresh').addEventListener('click', () => loadTwitchClips());
  $('#twitchGrid').addEventListener('click', (event) => {
    const btn = event.target.closest('[data-import]');
    if (btn) importTwitchClip(btn);
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      closeClip();
      $('#storageModal').classList.add('hidden');
    }
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
      event.preventDefault();
      generate();
    }
  });
}

async function checkHealth() {
  const chip = $('#chipTools');
  try {
    const health = await api('/api/health');
    state.whisper = !!health.whisper;
    applySettings();
    const missing = [];
    if (!health.ffmpeg) missing.push('ffmpeg');
    if (!health.yt_dlp) missing.push('yt-dlp');
    chip.innerHTML = missing.length
      ? `<i class="dot warn"></i> ${missing.join(' / ')} manquant`
      : '<i class="dot ok"></i> outils prêts';
    chip.classList.toggle('warn', missing.length > 0);
  } catch {
    chip.innerHTML = '<i class="dot err"></i> serveur hors ligne';
    chip.classList.add('warn');
  }
}

bind();
restoreSettings();
refreshImportBtn();
checkHealth();
refreshStorage();
loadPublish();
