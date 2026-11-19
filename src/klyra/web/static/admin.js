/* ── Klyra Admin Console ─────────────────────────────────────────── */
let currentUser = null;
let allUsers = [];
let allSubjects = [];
let knownSubjectNames = [];
let currentInspectedUser = null;
let currentInspectedSessions = [];
let currentInspectedFiles = [];
let allLogEntries = [];

// ── Helpers ────────────────────────────────────────────────────────
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text == null ? '' : String(text);
    return div.innerHTML;
}

function fmtDate(iso) {
    if (!iso) return '—';
    try {
        const d = new Date(iso);
        if (isNaN(d)) return iso;
        return d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
    } catch (e) {
        return iso;
    }
}

function fmtTimestampExact(iso) {
    if (!iso) return '—';
    try {
        const d = new Date(iso);
        if (isNaN(d)) return iso;
        return d.toLocaleString(undefined, {
            year: 'numeric',
            month: 'short',
            day: '2-digit',
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
        });
    } catch (e) {
        return iso;
    }
}

function fmtBytes(n) {
    if (!n) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB'];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) {
        n /= 1024;
        i++;
    }
    return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
}

function showToast(message, type = 'info') {
    const container = document.getElementById('adminToastContainer');
    if (!container) return;
    const toast = document.createElement('div');
    toast.className = `admin-toast ${type}`;
    const icon = type === 'error' ? 'error' : (type === 'success' ? 'check_circle' : 'info');
    toast.innerHTML = `
        <span class="material-symbols-outlined" style="font-size:18px;">${icon}</span>
        <span>${escapeHtml(message)}</span>
    `;
    container.appendChild(toast);
    setTimeout(() => {
        toast.classList.add('fade-out');
        setTimeout(() => toast.remove(), 250);
    }, 3200);
}

// ── Custom UI Confirmation Dialog (replaces browser default confirm()) ──
function askConfirm({
    title = 'Confirm Action',
    message = 'Are you sure you want to proceed?',
    confirmText = 'Confirm',
    danger = true,
} = {}) {
    return new Promise((resolve) => {
        const modal = document.getElementById('confirmModal');
        if (!modal) {
            resolve(true);
            return;
        }
        const titleEl = document.getElementById('confirmModalTitle');
        const msgEl = document.getElementById('confirmModalMessage');
        const okBtn = document.getElementById('confirmModalOkBtn');
        const cancelBtn = document.getElementById('confirmModalCancelBtn');
        const closeBtn = document.getElementById('confirmModalCloseBtn');

        if (titleEl) titleEl.textContent = title;
        if (msgEl) msgEl.textContent = message;
        if (okBtn) {
            okBtn.textContent = confirmText;
            okBtn.className = danger ? 'btn danger-btn' : 'btn primary-btn';
        }

        let settled = false;
        const finish = (result) => {
            if (settled) return;
            settled = true;
            cleanup();
            if (modal.open) modal.close();
            resolve(result);
        };

        const onOk = () => finish(true);
        const onCancel = () => finish(false);
        const onDialogClose = () => finish(false);

        const cleanup = () => {
            okBtn?.removeEventListener('click', onOk);
            cancelBtn?.removeEventListener('click', onCancel);
            closeBtn?.removeEventListener('click', onCancel);
            modal.removeEventListener('close', onDialogClose);
        };

        okBtn?.addEventListener('click', onOk);
        cancelBtn?.addEventListener('click', onCancel);
        closeBtn?.addEventListener('click', onCancel);
        modal.addEventListener('close', onDialogClose);

        if (!modal.open) modal.showModal();
        okBtn?.focus();
    });
}

// ── Light-dismiss fallback for <dialog closedby="any"> ─────────────
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

// ── Auth gate ──────────────────────────────────────────────────────
async function requireAdmin() {
    try {
        const resp = await fetch('/api/auth/me');
        if (resp.ok) {
            currentUser = await resp.json();
            if (currentUser.role === 'admin') {
                const displayName = currentUser.full_name || currentUser.username;
                const badgeEl = document.getElementById('adminDisplayName');
                if (badgeEl) {
                    badgeEl.innerHTML = `<span class="material-symbols-outlined" style="font-size:16px;color:var(--accent);">verified_user</span> <span class="user-name">${escapeHtml(displayName)}</span>`;
                }
                document.getElementById('adminMain').style.display = 'block';
                init();
                return;
            }
        }
    } catch (err) {
        console.error('Auth check failed:', err);
    }
    document.getElementById('adminDenied').style.display = 'flex';
    document.getElementById('adminMain').style.display = 'none';
}

// ── View switching ─────────────────────────────────────────────────
function switchView(name) {
    document.querySelectorAll('.admin-nav-item').forEach(b => {
        b.classList.toggle('active', b.dataset.view === name);
    });
    document.querySelectorAll('.admin-view').forEach(v => {
        v.style.display = v.id === 'view-' + name ? 'block' : 'none';
    });
    if (name === 'overview') loadStats();
    if (name === 'users') loadUsers();
    if (name === 'chats') loadAdminAllChats();
    if (name === 'artifacts') loadAdminAllArtifacts();
    if (name === 'courses') loadCourses();
    if (name === 'settings') loadConfig();
    if (name === 'logs') loadLogs();
}

document.querySelectorAll('.admin-nav-item').forEach(btn => {
    btn.addEventListener('click', () => switchView(btn.dataset.view));
});

// ── Overview ───────────────────────────────────────────────────────
async function loadStats() {
    try {
        const resp = await fetch('/api/admin/stats');
        if (!resp.ok) throw new Error(await resp.text());
        const s = await resp.json();
        knownSubjectNames = s.subject_names || [];

        const cards = [
            {
                label: 'Users',
                value: s.users,
                sub: `${s.instructors} instructors · ${s.admins} admins`,
                icon: 'group',
                tone: 'tone-blue',
                targetView: 'users',
            },
            {
                label: 'Chat Sessions',
                value: s.chat_sessions,
                sub: 'across all users',
                icon: 'chat',
                tone: 'tone-violet',
                targetView: 'chats',
            },
            {
                label: 'Generated Files',
                value: s.generated_files,
                sub: 'tracked with RBAC ownership',
                icon: 'description',
                tone: 'tone-amber',
                targetView: 'artifacts',
            },
            {
                label: 'Courses',
                value: s.subjects,
                sub: (s.subject_names || []).slice(0, 4).join(', ') || 'none',
                icon: 'school',
                tone: 'tone-emerald',
                targetView: 'courses',
            },
        ];

        const statCardsEl = document.getElementById('statCards');
        if (statCardsEl) {
            statCardsEl.innerHTML = cards.map(c => `
                <div class="stat-card clickable ${c.tone}" onclick="switchView('${c.targetView}')" title="Go to ${c.label}">
                    <div class="stat-card-top">
                        <div class="stat-icon ${c.tone}">
                            <span class="material-symbols-outlined">${c.icon}</span>
                        </div>
                        <span class="material-symbols-outlined stat-arrow">arrow_outward</span>
                    </div>
                    <div class="stat-value">${c.value}</div>
                    <div class="stat-label">${c.label}</div>
                    <div class="stat-sub">${escapeHtml(c.sub)}</div>
                </div>`).join('');
        }

        const healthEl = document.getElementById('systemHealthStrip');
        if (healthEl && s.system_health) {
            const h = s.system_health;
            const keyOk = h.gemini_api_configured;
            healthEl.innerHTML = `
                <span class="course-chip" title="Database Backend">
                    <span class="material-symbols-outlined" style="font-size:14px;color:var(--accent);">database</span>
                    DB: <strong>${escapeHtml(h.database || 'SQLite')}</strong>
                </span>
                <span class="course-chip" title="Vector Store Backend">
                    <span class="material-symbols-outlined" style="font-size:14px;color:var(--accent);">hub</span>
                    Vectors: <strong>${escapeHtml(h.vector_store || 'ChromaDB')}</strong>
                </span>
                <span class="course-chip" title="Active Chat / Crew Model">
                    <span class="material-symbols-outlined" style="font-size:14px;color:var(--accent);">smart_toy</span>
                    Model: <strong>${escapeHtml(h.active_model || 'default')}</strong>
                </span>
                <span class="course-chip" title="Gemini API Key Status">
                    <span class="material-symbols-outlined" style="font-size:14px;color:${keyOk ? 'var(--success-color, #22c55e)' : 'var(--error-color, #ef4444)'};">${keyOk ? 'check_circle' : 'error'}</span>
                    Gemini Key: <strong>${keyOk ? 'Configured' : 'Missing'}</strong>
                </span>
            `;
        }

        const subjOverviewEl = document.getElementById('subjectsOverview');
        if (subjOverviewEl) {
            if ((s.subject_names || []).length === 0) {
                subjOverviewEl.innerHTML = '<div class="detail-empty">No subjects under <code>docs/</code> yet. Click <strong>New Subject</strong> to create one.</div>';
            } else {
                subjOverviewEl.innerHTML = (s.subject_names || []).map(n => `
                    <button type="button" class="course-chip interactive-chip" onclick="openSubjectDetail('${escapeHtml(n)}')" title="Inspect files in ${escapeHtml(n)}">
                        <span class="material-symbols-outlined" style="font-size:15px;color:var(--accent);">folder_open</span>
                        <span>${escapeHtml(n)}</span>
                    </button>`).join('');
            }
        }

        const recentEl = document.getElementById('recentSessionsList');
        if (recentEl) {
            const recent = s.recent_sessions || [];
            if (recent.length === 0) {
                recentEl.innerHTML = '<div class="detail-empty">No chat sessions recorded yet.</div>';
            } else {
                recentEl.innerHTML = recent.map(sess => `
                    <div class="detail-row">
                        <div class="detail-row-main">
                            <div class="detail-row-title">${escapeHtml(sess.title || 'Untitled Chat')}</div>
                            <div class="detail-row-sub">
                                <strong>@${escapeHtml(sess.owner_username)}</strong> · ${sess.message_count} events · ${fmtDate(sess.created_at)}
                            </div>
                        </div>
                        <button type="button" class="btn small-btn" data-sid="${escapeHtml(sess.session_id)}" data-title="${escapeHtml(sess.title || 'Chat')}" onclick="openChatViewer(this.dataset.sid, this.dataset.title)">
                            <span class="material-symbols-outlined" style="font-size:15px;">visibility</span> View
                        </button>
                    </div>`).join('');
            }
        }
    } catch (err) {
        console.error('Failed to load stats:', err);
    }
}

