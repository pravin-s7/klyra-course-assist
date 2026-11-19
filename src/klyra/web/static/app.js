/* ── Klyra Web UI — Client-side JavaScript ──────────────────────── */

// ── State ─────────────────────────────────────────────────────────
let ws = null;
let isVoiceActive = false;
let voiceResponseEnabled = false;
let audioContext = null;
let mediaStream = null;
let scriptProcessor = null;
let voiceSilenceGain = null;
let playbackContext = null;
let nextPlayTime = 0;
let currentUserMessageEl = null;
let currentUserText = '';
let currentKlyraMessageEl = null;
let currentKlyraText = '';
let reconnectAttempts = 0;
const MAX_RECONNECT = 5;

// ── DOM refs ──────────────────────────────────────────────────────
const chatInput = document.getElementById('chatInput');
const sendBtn = document.getElementById('sendBtn');
const micBtn = document.getElementById('micBtn');
const attachImageBtn = document.getElementById('attachImageBtn');
const chatImageInput = document.getElementById('chatImageInput');
const chatAttachmentBar = document.getElementById('chatAttachmentBar');
const chatComposerNotice = document.getElementById('chatComposerNotice');
const chatInputContainer = document.getElementById('chatInputContainer');
const chatScrollArea = document.getElementById('chatScrollArea');
const inputBar = document.getElementById('inputBar');

// Full-page Artifacts workspace view
const sidebarArtifactsBtn = document.getElementById('sidebarArtifactsBtn');
const artifactsView = document.getElementById('artifactsView');
const backToChatBtn = document.getElementById('backToChatBtn');
const artifactsRefreshBtn = document.getElementById('artifactsRefreshBtn');
const artifactsCountBadge = document.getElementById('artifactsCountBadge');
const artifactsSearchInput = document.getElementById('artifactsSearchInput');
const artifactsSubjectFilter = document.getElementById('artifactsSubjectFilter');
const artifactsTimePreset = document.getElementById('artifactsTimePreset');
const artifactsGroupBy = document.getElementById('artifactsGroupBy');
const artifactsResetFiltersBtn = document.getElementById('artifactsResetFiltersBtn');
const artifactsCustomRange = document.getElementById('artifactsCustomRange');
const artifactsDateFrom = document.getElementById('artifactsDateFrom');
const artifactsDateTo = document.getElementById('artifactsDateTo');
const artifactsListContainer = document.getElementById('artifactsListContainer');


// Modals
const uploadModal = document.getElementById('uploadModal');
const closeUploadModal = document.getElementById('closeUploadModal');
const uploadForm = document.getElementById('uploadForm');

const deleteModal = document.getElementById('deleteModal');
const closeDeleteModal = document.getElementById('closeDeleteModal');
const deleteForm = document.getElementById('deleteForm');
const deleteSubject = document.getElementById('deleteSubject');
const deleteFetchBtn = document.getElementById('deleteFetchBtn');
const deleteFoldersSection = document.getElementById('deleteFoldersSection');
const deleteFilesSection = document.getElementById('deleteFilesSection');
const deleteFoldersList = document.getElementById('deleteFoldersList');
const deleteFilesList = document.getElementById('deleteFilesList');
const deleteSubmitBtn = document.getElementById('deleteSubmitBtn');

// Layout
const sidebar = document.getElementById('sidebar');
if (sidebar) {
    sidebar.classList.add('collapsed');
}

const sidebarUploadBtn = document.getElementById('sidebarUploadBtn');
const sidebarDeleteBtn = document.getElementById('sidebarDeleteBtn');

const messages = document.getElementById('messages');
const chatArea = document.getElementById('chatArea');
const welcomeScreen = document.getElementById('welcomeScreen');
const connectionStatus = document.getElementById('connectionStatus');
const newChatBtn = document.getElementById('newChatBtn');
const themeToggleBtn = document.getElementById('themeToggleBtn');
const chatSessionsList = document.getElementById('chatSessionsList');

// ── Auth gate (RBAC) ─────────────────────────────────────────────
// Shows the login overlay when there's no valid session; only after a
// successful auth check do we open the WebSocket and load data.
let currentUser = null;
let accessibleSubjects = [];

// Light-dismiss fallback for <dialog closedby="any">
if (typeof HTMLDialogElement !== 'undefined' && !('closedBy' in HTMLDialogElement.prototype)) {
    for (const dialog of document.querySelectorAll('dialog[closedby="any"]')) {
        dialog.addEventListener('click', (event) => {
            if (event.target !== dialog) return;
            const rect = dialog.getBoundingClientRect();
            const isDialogContent = (
                rect.top <= event.clientY &&
                event.clientY <= rect.top + rect.height &&
                rect.left <= event.clientX &&
                event.clientX <= rect.left + rect.width
            );
            if (isDialogContent) return;
            dialog.close();
        });
    }
}

function formatChatTime(isoOrDate) {
    try {
        const d = isoOrDate ? new Date(isoOrDate) : new Date();
        if (isNaN(d)) return '';
        return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } catch (e) {
        return '';
    }
}

function showLogin() {
    const overlay = document.getElementById('loginOverlay');
    if (overlay) overlay.style.display = 'flex';
}

let selectedSidebarSubject = null;

function bindSidebarSectionPersistence(detailsEl, storageKey, defaultOpen = true) {
    if (!detailsEl) return;
    const saved = localStorage.getItem(storageKey);
    detailsEl.open = saved === null ? defaultOpen : saved === 'true';
    if (detailsEl.dataset.persistBound !== 'true') {
        detailsEl.dataset.persistBound = 'true';
        detailsEl.addEventListener('toggle', () => {
            localStorage.setItem(storageKey, String(detailsEl.open));
        });
    }
}

function selectSidebarSubject(subject) {
    selectedSidebarSubject = subject;
    document.querySelectorAll('.sidebar-subject-tag').forEach((btn) => {
        const isActive = btn.dataset.subject === subject;
        btn.classList.toggle('active', isActive);
        btn.setAttribute('aria-pressed', String(isActive));
    });

    const uploadSelect = document.getElementById('uploadSubject');
    if (uploadSelect && Array.from(uploadSelect.options).some((o) => o.value === subject)) {
        uploadSelect.value = subject;
    }
    const delSelect = document.getElementById('deleteSubject');
    if (delSelect && Array.from(delSelect.options).some((o) => o.value === subject)) {
        delSelect.value = subject;
    }

    if (typeof renderPendingChatAttachments === 'function' && pendingChatAttachments.length > 0) {
        renderPendingChatAttachments();
    }

    if (artifactsView && !artifactsView.hidden) {
        if (artifactsSubjectFilter && Array.from(artifactsSubjectFilter.options).some((o) => o.value === subject)) {
            artifactsSubjectFilter.value = subject;
            renderArtifactsView();
        }
        return;
    }

    if (chatInput) {
        const currentVal = chatInput.value;
        const prefixRegex = /^For [a-zA-Z0-9_-]+:\s*/;
        if (!currentVal.trim()) {
            chatInput.value = `For ${subject}: `;
        } else if (prefixRegex.test(currentVal)) {
            chatInput.value = currentVal.replace(prefixRegex, `For ${subject}: `);
        } else if (!currentVal.toLowerCase().includes(subject.toLowerCase())) {
            chatInput.value = `For ${subject}: ${currentVal}`;
        }
        chatInput.focus();
        chatInput.setSelectionRange(chatInput.value.length, chatInput.value.length);
        chatInput.dispatchEvent(new Event('input'));
    }
}

function renderAssignedCoursesUI(user, discoveredSubjects = []) {
    if (!user) return;
    const isAdmin = user.role === 'admin';
    const assigned = isAdmin
        ? (discoveredSubjects.length ? discoveredSubjects : (user.courses || []))
        : (user.courses || []);

    const section = document.getElementById('sidebarSubjectsSection');
    const titleEl = document.getElementById('sidebarSubjectsTitle');
    const countEl = document.getElementById('sidebarSubjectsCount');
    const listEl = document.getElementById('sidebarSubjectsList');
    if (!section || !listEl) return;

    bindSidebarSectionPersistence(section, 'klyra_sidebar_subjects', true);

    if (titleEl) {
        titleEl.textContent = isAdmin ? 'All Subjects' : 'Assigned Subjects';
    }
    if (countEl) {
        countEl.textContent = String(assigned.length);
    }

    listEl.innerHTML = '';
    if (assigned.length === 0) {
        const empty = document.createElement('span');
        empty.className = 'sidebar-subject-empty';
        empty.textContent = isAdmin ? 'No subjects found' : 'No subjects assigned';
        listEl.appendChild(empty);
        return;
    }

    assigned.forEach((subj) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'sidebar-subject-tag' + (selectedSidebarSubject === subj ? ' active' : '');
        btn.dataset.subject = subj;
        btn.setAttribute('aria-pressed', String(selectedSidebarSubject === subj));
        btn.textContent = subj;
        btn.addEventListener('click', () => selectSidebarSubject(subj));
        listEl.appendChild(btn);
    });
}

function showUserBadge(user) {
    const badge = document.getElementById('userBadge');
    const nameEl = document.getElementById('userDisplayName');
    if (!badge || !nameEl) return;
    const displayName = user.full_name || user.username;
    nameEl.textContent = `${displayName} (${user.role})`;
    nameEl.title = user.role === 'admin'
        ? 'Admin — access to all courses'
        : 'Courses: ' + ((user.courses || []).join(', ') || 'none assigned');
    badge.style.display = 'flex';
    renderAssignedCoursesUI(user, accessibleSubjects);

    // Admins get a link to the user-management panel
    let adminLink = document.getElementById('adminPanelLink');
    if (user.role === 'admin' && !adminLink) {
        adminLink = document.createElement('a');
        adminLink.id = 'adminPanelLink';
        adminLink.href = '/admin';
        adminLink.className = 'action-btn';
        adminLink.title = 'Admin panel';
        adminLink.style.marginRight = '4px';
        adminLink.style.textDecoration = 'none';
        adminLink.innerHTML =
            '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
            'stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7"></rect>' +
            '<rect x="14" y="3" width="7" height="7"></rect>' +
            '<rect x="14" y="14" width="7" height="7"></rect><rect x="3" y="14" width="7" height="7"></rect></svg>';
        const logoutBtn = document.getElementById('logoutBtn');
        if (logoutBtn && logoutBtn.parentElement) {
            logoutBtn.parentElement.insertBefore(adminLink, logoutBtn);
        }
    }
}

