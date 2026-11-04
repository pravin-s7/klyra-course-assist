/* ── Klyra Web UI — Client-side JavaScript ──────────────────────── */

// ── State ─────────────────────────────────────────────────────────
let ws = null;
let isVoiceActive = false;
let audioContext = null;
let mediaStream = null;
let scriptProcessor = null;
let playbackContext = null;
let nextPlayTime = 0;
let currentKlyraMessageEl = null;
let currentKlyraText = '';
let reconnectAttempts = 0;
const MAX_RECONNECT = 5;

// ── DOM refs ──────────────────────────────────────────────────────
const chatInput = document.getElementById('chatInput');
const sendBtn = document.getElementById('sendBtn');
const micBtn = document.getElementById('micBtn');


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

function showLogin() {
    const overlay = document.getElementById('loginOverlay');
    if (overlay) overlay.style.display = 'flex';
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
            '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
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
    localStorage.setItem(sessionStorageKey(), sid);
    
    // Clear the chat UI locally
    messages.innerHTML = '';
    currentKlyraMessageEl = null;
    currentKlyraText = '';
    if (window.toolCounters) window.toolCounters = {};
    
    // Show welcome screen
    welcomeScreen.style.display = 'flex';
    
    // Reconnect websocket
    if (ws) {
        ws.onclose = null; // Prevent disconnected UI/auto-reconnect from old socket
        ws.onerror = null;
        ws.close();
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
            // Binary = audio from Klyra voice
            playAudioChunk(event.data);
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
            hideWelcome();
            hideTypingIndicator();
            if (!currentKlyraMessageEl) {
                currentKlyraMessageEl = addMessage('klyra', '');
            }
            currentKlyraText += msg.text;
            updateMessageContent(currentKlyraMessageEl, currentKlyraText);
            scrollToBottom();
            break;

        case 'user_transcript':
            hideWelcome();
            if (msg.final) {
                addMessage('user', msg.text);
                scrollToBottom();
            }
            break;

        case 'klyra_transcript':
            hideWelcome();
            hideTypingIndicator();
            if (!currentKlyraMessageEl) {
                currentKlyraMessageEl = addMessage('klyra', '');
            }
            currentKlyraText += msg.text;
            updateMessageContent(currentKlyraMessageEl, currentKlyraText);
            scrollToBottom();
            break;

        case 'tool_start':
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
            currentKlyraMessageEl = null;
            currentKlyraText = '';
            hideTypingIndicator();
            isGenerating = false;
            updateSendButton();
            fetchSessions();
            break;

        case 'voice_started':
            micBtn.classList.add('active');
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

function addMessage(role, text) {
    const div = document.createElement('div');
    div.className = `message ${role}`;

    const content = document.createElement('div');
    content.className = 'message-content';

    const bubble = document.createElement('div');
    bubble.className = 'message-bubble';
    bubble.innerHTML = renderMarkdown(text);
    if (window.MathJax && window.MathJax.typesetPromise) {
        MathJax.typesetPromise([bubble]).catch(err => console.log(err));
    }

    const actions = document.createElement('div');
    actions.className = 'message-actions';

    const copyBtn = document.createElement('button');
    copyBtn.className = 'action-btn';
    copyBtn.title = 'Copy';
    const copyIcon = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>';
    const checkIcon = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"></polyline></svg>';

    copyBtn.innerHTML = copyIcon;
    copyBtn.onclick = () => {
        navigator.clipboard.writeText(text).then(() => {
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

function updateMessageContent(bubbleEl, text) {
    if (bubbleEl) {
        bubbleEl.innerHTML = renderMarkdown(text);
        if (window.MathJax && window.MathJax.typesetPromise) {
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

    const eyeSvg = `<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle></svg>`;
    const downloadSvg = `<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>`;

    div.innerHTML = `
        <div class="file-card-row" style="display: flex; align-items: center; justify-content: space-between; width: 100%;">
            <div style="display: flex; align-items: center; gap: 12px; cursor: default;">
                <span class="file-icon" style="display:flex; align-items:center; color: var(--text-muted);">${icon}</span>
                <div class="file-info">
                    <span class="file-name">${escapeHtml(name)}</span>
                    <span class="file-type">${ext.replace('.', '')}</span>
                </div>
            </div>
            <div class="file-card-actions">
                ${canPreview ? `<a href="#" class="preview-link" title="${isImage ? 'Toggle Image Display' : 'Preview'}" style="display:flex; align-items:center;">${eyeSvg}</a>` : ''}
                <a href="${url}?download=true" download="${escapeHtml(name)}" title="Download" style="display:flex; align-items:center;">${downloadSvg}</a>
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
                window.open(url, '_blank');
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

// ── Markdown & Image rendering ────────────────────────────────────
function resolveAssetUrl(rawUrl, baseUrl = '') {
    if (!rawUrl) return rawUrl;
    let url = rawUrl.trim();
    if (/^(https?:|data:|blob:)/i.test(url)) return url;

    // Extract subject from baseUrl like '/files/<subject>/generated/...' if available
    let subject = '';
    const baseMatch = (baseUrl || '').match(/^\/files\/([^/]+)\//);
    if (baseMatch) {
        subject = baseMatch[1];
    }

    // 1. 'docs/<subject>/.assets/...' or 'docs/<subject>/assets/...'
    const docsMatch = url.match(/^\/?docs\/([^/]+)\/\.?assets\/(.+)$/);
    if (docsMatch) {
        return `/files/${docsMatch[1]}/.assets/${docsMatch[2]}`;
    }

    // 2. '/files/<subject>/assets/...' (missing dot on .assets)
    const filesAssetsMatch = url.match(/^\/files\/([^/]+)\/assets\/(.+)$/);
    if (filesAssetsMatch) {
        return `/files/${filesAssetsMatch[1]}/.assets/${filesAssetsMatch[2]}`;
    }

    // 3. Relative '../.assets/...', './.assets/...', '.assets/...', or '../assets/...'
    const relAssetsMatch = url.match(/^(?:\.\.\/|\.\/)*\.?assets\/(.+)$/);
    if (relAssetsMatch) {
        if (subject) {
            return `/files/${subject}/.assets/${relAssetsMatch[1]}`;
        }
        return `/.assets/${relAssetsMatch[1]}`;
    }

    // 4. '<subject>/.assets/...' without leading '/files/'
    const subjAssetsMatch = url.match(/^([a-zA-Z0-9_-]+)\/\.assets\/(.+)$/);
    if (subjAssetsMatch) {
        return `/files/${subjAssetsMatch[1]}/.assets/${subjAssetsMatch[2]}`;
    }

    return url;
}

function normalizeMarkdownImages(text, baseUrl = '') {
    if (!text) return '';
    // Try to infer subject from text if baseUrl didn't provide it
    let inferredBase = baseUrl;
    if (!inferredBase) {
        const subjMatch = text.match(/\/files\/([a-zA-Z0-9_-]+)\/|docs\/([a-zA-Z0-9_-]+)\//);
        if (subjMatch) {
            const s = subjMatch[1] || subjMatch[2];
            inferredBase = `/files/${s}/`;
        }
    }

    // 1. Rewrite ![alt](url) image paths
    let out = text.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, (match, alt, rawUrl) => {
        const resolved = resolveAssetUrl(rawUrl, inferredBase);
        return `![${alt}](${resolved})`;
    });

    // 2. Convert non-image markdown links [label](url.png) that point to image files into inline images
    out = out.replace(/(?<!!)\[([^\]]+)\]\(([^)]+\.(?:png|jpg|jpeg|gif|svg|webp)(?:\?[^)]*)?)\)/gi, (match, label, rawUrl) => {
        const resolved = resolveAssetUrl(rawUrl, inferredBase);
        return `![${label}](${resolved})`;
    });

    return out;
}

function renderMarkdown(text, baseUrl = '') {
    if (!text) return '';
    try {
        const normalized = normalizeMarkdownImages(text, baseUrl);
        return marked.parse(normalized);
    } catch (e) {
        console.error("Markdown parsing failed:", e);
        return escapeHtml(text);
    }
}

function openImageLightbox(src, altText = '') {
    let overlay = document.getElementById('imageLightboxOverlay');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'imageLightboxOverlay';
        overlay.className = 'image-lightbox-overlay';
        overlay.innerHTML = `
            <div class="image-lightbox-content">
                <button class="image-lightbox-close" title="Close (Esc)">✕</button>
                <img class="image-lightbox-img" src="" alt="" />
                <div class="image-lightbox-caption"></div>
            </div>
        `;
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay || e.target.classList.contains('image-lightbox-close')) {
                overlay.style.display = 'none';
            }
        });
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && overlay.style.display === 'flex') {
                overlay.style.display = 'none';
            }
        });
        document.body.appendChild(overlay);
    }
    const img = overlay.querySelector('.image-lightbox-img');
    const cap = overlay.querySelector('.image-lightbox-caption');
    img.src = src;
    img.alt = altText || '';
    cap.textContent = altText || '';
    overlay.style.display = 'flex';
}

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

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
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

// ── Send message ──────────────────────────────────────────────────
function sendMessage() {
    if (isGenerating) {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'chat_stop' }));
        }
        return;
    }
    const text = chatInput.value.trim();
    if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;

    hideWelcome();
    addMessage('user', text);
    showTypingIndicator();
    scrollToBottom();

    isGenerating = true;
    updateSendButton();

    ws.send(JSON.stringify({ type: 'chat', text }));
    chatInput.value = '';
    chatInput.style.height = 'auto';
}

function sendQuickAction(text) {
    chatInput.value = text;
    sendMessage();
}

// ── Voice ─────────────────────────────────────────────────────────
async function startVoice() {
    if (isVoiceActive) {
        stopVoice();
        return;
    }

    try {
        // Request mic access
        mediaStream = await navigator.mediaDevices.getUserMedia({
            audio: { channelCount: 1, echoCancellation: true }
        });

        const track = mediaStream.getAudioTracks()[0];
        const settings = track.getSettings();
        const streamRate = settings.sampleRate;
        const ctxOptions = streamRate ? { sampleRate: streamRate } : undefined;
        
        // Set up audio capture using the stream's exact sample rate
        audioContext = new (window.AudioContext || window.webkitAudioContext)(ctxOptions);
        const nativeSampleRate = audioContext.sampleRate;
        const targetSampleRate = 16000;
        
        const source = audioContext.createMediaStreamSource(mediaStream);

        // ScriptProcessor for capturing PCM data
        scriptProcessor = audioContext.createScriptProcessor(4096, 1, 1);
        source.connect(scriptProcessor);
        scriptProcessor.connect(audioContext.destination);

        scriptProcessor.onaudioprocess = (e) => {
            if (!isVoiceActive || !ws || ws.readyState !== WebSocket.OPEN) return;
            const float32 = e.inputBuffer.getChannelData(0);
            
            // Simple downsampling
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

        // Set up playback
        playbackContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 24000 });
        nextPlayTime = 0;

        // Tell server to start voice session
        ws.send(JSON.stringify({ type: 'voice_start' }));
        isVoiceActive = true;
        micBtn.classList.add('active');

    } catch (err) {
        console.error('Failed to start voice:', err);
        addMessage('klyra', `⚠️ Could not access microphone: ${err.message}`);
    }
}

function stopVoice() {
    isVoiceActive = false;
    micBtn.classList.remove('active');

    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'voice_stop' }));
    }

    if (scriptProcessor) {
        scriptProcessor.disconnect();
        scriptProcessor = null;
    }
    if (audioContext) {
        audioContext.close();
        audioContext = null;
    }
    if (mediaStream) {
        mediaStream.getTracks().forEach(t => t.stop());
        mediaStream = null;
    }
    if (playbackContext) {
        playbackContext.close();
        playbackContext = null;
    }
}

async function playAudioChunk(blob) {
    if (!playbackContext) return;

    try {
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
    // Clear backend chat history and local session ID (per-user)
    localStorage.removeItem(sessionStorageKey());
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'clear_chat' }));
        ws.close(); // Force reconnect to get a fresh session
    }

    // Clear frontend messages
    const messageNodes = Array.from(messages.childNodes);
    messageNodes.forEach(node => {
        if (node.id !== 'welcomeScreen') {
            node.remove();
        }
    });

    // Show welcome screen again
    if (welcomeScreen) {
        welcomeScreen.style.display = 'flex';
    }

    // Reset state
    currentKlyraMessageEl = null;
    currentKlyraText = '';
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

    const renderSessionList = (list, title, isTop) => {
        if (list.length === 0) return;
        
        const header = document.createElement('div');
        header.className = 'sidebar-header';
        header.style.marginTop = isTop ? '0' : '16px';
        header.innerHTML = `<h3>${title}</h3>`;
        chatSessionsList.appendChild(header);

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

        chatSessionsList.appendChild(item);
    });
    };

    renderSessionList(pinnedSessions, "Pinned Chats", true);
    renderSessionList(unpinnedSessions, "Recent Chats", pinnedSessions.length === 0);
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