// ── Course Picker Chips for User Modals ────────────────────────────
function renderCoursePickerChips(containerId, inputId) {
    const container = document.getElementById(containerId);
    const input = document.getElementById(inputId);
    if (!container || !input) return;

    const subjects = knownSubjectNames.length
        ? knownSubjectNames
        : allSubjects.map(s => s.name);

    if (!subjects.length) {
        container.innerHTML = '';
        return;
    }

    const getSelectedSet = () => new Set(
        input.value.split(',').map(s => s.trim().toLowerCase()).filter(Boolean)
    );

    const syncButtons = () => {
        const selected = getSelectedSet();
        container.querySelectorAll('.picker-chip').forEach(btn => {
            btn.classList.toggle('selected', selected.has(btn.dataset.course));
        });
    };

    container.innerHTML = subjects.map(subj => `
        <button type="button" class="picker-chip" data-course="${escapeHtml(subj.toLowerCase())}">
            <span class="material-symbols-outlined" style="font-size:14px;">school</span>
            ${escapeHtml(subj)}
        </button>
    `).join('');

    container.querySelectorAll('.picker-chip').forEach(btn => {
        btn.addEventListener('click', () => {
            const c = btn.dataset.course;
            const selected = getSelectedSet();
            if (selected.has(c)) selected.delete(c);
            else selected.add(c);
            input.value = Array.from(selected).join(', ');
            syncButtons();
        });
    });

    input.oninput = syncButtons;
    syncButtons();
}

function openCreateUserModal() {
    const form = document.getElementById('createUserForm');
    if (form) form.reset();
    const errEl = document.getElementById('createError');
    if (errEl) errEl.style.display = 'none';
    renderCoursePickerChips('newCoursesPicker', 'newCourses');
    document.getElementById('createUserModal').showModal();
}

// ── Users ──────────────────────────────────────────────────────────
async function loadUsers() {
    try {
        const resp = await fetch('/api/admin/users');
        if (!resp.ok) throw new Error(await resp.text());
        allUsers = (await resp.json()).users || [];
        renderUsers(allUsers);
    } catch (err) {
        console.error('Failed to load users:', err);
    }
}

function coursesToChips(courses) {
    if (!courses || courses.length === 0) {
        return '<span style="color:var(--text-muted); font-size:12px;">None assigned</span>';
    }
    return courses.map(c => `<span class="course-chip">${escapeHtml(c)}</span>`).join('');
}

function renderUsers(users) {
    const tbody = document.getElementById('usersBody');
    const q = (document.getElementById('usersSearch')?.value || '').toLowerCase().trim();
    const roleFilter = (document.getElementById('usersRoleFilter')?.value || '').toLowerCase().trim();

    const filtered = users.filter(u => {
        if (roleFilter && (u.role || '').toLowerCase() !== roleFilter) return false;
        if (!q) return true;
        return (
            (u.username || '').toLowerCase().includes(q) ||
            (u.full_name || '').toLowerCase().includes(q) ||
            (u.email || '').toLowerCase().includes(q) ||
            (u.role || '').toLowerCase().includes(q) ||
            (u.courses || []).some(c => c.toLowerCase().includes(q))
        );
    });

    const badge = document.getElementById('usersCountBadge');
    if (badge) badge.textContent = filtered.length;

    tbody.innerHTML = '';
    if (filtered.length === 0) {
        const tr = document.createElement('tr');
        tr.innerHTML = `<td colspan="6" style="text-align:center; color:var(--text-muted); padding:24px;">No matching users found.</td>`;
        tbody.appendChild(tr);
        return;
    }

    filtered.forEach(user => {
        const isSelf = currentUser && user.id === currentUser.id;
        const tr = document.createElement('tr');

        const nameTd = document.createElement('td');
        nameTd.innerHTML = `
            <div style="display:flex; align-items:center; gap:8px;">
                <span style="font-weight:600;">${escapeHtml(user.username)}</span>
                ${isSelf ? '<span class="you-pill">You</span>' : ''}
            </div>
            <div style="font-size:11.5px; color:var(--text-muted); margin-top:2px;">Created ${fmtDate(user.created_at)}</div>
        `;

        const fnTd = document.createElement('td');
        fnTd.innerHTML = user.full_name ? escapeHtml(user.full_name) : '<span style="color:var(--text-muted);">—</span>';

        const emTd = document.createElement('td');
        emTd.innerHTML = user.email ? escapeHtml(user.email) : '<span style="color:var(--text-muted);">—</span>';

        const roleTd = document.createElement('td');
        roleTd.innerHTML = `<span class="role-badge ${escapeHtml(user.role)}">${escapeHtml(user.role)}</span>`;

        const coursesTd = document.createElement('td');
        coursesTd.innerHTML = user.role === 'admin'
            ? '<span class="course-chip admin-all-chip">All courses (Admin)</span>'
            : coursesToChips(user.courses);

        const actionsTd = document.createElement('td');
        actionsTd.style.textAlign = 'right';
        const btnRow = document.createElement('div');
        btnRow.className = 'admin-edit-actions';
        btnRow.style.justifyContent = 'flex-end';

        const viewBtn = document.createElement('button');
        viewBtn.type = 'button';
        viewBtn.className = 'btn small-btn';
        viewBtn.innerHTML = '<span class="material-symbols-outlined" style="font-size:15px;">chat</span> Chats';
        viewBtn.title = 'Inspect chat sessions & history';
        viewBtn.addEventListener('click', () => openUserDetail(user, 'chats'));

        const artBtn = document.createElement('button');
        artBtn.type = 'button';
        artBtn.className = 'btn small-btn';
        artBtn.innerHTML = '<span class="material-symbols-outlined" style="font-size:15px;">collections_bookmark</span> Artifacts';
        artBtn.title = 'Inspect uploaded & generated artifacts';
        artBtn.addEventListener('click', () => openUserDetail(user, 'artifacts'));

        const editBtn = document.createElement('button');
        editBtn.type = 'button';
        editBtn.className = 'btn small-btn';
        editBtn.innerHTML = '<span class="material-symbols-outlined" style="font-size:15px;">edit</span> Edit';
        editBtn.addEventListener('click', () => openEditUserModal(user));

        btnRow.appendChild(viewBtn);
        btnRow.appendChild(artBtn);
        btnRow.appendChild(editBtn);

        if (!isSelf) {
            const delBtn = document.createElement('button');
            delBtn.type = 'button';
            delBtn.className = 'btn small-btn danger-btn';
            delBtn.innerHTML = '<span class="material-symbols-outlined" style="font-size:15px;">delete</span>';
            delBtn.title = `Delete user ${user.username}`;
            delBtn.addEventListener('click', async () => {
                const ok = await askConfirm({
                    title: `Delete User "@${user.username}"`,
                    message: `Delete user "${user.username}"? This action permanently removes the user account and cannot be undone.`,
                    confirmText: 'Delete User',
                    danger: true,
                });
                if (!ok) return;
                const resp = await fetch(`/api/admin/users/${user.id}`, { method: 'DELETE' });
                if (resp.ok) {
                    showToast(`Deleted user "${user.username}"`, 'success');
                    loadUsers();
                    loadStats();
                } else {
                    showToast('Failed to delete: ' + await resp.text(), 'error');
                }
            });
            btnRow.appendChild(delBtn);
        }

        actionsTd.appendChild(btnRow);

        tr.appendChild(nameTd);
        tr.appendChild(fnTd);
        tr.appendChild(emTd);
        tr.appendChild(roleTd);
        tr.appendChild(coursesTd);
        tr.appendChild(actionsTd);
        tbody.appendChild(tr);
    });
}