async function initAuth() {
    try {
        const resp = await fetch('/api/auth/me');
        if (resp.ok) {
            currentUser = await resp.json();
            showUserBadge(currentUser);
            return true;
        }
    } catch (err) {
        console.error('Auth check failed:', err);
    }
    showLogin();
    // Show the Google button only when OAuth is configured server-side
    fetch('/api/auth/google/status')
        .then((r) => (r.ok ? r.json() : { enabled: false }))
        .then((data) => {
            if (data.enabled) {
                const btn = document.getElementById('googleBtn');
                const divider = document.getElementById('googleDivider');
                if (btn) btn.style.display = 'flex';
                if (divider) divider.style.display = 'flex';
            }
        })
        .catch(() => {});
    return false;
}

function bindAuthUI() {
    const form = document.getElementById('loginForm');
    if (form) {
        form.addEventListener('submit', async (e) => {
            e.preventDefault();
            const btn = document.getElementById('loginSubmitBtn');
            const errEl = document.getElementById('loginError');
            const username = document.getElementById('loginUsername').value.trim();
            const password = document.getElementById('loginPassword').value;
            btn.disabled = true;
            errEl.style.display = 'none';
            try {
                const resp = await fetch('/api/auth/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ username, password })
                });
                if (resp.ok) {
                    location.reload();  // cookie set — reload into the app
                    return;
                }
                const data = await resp.json().catch(() => ({}));
                errEl.textContent = data.detail || 'Login failed';
                errEl.style.display = 'block';
            } catch (err) {
                errEl.textContent = 'Login failed: ' + err.message;
                errEl.style.display = 'block';
            } finally {
                btn.disabled = false;
            }
        });
    }

    const logoutBtn = document.getElementById('logoutBtn');
    if (logoutBtn) {
        logoutBtn.addEventListener('click', async () => {
            try { await fetch('/api/auth/logout', { method: 'POST' }); } catch (err) {}
            localStorage.removeItem(sessionStorageKey());
            location.reload();
        });
    }
}
bindAuthUI();

// ── WebSocket ─────────────────────────────────────────────────────
// The chat session id is kept PER-USER so logging in as another user on
// the same browser starts a fresh chat instead of reusing the previous
// user's privately-owned session (which the server rejects with 4403).
function sessionStorageKey() {
    const uid = (currentUser && currentUser.id) || 'anon';
    return 'klyra_session_id_' + uid;
}

function getSessionId() {
    const key = sessionStorageKey();
    let sid = localStorage.getItem(key);
    if (!sid) {
        sid = Math.random().toString(36).substring(2) + Date.now().toString(36);
        localStorage.setItem(key, sid);
    }
    return sid;
}

function switchSession(sid) {
    if (isVoiceActive) {
        stopVoice();
    }
    if (typeof hideArtifactsView === 'function') {
        hideArtifactsView();
    }
    localStorage.setItem(sessionStorageKey(), sid);

    // Clear message bubbles while keeping #welcomeScreen attached
    Array.from(messages.childNodes).forEach((node) => {
        if (node.id !== 'welcomeScreen') {
            node.remove();
        }
    });
    if (welcomeScreen && !messages.contains(welcomeScreen)) {
        messages.appendChild(welcomeScreen);
    }
    if (welcomeScreen) {
        welcomeScreen.style.display = 'flex';
    }

    currentUserMessageEl = null;
    currentUserText = '';
    currentKlyraMessageEl = null;
    currentKlyraText = '';
    isGenerating = false;
    updateSendButton();
    hideTypingIndicator();
    if (window.toolCounters) window.toolCounters = {};

    // Cleanly reconnect WebSocket to the target session
    reconnectAttempts = 0;
    if (ws) {
        ws.onclose = null; // Prevent disconnected UI/auto-reconnect from old socket
        ws.onerror = null;
        try {
            ws.close();
        } catch (e) {
            // ignore
        }
        ws = null;
    }
    connect();

    // Update the sidebar highlighting
    fetchSessions();
}

function connect() {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const sid = getSessionId();
    ws = new WebSocket(`${protocol}//${location.host}/ws?session_id=${sid}`);

    ws.onopen = () => {
        reconnectAttempts = 0;
        setConnectionStatus('connected', 'Connected');
    };

    ws.onclose = () => {
        setConnectionStatus('disconnected', 'Disconnected');
        if (reconnectAttempts < MAX_RECONNECT) {
            reconnectAttempts++;
            setTimeout(connect, 2000 * reconnectAttempts);
        }
    };

    ws.onerror = () => {
        setConnectionStatus('error', 'Error');
    };

    ws.onmessage = (event) => {
        if (event.data instanceof Blob) {
            if (voiceResponseEnabled && playbackContext) {
                playAudioChunk(event.data);
            }
            return;
        }

        try {
            const msg = JSON.parse(event.data);
            handleServerMessage(msg);
        } catch (e) {
            console.error('Failed to parse message:', e);
        }
    };
}

function setConnectionStatus(state, text) {
    const dot = connectionStatus.querySelector('.status-dot');
    if (dot) dot.className = 'status-dot ' + state;
    
    const label = connectionStatus.querySelector('.status-text');
    if (label) label.textContent = text;
    
    // Update tooltip instead of showing text
    if (connectionStatus) connectionStatus.title = text;
}

// ── Message handling ──────────────────────────────────────────────
function handleServerMessage(msg) {
    switch (msg.type) {
        case 'klyra_text':
            currentUserMessageEl = null;
            currentUserText = '';
            hideWelcome();
            hideTypingIndicator();
            if (!currentKlyraMessageEl) {
                currentKlyraMessageEl = addMessage('klyra', '', msg.timestamp);
            }
            currentKlyraText += msg.text;
            updateMessageContent(currentKlyraMessageEl, currentKlyraText, true);
            scrollToBottom();
            break;

        case 'user_transcript':
            hideWelcome();
            if (msg.streaming) {
                if (!currentUserMessageEl) {
                    currentUserMessageEl = addMessage('user', '', msg.timestamp);
                }
                currentUserText += msg.text;
                updateMessageContent(currentUserMessageEl, currentUserText);
                scrollToBottom();
            } else if (msg.final) {
                if (!currentUserMessageEl) {
                    addMessage('user', msg.text, msg.timestamp, msg.attachments || null);
                }
                currentUserMessageEl = null;
                currentUserText = '';
                scrollToBottom();
            }
            break;

        case 'klyra_transcript':
            currentUserMessageEl = null;
            currentUserText = '';
            hideWelcome();
            hideTypingIndicator();
            if (!currentKlyraMessageEl) {
                currentKlyraMessageEl = addMessage('klyra', '', msg.timestamp);
            }
            currentKlyraText += msg.text;
            updateMessageContent(currentKlyraMessageEl, currentKlyraText);
            scrollToBottom();
            break;

        case 'tool_start':
            currentUserMessageEl = null;
            currentUserText = '';
            if (!window.toolCounters) window.toolCounters = {};
            if (window.toolCounters[msg.tool] === undefined) window.toolCounters[msg.tool] = 0;
            window.toolCounters[msg.tool]++;
            addToolStatus(msg.tool, 'running', window.toolCounters[msg.tool]);
            scrollToBottom();
            break;

        case 'tool_done':
            updateToolStatus(msg.tool, msg.status, window.toolCounters && window.toolCounters[msg.tool] ? window.toolCounters[msg.tool] : 1);
            scrollToBottom();
            break;

        case 'tool_log':
            appendToolLog(msg.tool, msg.text, window.toolCounters && window.toolCounters[msg.tool] ? window.toolCounters[msg.tool] : 1);
            break;

        case 'file_link':
            addFileCard(msg.name, msg.url, msg.file_type);
            scrollToBottom();
            break;

        case 'doc_preview':
            addDocPreview(msg.filename, msg.content);
            scrollToBottom();
            break;

        case 'turn_complete':
            if (currentKlyraMessageEl && window.MathJax && window.MathJax.typesetPromise) {
                const elToTypeset = currentKlyraMessageEl;
                MathJax.typesetPromise([elToTypeset]).catch(err => console.log(err));
            }
            currentUserMessageEl = null;
            currentUserText = '';
            currentKlyraMessageEl = null;
            currentKlyraText = '';
            hideTypingIndicator();
            isGenerating = false;
            updateSendButton();
            fetchSessions();
            break;

        case 'voice_started':
            micBtn.classList.add('active');
            voiceResponseEnabled = Boolean(msg.voice_response);
            if (voiceResponseEnabled) {
                if (!playbackContext) {
                    playbackContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 24000 });
                    nextPlayTime = 0;
                }
                if (playbackContext.state === 'suspended') {
                    playbackContext.resume().catch(() => {});
                }
            } else if (playbackContext) {
                try { playbackContext.close(); } catch (_) {}
                playbackContext = null;
                nextPlayTime = 0;
            }
            break;

        case 'voice_interrupted':
            if (playbackContext) {
                try { playbackContext.close(); } catch (_) {}
                playbackContext = voiceResponseEnabled
                    ? new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 24000 })
                    : null;
            }
            nextPlayTime = 0;
            currentKlyraMessageEl = null;
            currentKlyraText = '';
            break;

        case 'voice_stopped':
            micBtn.classList.remove('active');
            stopVoice();
            break;

        case 'voice_error':
            micBtn.classList.remove('active');
            stopVoice();
            addMessage('klyra', `⚠️ Voice error: ${msg.message}`);
            break;
    }
}

// ── UI helpers ────────────────────────────────────────────────────
function hideWelcome() {
    if (welcomeScreen) {
        welcomeScreen.style.display = 'none';
    }
}