async function putUser(id, payload) {
    const resp = await fetch(`/api/admin/users/${id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });
    if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        throw new Error(data.detail || await resp.text() || 'Failed to update user');
    }
    await loadUsers();
}

// ── User detail modal (with search bar & filters) ──────────────────
async function openUserDetail(user, initialSection = 'all') {
    if (!user) return;
    currentInspectedUser = user;
    const modal = document.getElementById('userDetailModal');
    document.getElementById('detailUserName').textContent = user.full_name
        ? `${user.full_name} (@${user.username})`
        : user.username;
    document.getElementById('detailUserMeta').textContent =
        `${user.role.toUpperCase()} · courses: ${user.role === 'admin' ? 'all' : ((user.courses || []).join(', ') || 'none')}`;

    const searchInput = document.getElementById('userDetailSearch');
    const secSelect = document.getElementById('userDetailSectionFilter');
    const courseSelect = document.getElementById('userDetailCourseFilter');
    const typeSelect = document.getElementById('userDetailTypeFilter');
    const timePreset = document.getElementById('userDetailTimePreset');
    const startInput = document.getElementById('userDetailStartDate');
    const endInput = document.getElementById('userDetailEndDate');
    if (searchInput) searchInput.value = '';
    if (secSelect) secSelect.value = initialSection || 'all';
    if (typeSelect) typeSelect.value = '';
    if (timePreset) timePreset.value = '';
    if (startInput) startInput.value = '';
    if (endInput) endInput.value = '';

    currentInspectedSessions = [];
    try {
        const sResp = await fetch(`/api/admin/users/${user.id}/sessions`);
        if (sResp.ok) currentInspectedSessions = (await sResp.json()).sessions || [];
    } catch (e) {
        console.error(e);
    }

    currentInspectedFiles = [];
    try {
        const aResp = await fetch(`/api/admin/users/${user.id}/artifacts`);
        if (aResp.ok) {
            currentInspectedFiles = (await aResp.json()).artifacts || [];
        } else {
            const fResp = await fetch(`/api/admin/users/${user.id}/files`);
            if (fResp.ok) currentInspectedFiles = (await fResp.json()).files || [];
        }
    } catch (e) {
        console.error(e);
    }

    // Populate course filter dropdown with user's courses + artifact subjects
    if (courseSelect) {
        const courseSet = new Set([
            ...(user.courses || []).map(c => c.toLowerCase()),
            ...currentInspectedFiles.map(f => (f.subject || '').toLowerCase()).filter(Boolean),
        ]);
        const hasGeneral = currentInspectedFiles.some(f => !(f.subject || '').trim());
        courseSelect.innerHTML = '<option value="">All Courses</option>' +
            (hasGeneral ? '<option value="__general__">General (no course)</option>' : '') +
            Array.from(courseSet).sort().map(c => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join('');
    }

    filterUserDetailItems();
    if (!modal.open) modal.showModal();
}

function onUserDetailTimePresetChange() {
    const preset = document.getElementById('userDetailTimePreset')?.value || '';
    const startInput = document.getElementById('userDetailStartDate');
    const endInput = document.getElementById('userDetailEndDate');
    if (preset !== 'custom') {
        if (startInput) startInput.value = '';
        if (endInput) endInput.value = '';
    }
    filterUserDetailItems();
}

function onUserDetailCustomDateChange() {
    const startVal = document.getElementById('userDetailStartDate')?.value || '';
    const endVal = document.getElementById('userDetailEndDate')?.value || '';
    const presetEl = document.getElementById('userDetailTimePreset');
    if (presetEl) {
        presetEl.value = (startVal || endVal) ? 'custom' : '';
    }
    filterUserDetailItems();
}

function resetUserDetailFilters() {
    const ids = [
        ['userDetailSearch', ''],
        ['userDetailSectionFilter', 'all'],
        ['userDetailCourseFilter', ''],
        ['userDetailTypeFilter', ''],
        ['userDetailTimePreset', ''],
        ['userDetailStartDate', ''],
        ['userDetailEndDate', ''],
    ];
    for (const [id, val] of ids) {
        const el = document.getElementById(id);
        if (el) el.value = val;
    }
    filterUserDetailItems();
}

function _resolveTimeBounds(presetId, startId, endId) {
    const preset = document.getElementById(presetId)?.value || '';
    const now = Date.now();
    if (preset === '24h') return { startMs: now - 24 * 3600 * 1000, endMs: null };
    if (preset === '7d') return { startMs: now - 7 * 24 * 3600 * 1000, endMs: null };
    if (preset === '30d') return { startMs: now - 30 * 24 * 3600 * 1000, endMs: null };
    const startVal = document.getElementById(startId)?.value || '';
    const endVal = document.getElementById(endId)?.value || '';
    const startMs = startVal ? new Date(startVal).getTime() : null;
    const endMs = endVal ? new Date(endVal).getTime() : null;
    return {
        startMs: startMs && !isNaN(startMs) ? startMs : null,
        endMs: endMs && !isNaN(endMs) ? endMs : null,
    };
}

function _getUserDetailTimeBounds() {
    return _resolveTimeBounds('userDetailTimePreset', 'userDetailStartDate', 'userDetailEndDate');
}

function _renderAdminChatRow(s, opts = {}) {
    const ownerId = opts.userId || s.owner_id || '';
    const title = s.title || 'Untitled Chat';
    const tsRef = s.modified_at || s.created_at;
    const ownerChipHtml = opts.showOwner && s.owner_username
        ? `<span class="admin-art-owner-chip" title="Chat owner">@${escapeHtml(s.owner_username)}</span>`
        : '';
    const attBadge = (s.attachment_count || 0) > 0
        ? `<span class="admin-art-source-tag uploaded">${s.attachment_count} attachment${s.attachment_count === 1 ? '' : 's'}</span>`
        : '';
    const fileBadge = (s.file_link_count || 0) > 0
        ? `<span class="admin-art-source-tag generated">${s.file_link_count} file${s.file_link_count === 1 ? '' : 's'}</span>`
        : '';
    const previewHtml = opts.showPreview && s.preview
        ? `<div class="detail-row-sub" style="margin-top:3px; opacity:0.85;">${escapeHtml(s.preview)}</div>`
        : '';
    return `
    <div class="detail-row">
        <div class="detail-row-main">
            <div class="detail-row-title" style="display:flex; align-items:center; gap:7px; flex-wrap:wrap;">
                <span>${escapeHtml(title)}</span>
                ${ownerChipHtml}
                ${attBadge}
                ${fileBadge}
            </div>
            <div class="detail-row-sub">${s.message_count} events · ${fmtTimestampExact(tsRef)}</div>
            ${previewHtml}
        </div>
        <div style="display:flex; gap:6px; flex-shrink:0;">
            <button type="button" class="btn small-btn" data-sid="${escapeHtml(s.session_id)}" data-title="${escapeHtml(title)}" onclick="openChatViewer(this.dataset.sid, this.dataset.title)">
                <span class="material-symbols-outlined" style="font-size:14px;">visibility</span> View Chat
            </button>
            ${ownerId ? `
            <button type="button" class="btn small-btn danger-btn" onclick="deleteUserSession('${escapeHtml(ownerId)}','${escapeHtml(s.session_id)}', this)" title="Delete session">
                <span class="material-symbols-outlined" style="font-size:14px;">delete</span>
            </button>` : ''}
        </div>
    </div>`;
}

function _matchesTimeBounds(isoTs, startMs, endMs) {
    if (!startMs && !endMs) return true;
    const tsMs = isoTs ? new Date(isoTs).getTime() : NaN;
    if (isNaN(tsMs)) return false;
    if (startMs && tsMs < startMs) return false;
    if (endMs && tsMs > endMs) return false;
    return true;
}

function _inferArtifactSource(f) {
    if (f && f.source) return String(f.source).toLowerCase();
    const rel = String(f?.rel_path || '');
    if (rel.startsWith('.chat_uploads/') || rel.includes('/generated/uploads/')) return 'uploaded';
    return 'generated';
}

function _matchesArtifactExt(f, extFilter) {
    if (!extFilter) return true;
    const rel = (f.rel_path || '').toLowerCase();
    const name = (f.name || f.rel_path || '').toLowerCase();
    if (extFilter === '.png') {
        return ['.png', '.jpg', '.jpeg', '.svg', '.webp', '.gif'].some(ext => rel.endsWith(ext) || name.endsWith(ext));
    }
    if (extFilter === '.py') {
        return ['.py', '.sql', '.sh', '.java', '.c', '.cpp', '.js', '.ts'].some(ext => rel.endsWith(ext) || name.endsWith(ext));
    }
    if (extFilter === '.docx') {
        return ['.doc', '.docx'].some(ext => rel.endsWith(ext) || name.endsWith(ext));
    }
    return rel.endsWith(extFilter) || name.endsWith(extFilter);
}

function _renderAdminArtifactRow(f, opts = {}) {
    const relPath = f.rel_path || '';
    const safePath = relPath.replace(/'/g, "\\'");
    const displayName = f.name || relPath.split('/').pop() || relPath;
    const source = _inferArtifactSource(f);
    const sourceLabel = source === 'uploaded' ? 'Uploaded' : 'Generated';
    const previewUrl = f.preview_url !== undefined ? f.preview_url : `/preview/${encodeURI(relPath)}`;
    const fileUrl = f.download_url || f.url || `/files/${encodeURI(relPath)}`;
    const exists = f.exists !== false;
    const tsRef = f.timestamp || f.created_at;
    const extLabel = ((f.file_type || '').replace(/^\./, '') || (displayName.includes('.') ? displayName.split('.').pop() : 'FILE')).toUpperCase().slice(0, 5);
    const isImg = Boolean(
        f.is_image !== undefined
            ? f.is_image
            : ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg'].some(ext => displayName.toLowerCase().endsWith(ext))
    );
    const iconHtml = isImg && fileUrl
        ? `<a href="${escapeHtml(previewUrl || fileUrl)}" target="_blank" rel="noopener" class="admin-art-thumb" title="Preview image"><img src="${escapeHtml(fileUrl)}" alt="${escapeHtml(displayName)}" loading="lazy"></a>`
        : `<span class="admin-art-ext">${escapeHtml(extLabel)}</span>`;
    const ownerChipHtml = opts.showOwner && f.owner_username
        ? `<span class="admin-art-owner-chip" title="Owner">@${escapeHtml(f.owner_username)}</span>`
        : '';
    const chatChipHtml = f.session_id
        ? `<button type="button" class="admin-art-chat-chip" data-sid="${escapeHtml(f.session_id)}" data-title="${escapeHtml(f.session_title || 'Chat')}" onclick="openChatViewer(this.dataset.sid, this.dataset.title)" title="Open linked chat history"><span class="material-symbols-outlined" style="font-size:12px;">chat</span>${escapeHtml((f.session_title || 'Chat').slice(0, 28))}</button>`
        : '';
    const ownerId = opts.userId || f.owner_id || '';
    const canRevoke = Boolean(opts.allowRevoke) && Boolean(f.owned) && source !== 'uploaded' && relPath && ownerId;
    const dlHref = fileUrl.includes('?') ? fileUrl : `${fileUrl}?download=true`;
    return `
    <div class="detail-row">
        <div class="admin-art-row-left">
            ${iconHtml}
            <div class="detail-row-main">
                <div class="detail-row-title" style="display:flex; align-items:center; gap:7px; flex-wrap:wrap;">
                    <span title="${escapeHtml(relPath)}">${escapeHtml(displayName)}</span>
                    <span class="admin-art-source-tag ${source}">${sourceLabel}</span>
                    ${ownerChipHtml}
                </div>
                <div class="detail-row-sub" style="display:flex; align-items:center; gap:6px; flex-wrap:wrap; margin-top:3px;">
                    <span class="folder-mini-tag">${escapeHtml(f.subject || 'general')}</span>
                    ${chatChipHtml}
                    <span>${exists ? fmtBytes(f.size) : 'missing on disk'} · ${fmtTimestampExact(tsRef)}</span>
                </div>
            </div>
        </div>
        <div style="display:flex; gap:6px; align-items:center; flex-wrap:wrap; flex-shrink:0;">
            ${exists && previewUrl ? `
                <a href="${escapeHtml(previewUrl)}" target="_blank" rel="noopener" class="btn small-btn" title="Preview in new tab">
                    <span class="material-symbols-outlined" style="font-size:14px;">open_in_new</span> Preview
                </a>
            ` : ''}
            ${exists ? `
                <a href="${escapeHtml(dlHref)}" download="${escapeHtml(displayName)}" class="btn small-btn" title="Download artifact">
                    <span class="material-symbols-outlined" style="font-size:14px;">download</span>
                </a>
            ` : ''}
            ${canRevoke ? `
                <button type="button" class="btn small-btn danger-btn" onclick="revokeFile('${escapeHtml(ownerId)}', '${safePath}', this)" title="Revoke user ownership (makes file shared across course)">
                    Revoke
                </button>
            ` : ''}
        </div>
    </div>`;
}

function _renderGroupedArtifactsHtml(items, sourceMode, opts = {}) {
    if (!items.length) return '<div class="detail-empty">No matching artifacts.</div>';
    if (sourceMode === 'uploaded' || sourceMode === 'generated') {
        return items.map(f => _renderAdminArtifactRow(f, opts)).join('');
    }
    const uploaded = items.filter(f => _inferArtifactSource(f) === 'uploaded');
    const generated = items.filter(f => _inferArtifactSource(f) !== 'uploaded');
    let html = '';
    if (uploaded.length > 0) {
        html += `<div class="admin-art-subhead"><span class="admin-art-source-tag uploaded">Uploaded</span><span>Uploaded Artifacts</span><span class="count-badge">${uploaded.length}</span></div>`;
        html += uploaded.map(f => _renderAdminArtifactRow(f, opts)).join('');
    }
    if (generated.length > 0) {
        html += `<div class="admin-art-subhead" style="margin-top:${uploaded.length ? '14px' : '0'};"><span class="admin-art-source-tag generated">Generated</span><span>Generated Artifacts</span><span class="count-badge">${generated.length}</span></div>`;
        html += generated.map(f => _renderAdminArtifactRow(f, opts)).join('');
    }
    return html || '<div class="detail-empty">No matching artifacts.</div>';
}

function setUserDetailArtifactTab(sec) {
    const secSelect = document.getElementById('userDetailSectionFilter');
    if (secSelect) secSelect.value = sec;
    filterUserDetailItems();
}

function filterUserDetailItems() {
    if (!currentInspectedUser) return;
    const user = currentInspectedUser;
    const q = (document.getElementById('userDetailSearch')?.value || '').toLowerCase().trim();
    const section = document.getElementById('userDetailSectionFilter')?.value || 'all';
    const course = (document.getElementById('userDetailCourseFilter')?.value || '').toLowerCase().trim();
    const extFilter = (document.getElementById('userDetailTypeFilter')?.value || '').toLowerCase().trim();
    const { startMs, endMs } = _getUserDetailTimeBounds();

    const chatsSec = document.getElementById('userDetailChatsSection');
    const filesSec = document.getElementById('userDetailFilesSection');
    const showChats = (section === 'all' || section === 'chats');
    const showArtifacts = (section === 'all' || section === 'artifacts' || section === 'uploaded' || section === 'generated');
    if (chatsSec) chatsSec.style.display = showChats ? 'block' : 'none';
    if (filesSec) filesSec.style.display = showArtifacts ? 'block' : 'none';

    const filteredSessions = currentInspectedSessions.filter(s => {
        const title = (s.title || 'Untitled Chat').toLowerCase();
        const tsRef = s.modified_at || s.created_at;
        const dateStr = fmtTimestampExact(tsRef).toLowerCase();
        if (course && course !== '__general__' && !title.includes(course)) return false;
        if (!_matchesTimeBounds(tsRef, startMs, endMs) && !_matchesTimeBounds(s.created_at, startMs, endMs)) return false;
        if (q && !title.includes(q) && !dateStr.includes(q) && !(s.session_id || '').toLowerCase().includes(q)) return false;
        return true;
    });

    document.getElementById('detailSessionCount').textContent = filteredSessions.length;
    document.getElementById('detailSessions').innerHTML = filteredSessions.length === 0
        ? '<div class="detail-empty">No matching chat sessions.</div>'
        : filteredSessions.map(s => _renderAdminChatRow(s, { userId: user.id })).join('');

    const baseFiles = currentInspectedFiles.filter(f => {
        const rel = (f.rel_path || '').toLowerCase();
        const name = (f.name || f.rel_path || '').toLowerCase();
        const subj = (f.subject || '').toLowerCase();
        const source = _inferArtifactSource(f);
        const sessionTitle = (f.session_title || '').toLowerCase();
        const tsRef = f.timestamp || f.created_at;
        const dateStr = fmtTimestampExact(tsRef).toLowerCase();
        if (course === '__general__') {
            if (subj) return false;
        } else if (course && subj !== course && !rel.startsWith(course + '/')) {
            return false;
        }
        if (!_matchesTimeBounds(tsRef, startMs, endMs)) return false;
        if (!_matchesArtifactExt(f, extFilter)) return false;
        if (q && !rel.includes(q) && !name.includes(q) && !subj.includes(q) && !sessionTitle.includes(q) && !source.includes(q) && !dateStr.includes(q)) return false;
        return true;
    });

    const uploadedCount = baseFiles.filter(f => _inferArtifactSource(f) === 'uploaded').length;
    const generatedCount = baseFiles.length - uploadedCount;
    const allCountEl = document.getElementById('detailArtAllCount');
    const upCountEl = document.getElementById('detailArtUploadedCount');
    const genCountEl = document.getElementById('detailArtGeneratedCount');
    if (allCountEl) allCountEl.textContent = baseFiles.length;
    if (upCountEl) upCountEl.textContent = uploadedCount;
    if (genCountEl) genCountEl.textContent = generatedCount;

    const activeTabKey = (section === 'uploaded' || section === 'generated') ? section : 'artifacts';
    document.querySelectorAll('#detailArtSourceTabs .admin-art-tab').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.section === activeTabKey);
    });

    const filteredFiles = baseFiles.filter(f => {
        const source = _inferArtifactSource(f);
        if (section === 'uploaded' && source !== 'uploaded') return false;
        if (section === 'generated' && source !== 'generated') return false;
        return true;
    });

    document.getElementById('detailFileCount').textContent = filteredFiles.length;
    document.getElementById('detailFiles').innerHTML = _renderGroupedArtifactsHtml(
        filteredFiles,
        section,
        { userId: user.id, allowRevoke: true }
    );
}

// ── Admin Global Chats View (all users) ────────────────────────────
let adminAllChatsCache = [];
let adminAllChatsMode = 'all';

async function loadAdminAllChats() {
    const listEl = document.getElementById('adminAllChatsList');
    if (listEl) {
        listEl.innerHTML = '<div style="color:var(--text-muted);text-align:center;padding:24px;">Loading chats…</div>';
    }
    try {
        const resp = await fetch('/api/admin/sessions');
        if (!resp.ok) throw new Error(await resp.text());
        const data = await resp.json();
        adminAllChatsCache = data.sessions || [];

        const userSel = document.getElementById('adminChatUserFilter');
        if (userSel) {
            const prevUser = userSel.value;
            const users = data.users || [];
            userSel.innerHTML = '<option value="">All Users</option>' +
                users.map(u => `<option value="${escapeHtml(u.id)}">@${escapeHtml(u.username)} (${u.session_count || 0})</option>`).join('');
            userSel.value = prevUser;
        }

        filterAdminAllChats();
    } catch (err) {
        if (listEl) listEl.innerHTML = `<div class="login-error">Error loading chats: ${escapeHtml(err.message)}</div>`;
    }
}

function setAdminAllChatsMode(mode) {
    adminAllChatsMode = mode || 'all';
    document.querySelectorAll('#adminAllChatTabs .admin-art-tab').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.mode === adminAllChatsMode);
    });
    filterAdminAllChats();
}

function onAdminChatsTimePresetChange() {
    const preset = document.getElementById('adminChatTimePreset')?.value || '';
    if (preset !== 'custom') {
        const s = document.getElementById('adminChatStartDate');
        const e = document.getElementById('adminChatEndDate');
        if (s) s.value = '';
        if (e) e.value = '';
    }
    filterAdminAllChats();
}

function onAdminChatsCustomDateChange() {
    const startVal = document.getElementById('adminChatStartDate')?.value || '';
    const endVal = document.getElementById('adminChatEndDate')?.value || '';
    const presetEl = document.getElementById('adminChatTimePreset');
    if (presetEl) presetEl.value = (startVal || endVal) ? 'custom' : '';
    filterAdminAllChats();
}

function resetAdminAllChatsFilters() {
    const ids = ['adminChatSearch', 'adminChatUserFilter', 'adminChatTimePreset', 'adminChatStartDate', 'adminChatEndDate'];
    ids.forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = '';
    });
    setAdminAllChatsMode('all');
}

function filterAdminAllChats() {
    const listEl = document.getElementById('adminAllChatsList');
    if (!listEl) return;
    const q = (document.getElementById('adminChatSearch')?.value || '').toLowerCase().trim();
    const uid = (document.getElementById('adminChatUserFilter')?.value || '').trim();
    const { startMs, endMs } = _resolveTimeBounds('adminChatTimePreset', 'adminChatStartDate', 'adminChatEndDate');

    const base = adminAllChatsCache.filter(s => {
        if (uid && s.owner_id !== uid) return false;
        const tsRef = s.modified_at || s.created_at;
        if (!_matchesTimeBounds(tsRef, startMs, endMs) && !_matchesTimeBounds(s.created_at, startMs, endMs)) return false;
        if (q) {
            const hay = `${s.title || ''} ${s.owner_username || ''} ${s.preview || ''} ${s.session_id || ''} ${fmtTimestampExact(tsRef)}`.toLowerCase();
            if (!hay.includes(q)) return false;
        }
        return true;
    });

    const attCount = base.filter(s => (s.attachment_count || 0) > 0).length;
    const filesCount = base.filter(s => (s.file_link_count || 0) > 0).length;
    const cAll = document.getElementById('adminAllChatCountAll');
    const cAtt = document.getElementById('adminAllChatCountAtt');
    const cFiles = document.getElementById('adminAllChatCountFiles');
    if (cAll) cAll.textContent = base.length;
    if (cAtt) cAtt.textContent = attCount;
    if (cFiles) cFiles.textContent = filesCount;

    const visible = base.filter(s => {
        if (adminAllChatsMode === 'attachments' && !(s.attachment_count > 0)) return false;
        if (adminAllChatsMode === 'files' && !(s.file_link_count > 0)) return false;
        return true;
    });

    listEl.innerHTML = visible.length === 0
        ? '<div class="detail-empty">No matching chat sessions.</div>'
        : visible.map(s => _renderAdminChatRow(s, { showOwner: true, showPreview: true })).join('');
}

// ── Admin Global Artifacts View (all users) ────────────────────────
let adminAllArtifactsCache = [];
let adminAllArtifactsSource = 'all';

async function loadAdminAllArtifacts() {
    const listEl = document.getElementById('adminAllArtifactsList');
    if (listEl) {
        listEl.innerHTML = '<div style="color:var(--text-muted);text-align:center;padding:24px;">Loading artifacts…</div>';
    }
    try {
        const resp = await fetch('/api/admin/artifacts');
        if (!resp.ok) throw new Error(await resp.text());
        const data = await resp.json();
        adminAllArtifactsCache = data.artifacts || [];

        const userSel = document.getElementById('adminArtUserFilter');
        if (userSel) {
            const prevUser = userSel.value;
            const users = data.users || [];
            userSel.innerHTML = '<option value="">All Users</option>' +
                users.map(u => `<option value="${escapeHtml(u.id)}">@${escapeHtml(u.username)} (${u.uploaded_count || 0} up · ${u.generated_count || 0} gen)</option>`).join('');
            userSel.value = prevUser;
        }

        const courseSel = document.getElementById('adminArtCourseFilter');
        if (courseSel) {
            const prevCourse = courseSel.value;
            const subjects = data.subjects || [];
            const hasGeneral = adminAllArtifactsCache.some(f => !(f.subject || '').trim());
            courseSel.innerHTML = '<option value="">All Courses</option>' +
                (hasGeneral ? '<option value="__general__">General (no course)</option>' : '') +
                subjects.map(s => `<option value="${escapeHtml(s.toLowerCase())}">${escapeHtml(s)}</option>`).join('');
            courseSel.value = prevCourse;
        }

        filterAdminAllArtifacts();
    } catch (err) {
        if (listEl) listEl.innerHTML = `<div class="login-error">Error loading artifacts: ${escapeHtml(err.message)}</div>`;
    }
}

function setAdminAllArtifactsSource(src) {
    adminAllArtifactsSource = src || 'all';
    document.querySelectorAll('#adminAllArtTabs .admin-art-tab').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.source === adminAllArtifactsSource);
    });
    filterAdminAllArtifacts();
}

function resetAdminAllArtifactsFilters() {
    const ids = ['adminArtSearch', 'adminArtUserFilter', 'adminArtCourseFilter', 'adminArtTypeFilter'];
    ids.forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = '';
    });
    setAdminAllArtifactsSource('all');
}

function filterAdminAllArtifacts() {
    const listEl = document.getElementById('adminAllArtifactsList');
    if (!listEl) return;
    const q = (document.getElementById('adminArtSearch')?.value || '').toLowerCase().trim();
    const uid = (document.getElementById('adminArtUserFilter')?.value || '').trim();
    const course = (document.getElementById('adminArtCourseFilter')?.value || '').toLowerCase().trim();
    const extFilter = (document.getElementById('adminArtTypeFilter')?.value || '').toLowerCase().trim();

    const base = adminAllArtifactsCache.filter(f => {
        if (uid && f.owner_id !== uid) return false;
        const subj = (f.subject || '').toLowerCase();
        const rel = (f.rel_path || '').toLowerCase();
        if (course === '__general__') {
            if (subj) return false;
        } else if (course && subj !== course && !rel.startsWith(course + '/')) {
            return false;
        }
        if (!_matchesArtifactExt(f, extFilter)) return false;
        if (q) {
            const hay = `${f.name || ''} ${rel} ${subj} ${f.owner_username || ''} ${f.session_title || ''} ${_inferArtifactSource(f)}`.toLowerCase();
            if (!hay.includes(q)) return false;
        }
        return true;
    });

    const upCount = base.filter(f => _inferArtifactSource(f) === 'uploaded').length;
    const genCount = base.length - upCount;
    const cAll = document.getElementById('adminAllArtCountAll');
    const cUp = document.getElementById('adminAllArtCountUploaded');
    const cGen = document.getElementById('adminAllArtCountGenerated');
    if (cAll) cAll.textContent = base.length;
    if (cUp) cUp.textContent = upCount;
    if (cGen) cGen.textContent = genCount;

    const visible = base.filter(f => {
        const src = _inferArtifactSource(f);
        if (adminAllArtifactsSource === 'uploaded' && src !== 'uploaded') return false;
        if (adminAllArtifactsSource === 'generated' && src !== 'generated') return false;
        return true;
    });

    listEl.innerHTML = _renderGroupedArtifactsHtml(
        visible,
        adminAllArtifactsSource,
        { showOwner: true, allowRevoke: true }
    );
}

function formatTranscriptMarkdown(rawText) {
    const trimmed = String(rawText ?? '').replace(/\r\n/g, '\n').trim();
    if (!trimmed) return '';
    // Escape HTML first so user/model output is always XSS-safe
    let safe = escapeHtml(trimmed);

    // Inline code spans (`...`)
    const codeTokens = [];
    safe = safe.replace(/`([^`\n]+)`/g, (_, code) => {
        const idx = codeTokens.length;
        codeTokens.push(`<code>${code}</code>`);
        return `\x00CODE${idx}\x00`;
    });

    // Bold (**...**) and italic (*...*)
    safe = safe.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    safe = safe.replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,!?:;]|$)/g, '$1<em>$2</em>');

    // Convert lines into compact paragraphs & lists
    const lines = safe.split('\n');
    const blocks = [];
    let listType = null;
    let listItems = [];

    const flushList = () => {
        if (!listType) return;
        blocks.push(`<${listType}>${listItems.join('')}</${listType}>`);
        listType = null;
        listItems = [];
    };

    for (const line of lines) {
        const t = line.trim();
        if (!t) {
            flushList();
            continue;
        }
        const olMatch = t.match(/^\d+\.\s+(.+)$/);
        const ulMatch = t.match(/^[-•*]\s+(.+)$/);
        const hMatch = t.match(/^#{1,4}\s+(.+)$/);
        if (olMatch) {
            if (listType !== 'ol') { flushList(); listType = 'ol'; }
            listItems.push(`<li>${olMatch[1]}</li>`);
        } else if (ulMatch) {
            if (listType !== 'ul') { flushList(); listType = 'ul'; }
            listItems.push(`<li>${ulMatch[1]}</li>`);
        } else if (hMatch) {
            flushList();
            blocks.push(`<p class="chat-md-heading">${hMatch[1]}</p>`);
        } else {
            flushList();
            blocks.push(`<p>${t}</p>`);
        }
    }
    flushList();

    return blocks.join('').replace(/\x00CODE(\d+)\x00/g, (_, idx) => codeTokens[Number(idx)] || '');
}

async function openChatViewer(sessionId, title) {
    document.getElementById('chatViewerTitle').textContent = `Chat: ${title || sessionId}`;
    const metaEl = document.getElementById('chatViewerMeta');
    if (metaEl) metaEl.textContent = `Session ID: ${sessionId}`;
    const content = document.getElementById('chatViewerContent');
    content.innerHTML = '<div style="color:var(--text-muted);text-align:center;padding:24px;">Loading conversation…</div>';
    const modal = document.getElementById('chatViewerModal');
    if (!modal.open) modal.showModal();

    try {
        const resp = await fetch(`/api/admin/sessions/${encodeURIComponent(sessionId)}/history`);
        if (!resp.ok) throw new Error(await resp.text());
        const data = await resp.json();
        const history = data.history || [];

        if (metaEl) {
            const ownerPart = data.owner_username ? `@${data.owner_username} · ` : '';
            const createdPart = data.created_at ? `Started ${fmtTimestampExact(data.created_at)}` : '';
            metaEl.textContent = `${ownerPart}${createdPart} · ID: ${sessionId.slice(0, 8)}`;
        }

        if (history.length === 0) {
            content.innerHTML = '<div class="detail-empty">No messages recorded in this session yet.</div>';
            return;
        }

        content.innerHTML = history.map(ev => {
            const tsLabel = ev.timestamp ? fmtTimestampExact(ev.timestamp) : '';
            if (ev.type === 'user_transcript') {
                const promptText = String(ev.text ?? '').trim();
                const attsHtml = Array.isArray(ev.attachments) && ev.attachments.length > 0
                    ? `<div class="chat-msg-attachments">${ev.attachments.map(att => {
                        const fileUrl = att.url || '';
                        const previewHref = att.preview_url || fileUrl;
                        const name = att.name || 'artifact';
                        const isImg = att.is_image !== undefined
                            ? Boolean(att.is_image)
                            : /\.(png|jpe?g|gif|webp|svg)$/i.test(name);
                        if (isImg) {
                            return `<a class="chat-att-thumb" href="${escapeHtml(previewHref)}" target="_blank" rel="noopener" title="${escapeHtml(name)}"><img src="${escapeHtml(fileUrl)}" alt="${escapeHtml(name)}"><span>${escapeHtml(name)}</span></a>`;
                        }
                        return `<a class="chat-att-file-pill" href="${escapeHtml(previewHref)}" target="_blank" rel="noopener" title="Preview in new tab"><span class="material-symbols-outlined" style="font-size:15px;">description</span><span>${escapeHtml(name)}</span></a>`;
                    }).join('')}</div>`
                    : '';
                return `<div class="chat-msg user-msg"><div class="chat-msg-header"><span class="chat-msg-label">User Prompt</span>${tsLabel ? `<span class="chat-msg-time" title="${escapeHtml(ev.timestamp)}">${escapeHtml(tsLabel)}</span>` : ''}</div>${attsHtml}${promptText ? `<div class="chat-msg-body plain">${escapeHtml(promptText)}</div>` : ''}</div>`;
            } else if (ev.type === 'klyra_text' || ev.type === 'klyra_transcript') {
                return `<div class="chat-msg klyra-msg"><div class="chat-msg-header"><span class="chat-msg-label"><span class="chat-msg-dot" aria-hidden="true"></span>Klyra</span>${tsLabel ? `<span class="chat-msg-time" title="${escapeHtml(ev.timestamp)}">${escapeHtml(tsLabel)}</span>` : ''}</div><div class="chat-msg-body md">${formatTranscriptMarkdown(ev.text)}</div></div>`;
            } else if (ev.type === 'tool_done' || ev.type === 'tool_start') {
                const toolName = ev.name || ev.tool || 'tool';
                const summary = ev.summary ? ` — ${escapeHtml(ev.summary)}` : '';
                return `<div class="chat-tool-pill"><span class="material-symbols-outlined" style="font-size:14px;">build</span><span>Tool <strong>${escapeHtml(toolName)}</strong>${summary}</span>${tsLabel ? `<span class="chat-msg-time" style="margin-left:auto;">${escapeHtml(tsLabel)}</span>` : ''}</div>`;
            } else if (ev.type === 'file_link') {
                const rawUrl = ev.url || '#';
                const previewHref = rawUrl.startsWith('/files/')
                    ? `/preview/${rawUrl.slice('/files/'.length)}`
                    : rawUrl;
                return `<div class="chat-file-pill"><span class="material-symbols-outlined" style="font-size:15px;color:var(--accent);">attach_file</span><a href="${escapeHtml(previewHref)}" target="_blank" rel="noopener" title="Preview in new tab">${escapeHtml(ev.name || rawUrl)}</a><a href="${escapeHtml(rawUrl)}?download=true" download class="btn small-btn" style="margin-left:auto;padding:2px 8px;font-size:11px;">Download</a></div>`;
            }
            return '';
        }).join('');
    } catch (err) {
        content.innerHTML = `<div class="login-error">Error loading chat: ${escapeHtml(err.message)}</div>`;
    }
}