function addMessage(role, text, timestamp = null, attachments = null) {
    const div = document.createElement('div');
    div.className = `message ${role}`;

    const content = document.createElement('div');
    content.className = 'message-content';

    const bubble = document.createElement('div');
    bubble.className = 'message-bubble';
    const hasAttachments = Array.isArray(attachments) && attachments.length > 0;
    if (hasAttachments) {
        const attGrid = document.createElement('div');
        attGrid.className = 'message-attachments';
        const downloadIconSvg = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>';
        attachments.forEach((att) => {
            const fileUrl = att.url || '';
            const previewUrl = att.preview_url || fileUrl;
            const name = att.name || 'artifact';
            const dotIdx = name.lastIndexOf('.');
            const ext = (att.file_type || (dotIdx >= 0 ? name.slice(dotIdx) : '')).toLowerCase();
            const isImg = Boolean(
                att.is_image !== undefined
                    ? att.is_image
                    : (['.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg'].includes(ext)
                        || (att.mime_type && String(att.mime_type).startsWith('image/')))
            );

            if (isImg) {
                const card = document.createElement('div');
                card.className = 'message-attachment-card';

                const img = document.createElement('img');
                img.src = fileUrl;
                img.alt = name;
                img.loading = 'lazy';
                img.title = `${name} — Click to enlarge`;
                img.addEventListener('click', () => {
                    if (typeof openImageLightbox === 'function') {
                        openImageLightbox(fileUrl, name);
                    } else {
                        window.open(previewUrl, '_blank', 'noopener');
                    }
                });

                const meta = document.createElement('div');
                meta.className = 'message-attachment-meta';
                meta.innerHTML = `
                    <a href="${escapeHtml(previewUrl)}" target="_blank" rel="noopener" class="message-attachment-name" title="Preview in new tab">${escapeHtml(name)}</a>
                    <a href="${escapeHtml(fileUrl)}" download="${escapeHtml(name)}" class="message-attachment-dl-icon" title="Download ${escapeHtml(name)}" aria-label="Download ${escapeHtml(name)}">${downloadIconSvg}</a>
                `;

                card.appendChild(img);
                card.appendChild(meta);
                attGrid.appendChild(card);
            } else {
                const badgeText = (ext.replace(/^\./, '').toUpperCase() || 'FILE').slice(0, 5);
                const chip = document.createElement('div');
                chip.className = 'message-attachment-file-card';
                chip.innerHTML = `
                    <span class="message-attachment-ext">${escapeHtml(badgeText)}</span>
                    <a href="${escapeHtml(previewUrl)}" target="_blank" rel="noopener" class="message-attachment-name" title="Preview in new tab">${escapeHtml(name)}</a>
                    <a href="${escapeHtml(fileUrl)}" download="${escapeHtml(name)}" class="message-attachment-dl-icon" title="Download ${escapeHtml(name)}" aria-label="Download ${escapeHtml(name)}">${downloadIconSvg}</a>
                `;
                attGrid.appendChild(chip);
            }
        });
        bubble.appendChild(attGrid);

        if (text && String(text).trim()) {
            const textWrap = document.createElement('div');
            textWrap.className = 'message-text-body';
            textWrap.innerHTML = renderMarkdown(text);
            bubble.appendChild(textWrap);
        }
    } else {
        bubble.innerHTML = renderMarkdown(text);
    }
    if (window.MathJax && window.MathJax.typesetPromise) {
        MathJax.typesetPromise([bubble]).catch(err => console.log(err));
    }

    const actions = document.createElement('div');
    actions.className = 'message-actions';

    const tsSpan = document.createElement('span');
    tsSpan.className = 'message-timestamp';
    const formattedTs = formatChatTime(timestamp);
    tsSpan.textContent = formattedTs;
    tsSpan.title = timestamp || new Date().toISOString();
    actions.appendChild(tsSpan);

    const copyBtn = document.createElement('button');
    copyBtn.className = 'action-btn';
    copyBtn.title = 'Copy';
    const copyIcon = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>';
    const checkIcon = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"></polyline></svg>';

    copyBtn.innerHTML = copyIcon;
    copyBtn.onclick = () => {
        navigator.clipboard.writeText(bubble.textContent || text).then(() => {
            copyBtn.innerHTML = checkIcon;
            setTimeout(() => copyBtn.innerHTML = copyIcon, 2000);
        });
    };
    actions.appendChild(copyBtn);

    content.appendChild(bubble);
    content.appendChild(actions);
    div.appendChild(content);
    messages.appendChild(div);

    return bubble;
}

function updateMessageContent(bubbleEl, text, streaming = false) {
    if (bubbleEl) {
        bubbleEl.innerHTML = renderMarkdown(text);
        if (!streaming && window.MathJax && window.MathJax.typesetPromise) {
            MathJax.typesetPromise([bubbleEl]).catch(err => console.log(err));
        }
    }
}

function addToolStatus(toolName, status, counter = 1) {
    const div = document.createElement('div');
    div.className = 'tool-status';
    div.id = `tool-${toolName}-${counter}`;
    const spinnerSvg = `<svg class="spin-icon" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="margin-right:6px;"><line x1="12" y1="2" x2="12" y2="6"></line><line x1="12" y1="18" x2="12" y2="22"></line><line x1="4.93" y1="4.93" x2="7.76" y2="7.76"></line><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"></line><line x1="2" y1="12" x2="6" y2="12"></line><line x1="18" y1="12" x2="22" y2="12"></line><line x1="4.93" y1="19.07" x2="7.76" y2="16.24"></line><line x1="16.24" y1="7.76" x2="19.07" y2="4.93"></line></svg>`;
    div.innerHTML = `
        <details class="tool-logs-details">
            <summary class="klyra-tool-active" style="display: flex; align-items: center; cursor: pointer;">
                ${spinnerSvg}
                <span>Executing <strong>${toolName}</strong>... </span>
            </summary>
            <pre class="tool-terminal-log" id="logs-${toolName}-${counter}"></pre>
        </details>
    `;
    messages.appendChild(div);
}