async function deleteUserSession(userId, sessionId, btn) {
    const ok = await askConfirm({
        title: 'Delete Chat Session',
        message: 'Delete this chat session permanently? This cannot be undone.',
        confirmText: 'Delete Session',
        danger: true,
    });
    if (!ok) return;
    btn.disabled = true;
    const resp = await fetch(`/api/admin/users/${userId}/sessions/${sessionId}`, { method: 'DELETE' });
    if (resp.ok) {
        showToast('Chat session deleted', 'success');
        if (document.getElementById('userDetailModal')?.open) {
            openUserDetail(allUsers.find(u => u.id === userId));
        }
        if (document.getElementById('view-chats')?.style.display !== 'none') {
            loadAdminAllChats();
        }
        loadStats();
    } else {
        showToast('Failed: ' + await resp.text(), 'error');
        btn.disabled = false;
    }
}

async function revokeFile(userId, relPath, btn) {
    const ok = await askConfirm({
        title: 'Revoke File Ownership',
        message: `Revoke this user's exclusive ownership of "${relPath}"? Once revoked, the file becomes shared across all instructors assigned to that course.`,
        confirmText: 'Revoke Ownership',
        danger: true,
    });
    if (!ok) return;
    btn.disabled = true;
    const resp = await fetch(`/api/admin/users/${userId}/files/${relPath}`, { method: 'DELETE' });
    if (resp.ok) {
        showToast('File ownership revoked (file is now course-shared)', 'success');
        if (document.getElementById('userDetailModal')?.open) {
            openUserDetail(allUsers.find(u => u.id === userId));
        }
        if (document.getElementById('view-artifacts')?.style.display !== 'none') {
            loadAdminAllArtifacts();
        }
    } else {
        showToast('Failed: ' + await resp.text(), 'error');
        btn.disabled = false;
    }
}

// ── Courses ────────────────────────────────────────────────────────
async function loadCourses() {
    try {
        if (!allUsers.length) await loadUsers();
        const resp = await fetch('/api/admin/subjects');
        if (!resp.ok) throw new Error(await resp.text());
        allSubjects = (await resp.json()).subjects || [];
        knownSubjectNames = allSubjects.map(s => s.name);
        renderCourses();
    } catch (err) {
        console.error('Failed to load courses:', err);
    }
}

function renderCourses() {
    const container = document.getElementById('coursesList');
    if (allSubjects.length === 0) {
        container.innerHTML = '<div class="admin-card"><p style="color:var(--text-muted);">No subjects discovered under <code>docs/</code>. Click <strong>Create Subject</strong> to add one.</p></div>';
        return;
    }
    const query = (document.getElementById('coursesSearch')?.value || '').toLowerCase().trim();

    const filteredSubjects = allSubjects.filter(s =>
        s.name.toLowerCase().includes(query) ||
        (s.skill_summary?.display_name || '').toLowerCase().includes(query) ||
        (s.skill_summary?.domain || '').toLowerCase().includes(query) ||
        (s.folders || []).some(f => f.toLowerCase().includes(query)) ||
        (s.instructors || []).some(inst =>
            (inst.username || '').toLowerCase().includes(query) ||
            (inst.full_name || '').toLowerCase().includes(query)
        )
    );

    if (filteredSubjects.length === 0) {
        container.innerHTML = '<div class="admin-card"><p style="color:var(--text-muted);">No matching courses found.</p></div>';
        return;
    }
    const instructors = allUsers.filter(u => u.role !== 'admin');

    container.innerHTML = filteredSubjects.map(subj => {
        const assigned = instructors.filter(u =>
            (u.courses || []).map(c => c.toLowerCase()).includes(subj.name.toLowerCase())
        );
        const unassigned = instructors.filter(u =>
            !(u.courses || []).map(c => c.toLowerCase()).includes(subj.name.toLowerCase())
        );

        const chips = assigned.map(u => `
            <span class="course-chip" style="display:inline-flex;align-items:center;gap:4px;">
                ${escapeHtml(u.full_name || u.username)}
                <button type="button" class="chip-x" title="Unassign ${escapeHtml(u.username)}"
                    onclick="setCourseForUser('${u.id}', '${escapeHtml(subj.name)}', false)">×</button>
            </span>`).join('') || '<span style="color:var(--text-muted); font-size:12.5px;">No instructors assigned yet</span>';

        const options = unassigned.map(u => {
            const label = u.full_name ? `${escapeHtml(u.full_name)} (@${escapeHtml(u.username)})` : `@${escapeHtml(u.username)}`;
            return `<option value="${u.id}">${label}</option>`;
        }).join('');

        const skillBadge = subj.has_skill_md
            ? `<span class="skill-badge ok" title="skill.md configured on disk">skill.md ✓</span>`
            : `<span class="skill-badge missing" title="Using default skill configuration — click Skill to save">default skill</span>`;

        const folderCount = (subj.folders || []).length;
        const folderTags = folderCount
            ? subj.folders.map(f => `<span class="folder-mini-tag">${escapeHtml(f)}</span>`).join('')
            : '<span style="color:var(--text-muted);font-size:12px;">No folders yet</span>';

        return `
        <div class="admin-card course-card">
            <div>
                <div class="course-card-head">
                    <div class="course-card-title-wrap">
                        <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap; min-width:0;">
                            <h2 class="course-card-title" onclick="openSubjectDetail('${escapeHtml(subj.name)}')" title="${escapeHtml(subj.name)} — Browse files">
                                <span class="material-symbols-outlined" style="color:var(--accent);font-size:18px;flex-shrink:0;">school</span>
                                <span class="course-card-name-text">${escapeHtml(subj.name)}</span>
                            </h2>
                            ${skillBadge}
                        </div>
                        <p class="detail-row-sub" style="margin-top:4px;">${subj.file_count} files · ${folderCount} indexed folders</p>
                    </div>
                    <div class="course-card-actions">
                        <button type="button" class="btn small-btn" onclick="openSubjectDetail('${escapeHtml(subj.name)}')" title="Browse & preview files">
                            <span class="material-symbols-outlined" style="font-size:15px;">folder_open</span> Files
                        </button>
                        <button type="button" class="btn small-btn" onclick="openSkillEditor('${escapeHtml(subj.name)}')" title="View & edit docs/${escapeHtml(subj.name)}/skill.md">
                            <span class="material-symbols-outlined" style="font-size:15px;">edit_note</span> Skill
                        </button>
                        <button type="button" class="btn small-btn danger-btn" onclick="confirmDeleteSubject('${escapeHtml(subj.name)}')" title="Delete subject">
                            <span class="material-symbols-outlined" style="font-size:15px;">delete</span>
                        </button>
                    </div>
                </div>

                <div class="course-assigned">
                    <span class="course-section-label">Instructors</span>
                    <div class="course-chips-inline">${chips}</div>
                </div>

                <details class="course-details-toggle">
                    <summary class="course-details-summary">
                        <span>Indexed folders (${folderCount})</span>
                    </summary>
                    <div class="course-details-body">
                        <div class="course-folders-row" style="margin-top:0;">${folderTags}</div>
                    </div>
                </details>
            </div>

            <div class="course-assign-row">
                <select class="course-user-select" aria-label="Assign instructor to ${escapeHtml(subj.name)}" data-subject="${escapeHtml(subj.name)}" onchange="applyCourseSelect(this, '${escapeHtml(subj.name)}')" ${unassigned.length === 0 ? 'disabled' : ''}>
                    <option value="">${unassigned.length === 0 ? 'All instructors assigned' : '+ Assign instructor…'}</option>
                    ${options}
                </select>
            </div>
        </div>`;
    }).join('');
}

async function applyCourseSelect(select, subject) {
    const userId = select.value;
    select.value = '';
    if (!userId) return;
    await setCourseForUser(userId, subject, true);
}

async function setCourseForUser(userId, subject, add) {
    const user = allUsers.find(u => u.id === userId);
    if (!user) return;
    const courses = new Set((user.courses || []).map(c => c.toLowerCase()));
    if (add) courses.add(subject.toLowerCase());
    else courses.delete(subject.toLowerCase());
    try {
        await putUser(userId, { courses: Array.from(courses) });
        showToast(`${add ? 'Assigned' : 'Unassigned'} @${user.username} ${add ? 'to' : 'from'} ${subject}`, 'success');
        if (allSubjects.length) renderCourses();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

async function confirmDeleteSubject(subjectName) {
    const ok = await askConfirm({
        title: `Delete Subject "${subjectName}"`,
        message: `Delete subject "${subjectName}" and all files inside docs/${subjectName}/? This cannot be undone.`,
        confirmText: 'Delete Subject',
        danger: true,
    });
    if (!ok) return;
    try {
        const resp = await fetch(`/api/admin/subjects/${encodeURIComponent(subjectName)}`, {
            method: 'DELETE',
        });
        if (!resp.ok) {
            const data = await resp.json().catch(() => ({}));
            throw new Error(data.detail || await resp.text());
        }
        showToast(`Deleted subject "${subjectName}"`, 'success');
        await loadUsers();
        await loadCourses();
        await loadStats();
    } catch (err) {
        showToast(`Failed to delete subject: ${err.message}`, 'error');
    }
}

// ── Subject Detail View ────────────────────────────────────────────
let currentSubjectFilesData = null;

async function openSubjectDetail(subjectName) {
    document.getElementById('subjectDetailTitle').textContent = `Subject: ${subjectName}`;
    const dlAllBtn = document.getElementById('subjectDownloadAllBtn');
    if (dlAllBtn) {
        dlAllBtn.href = `/api/download/${encodeURIComponent(subjectName)}/.`;
    }
    const content = document.getElementById('subjectDetailContent');
    content.innerHTML = '<div style="color:var(--text-muted);text-align:center;padding:24px;">Loading files…</div>';
    document.getElementById('subjectFilesSearch').value = '';
    const modal = document.getElementById('subjectDetailModal');
    if (!modal.open) modal.showModal();

    try {
        if (!allUsers.length) await loadUsers();
        const resp = await fetch(`/api/admin/subjects/${encodeURIComponent(subjectName)}/files`);
        if (!resp.ok) throw new Error(await resp.text());
        currentSubjectFilesData = await resp.json();
        renderSubjectFiles();
    } catch (err) {
        content.innerHTML = `<div class="login-error">Error: ${escapeHtml(err.message)}</div>`;
    }
}

function filterSubjectFiles() {
    renderSubjectFiles();
}

function renderSubjectFiles() {
    const content = document.getElementById('subjectDetailContent');
    if (!currentSubjectFilesData) return;

    const subjectName = currentSubjectFilesData.subject;
    const query = (document.getElementById('subjectFilesSearch').value || '').toLowerCase().trim();
    const detailsByFolder = currentSubjectFilesData.file_details || {};
    const folders = currentSubjectFilesData.folders || {};

    const folderKeys = Object.keys(folders).sort();
    if (folderKeys.length === 0) {
        content.innerHTML = '<div class="detail-empty">No files found in this subject yet.</div>';
        return;
    }

    let html = '';
    let hasMatches = false;

    for (const folderName of folderKeys) {
        const rawDetails = detailsByFolder[folderName] || (folders[folderName] || []).map(f => ({
            name: f,
            rel_path: folderName === '/' ? `${subjectName}/${f}` : `${subjectName}/${folderName}/${f}`,
            sub_path: folderName === '/' ? f : `${folderName}/${f}`,
            size: 0,
        }));

        const folderMatches = folderName.toLowerCase().includes(query);
        const matchingFiles = rawDetails.filter(item =>
            item.name.toLowerCase().includes(query) ||
            (item.owner_username || '').toLowerCase().includes(query) ||
            folderMatches
        );

        if (matchingFiles.length > 0) {
            hasMatches = true;
            const folderDlPath = folderName === '/' ? '.' : folderName;
            const isGeneratedFolder = folderName === 'generated' || folderName.startsWith('generated/');
            html += `
                <div class="subject-folder-block">
                    <div class="subject-folder-header">
                        <div style="display:flex; align-items:center; gap:6px; font-weight:600;">
                            <span class="material-symbols-outlined" style="font-size: 18px; color: var(--accent);">folder</span>
                            <span>${escapeHtml(folderName)}</span>
                            <span class="count-badge">${matchingFiles.length}</span>
                        </div>
                        <a href="/api/download/${encodeURIComponent(subjectName)}/${encodeURI(folderDlPath)}" class="btn small-btn" download title="Download this folder as ZIP">
                            <span class="material-symbols-outlined" style="font-size:14px;">folder_zip</span> ZIP
                        </a>
                    </div>
                    <div class="subject-file-list">
                        ${matchingFiles.map(item => {
                            const previewUrl = `/preview/${encodeURI(item.rel_path)}`;
                            const fileUrl = `/files/${encodeURI(item.rel_path)}`;
                            const safeSubPath = (item.sub_path || item.name).replace(/'/g, "\\'");
                            const safeRelPath = (item.rel_path || '').replace(/'/g, "\\'");
                            const ownerChip = isGeneratedFolder
                                ? `<span class="owner-pill" title="Generated file owner">${item.owner_username ? '@' + escapeHtml(item.owner_username) : 'Shared (all course users)'}</span>`
                                : '';
                            const ownerSelect = isGeneratedFolder ? `
                                <select class="course-user-select owner-transfer-select" title="Transfer/share generated file ownership" onchange="reassignFileOwner('${safeRelPath}', this.value, '${escapeHtml(subjectName)}')">
                                    <option value="" ${!item.owner_id ? 'selected' : ''}>Shared (Course)</option>
                                    ${allUsers.map(u => `<option value="${escapeHtml(u.id)}" ${item.owner_id === u.id ? 'selected' : ''}>@${escapeHtml(u.username)}</option>`).join('')}
                                </select>
                            ` : '';
                            return `
                            <div class="subject-file-row">
                                <div class="subject-file-info">
                                    <span class="material-symbols-outlined" style="font-size: 16px; color: var(--text-muted); flex-shrink:0;">description</span>
                                    <a href="${previewUrl}" target="_blank" rel="noopener" class="subject-file-link" title="Preview ${escapeHtml(item.name)} in new tab">${escapeHtml(item.name)}</a>
                                    ${item.size ? `<span class="subject-file-size">${fmtBytes(item.size)}</span>` : ''}
                                    ${ownerChip}
                                </div>
                                <div style="display:flex; gap:6px; align-items:center; flex-wrap:wrap;">
                                    ${ownerSelect}
                                    <a href="${previewUrl}" target="_blank" rel="noopener" class="btn small-btn" title="Preview ${escapeHtml(item.name)} in new tab">
                                        <span class="material-symbols-outlined" style="font-size:14px;">open_in_new</span> Preview
                                    </a>
                                    <a href="${fileUrl}?download=true" download class="btn small-btn" title="Download ${escapeHtml(item.name)}">
                                        <span class="material-symbols-outlined" style="font-size:14px;">download</span>
                                    </a>
                                    <button type="button" class="btn small-btn danger-btn" onclick="deleteSubjectFile('${escapeHtml(subjectName)}', '${safeSubPath}')" title="Delete file">
                                        <span class="material-symbols-outlined" style="font-size:14px;">delete</span>
                                    </button>
                                </div>
                            </div>`;
                        }).join('')}
                    </div>
                </div>
            `;
        }
    }

    if (!hasMatches) {
        html = '<div class="detail-empty">No files match your filter.</div>';
    }

    content.innerHTML = html;
}

async function reassignFileOwner(relPath, newOwnerId, subjectName) {
    try {
        const resp = await fetch('/api/admin/files/reassign', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ rel_path: relPath, new_owner_id: newOwnerId || null }),
        });
        if (!resp.ok) throw new Error(await resp.text());
        const data = await resp.json();
        showToast(
            data.owner_username
                ? `Assigned "${relPath}" to @${data.owner_username}`
                : `Shared "${relPath}" with all instructors in ${subjectName}`,
            'success'
        );
        await openSubjectDetail(subjectName);
    } catch (err) {
        showToast(`Failed to update owner: ${err.message}`, 'error');
    }
}