function stripAnsi(text) {
    return text.replace(/\x1B\[[0-9;]*[a-zA-Z]/g, '');
}

function appendToolLog(toolName, text, counter = 1) {
    const logEl = document.getElementById(`logs-${toolName}-${counter}`);
    if (logEl) {
        logEl.textContent += stripAnsi(text);
        logEl.scrollTop = logEl.scrollHeight;
    }
}

function updateToolStatus(toolName, status, counter = 1, extraMessage = '', autoRemoveMs = 0) {
    const el = document.getElementById(`tool-${toolName}-${counter}`);
    if (el) {
        if (status === 'pending') {
            const summaryEl = el.querySelector('summary');
            if (summaryEl) {
                let extraSpan = summaryEl.querySelector('.extra-message');
                if (extraSpan) {
                    extraSpan.innerHTML = `— ${extraMessage}`;
                } else {
                    summaryEl.innerHTML += `<span class="extra-message" style="font-size: 14px; margin-left: 4px;">— ${extraMessage}</span>`;
                }
            }
            return;
        }

        el.className = `tool-status ${status === 'success' ? 'done' : 'error'}`;
        const summaryEl = el.querySelector('summary');
        if (summaryEl) {
            const iconSvg = status === 'success' 
                ? '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--success-color)" stroke-width="2"><polyline points="20 6 9 17 4 12"></polyline></svg>'
                : '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--error-color)" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>';
            
            summaryEl.className = status === 'success' ? 'klyra-tool-done' : 'klyra-tool-error';
            let label = status === 'success' ? 'completed' : 'failed';
            
            let extra = extraMessage ? `<span class="extra-message" style="font-size: 14px; margin-left: 4px;">— ${extraMessage}</span>` : '';
            summaryEl.innerHTML = `${iconSvg} <span><strong>${toolName}</strong> ${label}</span> ${extra} <span style="margin-left: 8px; font-size: 12px; opacity: 0.7;">(click to expand logs)</span>`;
        }

        if (autoRemoveMs > 0 && status === 'success') {
            setTimeout(() => {
                el.style.transition = 'opacity 0.5s ease, margin 0.5s ease, padding 0.5s ease, max-height 0.5s ease';
                el.style.opacity = '0';
                el.style.margin = '0';
                el.style.padding = '0';
                el.style.maxHeight = '0px';
                el.style.overflow = 'hidden';
                el.style.border = 'none';
                setTimeout(() => el.remove(), 500);
            }, autoRemoveMs);
        }
    }
}

function addFileCard(name, url, fileType) {
    const ext = (fileType || '').toLowerCase();
    const imageExts = ['.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp'];
    const isImage = imageExts.includes(ext);

    const icons = {
        '.md': '<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="16" y1="13" x2="8" y2="13"></line><line x1="16" y1="17" x2="8" y2="17"></line><polyline points="10 9 9 9 8 9"></polyline></svg>',
        '.pdf': '<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#ef4444" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline></svg>',
        '.py': '<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#eab308" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><polyline points="8 13 10 15 16 9"></polyline></svg>',
    };
    const imgIcon = '<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#3b82f6" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect><circle cx="8.5" cy="8.5" r="1.5"></circle><polyline points="21 15 16 10 5 21"></polyline></svg>';
    const icon = isImage ? imgIcon : (icons[ext] || '<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"></path><polyline points="13 2 13 9 20 9"></polyline></svg>');

    const div = document.createElement('div');
    div.className = isImage ? 'file-card file-card-image' : 'file-card';

    const previewable = ['.md', '.txt', '.tex', '.py', '.json', '.yaml', '.yml', ...imageExts];
    const canPreview = previewable.includes(ext);
    const isZipBundle = url.startsWith('/api/download/') || ext === '.zip';
    const newTabPreviewHref = (!isZipBundle && url.startsWith('/files/'))
        ? `/preview/${url.slice('/files/'.length)}`
        : url;

    const eyeSvg = `<svg xmlns="http://www.w3.org/2000/svg" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle></svg>`;
    const newTabSvg = `<svg xmlns="http://www.w3.org/2000/svg" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path><polyline points="15 3 21 3 21 9"></polyline><line x1="10" y1="14" x2="21" y2="3"></line></svg>`;
    const downloadSvg = `<svg xmlns="http://www.w3.org/2000/svg" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>`;

    const downloadHref = url.startsWith('/api/download/') ? url : `${url}?download=true`;
    div.innerHTML = `
        <div class="file-card-row" style="display: flex; align-items: center; justify-content: space-between; width: 100%;">
            <div style="display: flex; align-items: center; gap: 12px; cursor: default;">
                <span class="file-icon" style="display:flex; align-items:center; color: var(--text-muted);">${icon}</span>
                <div class="file-info">
                    <a href="${isZipBundle ? downloadHref : newTabPreviewHref}" ${isZipBundle ? `download="${escapeHtml(name)}"` : 'target="_blank" rel="noopener"'} class="file-name" style="color: inherit; text-decoration: none;" title="${isZipBundle ? 'Download ZIP' : 'Preview in new tab'}">${escapeHtml(name)}</a>
                    <span class="file-type">${ext.replace('.', '')}</span>
                </div>
            </div>
            <div class="file-card-actions">
                ${canPreview ? `<a href="#" class="preview-link" title="${isImage ? 'Toggle Inline Image' : 'Inline Preview'}" style="display:flex; align-items:center;">${eyeSvg}</a>` : ''}
                ${!isZipBundle ? `<a href="${newTabPreviewHref}" target="_blank" rel="noopener" title="Preview in new tab" style="display:flex; align-items:center;">${newTabSvg}</a>` : ''}
                <a href="${downloadHref}" download="${escapeHtml(name)}" title="Download" style="display:flex; align-items:center;">${downloadSvg}</a>
            </div>
        </div>
        ${isImage ? `
        <div class="file-card-image-preview">
            <img src="${url}" alt="${escapeHtml(name)}" loading="lazy" title="Click to enlarge" />
        </div>
        ` : ''}
    `;

    if (canPreview) {
        div.querySelector('.preview-link').addEventListener('click', async (e) => {
            e.preventDefault();
            if (isImage) {
                const imgWrap = div.querySelector('.file-card-image-preview');
                if (imgWrap) {
                    imgWrap.style.display = imgWrap.style.display === 'none' ? 'flex' : 'none';
                } else {
                    openImageLightbox(url, name);
                }
                return;
            }
            try {
                const resp = await fetch(url + '?preview=true');
                const content = await resp.text();
                addDocPreview(name, content, div, url);
            } catch (err) {
                window.open(newTabPreviewHref, '_blank');
            }
        });
    }

    messages.appendChild(div);
}

function addDocPreview(filename, content, insertAfterEl = null, fileUrl = '') {
    if (insertAfterEl && insertAfterEl.nextSibling && insertAfterEl.nextSibling.classList && insertAfterEl.nextSibling.classList.contains('doc-preview')) {
        insertAfterEl.nextSibling.remove();
        return;
    }
    const isMarkdown = (filename || '').toLowerCase().endsWith('.md');
    const div = document.createElement('div');
    div.className = 'doc-preview';
    div.innerHTML = `
        <div class="doc-preview-header">
            <span style="display:flex; align-items:center;">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="margin-right:6px;"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline></svg>
                ${escapeHtml(filename)}
            </span>
            <div style="display:flex; align-items:center; gap:8px;">
                ${isMarkdown ? `<button class="action-btn toggle-raw-btn" title="Switch between rendered view (with images & math) and raw Markdown">Raw</button>` : ''}
                <button class="action-btn" onclick="this.closest('.doc-preview').remove()">✕ Close</button>
            </div>
        </div>
        <div class="doc-preview-content ${isMarkdown ? 'markdown-rendered' : ''}"></div>
    `;

    const contentEl = div.querySelector('.doc-preview-content');
    if (isMarkdown) {
        let showingRaw = false;
        const renderView = () => {
            if (showingRaw) {
                contentEl.classList.remove('markdown-rendered');
                contentEl.innerHTML = escapeHtml(content);
            } else {
                contentEl.classList.add('markdown-rendered');
                contentEl.innerHTML = renderMarkdown(content, fileUrl);
                if (window.MathJax && window.MathJax.typesetPromise) {
                    MathJax.typesetPromise([contentEl]).catch(err => console.log(err));
                }
            }
        };
        renderView();
        const toggleBtn = div.querySelector('.toggle-raw-btn');
        if (toggleBtn) {
            toggleBtn.addEventListener('click', () => {
                showingRaw = !showingRaw;
                toggleBtn.textContent = showingRaw ? 'Rendered' : 'Raw';
                renderView();
            });
        }
    } else {
        contentEl.innerHTML = escapeHtml(content);
    }

    if (insertAfterEl && insertAfterEl.parentNode) {
        insertAfterEl.parentNode.insertBefore(div, insertAfterEl.nextSibling);
    } else {
        messages.appendChild(div);
    }
}

function showTypingIndicator() {
    if (document.querySelector('.typing-indicator')) return;
    const div = document.createElement('div');
    div.className = 'typing-indicator';
    div.innerHTML = '<span></span><span></span><span></span>';
    messages.appendChild(div);
    scrollToBottom();
}

function hideTypingIndicator() {
    const el = document.querySelector('.typing-indicator');
    if (el) el.remove();
}

function scrollToBottom() {
    const scrollArea = document.getElementById('chatScrollArea');
    if (scrollArea) {
        scrollArea.scrollTop = scrollArea.scrollHeight;
    }
}

// Markdown & Lightbox helpers (`resolveAssetUrl`, `normalizeMarkdownImages`,
// `renderMarkdown`, `openImageLightbox`, `escapeHtml`) are loaded from `/static/markdown.js`.

// Click-to-zoom delegation for any rendered image inside chat messages or previews
if (messages) {
    messages.addEventListener('click', (e) => {
        const target = e.target;
        if (target && target.tagName === 'IMG' && (
            target.closest('.message-bubble') ||
            target.closest('.doc-preview-content') ||
            target.closest('.file-card-image-preview')
        )) {
            openImageLightbox(target.src, target.alt || '');
        }
    });
}

let isGenerating = false;

function updateSendButton() {
    const sendBtn = document.getElementById('sendBtn');
    if (!sendBtn) return;
    if (isGenerating) {
        sendBtn.innerHTML = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="6" width="12" height="12"></rect></svg>`;
        sendBtn.title = 'Stop generating';
        sendBtn.classList.add('stop-mode');
    } else {
        sendBtn.innerHTML = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="19" x2="12" y2="5" /><polyline points="5 12 12 5 19 12" /></svg>`;
        sendBtn.title = 'Send message';
        sendBtn.classList.remove('stop-mode');
    }
}

// ── Chat file & image attachments ─────────────────────────────────
const CHAT_IMAGE_ALLOWED_EXTS = new Set(['.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg']);
const CHAT_FILE_ALLOWED_EXTS = new Set([
    '.pdf', '.txt', '.md', '.doc', '.docx', '.csv', '.json', '.tex',
    '.py', '.c', '.cpp', '.h', '.java', '.js', '.ts', '.sql', '.sh',
    '.bash', '.yaml', '.yml', '.xml', '.html', '.css', '.pptx', '.xlsx', '.rtf',
]);
const CHAT_ARTIFACT_MAX_BYTES = 25 * 1024 * 1024;
const CHAT_ARTIFACT_MAX_COUNT = 10;
let pendingChatAttachments = [];
let isUploadingChatAttachments = false;
let composerNoticeTimer = null;

function showComposerNotice(message, isError = true) {
    if (!chatComposerNotice) {
        console.warn(message);
        return;
    }
    chatComposerNotice.textContent = message;
    chatComposerNotice.classList.toggle('error', isError);
    chatComposerNotice.hidden = false;
    if (composerNoticeTimer) clearTimeout(composerNoticeTimer);
    composerNoticeTimer = setTimeout(() => {
        chatComposerNotice.hidden = true;
    }, 6000);
}

function formatBytesShort(bytes) {
    if (!bytes || bytes <= 0) return '';
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function getFileExtension(filename) {
    const dotIdx = (filename || '').lastIndexOf('.');
    return dotIdx >= 0 ? filename.slice(dotIdx).toLowerCase() : '';
}

function isImageFile(file) {
    if (!file) return false;
    if (file.type && file.type.startsWith('image/')) return true;
    return CHAT_IMAGE_ALLOWED_EXTS.has(getFileExtension(file.name));
}

function isSupportedChatArtifact(file) {
    if (!file) return false;
    if (isImageFile(file)) return true;
    const ext = getFileExtension(file.name);
    if (CHAT_FILE_ALLOWED_EXTS.has(ext)) return true;
    if (file.type && (file.type.startsWith('text/') || file.type === 'application/pdf' || file.type === 'application/json')) {
        return true;
    }
    return false;
}

function renderPendingChatAttachments() {
    if (!chatAttachmentBar) return;
    if (pendingChatAttachments.length === 0) {
        chatAttachmentBar.hidden = true;
        chatAttachmentBar.innerHTML = '';
        return;
    }
    chatAttachmentBar.hidden = false;
    chatAttachmentBar.innerHTML = '';

    const list = document.createElement('div');
    list.className = 'chat-attachment-list';
    pendingChatAttachments.forEach((item, idx) => {
        const chip = document.createElement('div');
        chip.className = 'chat-attachment-item';
        const previewHtml = item.isImage && item.objectUrl
            ? `<img src="${escapeHtml(item.objectUrl)}" alt="${escapeHtml(item.name)}" class="chat-attachment-thumb">`
            : `<span class="chat-attachment-file-badge">${escapeHtml((item.ext.replace(/^\./, '').toUpperCase() || 'FILE').slice(0, 5))}</span>`;
        chip.innerHTML = `
            ${previewHtml}
            <div class="chat-attachment-info">
                <span class="chat-attachment-filename" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</span>
                <span class="chat-attachment-sub">${escapeHtml(formatBytesShort(item.size))}${selectedSidebarSubject ? ` · ${escapeHtml(selectedSidebarSubject)}` : ''}</span>
            </div>
            <button type="button" class="chat-attachment-remove" title="Remove attachment" aria-label="Remove ${escapeHtml(item.name)}">&times;</button>
        `;
        chip.querySelector('.chat-attachment-remove').addEventListener('click', () => {
            if (item.objectUrl) URL.revokeObjectURL(item.objectUrl);
            pendingChatAttachments.splice(idx, 1);
            renderPendingChatAttachments();
        });
        list.appendChild(chip);
    });
    chatAttachmentBar.appendChild(list);
}

function clearPendingChatAttachments() {
    pendingChatAttachments.forEach((item) => {
        if (item.objectUrl) URL.revokeObjectURL(item.objectUrl);
    });
    pendingChatAttachments = [];
    renderPendingChatAttachments();
}

function addPendingChatFiles(fileList) {
    if (!fileList || !fileList.length) return;
    for (const file of Array.from(fileList)) {
        if (pendingChatAttachments.length >= CHAT_ARTIFACT_MAX_COUNT) {
            showComposerNotice(`Maximum ${CHAT_ARTIFACT_MAX_COUNT} attachments per message.`);
            break;
        }
        if (!isSupportedChatArtifact(file)) {
            showComposerNotice(`Unsupported file "${file.name}". Attach images or documents (PDF, TXT, MD, DOC, DOCX, CSV, JSON, TEX, code).`);
            continue;
        }
        if (file.size > CHAT_ARTIFACT_MAX_BYTES) {
            showComposerNotice(`File "${file.name}" exceeds the 25 MB limit.`);
            continue;
        }
        const img = isImageFile(file);
        pendingChatAttachments.push({
            file,
            name: file.name || (img ? `image-${Date.now()}.png` : `file-${Date.now()}.txt`),
            ext: getFileExtension(file.name) || (img ? '.png' : ''),
            size: file.size || 0,
            isImage: img,
            objectUrl: img ? URL.createObjectURL(file) : null,
        });
    }
    renderPendingChatAttachments();
}

if (attachImageBtn && chatImageInput) {
    attachImageBtn.addEventListener('click', () => {
        chatImageInput.click();
    });
    chatImageInput.addEventListener('change', (e) => {
        if (e.target.files && e.target.files.length > 0) {
            addPendingChatFiles(e.target.files);
            chatImageInput.value = '';
            chatInput.focus();
        }
    });
}

if (chatInput) {
    chatInput.addEventListener('paste', (e) => {
        const items = e.clipboardData && e.clipboardData.items ? Array.from(e.clipboardData.items) : [];
        const pastedFiles = items
            .filter((it) => it.kind === 'file')
            .map((it) => it.getAsFile())
            .filter((f) => f && isSupportedChatArtifact(f));
        if (pastedFiles.length > 0) {
            e.preventDefault();
            addPendingChatFiles(pastedFiles);
        }
    });
}

if (chatInputContainer) {
    chatInputContainer.addEventListener('dragover', (e) => {
        if (e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files')) {
            e.preventDefault();
            chatInputContainer.classList.add('drag-over');
        }
    });
    chatInputContainer.addEventListener('dragleave', () => {
        chatInputContainer.classList.remove('drag-over');
    });
    chatInputContainer.addEventListener('drop', (e) => {
        chatInputContainer.classList.remove('drag-over');
        if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length > 0) {
            e.preventDefault();
            addPendingChatFiles(e.dataTransfer.files);
        }
    });
}

// ── Send message (unified for text + voice + attachments) ─────────
async function uploadPendingChatAttachments() {
    if (pendingChatAttachments.length === 0) return [];
    const uploadedAttachments = [];
    isUploadingChatAttachments = true;
    if (attachImageBtn) attachImageBtn.disabled = true;
    if (sendBtn) sendBtn.disabled = true;
    try {
        const subjectForUpload = selectedSidebarSubject || '';
        const activeSid = typeof getSessionId === 'function' ? (getSessionId() || '') : '';
        for (const item of pendingChatAttachments) {
            const formData = new FormData();
            formData.append('file', item.file, item.name);
            formData.append('subject', subjectForUpload);
            formData.append('session_id', activeSid);
            const resp = await fetch('/api/chat/artifacts/upload', {
                method: 'POST',
                body: formData,
            });
            if (!resp.ok) {
                const errData = await resp.json().catch(() => ({}));
                throw new Error(errData.detail || `Failed to upload ${item.name} (HTTP ${resp.status})`);
            }
            uploadedAttachments.push(await resp.json());
        }
        clearPendingChatAttachments();
        return uploadedAttachments;
    } finally {
        isUploadingChatAttachments = false;
        if (attachImageBtn) attachImageBtn.disabled = false;
        if (sendBtn) sendBtn.disabled = false;
    }
}

function dispatchChatMessage(text, uploadedAttachments = []) {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
        showComposerNotice('Not connected to server.');
        return false;
    }
    hideWelcome();
    addMessage('user', text, null, uploadedAttachments.length > 0 ? uploadedAttachments : null);
    showTypingIndicator();
    scrollToBottom();

    isGenerating = true;
    updateSendButton();

    const payload = { type: 'chat', text };
    if (uploadedAttachments.length > 0) {
        payload.attachments = uploadedAttachments;
    }
    ws.send(JSON.stringify(payload));
    chatInput.value = '';
    chatInput.style.height = 'auto';
    return true;
}