async function deleteSubjectFile(subjectName, subPath) {
    const ok = await askConfirm({
        title: 'Delete Subject File',
        message: `Delete file "${subPath}" from ${subjectName}? This cannot be undone.`,
        confirmText: 'Delete File',
        danger: true,
    });
    if (!ok) return;
    try {
        const resp = await fetch(`/api/admin/subjects/${encodeURIComponent(subjectName)}/files/${encodeURI(subPath)}`, {
            method: 'DELETE',
        });
        if (!resp.ok) throw new Error(await resp.text());
        showToast(`Deleted ${subPath}`, 'success');
        await openSubjectDetail(subjectName);
        await loadCourses();
    } catch (err) {
        showToast(`Failed to delete file: ${err.message}`, 'error');
    }
}

// ── Subject skill.md Editor ────────────────────────────────────────
async function openSkillEditor(subjectName) {
    document.getElementById('skillEditorSubject').value = subjectName;
    document.getElementById('skillEditorTitle').textContent = `${subjectName} — skill.md`;
    const textarea = document.getElementById('skillEditorTextarea');
    textarea.value = 'Loading skill.md…';
    const modal = document.getElementById('skillEditorModal');
    if (!modal.open) modal.showModal();

    try {
        const resp = await fetch(`/api/admin/subjects/${encodeURIComponent(subjectName)}/skill`);
        if (!resp.ok) throw new Error(await resp.text());
        const data = await resp.json();
        textarea.value = data.content || '';
        const subEl = document.getElementById('skillEditorSub');
        if (subEl) {
            subEl.textContent = data.exists
                ? `Editing docs/${subjectName}/skill.md`
                : `docs/${subjectName}/skill.md does not exist yet — click Save to initialize it.`;
        }
    } catch (err) {
        textarea.value = `# Error loading skill.md: ${err.message}`;
    }
}

async function resetSkillToDefaultTemplate() {
    const subjectName = document.getElementById('skillEditorSubject').value;
    if (!subjectName) return;
    const ok = await askConfirm({
        title: 'Load Default Skill Template',
        message: `Replace the current editor contents for "${subjectName}" with the default skill.md template? Any unsaved edits in this editor will be overwritten.`,
        confirmText: 'Load Default',
        danger: true,
    });
    if (!ok) return;
    try {
        const resp = await fetch(`/api/admin/subjects/${encodeURIComponent(subjectName)}/skill?default=true`);
        if (!resp.ok) throw new Error(await resp.text());
        const data = await resp.json();
        document.getElementById('skillEditorTextarea').value = data.content || '';
        showToast(`Loaded default skill.md template for ${subjectName} — click Save skill.md to apply`, 'info');
    } catch (err) {
        showToast(`Failed to load template: ${err.message}`, 'error');
    }
}

document.getElementById('skillEditorForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const subjectName = document.getElementById('skillEditorSubject').value;
    const content = document.getElementById('skillEditorTextarea').value;
    const btn = e.target.querySelector('button[type="submit"]');
    btn.disabled = true;
    try {
        const resp = await fetch(`/api/admin/subjects/${encodeURIComponent(subjectName)}/skill`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content }),
        });
        if (!resp.ok) throw new Error(await resp.text());
        const res = await resp.json();
        showToast(res.message || `Saved skill.md for ${subjectName}`, 'success');
        document.getElementById('skillEditorModal').close();
        loadCourses();
    } catch (err) {
        showToast(`Failed to save skill.md: ${err.message}`, 'error');
    } finally {
        btn.disabled = false;
    }
});

// ── Settings ───────────────────────────────────────────────────────
async function loadConfig() {
    try {
        const resp = await fetch('/api/admin/config');
        if (!resp.ok) throw new Error(await resp.text());
        const cfg = await resp.json();
        const group = (title, rows) => `
            <div class="config-group">
                <h2>${title}</h2>
                ${rows.map(r => {
                    const isSecret = r.type === 'password';
                    return `
                    <div class="config-row">
                        <div class="config-key-wrap">
                            <span class="config-key">${escapeHtml(r.k)}</span>
                            <code class="config-env-name">${escapeHtml(r.env_key)}</code>
                        </div>
                        <div class="config-input-wrap">
                            <input type="${r.type || 'text'}" name="${escapeHtml(r.env_key)}" value="${escapeHtml(r.v)}" class="admin-edit-row" autocomplete="off">
                            ${isSecret ? `
                                <button type="button" class="btn small-btn secret-toggle-btn" onclick="toggleSecretInput(this)" title="Show/hide secret">
                                    <span class="material-symbols-outlined" style="font-size:15px;">visibility</span>
                                </button>
                            ` : ''}
                        </div>
                    </div>`;
                }).join('')}
            </div>`;
        const sections = [
            group('LLM & Embeddings', [
                { k: 'Gemini API Key', v: cfg.llm.gemini_api_key || '', env_key: 'GEMINI_API_KEY', type: 'password' },
                { k: 'Primary CrewAI Model', v: cfg.llm.model, env_key: 'KLYRA_MODEL' },
                { k: 'Chat Model', v: cfg.llm.chat_model, env_key: 'KLYRA_CHAT_MODEL' },
                { k: 'Gemini Live Voice Model', v: cfg.llm.voice_model, env_key: 'KLYRA_VOICE_MODEL' },
                { k: 'Default Voice Persona', v: cfg.llm.voice_name || 'Puck', env_key: 'KLYRA_VOICE_NAME' },
                { k: 'Embedding Provider', v: cfg.llm.embedding_provider || 'google', env_key: 'EMBEDDING_PROVIDER' },
                { k: 'Google Embedding Model', v: cfg.llm.embedding_model || 'models/gemini-embedding-2-preview', env_key: 'GOOGLE_EMBEDDING_MODEL' },
            ]),
            group('Authentication & OAuth', [
                { k: 'Session Cookie Secret', v: cfg.auth.session_secret, env_key: 'KLYRA_SESSION_SECRET', type: 'password' },
                { k: 'REST API Key (X-API-Key)', v: cfg.auth.api_key || '', env_key: 'KLYRA_API_KEY', type: 'password' },
                { k: 'Google OAuth Client ID', v: cfg.auth.google_client_id, env_key: 'GOOGLE_CLIENT_ID' },
                { k: 'Google OAuth Client Secret', v: cfg.auth.google_client_secret, env_key: 'GOOGLE_CLIENT_SECRET', type: 'password' },
                { k: 'Auto-provision OAuth Users (1/0)', v: cfg.auth.oauth_auto_provision, env_key: 'KLYRA_OAUTH_AUTO_PROVISION' },
            ]),
            group('Storage & Databases', [
                { k: 'SQLAlchemy Database URL', v: cfg.storage.database, env_key: 'DATABASE_URL' },
                { k: 'Local SQLite Path', v: cfg.storage.klyra_db, env_key: 'KLYRA_DB' },
                { k: 'Jobs SQLite Path', v: cfg.storage.jobs_db, env_key: 'KLYRA_JOBS_DB' },
            ]),
            group('Infrastructure & Queue', [
                { k: 'Qdrant Vector DB URL', v: cfg.infra.qdrant_url, env_key: 'QDRANT_URL' },
                { k: 'Celery Broker URL', v: cfg.infra.celery_broker, env_key: 'CELERY_BROKER_URL' },
                { k: 'Redis Password', v: cfg.infra.redis_key, env_key: 'REDIS_PASSWORD', type: 'password' },
            ]),
        ];
        document.getElementById('settingsConfig').innerHTML = sections.join('');
    } catch (err) {
        console.error('Failed to load config:', err);
    }
}

function toggleSecretInput(btn) {
    const input = btn.parentElement.querySelector('input');
    const icon = btn.querySelector('.material-symbols-outlined');
    if (!input) return;
    if (input.type === 'password') {
        input.type = 'text';
        if (icon) icon.textContent = 'visibility_off';
    } else {
        input.type = 'password';
        if (icon) icon.textContent = 'visibility';
    }
}