async function sendMessage() {
    if (isGenerating) {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'chat_stop' }));
        }
        return;
    }
    if (isVoiceActive) {
        stopVoice();
    }
    if (isUploadingChatAttachments) return;
    const text = chatInput.value.trim();
    const hasPendingFiles = pendingChatAttachments.length > 0;
    if ((!text && !hasPendingFiles) || !ws || ws.readyState !== WebSocket.OPEN) return;

    let uploadedAttachments = [];
    if (hasPendingFiles) {
        try {
            uploadedAttachments = await uploadPendingChatAttachments();
        } catch (err) {
            showComposerNotice(`Attachment upload failed: ${err.message}`);
            return;
        }
    }

    dispatchChatMessage(text, uploadedAttachments);
}

function sendQuickAction(text) {
    chatInput.value = text;
    sendMessage();
}

// ── Voice (Gemini Live API streaming; audio output enabled via CLI --voice-response) ─
async function startVoice() {
    if (isVoiceActive) {
        stopVoice();
        return;
    }

    if (!ws || ws.readyState !== WebSocket.OPEN) {
        addMessage('klyra', '⚠️ Not connected to server yet. Please wait a moment and try voice again.');
        return;
    }

    try {
        // Pre-warm playback AudioContext inside the click gesture so browser autoplay
        // allows spoken audio immediately if `--voice-response` is enabled on the server.
        if (!playbackContext) {
            playbackContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 24000 });
            nextPlayTime = 0;
        }
        if (playbackContext.state === 'suspended') {
            await playbackContext.resume().catch(() => {});
        }

        mediaStream = await navigator.mediaDevices.getUserMedia({
            audio: { channelCount: 1, echoCancellation: true },
        });

        const track = mediaStream.getAudioTracks()[0];
        const settings = track.getSettings();
        const streamRate = settings.sampleRate;
        const ctxOptions = streamRate ? { sampleRate: streamRate } : undefined;

        audioContext = new (window.AudioContext || window.webkitAudioContext)(ctxOptions);
        if (audioContext.state === 'suspended') {
            await audioContext.resume().catch(() => {});
        }
        const nativeSampleRate = audioContext.sampleRate;
        const targetSampleRate = 16000;

        const source = audioContext.createMediaStreamSource(mediaStream);
        scriptProcessor = audioContext.createScriptProcessor(4096, 1, 1);
        voiceSilenceGain = audioContext.createGain();
        voiceSilenceGain.gain.value = 0;
        source.connect(scriptProcessor);
        scriptProcessor.connect(voiceSilenceGain);
        voiceSilenceGain.connect(audioContext.destination);

        scriptProcessor.onaudioprocess = (e) => {
            if (!isVoiceActive || !ws || ws.readyState !== WebSocket.OPEN) return;
            const float32 = e.inputBuffer.getChannelData(0);

            const ratio = nativeSampleRate / targetSampleRate;
            const outLength = Math.round(float32.length / ratio);
            const int16 = new Int16Array(outLength);

            for (let i = 0; i < outLength; i++) {
                const inIdx = Math.floor(i * ratio);
                const s = float32[inIdx] || 0;
                const sClamped = Math.max(-1, Math.min(1, s));
                int16[i] = sClamped < 0 ? sClamped * 0x8000 : sClamped * 0x7FFF;
            }
            ws.send(int16.buffer);
        };

        ws.send(JSON.stringify({ type: 'voice_start' }));
        isVoiceActive = true;
        micBtn.classList.add('active');
    } catch (err) {
        console.error('Failed to start voice:', err);
        stopVoice();
        addMessage('klyra', `⚠️ Could not access microphone: ${err.message}`);
    }
}

function stopVoice() {
    const wasActive = isVoiceActive;
    isVoiceActive = false;
    voiceResponseEnabled = false;
    if (micBtn) micBtn.classList.remove('active');

    if (wasActive && ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'voice_stop' }));
    }

    if (scriptProcessor) {
        scriptProcessor.onaudioprocess = null;
        try { scriptProcessor.disconnect(); } catch (_) {}
        scriptProcessor = null;
    }
    if (voiceSilenceGain) {
        try { voiceSilenceGain.disconnect(); } catch (_) {}
        voiceSilenceGain = null;
    }
    if (audioContext) {
        try { audioContext.close(); } catch (_) {}
        audioContext = null;
    }
    if (mediaStream) {
        mediaStream.getTracks().forEach((t) => t.stop());
        mediaStream = null;
    }
    if (playbackContext) {
        try { playbackContext.close(); } catch (_) {}
        playbackContext = null;
    }
    nextPlayTime = 0;
}

async function playAudioChunk(blob) {
    if (!playbackContext) return;

    try {
        if (playbackContext.state === 'suspended') {
            await playbackContext.resume();
        }
        const arrayBuffer = await blob.arrayBuffer();
        const int16 = new Int16Array(arrayBuffer);
        const float32 = new Float32Array(int16.length);
        for (let i = 0; i < int16.length; i++) {
            float32[i] = int16[i] / 32768.0;
        }

        const audioBuffer = playbackContext.createBuffer(1, float32.length, 24000);
        audioBuffer.getChannelData(0).set(float32);

        const source = playbackContext.createBufferSource();
        source.buffer = audioBuffer;
        source.connect(playbackContext.destination);

        const now = playbackContext.currentTime;
        const startTime = Math.max(now + 0.05, nextPlayTime);
        source.start(startTime);
        nextPlayTime = startTime + audioBuffer.duration;
    } catch (err) {
        console.error('Audio playback error:', err);
    }
}

// ── Event listeners ───────────────────────────────────────────────
chatInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
});

chatInput.addEventListener('input', function () {
    this.style.height = 'auto';
    this.style.height = (this.scrollHeight) + 'px';
});

sendBtn.addEventListener('click', sendMessage);
micBtn.addEventListener('click', startVoice);

// Upload Modal Logic
const dropZone = document.getElementById('dropZone');
const uploadFiles = document.getElementById('uploadFiles');
const fileList = document.getElementById('fileList');

function resetUploadState() {
    uploadForm.reset();
    fileList.innerHTML = '';
}

closeUploadModal.addEventListener('click', () => {
    uploadModal.close();
    resetUploadState();
});

// Drag and drop logic
dropZone.addEventListener('click', () => {
    uploadFiles.click();
});

dropZone.addEventListener('dragover', (e) => {
    e.preventDefault();
    dropZone.classList.add('dragover');
});

dropZone.addEventListener('dragleave', () => {
    dropZone.classList.remove('dragover');
});

dropZone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropZone.classList.remove('dragover');

    if (e.dataTransfer.files.length) {
        uploadFiles.files = e.dataTransfer.files;
        updateFileList();
    }
});

uploadFiles.addEventListener('change', updateFileList);

function updateFileList() {
    fileList.innerHTML = '';
    const files = Array.from(uploadFiles.files);
    const maxFilesToShow = 4;

    files.slice(0, maxFilesToShow).forEach(file => {
        const item = document.createElement('div');
        item.className = 'file-list-item';
        item.innerHTML = `
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"></path>
                <polyline points="13 2 13 9 20 9"></polyline>
            </svg>
            ${file.name}
        `;
        fileList.appendChild(item);
    });

    if (files.length > maxFilesToShow) {
        const item = document.createElement('div');
        item.className = 'file-list-item';
        item.style.justifyContent = 'center';
        item.style.opacity = '0.7';
        item.innerHTML = `+ ${files.length - maxFilesToShow} more files ready to upload`;
        fileList.appendChild(item);
    }
}

uploadForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const formData = new FormData(uploadForm);
    const subject = formData.get('subject');
    const category = formData.get('category');

    uploadModal.close();
    
    if (!window.toolCounters) window.toolCounters = {};
    const toolName = `upload_and_index_${category}`;
    if (window.toolCounters[toolName] === undefined) window.toolCounters[toolName] = 0;
    window.toolCounters[toolName]++;
    const counter = window.toolCounters[toolName];

    // Add a loading message to the chat
    addToolStatus(toolName, 'pending', counter);

    try {
        const resp = await fetch(`/api/upload/${subject}/${category}`, {
            method: 'POST',
            body: formData
        });

        if (resp.ok) {
            const data = await resp.json();
            if (data.task_id) {
                pollTaskProgress(data.task_id, toolName, counter);
            } else {
                updateToolStatus(toolName, 'success', counter, `Files successfully uploaded to ${subject}/${category} and background indexing has started!`, 5000);
            }
            scrollToBottom();
        } else {
            throw new Error(await resp.text());
        }
    } catch (err) {
        updateToolStatus(`upload_and_index_${category}`, 'error');
        console.error("Upload failed", err);
    }
    resetUploadState();
});

function updateThemeIcon() {
    const isLight = document.documentElement.getAttribute('data-theme') === 'light';
    const icon = document.getElementById('themeIcon');
    if (icon) {
        if (isLight) {
            icon.innerHTML = '<circle cx="12" cy="12" r="5"></circle><line x1="12" y1="1" x2="12" y2="3"></line><line x1="12" y1="21" x2="12" y2="23"></line><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"></line><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"></line><line x1="1" y1="12" x2="3" y2="12"></line><line x1="21" y1="12" x2="23" y2="12"></line><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"></line><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"></line>';
        } else {
            icon.innerHTML = '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path>';
        }
    }
}

// Initial icon setup
updateThemeIcon();

themeToggleBtn.addEventListener('click', () => {
    const current = document.documentElement.getAttribute('data-theme');
    const next = current === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('klyra_theme', next);
    updateThemeIcon();
});

if (newSessionBtn) {
    newSessionBtn.addEventListener('click', clearChat);
}

function clearChat() {
    if (typeof clearPendingChatAttachments === 'function') {
        clearPendingChatAttachments();
    }
    const freshSid = Math.random().toString(36).substring(2) + Date.now().toString(36);
    switchSession(freshSid);
    if (chatInput) {
        chatInput.focus();
    }
}

// ── UI Events ─────────────────────────────────────────────────────

// Sidebar Logic


const sidebarCloseBtn = document.getElementById('sidebarCloseBtn');
if (sidebarCloseBtn) {
    sidebarCloseBtn.addEventListener('click', () => {
        sidebar.classList.toggle('collapsed');
    });
}

const sidebarNewChatIconBtn = document.getElementById('sidebarNewChatIconBtn');
if (sidebarNewChatIconBtn) {
    sidebarNewChatIconBtn.addEventListener('click', clearChat);
}

sidebarUploadBtn.addEventListener('click', () => {
    uploadModal.showModal();
    if (window.innerWidth <= 768) {
        sidebar.classList.add('collapsed');
    }
});

// Delete Modal Logic
let currentIndexedData = { folders: [], files: [] };

sidebarDeleteBtn.addEventListener('click', () => {
    deleteModal.showModal();
    if (window.innerWidth <= 768) {
        sidebar.classList.add('collapsed');
    }
});

closeDeleteModal.addEventListener('click', () => {
    deleteModal.close();
    deleteForm.reset();
    deleteFoldersSection.style.display = 'none';
    deleteFilesSection.style.display = 'none';
    deleteSubmitBtn.disabled = true;
});

// Toggle between folders and files
document.querySelectorAll('input[name="deleteType"]').forEach(radio => {
    radio.addEventListener('change', (e) => {
        if (e.target.value === 'folders') {
            deleteFoldersSection.style.display = 'flex';
            deleteFilesSection.style.display = 'none';
        } else {
            deleteFoldersSection.style.display = 'none';
            deleteFilesSection.style.display = 'flex';
        }
    });
});

deleteFetchBtn.addEventListener('click', async () => {
    const subject = deleteSubject.value.trim();
    if (!subject) return;

    deleteFetchBtn.disabled = true;
    deleteFetchBtn.textContent = 'Fetching...';

    try {
        const resp = await fetch(`/api/index/${subject}`);
        if (resp.ok) {
            const data = await resp.json();
            currentIndexedData = data;

            // Populate UI
            renderDeleteList('folders', data.folders, deleteFoldersList);
            renderDeleteList('files', data.files, deleteFilesList);

            // Populate folder filter dropdown
            const folderFilter = document.getElementById('deleteFolderFilter');
            folderFilter.innerHTML = '<option value="">All Folders</option>' +
                data.folders.map(f => `<option value="${f}">${f}</option>`).join('');

            // Folder filter listener
            folderFilter.onchange = (e) => {
                const selectedFolder = e.target.value;
                const filteredFiles = selectedFolder
                    ? data.files.filter(f => f.includes(`/${selectedFolder}/`))
                    : data.files;
                renderDeleteList('files', filteredFiles, deleteFilesList);
            };

            // Show appropriate section based on radio button
            const type = document.querySelector('input[name="deleteType"]:checked').value;
            if (type === 'folders') {
                deleteFoldersSection.style.display = 'flex';
                deleteFilesSection.style.display = 'none';
            } else {
                deleteFoldersSection.style.display = 'none';
                deleteFilesSection.style.display = 'flex';
            }
            deleteSubmitBtn.disabled = false;
        } else {
            throw new Error('Failed to fetch');
        }
    } catch (err) {
        deleteFoldersList.innerHTML = `<div class="file-list-item" style="color: var(--error);">Error fetching data</div>`;
        deleteFilesList.innerHTML = `<div class="file-list-item" style="color: var(--error);">Error fetching data</div>`;
    } finally {
        deleteFetchBtn.disabled = false;
        deleteFetchBtn.textContent = 'Fetch Files';
    }
});

function renderDeleteList(type, items, container) {
    if (!items || items.length === 0) {
        container.innerHTML = `<div class="file-list-item" style="opacity: 0.7;">No ${type} found.</div>`;
        return;
    }
    container.innerHTML = items.map(item => `
        <label class="file-list-item" style="cursor: pointer; gap: 8px;">
            <input type="checkbox" name="delete_${type}" value="${item}">
            ${item}
        </label>
    `).join('');
}

deleteForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const subject = deleteSubject.value.trim();
    const type = document.querySelector('input[name="deleteType"]:checked').value;

    // Get selected checkboxes
    const checkboxes = document.querySelectorAll(`input[name="delete_${type}"]:checked`);
    const selected = Array.from(checkboxes).map(cb => cb.value);

    if (selected.length === 0) {
        alert(`Please select ${type} to delete.`);
        return;
    }

    deleteModal.close();
    
    if (!window.toolCounters) window.toolCounters = {};
    const toolName = `delete_index_${subject}`;
    if (window.toolCounters[toolName] === undefined) window.toolCounters[toolName] = 0;
    window.toolCounters[toolName]++;
    const counter = window.toolCounters[toolName];
    
    addToolStatus(toolName, 'pending', counter);

    try {
        const resp = await fetch(`/api/index/${subject}/delete`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ type, items: selected })
        });

        if (resp.ok) {
            const data = await resp.json();
            if (data.task_id) {
                pollTaskProgress(data.task_id, toolName, counter);
            } else {
                updateToolStatus(toolName, 'success', counter, `Successfully deleted ${selected.length} ${type} from index.`, 5000);
            }
            scrollToBottom();
        } else {
            throw new Error(await resp.text());
        }
    } catch (err) {
        updateToolStatus(`delete_index_${subject}`, 'error');
        console.error("Delete failed", err);
    }

    deleteForm.reset();
    deleteFoldersSection.style.display = 'none';
    deleteFilesSection.style.display = 'none';
    deleteSubmitBtn.disabled = true;
});

async function fetchSubjects() {
    try {
        const resp = await fetch('/api/subjects');
        if (resp.ok) {
            const data = await resp.json();
            const subjects = data.subjects || [];
            accessibleSubjects = subjects;
            if (currentUser) {
                renderAssignedCoursesUI(currentUser, accessibleSubjects);
            }

            const uploadSelect = document.getElementById('uploadSubject');
            const deleteSelect = document.getElementById('deleteSubject');

            // clear options except first
            uploadSelect.innerHTML = '<option value="" disabled selected>Select a subject...</option>';
            deleteSelect.innerHTML = '<option value="" disabled selected>Select a subject...</option>';

            subjects.forEach(subject => {
                uploadSelect.add(new Option(subject, subject));
                deleteSelect.add(new Option(subject, subject));
            });
        }
    } catch (err) {
        console.error("Failed to load subjects:", err);
    }
}

async function fetchSessions() {
    if (!chatSessionsList) return;
    try {
        const resp = await fetch('/api/sessions');
        if (resp.ok) {
            const data = await resp.json();
            renderSessions(data.sessions || []);
        }
    } catch (err) {
        console.error("Failed to load sessions:", err);
    }
}