// ── Logs & Background Jobs ─────────────────────────────────────────
async function loadLogs() {
    const lines = document.getElementById('logLinesSelect')?.value || '250';
    const logsEl = document.getElementById('adminLogsOutput');
    const jobsEl = document.getElementById('adminJobsList');

    try {
        const [logsResp, jobsResp] = await Promise.all([
            fetch(`/api/admin/logs?lines=${encodeURIComponent(lines)}`),
            fetch('/api/admin/jobs'),
        ]);

        if (logsResp.ok) {
            const data = await logsResp.json();
            allLogEntries = data.entries || [];
            const userSel = document.getElementById('logUserFilter');
            if (userSel) {
                const currentVal = userSel.value;
                const unames = Array.from(new Set(['system', ...(data.usernames || []), ...allLogEntries.map(e => e.username).filter(Boolean)])).sort();
                userSel.innerHTML = '<option value="">All Users</option>' +
                    unames.map(u => `<option value="${escapeHtml(u)}">@${escapeHtml(u)}</option>`).join('');
                userSel.value = currentVal;
            }
            applyLogFilters();
        }

        if (jobsResp.ok && jobsEl) {
            const jData = await jobsResp.json();
            const jobs = jData.jobs || [];
            const jobsBadge = document.getElementById('jobsCountBadge');
            if (jobsBadge) jobsBadge.textContent = String(jobs.length);
            if (jobs.length === 0) {
                jobsEl.innerHTML = '<div class="detail-empty">No recent background tasks in this server session.</div>';
            } else {
                jobsEl.innerHTML = jobs.map(j => `
                    <div class="detail-row">
                        <div class="detail-row-main">
                            <div class="detail-row-title">${escapeHtml(j.task_type)} — <code>${escapeHtml(j.subject)}</code></div>
                            <div class="detail-row-sub">ID: ${escapeHtml(j.id)} · Files: ${(j.files || []).map(escapeHtml).join(', ') || '—'}</div>
                        </div>
                        <span class="role-badge ${j.status === 'completed' ? 'admin' : 'instructor'}">${escapeHtml(j.status)}</span>
                    </div>
                `).join('');
            }
        }
    } catch (err) {
        if (logsEl) logsEl.textContent = `Error loading logs: ${err.message}`;
    }
}

function resetLogFilters() {
    const qEl = document.getElementById('logSearchInput');
    const uEl = document.getElementById('logUserFilter');
    const lEl = document.getElementById('logLevelFilter');
    const sEl = document.getElementById('logStartDate');
    const eEl = document.getElementById('logEndDate');
    if (qEl) qEl.value = '';
    if (uEl) uEl.value = '';
    if (lEl) lEl.value = '';
    if (sEl) sEl.value = '';
    if (eEl) eEl.value = '';
    applyLogFilters();
}

function applyLogFilters() {
    const q = (document.getElementById('logSearchInput')?.value || '').toLowerCase().trim();
    const userFilter = (document.getElementById('logUserFilter')?.value || '').toLowerCase().trim();
    const levelFilter = (document.getElementById('logLevelFilter')?.value || '').toUpperCase().trim();
    const startVal = document.getElementById('logStartDate')?.value || '';
    const endVal = document.getElementById('logEndDate')?.value || '';
    const startMs = startVal ? new Date(startVal).getTime() : null;
    const endMs = endVal ? new Date(endVal).getTime() : null;

    const filtered = allLogEntries.filter(e => {
        if (userFilter && (e.username || '').toLowerCase() !== userFilter) return false;
        if (levelFilter && (e.level || '').toUpperCase() !== levelFilter) return false;
        if (startMs || endMs) {
            const tsMs = e.timestamp ? new Date(e.timestamp).getTime() : NaN;
            if (!isNaN(tsMs)) {
                if (startMs && tsMs < startMs) return false;
                if (endMs && tsMs > endMs) return false;
            }
        }
        if (q) {
            const hay = `${e.timestamp || ''} ${e.username || ''} ${e.source || ''} ${e.level || ''} ${e.message || ''}`.toLowerCase();
            if (!hay.includes(q)) return false;
        }
        return true;
    });

    const badge = document.getElementById('logsCountBadge');
    if (badge) badge.textContent = filtered.length;

    const structEl = document.getElementById('adminLogsStructured');
    if (structEl) {
        if (filtered.length === 0) {
            structEl.innerHTML = '<div class="detail-empty">No log entries match your search/filter criteria.</div>';
        } else {
            structEl.innerHTML = filtered.map(e => {
                const lvl = (e.level || 'INFO').toUpperCase();
                const lvlClass = lvl === 'ERROR' ? 'error' : (lvl === 'WARNING' ? 'warn' : 'info');
                return `
                <div class="log-entry-row ${lvlClass}">
                    <span class="log-entry-time">${escapeHtml(fmtTimestampExact(e.timestamp))}</span>
                    <span class="log-entry-level ${lvlClass}">${escapeHtml(lvl)}</span>
                    <span class="log-entry-user">@${escapeHtml(e.username || 'system')}</span>
                    <span class="log-entry-source">${escapeHtml(e.source || 'app')}</span>
                    <span class="log-entry-msg">${escapeHtml(e.message || '')}</span>
                </div>`;
            }).join('');
        }
    }

    const rawEl = document.getElementById('adminLogsOutput');
    if (rawEl) {
        rawEl.textContent = filtered.length
            ? filtered.map(e => `${e.timestamp || '—'} [${e.level || 'INFO'}] @${e.username || 'system'} (${e.source || 'app'}): ${e.message || ''}`).join('\n')
            : 'No log entries match the current filters.';
    }
}

// ── Create user form ───────────────────────────────────────────────
const createForm = document.getElementById('createUserForm');
createForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const errEl = document.getElementById('createError');
    errEl.style.display = 'none';
    const payload = {
        username: document.getElementById('newUsername').value.trim(),
        password: document.getElementById('newPassword').value,
        full_name: document.getElementById('newFullName').value.trim() || null,
        email: document.getElementById('newEmail').value.trim() || null,
        role: document.getElementById('newRole').value,
        courses: document.getElementById('newCourses').value.split(',')
            .map(s => s.trim().toLowerCase()).filter(Boolean),
    };
    try {
        const resp = await fetch('/api/admin/users', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (!resp.ok) {
            const data = await resp.json().catch(() => ({}));
            throw new Error(data.detail || 'Failed to create user');
        }
        createForm.reset();
        document.getElementById('createUserModal').close();
        showToast(`Created user "@${payload.username}"`, 'success');
        loadUsers();
        loadStats();
    } catch (err) {
        errEl.textContent = err.message;
        errEl.style.display = 'block';
    }
});

// ── Edit user form ─────────────────────────────────────────────────
function openEditUserModal(user) {
    document.getElementById('editUserId').value = user.id;
    document.getElementById('editUserModalTitle').textContent = `Edit User — @${user.username}`;
    const uInput = document.getElementById('editUsername');
    if (uInput) uInput.value = user.username || '';
    document.getElementById('editFullName').value = user.full_name || '';
    document.getElementById('editEmail').value = user.email || '';
    document.getElementById('editPassword').value = '';

    const roleSelect = document.getElementById('editRole');
    roleSelect.value = user.role || 'instructor';
    roleSelect.disabled = Boolean(currentUser && user.id === currentUser.id);

    document.getElementById('editCourses').value = (user.courses || []).join(', ');
    renderCoursePickerChips('editCoursesPicker', 'editCourses');
    document.getElementById('editUserModal').showModal();
}

document.getElementById('editUserForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const userId = document.getElementById('editUserId').value;
    const roleSelect = document.getElementById('editRole');
    const uInput = document.getElementById('editUsername');

    const payload = {
        username: uInput ? uInput.value.trim() : undefined,
        full_name: document.getElementById('editFullName').value.trim(),
        email: document.getElementById('editEmail').value.trim(),
        courses: document.getElementById('editCourses').value.split(',')
            .map(s => s.trim().toLowerCase()).filter(Boolean),
    };

    if (!roleSelect.disabled) {
        payload.role = roleSelect.value;
    }

    const pwd = document.getElementById('editPassword').value;
    if (pwd) {
        payload.password = pwd;
    }

    const btn = e.target.querySelector('button[type="submit"]');
    btn.disabled = true;
    try {
        await putUser(userId, payload);
        document.getElementById('editUserModal').close();
        showToast('User updated successfully', 'success');
        if (allSubjects.length) renderCourses();
    } catch (err) {
        showToast(err.message, 'error');
    } finally {
        btn.disabled = false;
    }
});

// ── Create subject form ────────────────────────────────────────────
const createSubjectForm = document.getElementById('createSubjectForm');
createSubjectForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const errEl = document.getElementById('createSubjectError');
    errEl.style.display = 'none';
    const name = document.getElementById('newSubjectName').value.trim().toLowerCase();
    const initFolders = document.getElementById('newSubjectInitFolders')?.checked ?? true;
    const initSkill = document.getElementById('newSubjectInitSkill')?.checked ?? true;

    const btn = e.target.querySelector('button[type="submit"]');
    btn.disabled = true;
    try {
        const resp = await fetch('/api/admin/subjects', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, init_folders: initFolders, init_skill: initSkill }),
        });
        if (!resp.ok) {
            const data = await resp.json().catch(() => ({}));
            throw new Error(data.detail || 'Failed to create subject');
        }
        createSubjectForm.reset();
        document.getElementById('createSubjectModal').close();
        showToast(`Created subject "${name}"`, 'success');

        await loadCourses();
        await loadStats();
    } catch (err) {
        errEl.textContent = err.message;
        errEl.style.display = 'block';
    } finally {
        btn.disabled = false;
    }
});

// Search & role filters
document.getElementById('usersSearch').addEventListener('input', () => renderUsers(allUsers));
document.getElementById('usersRoleFilter')?.addEventListener('change', () => renderUsers(allUsers));
document.getElementById('coursesSearch').addEventListener('input', () => renderCourses());

// Logout
document.getElementById('adminLogoutBtn').addEventListener('click', async () => {
    try {
        await fetch('/api/auth/logout', { method: 'POST' });
    } catch (err) {}
    window.location.href = '/';
});

// ── Settings Save (includes BOTH text and password inputs) ─────────
document.getElementById('updateSettingsForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const inputs = e.target.querySelectorAll('input[name]');
    const settings = {};
    inputs.forEach(input => {
        if (input.name) {
            settings[input.name] = input.value.trim();
        }
    });

    const btn = e.target.querySelector('button[type="submit"]');
    const origHtml = btn.innerHTML;
    btn.disabled = true;
    btn.textContent = 'Saving…';

    try {
        const resp = await fetch('/api/admin/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ settings }),
        });
        if (!resp.ok) throw new Error(await resp.text());

        showToast('Settings saved to .env and applied in memory', 'success');
    } catch (err) {
        showToast('Failed to save settings: ' + err.message, 'error');
    } finally {
        btn.disabled = false;
        btn.innerHTML = origHtml;
    }
});

// ── Theme Toggle ───────────────────────────────────────────────────
const themeToggle = document.getElementById('themeToggle');
const savedTheme = localStorage.getItem('klyra_theme') || 'dark';
document.documentElement.setAttribute('data-theme', savedTheme);

themeToggle.addEventListener('click', () => {
    const current = document.documentElement.getAttribute('data-theme');
    const next = current === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('klyra_theme', next);
});

// ── Init ───────────────────────────────────────────────────────────
async function clearLogs() {
    const ok = await askConfirm({
        title: 'Clear Server & Tool Logs',
        message: 'Clear captured server and tool logs from memory and voice_tools.log?',
        confirmText: 'Clear Logs',
        danger: true,
    });
    if (!ok) return;
    try {
        const resp = await fetch('/api/admin/logs', { method: 'DELETE' });
        if (!resp.ok) throw new Error(await resp.text());
        showToast('Logs cleared', 'success');
        await loadLogs();
    } catch (err) {
        showToast('Failed to clear logs: ' + err.message, 'error');
    }
}

function init() {
    loadStats();
    loadUsers();
}

requireAdmin();