function renderSessions(sessions) {
    chatSessionsList.innerHTML = '';
    const currentSid = getSessionId();

    const pinnedSessions = sessions.filter(s => s.pinned);
    const unpinnedSessions = sessions.filter(s => !s.pinned);

    const renderSessionList = (list, title, storageKey) => {
        if (list.length === 0) return;

        const details = document.createElement('details');
        details.className = 'sidebar-section sidebar-sessions-group';
        bindSidebarSectionPersistence(details, storageKey, true);

        const summary = document.createElement('summary');
        summary.className = 'sidebar-section-summary';
        const titleSpan = document.createElement('span');
        titleSpan.className = 'sidebar-section-title';
        titleSpan.textContent = title;
        const countSpan = document.createElement('span');
        countSpan.className = 'sidebar-section-count';
        countSpan.textContent = String(list.length);
        summary.appendChild(titleSpan);
        summary.appendChild(countSpan);
        details.appendChild(summary);

        const sectionBody = document.createElement('div');
        sectionBody.className = 'sidebar-section-body';

        list.forEach(session => {
            const item = document.createElement('div');
            item.className = 'session-item';
            if (session.id === currentSid) {
                item.classList.add('active');
            }

            const content = document.createElement('div');
            content.className = 'session-item-content';

            const titleText = document.createElement('div');
            titleText.className = 'session-title';
            titleText.textContent = session.title;
            titleText.title = session.title;
            content.appendChild(titleText);
        
        const actions = document.createElement('div');
        actions.className = 'session-actions';

        // Pin button
        const pinBtn = document.createElement('button');
        pinBtn.className = 'session-action-btn' + (session.pinned ? ' pinned' : '');
        pinBtn.title = session.pinned ? "Unpin Chat" : "Pin Chat";
        pinBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 11V7a4 4 0 0 0-8 0v4l-2 3v2h5v6l1 1 1-1v-6h5v-2l-2-3z"/></svg>`;
        
        // Rename button
        const renameBtn = document.createElement('button');
        renameBtn.className = 'session-action-btn';
        renameBtn.title = "Rename Chat";
        renameBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>`;

        // Delete button
        const deleteBtn = document.createElement('button');
        deleteBtn.className = 'session-action-btn';
        deleteBtn.title = "Delete Chat";
        deleteBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>`;

        actions.appendChild(pinBtn);
        actions.appendChild(renameBtn);
        actions.appendChild(deleteBtn);

        item.appendChild(content);
        item.appendChild(actions);

        // Events
        item.addEventListener('click', (e) => {
            if (e.target.closest('.session-actions')) return; // Ignore if clicking action buttons
            if (session.id !== currentSid) {
                switchSession(session.id);
            }
        });

        // Auto-scroll long titles on hover
        let scrollInterval;
        item.addEventListener('mouseenter', () => {
            if (titleText.scrollWidth > titleText.clientWidth) {
                let dir = 1;
                titleText.style.textOverflow = 'clip';
                scrollInterval = setInterval(() => {
                    titleText.scrollLeft += dir;
                    if (titleText.scrollLeft >= titleText.scrollWidth - titleText.clientWidth) {
                        dir = -1;
                    } else if (titleText.scrollLeft <= 0) {
                        dir = 1;
                    }
                }, 40);
            }
        });
        
        item.addEventListener('mouseleave', () => {
            clearInterval(scrollInterval);
            titleText.scrollLeft = 0;
            titleText.style.textOverflow = 'ellipsis';
        });

        pinBtn.addEventListener('click', async (e) => {
            e.stopPropagation();
            try {
                await fetch(`/api/sessions/${session.id}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ pinned: !session.pinned })
                });
                fetchSessions();
            } catch (err) { console.error("Pin failed", err); }
        });

        deleteBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            
            const deleteModal = document.getElementById('deleteSessionModal');
            const titleEl = document.getElementById('deleteSessionTitle');
            const cancelBtn = document.getElementById('cancelDeleteSessionBtn');
            const confirmBtn = document.getElementById('confirmDeleteSessionBtn');
            
            titleEl.textContent = session.title;
            deleteModal.showModal();
            
            const closeModal = () => {
                deleteModal.close();
            };
            
            const handleConfirm = async (ev) => {
                ev.preventDefault();
                try {
                    await fetch(`/api/sessions/${session.id}`, { method: 'DELETE' });
                    if (session.id === currentSid) {
                        const newSid = Math.random().toString(36).substring(2) + Date.now().toString(36);
                        switchSession(newSid);
                    } else {
                        fetchSessions();
                    }
                } catch (err) { console.error("Delete failed", err); }
                closeModal();
            };
            
            // Use onclick to overwrite any previous bindings and avoid duplicate triggers
            cancelBtn.onclick = closeModal;
            confirmBtn.onclick = handleConfirm;
        });

        renameBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            const input = document.createElement('input');
            input.className = 'session-rename-input';
            input.value = session.title;
            
            content.innerHTML = '';
            content.appendChild(input);
            input.focus();
            input.select();

            const saveRename = async () => {
                const newTitle = input.value.trim() || "New Chat";
                try {
                    await fetch(`/api/sessions/${session.id}`, {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ title: newTitle })
                    });
                    fetchSessions();
                } catch (err) { console.error("Rename failed", err); }
            };

            input.addEventListener('blur', saveRename);
            input.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    input.blur();
                } else if (e.key === 'Escape') {
                    fetchSessions(); // abort
                }
            });
        });

        sectionBody.appendChild(item);
    });

        details.appendChild(sectionBody);
        chatSessionsList.appendChild(details);
    };

    renderSessionList(pinnedSessions, "Pinned Chats", "klyra_sidebar_pinned");
    renderSessionList(unpinnedSessions, "Recent Chats", "klyra_sidebar_recent");
}

async function pollTaskProgress(taskId, toolName, counter) {
    let lastLogLength = 0;
    const interval = setInterval(async () => {
        try {
            const resp = await fetch(`/api/tasks/${taskId}`);
            if (resp.ok) {
                const data = await resp.json();
                
                let msg = data.message || '';
                
                if (data.logs && data.logs.length > lastLogLength) {
                    const newLogs = data.logs.substring(lastLogLength);
                    appendToolLog(toolName, newLogs, counter);
                    lastLogLength = data.logs.length;
                }
                
                if (data.status === 'completed') {
                    clearInterval(interval);
                    updateToolStatus(toolName, 'success', counter, data.message || 'Completed successfully.', 5000);
                } else if (data.status === 'error') {
                    clearInterval(interval);
                    updateToolStatus(toolName, 'error', counter, data.message || 'Task failed.', 0);
                } else {
                    updateToolStatus(toolName, 'pending', counter, msg, 0);
                }
            }
        } catch (err) {
            console.error("Failed to poll task:", err);
        }
    }, 1000);
}

// ── Full-Page Artifacts Workspace View ───────────────────────────
let artifactsAll = [];
let artifactsSubjectsList = [];
let artifactsChatsList = [];
let artifactsActiveSource = '';
let artifactsActiveType = '';

function formatArtifactTimestamp(isoStr) {
    if (!isoStr) return '';
    const d = new Date(isoStr);
    if (Number.isNaN(d.getTime())) return isoStr;
    return d.toLocaleString(undefined, {
        month: 'short',
        day: 'numeric',
        year: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
    });
}

function getArtifactBadgeLabel(fileType) {
    const clean = (fileType || '').replace(/^\./, '').toUpperCase();
    return clean ? clean.slice(0, 5) : 'FILE';
}

function populateArtifactsFilterDropdowns() {
    if (artifactsSubjectFilter) {
        const prev = artifactsSubjectFilter.value;
        const hasGeneral = artifactsAll.some((a) => !a.subject);
        artifactsSubjectFilter.innerHTML = '<option value="">All Subjects</option>';
        if (hasGeneral) {
            const opt = document.createElement('option');
            opt.value = '__general__';
            opt.textContent = 'General (No Subject)';
            artifactsSubjectFilter.appendChild(opt);
        }
        artifactsSubjectsList.forEach((subj) => {
            const opt = document.createElement('option');
            opt.value = subj;
            opt.textContent = subj;
            artifactsSubjectFilter.appendChild(opt);
        });
        if ([...artifactsSubjectFilter.options].some((o) => o.value === prev)) {
            artifactsSubjectFilter.value = prev;
        }
    }
}

function getFilteredArtifacts() {
    const query = (artifactsSearchInput ? artifactsSearchInput.value : '').trim().toLowerCase();
    const subjectVal = artifactsSubjectFilter ? artifactsSubjectFilter.value : '';
    const sourceVal = artifactsActiveSource;
    const timePreset = artifactsTimePreset ? artifactsTimePreset.value : '';

    let fromMs = null;
    let toMs = null;
    const nowMs = Date.now();
    if (timePreset === '24h') {
        fromMs = nowMs - 24 * 60 * 60 * 1000;
    } else if (timePreset === '7d') {
        fromMs = nowMs - 7 * 24 * 60 * 60 * 1000;
    } else if (timePreset === '30d') {
        fromMs = nowMs - 30 * 24 * 60 * 60 * 1000;
    } else if (timePreset === 'custom') {
        if (artifactsDateFrom && artifactsDateFrom.value) {
            const parsed = Date.parse(artifactsDateFrom.value);
            if (!Number.isNaN(parsed)) fromMs = parsed;
        }
        if (artifactsDateTo && artifactsDateTo.value) {
            const parsed = Date.parse(artifactsDateTo.value);
            if (!Number.isNaN(parsed)) toMs = parsed;
        }
    }

    return artifactsAll.filter((item) => {
        if (sourceVal && item.source !== sourceVal) return false;
        if (artifactsActiveType === 'image' && !item.is_image) return false;
        if (artifactsActiveType === 'file' && item.is_image) return false;
        if (subjectVal === '__general__') {
            if (item.subject) return false;
        } else if (subjectVal && item.subject !== subjectVal) {
            return false;
        }
        if (fromMs !== null || toMs !== null) {
            const tsMs = item.timestamp ? Date.parse(item.timestamp) : NaN;
            if (Number.isNaN(tsMs)) return false;
            if (fromMs !== null && tsMs < fromMs) return false;
            if (toMs !== null && tsMs > toMs) return false;
        }
        if (query) {
            const hay = `${item.name || ''} ${item.rel_path || ''} ${item.subject || ''} ${item.session_title || ''} ${item.source || ''}`.toLowerCase();
            if (!hay.includes(query)) return false;
        }
        return true;
    });
}

function buildArtifactRow(item) {
    const row = document.createElement('div');
    row.className = 'artifact-row';

    const sourceClass = item.source === 'uploaded' ? 'uploaded' : 'generated';
    const sourceLabel = item.source === 'uploaded' ? 'Uploaded' : 'Generated';
    const previewHref = item.preview_url || '';
    const downloadHref = item.download_url || item.url || '#';
    const tsLabel = formatArtifactTimestamp(item.timestamp);
    const sizeLabel = formatBytesShort(item.size);
    const subjectText = item.subject || 'General';

    const iconEl = document.createElement('div');
    iconEl.className = 'artifact-row-icon';
    if (item.is_image && item.url) {
        const img = document.createElement('img');
        img.src = item.url;
        img.alt = item.name || 'artifact';
        img.loading = 'lazy';
        img.title = 'Click to enlarge';
        img.addEventListener('click', () => {
            if (typeof openImageLightbox === 'function') {
                openImageLightbox(item.url, item.name || 'artifact');
            } else if (previewHref) {
                window.open(previewHref, '_blank', 'noopener');
            }
        });
        iconEl.appendChild(img);
    } else {
        const badge = document.createElement('span');
        badge.className = 'artifact-row-badge';
        badge.textContent = getArtifactBadgeLabel(item.file_type);
        iconEl.appendChild(badge);
    }

    const mainEl = document.createElement('div');
    mainEl.className = 'artifact-row-main';

    const topRow = document.createElement('div');
    topRow.className = 'artifact-row-top';
    const nameLink = document.createElement('a');
    nameLink.className = 'artifact-row-name';
    nameLink.textContent = item.name || 'Unnamed';
    nameLink.title = item.name || 'Unnamed';
    if (item.is_image && item.url) {
        nameLink.href = item.url;
        nameLink.addEventListener('click', (e) => {
            if (typeof openImageLightbox === 'function') {
                e.preventDefault();
                openImageLightbox(item.url, item.name || 'artifact');
            }
        });
    } else if (previewHref) {
        nameLink.href = previewHref;
        nameLink.target = '_blank';
        nameLink.rel = 'noopener';
    } else {
        nameLink.href = downloadHref;
        nameLink.download = item.name || '';
    }
    const sourceTag = document.createElement('span');
    sourceTag.className = `artifact-source-tag ${sourceClass}`;
    sourceTag.textContent = sourceLabel;
    topRow.appendChild(nameLink);
    topRow.appendChild(sourceTag);

    const metaRow = document.createElement('div');
    metaRow.className = 'artifact-row-meta';
    const subjChip = document.createElement('span');
    subjChip.className = 'artifact-meta-chip subject-chip';
    subjChip.textContent = subjectText;
    metaRow.appendChild(subjChip);

    if (item.session_id) {
        const chatBtn = document.createElement('button');
        chatBtn.type = 'button';
        chatBtn.className = 'artifact-meta-chip chat-chip';
        const chatTitle = item.session_title || 'Open Chat';
        chatBtn.textContent = chatTitle.length > 36 ? `${chatTitle.slice(0, 36)}…` : chatTitle;
        chatBtn.title = `Open chat: ${chatTitle}`;
        chatBtn.addEventListener('click', () => {
            hideArtifactsView();
            if (item.session_id !== getSessionId()) {
                switchSession(item.session_id);
            }
        });
        metaRow.appendChild(chatBtn);
    }

    const subText = [tsLabel, sizeLabel].filter(Boolean).join(' · ');
    if (subText) {
        const subEl = document.createElement('span');
        subEl.className = 'artifact-row-sub';
        subEl.textContent = subText;
        metaRow.appendChild(subEl);
    }

    mainEl.appendChild(topRow);
    mainEl.appendChild(metaRow);

    const actionsEl = document.createElement('div');
    actionsEl.className = 'artifact-row-actions';
    if (previewHref) {
        const previewLink = document.createElement('a');
        previewLink.href = previewHref;
        previewLink.target = '_blank';
        previewLink.rel = 'noopener';
        previewLink.className = 'artifact-row-icon-btn';
        previewLink.title = 'Preview';
        previewLink.setAttribute('aria-label', `Preview ${item.name || 'artifact'}`);
        previewLink.innerHTML = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle></svg>`;
        actionsEl.appendChild(previewLink);
    }
    const dlLink = document.createElement('a');
    dlLink.href = downloadHref;
    dlLink.download = item.name || '';
    dlLink.className = 'artifact-row-icon-btn';
    dlLink.title = 'Download';
    dlLink.setAttribute('aria-label', `Download ${item.name || 'artifact'}`);
    dlLink.innerHTML = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>`;
    actionsEl.appendChild(dlLink);

    row.appendChild(iconEl);
    row.appendChild(mainEl);
    row.appendChild(actionsEl);
    return row;
}

function renderArtifactsView() {
    if (!artifactsListContainer) return;
    const filtered = getFilteredArtifacts();
    if (artifactsCountBadge) {
        artifactsCountBadge.textContent = filtered.length === artifactsAll.length
            ? `${artifactsAll.length}`
            : `${filtered.length} / ${artifactsAll.length}`;
    }

    artifactsListContainer.innerHTML = '';
    if (filtered.length === 0) {
        artifactsListContainer.innerHTML = '<div class="artifacts-empty">No artifacts match the current filters.</div>';
        return;
    }

    const groupMode = artifactsGroupBy ? artifactsGroupBy.value : 'all';
    if (groupMode === 'all') {
        const list = document.createElement('div');
        list.className = 'artifacts-list';
        filtered.forEach((item) => list.appendChild(buildArtifactRow(item)));
        artifactsListContainer.appendChild(list);
        return;
    }

    const groups = new Map();
    filtered.forEach((item) => {
        const key = groupMode === 'subject'
            ? (item.subject || 'General (No Subject)')
            : (item.session_id ? `${item.session_title || 'Untitled Chat'}||${item.session_id}` : 'Unlinked / Direct Files||');
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(item);
    });

    for (const [groupKey, items] of groups.entries()) {
        const section = document.createElement('details');
        section.className = 'artifacts-group';
        section.open = true;

        const summary = document.createElement('summary');
        summary.className = 'artifacts-group-summary';
        const displayLabel = groupMode === 'chat' ? groupKey.split('||')[0] : groupKey;
        summary.innerHTML = `
            <span class="artifacts-group-left">
                <span class="artifacts-group-toggle" title="Collapse / expand group" aria-hidden="true">
                    <svg class="artifacts-group-chevron" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"></polyline></svg>
                </span>
                <span class="artifacts-group-title">${escapeHtml(displayLabel)}</span>
            </span>
            <span class="artifacts-group-count">${items.length}</span>
        `;
        section.appendChild(summary);

        const list = document.createElement('div');
        list.className = 'artifacts-list';
        items.forEach((item) => list.appendChild(buildArtifactRow(item)));
        section.appendChild(list);
        artifactsListContainer.appendChild(section);
    }
}

async function fetchArtifacts() {
    if (!artifactsListContainer) return;
    artifactsListContainer.innerHTML = '<div class="artifacts-empty">Loading artifacts...</div>';
    try {
        const resp = await fetch('/api/artifacts');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        artifactsAll = Array.isArray(data.artifacts) ? data.artifacts : [];
        artifactsSubjectsList = Array.isArray(data.subjects) ? data.subjects : [];
        artifactsChatsList = Array.isArray(data.chats) ? data.chats : [];
        populateArtifactsFilterDropdowns();
        renderArtifactsView();
    } catch (err) {
        artifactsListContainer.innerHTML = `<div class="artifacts-empty">Failed to load artifacts: ${escapeHtml(err.message)}</div>`;
    }
}

function showArtifactsView() {
    if (isVoiceActive) {
        stopVoice();
    }
    if (!artifactsView) return;
    if (chatScrollArea) chatScrollArea.hidden = true;
    if (inputBar) inputBar.hidden = true;
    artifactsView.hidden = false;
    if (sidebarArtifactsBtn) sidebarArtifactsBtn.classList.add('active');
    fetchArtifacts();
}

function hideArtifactsView() {
    if (!artifactsView) return;
    artifactsView.hidden = true;
    if (chatScrollArea) chatScrollArea.hidden = false;
    if (inputBar) inputBar.hidden = false;
    if (sidebarArtifactsBtn) sidebarArtifactsBtn.classList.remove('active');
}

if (sidebarArtifactsBtn) {
    sidebarArtifactsBtn.addEventListener('click', () => {
        if (artifactsView && !artifactsView.hidden) {
            hideArtifactsView();
        } else {
            showArtifactsView();
        }
    });
}
if (backToChatBtn) {
    backToChatBtn.addEventListener('click', hideArtifactsView);
}
if (artifactsRefreshBtn) {
    artifactsRefreshBtn.addEventListener('click', fetchArtifacts);
}
if (artifactsSearchInput) {
    artifactsSearchInput.addEventListener('input', renderArtifactsView);
}
if (artifactsSubjectFilter) {
    artifactsSubjectFilter.addEventListener('change', renderArtifactsView);
}
if (artifactsGroupBy) {
    artifactsGroupBy.addEventListener('change', renderArtifactsView);
}
if (artifactsTimePreset) {
    artifactsTimePreset.addEventListener('change', () => {
        if (artifactsCustomRange) {
            artifactsCustomRange.hidden = artifactsTimePreset.value !== 'custom';
        }
        renderArtifactsView();
    });
}
if (artifactsDateFrom) {
    artifactsDateFrom.addEventListener('change', renderArtifactsView);
}
if (artifactsDateTo) {
    artifactsDateTo.addEventListener('change', renderArtifactsView);
}
document.querySelectorAll('.artifacts-pill').forEach((btn) => {
    btn.addEventListener('click', () => {
        artifactsActiveSource = btn.dataset.source || '';
        document.querySelectorAll('.artifacts-pill').forEach((b) => b.classList.toggle('active', b === btn));
        renderArtifactsView();
    });
});
document.querySelectorAll('.artifacts-type-pill').forEach((btn) => {
    btn.addEventListener('click', () => {
        artifactsActiveType = btn.dataset.type || '';
        document.querySelectorAll('.artifacts-type-pill').forEach((b) => b.classList.toggle('active', b === btn));
        renderArtifactsView();
    });
});
if (artifactsResetFiltersBtn) {
    artifactsResetFiltersBtn.addEventListener('click', () => {
        if (artifactsSearchInput) artifactsSearchInput.value = '';
        if (artifactsSubjectFilter) artifactsSubjectFilter.value = '';
        if (artifactsTimePreset) artifactsTimePreset.value = '';
        if (artifactsGroupBy) artifactsGroupBy.value = 'all';
        if (artifactsDateFrom) artifactsDateFrom.value = '';
        if (artifactsDateTo) artifactsDateTo.value = '';
        if (artifactsCustomRange) artifactsCustomRange.hidden = true;
        artifactsActiveSource = '';
        artifactsActiveType = '';
        document.querySelectorAll('.artifacts-pill').forEach((b) => b.classList.toggle('active', (b.dataset.source || '') === ''));
        document.querySelectorAll('.artifacts-type-pill').forEach((b) => b.classList.toggle('active', (b.dataset.type || '') === ''));
        renderArtifactsView();
    });
}

// ── Initialize ────────────────────────────────────────────────────
// Reconnect automatically if the user comes back to the tab after the device slept
document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
        if (!ws || ws.readyState === WebSocket.CLOSED) {
            reconnectAttempts = 0; // Reset attempts when they return
            connect();
        }
    }
});

if (newChatBtn) {
    newChatBtn.addEventListener('click', () => {
        // Start a fresh session without full page reload
        const sid = Math.random().toString(36).substring(2) + Date.now().toString(36);
        switchSession(sid);
    });
}

initAuth().then((ok) => {
    if (ok) {
        connect();
        fetchSubjects();
        fetchSessions();
    }
});
