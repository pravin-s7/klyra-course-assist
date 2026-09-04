/* ── Klyra Admin Console ─────────────────────────────────────────── */
let currentUser = null;
let allUsers = [];
let allSubjects = [];

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
    } catch (e) { return iso; }
}

function fmtBytes(n) {
    if (!n) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB'];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
}

// ── Auth gate ──────────────────────────────────────────────────────
async function requireAdmin() {
    try {
        const resp = await fetch('/api/auth/me');
        if (resp.ok) {
            currentUser = await resp.json();
            if (currentUser.role === 'admin') {
                const displayName = currentUser.full_name || currentUser.username;
                document.getElementById('adminDisplayName').textContent = `${displayName} (Admin)`;
                document.getElementById('adminMain').style.display = 'block';
                init();
                return;
            }
        }
    } catch (err) {
        console.error('Auth check failed:', err);
    }
    document.getElementById('adminDenied').style.display = 'block';
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
        const cards = [
            { label: 'Users', value: s.users, sub: `${s.instructors} instructors · ${s.admins} admins`, icon: '<span class="material-symbols-outlined" style="font-size: 28px;">group</span>' },
            { label: 'Chat sessions', value: s.chat_sessions, sub: 'across all users', icon: '<span class="material-symbols-outlined" style="font-size: 28px;">chat</span>' },
            { label: 'Generated files', value: s.generated_files, sub: 'with ownership records', icon: '<span class="material-symbols-outlined" style="font-size: 28px;">description</span>' },
            { label: 'Courses', value: s.subjects, sub: s.subject_names.slice(0, 3).join(', ') || 'none', icon: '<span class="material-symbols-outlined" style="font-size: 28px;">school</span>' },
        ];
        document.getElementById('statCards').innerHTML = cards.map(c => `
            <div class="stat-card">
                <div class="stat-icon" style="display:flex; align-items:center; justify-content:center;">${c.icon}</div>
                <div class="stat-value">${c.value}</div>
                <div class="stat-label">${c.label}</div>
                <div class="stat-sub">${escapeHtml(c.sub)}</div>
            </div>`).join('');
        document.getElementById('subjectsOverview').innerHTML =
            (s.subject_names || []).map(n => `<span class="course-chip">${escapeHtml(n)}</span>`).join('')
            || '<span style="color:var(--text-muted);">No subjects under docs/ yet.</span>';
    } catch (err) {
        console.error('Failed to load stats:', err);
    }
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
    if (!courses || courses.length === 0) return '<span style="color:var(--text-muted);">(none)</span>';
    return courses.map(c => `<span class="course-chip">${escapeHtml(c)}</span>`).join('');
}

function renderUsers(users) {
    const tbody = document.getElementById('usersBody');
    const q = (document.getElementById('usersSearch').value || '').toLowerCase();
    const filtered = users.filter(u =>
        u.username.toLowerCase().includes(q) ||
        (u.courses || []).some(c => c.toLowerCase().includes(q))
    );
    tbody.innerHTML = '';
    filtered.forEach(user => {
        const isSelf = currentUser && user.id === currentUser.id;
        const tr = document.createElement('tr');

        const nameTd = document.createElement('td');
        nameTd.innerHTML = `<div style="font-weight:600;">${escapeHtml(user.username)}</div>
                            <div style="font-size:12px;color:var(--text-muted);">created ${fmtDate(user.created_at)}</div>`;

        const roleTd = document.createElement('td');
        roleTd.innerHTML = `<span class="role-badge ${escapeHtml(user.role)}">${escapeHtml(user.role)}</span>`;

        const coursesTd = document.createElement('td');
        coursesTd.innerHTML = coursesToChips(user.courses);

        const actionsTd = document.createElement('td');
        const btnRow = document.createElement('div');
        btnRow.className = 'admin-edit-actions';
        
        const viewBtn = document.createElement('button');
        viewBtn.className = 'btn small-btn';
        viewBtn.textContent = 'View data';
        viewBtn.addEventListener('click', () => openUserDetail(user));
        
        const editBtn = document.createElement('button');
        editBtn.className = 'btn small-btn';
        editBtn.textContent = 'Edit';
        editBtn.addEventListener('click', () => openEditUserModal(user));
        
        btnRow.appendChild(viewBtn); 
        btnRow.appendChild(editBtn); 
        
        if (!isSelf) {
            const delBtn = document.createElement('button');
            delBtn.className = 'btn small-btn danger-btn';
            delBtn.textContent = 'Delete';
            delBtn.addEventListener('click', async () => {
                if (!confirm(`Delete user "${user.username}"? This cannot be undone.`)) return;
                const resp = await fetch(`/api/admin/users/${user.id}`, { method: 'DELETE' });
                if (resp.ok) loadUsers(); else alert('Failed to delete: ' + await resp.text());
            });
            btnRow.appendChild(delBtn);
        } else {
            const youSpan = document.createElement('span');
            youSpan.style.color = 'var(--text-muted)';
            youSpan.style.fontSize = '13px';
            youSpan.style.alignSelf = 'center';
            youSpan.style.marginLeft = '8px';
            youSpan.textContent = '(You)';
            btnRow.appendChild(youSpan);
        }
        
        actionsTd.appendChild(btnRow);


        const fnTd = document.createElement('td');
        fnTd.innerHTML = user.full_name ? escapeHtml(user.full_name) : '<span style="color:var(--text-muted);">—</span>';
        
        const emTd = document.createElement('td');
        emTd.innerHTML = user.email ? escapeHtml(user.email) : '<span style="color:var(--text-muted);">—</span>';

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
    try {
        const resp = await fetch(`/api/admin/users/${id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (!resp.ok) throw new Error(await resp.text());
        loadUsers();
    } catch (err) {
        alert('Error: ' + err.message);
    }
}
// ── User detail modal ──────────────────────────────────────────────
async function openUserDetail(user) {
    const modal = document.getElementById('userDetailModal');
    document.getElementById('detailUserName').textContent = user.username;
    document.getElementById('detailUserMeta').textContent =
        `${user.role} · courses: ${(user.courses || []).join(', ') || 'none'}`;

    let sessions = [];
    try {
        const sResp = await fetch(`/api/admin/users/${user.id}/sessions`);
        if (sResp.ok) sessions = (await sResp.json()).sessions || [];
    } catch (e) { console.error(e); }
    document.getElementById('detailSessionCount').textContent = sessions.length;
    document.getElementById('detailSessions').innerHTML = sessions.length === 0
        ? '<div class="detail-empty">No chat sessions.</div>'
        : sessions.map(s => `
            <div class="detail-row">
                <div class="detail-row-main">
                    <div class="detail-row-title">${escapeHtml(s.title)}</div>
                    <div class="detail-row-sub">${s.message_count} msgs · ${fmtDate(s.created_at)}</div>
                </div>
                <div style="display:flex; gap:6px;">
                    <button class="btn small-btn" onclick="openChatViewer('${s.session_id}', '${escapeHtml(s.title)}')">View Chat</button>
                    <button class="btn small-btn danger-btn" onclick="deleteUserSession('${user.id}','${s.session_id}', this)">Delete</button>
                </div>
            </div>`).join('');

    let files = [];
    try {
        const fResp = await fetch(`/api/admin/users/${user.id}/files`);
        if (fResp.ok) files = (await fResp.json()).files || [];
    } catch (e) { console.error(e); }
    document.getElementById('detailFileCount').textContent = files.length;
    document.getElementById('detailFiles').innerHTML = files.length === 0
        ? '<div class="detail-empty">No generated files recorded.</div>'
        : files.map(f => {
            const safePath = f.rel_path.replace(/'/g, "\\'");
            return `
            <div class="detail-row">
                <div class="detail-row-main">
                    <div class="detail-row-title">${escapeHtml(f.rel_path)}</div>
                    <div class="detail-row-sub">${f.exists ? fmtBytes(f.size) : 'missing on disk'} · ${fmtDate(f.created_at)}</div>
                </div>
                <button class="btn small-btn danger-btn" onclick="revokeFile('${user.id}', '${safePath}', this)">Revoke</button>
            </div>`;
        }).join('');

    modal.showModal();
}

async function openChatViewer(sessionId, title) {
    document.getElementById('chatViewerTitle').textContent = `Chat: ${title}`;
    const content = document.getElementById('chatViewerContent');
    content.innerHTML = '<div style="color:var(--text-muted);text-align:center;">Loading...</div>';
    document.getElementById('chatViewerModal').showModal();

    try {
        const resp = await fetch(`/api/admin/sessions/${sessionId}/history`);
        if (!resp.ok) throw new Error(await resp.text());
        const data = await resp.json();
        const history = data.history || [];
        
        if (history.length === 0) {
            content.innerHTML = '<div style="color:var(--text-muted);text-align:center;">No messages in this session.</div>';
            return;
        }

        content.innerHTML = history.map(ev => {
            if (ev.type === 'user_transcript') {
                return `<div style="background:var(--bg-tertiary); padding:10px; border-radius:8px; align-self:flex-end; max-width:85%;">
                    <div style="font-size:11px; color:var(--text-muted); margin-bottom:4px;">User</div>
                    ${escapeHtml(ev.text)}
                </div>`;
            } else if (ev.type === 'klyra_text') {
                return `<div style="background:rgba(59, 130, 246, 0.1); padding:10px; border-radius:8px; align-self:flex-start; max-width:85%;">
                    <div style="font-size:11px; color:var(--accent); margin-bottom:4px;">Klyra</div>
                    ${escapeHtml(ev.text)}
                </div>`;
            } else if (ev.type === 'tool_start') {
                return `<div style="font-size:12px; color:var(--text-muted); align-self:flex-start;">
                    <em>🛠 Tool: ${escapeHtml(ev.tool)}</em>
                </div>`;
            }
            return '';
        }).join('');
    } catch (err) {
        content.innerHTML = `<div style="color:red;">Error: ${err.message}</div>`;
    }
}

async function deleteUserSession(userId, sessionId, btn) {
    if (!confirm('Delete this chat session?')) return;
    btn.disabled = true;
    const resp = await fetch(`/api/admin/users/${userId}/sessions/${sessionId}`, { method: 'DELETE' });
    if (resp.ok) {
        openUserDetail(allUsers.find(u => u.id === userId));
    } else {
        alert('Failed: ' + await resp.text());
        btn.disabled = false;
    }
}

async function revokeFile(userId, relPath, btn) {
    if (!confirm('Revoke this user\'s access to this generated file? (Admin retains access; ownership record removed.)')) return;
    btn.disabled = true;
    const resp = await fetch(`/api/admin/users/${userId}/files/${relPath}`, { method: 'DELETE' });
    if (resp.ok) {
        openUserDetail(allUsers.find(u => u.id === userId));
    } else {
        alert('Failed: ' + await resp.text());
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
        renderCourses();
    } catch (err) {
        console.error('Failed to load courses:', err);
    }
}

function renderCourses() {
    const container = document.getElementById('coursesList');
    if (allSubjects.length === 0) {
        container.innerHTML = '<div class="admin-card"><p style="color:var(--text-muted);">No subjects discovered under docs/. Upload course material from the chat app first.</p></div>';
        return;
    }
    const query = (document.getElementById('coursesSearch')?.value || '').toLowerCase();
    
    const filteredSubjects = allSubjects.filter(s => 
        s.name.toLowerCase().includes(query) || 
        s.folders.some(f => f.toLowerCase().includes(query))
    );

    if (filteredSubjects.length === 0) {
        container.innerHTML = '<div class="admin-card"><p style="color:var(--text-muted);">No matching subjects found.</p></div>';
        return;
    }
    const instructors = allUsers.filter(u => u.role !== 'admin');

    container.innerHTML = filteredSubjects.map(subj => {
        const assigned = instructors.filter(u => (u.courses || []).includes(subj.name));
        const chips = assigned.map(u => `
            <span class="course-chip" style="display:inline-flex;align-items:center;gap:4px;">
                ${escapeHtml(u.username)}
                <button class="chip-x" title="Unassign"
                    onclick="setCourseForUser('${u.id}', '${subj.name}', false)">×</button>
            </span>`).join('') || '<span style="color:var(--text-muted);">Not assigned to any instructor</span>';

        const options = instructors.map(u => {
            const has = (u.courses || []).includes(subj.name);
            const label = u.full_name ? `${escapeHtml(u.full_name)} (${escapeHtml(u.email || u.username)})` : escapeHtml(u.username);
            return `<option value="${u.id}" ${has ? 'selected' : ''}>${label}</option>`;
        }).join('');

        return `
        <div class="admin-card course-card">
            <div class="course-card-head">
                <div>
                    <h2 style="display:flex;align-items:center;gap:6px;cursor:pointer;" onclick="openSubjectDetail('${subj.name}')" title="Click to view files">
                        <span class="material-symbols-outlined">school</span> ${escapeHtml(subj.name)}
                    </h2>
                    <p class="detail-row-sub">${subj.file_count} files · folders: ${subj.folders.map(f => escapeHtml(f)).join(', ') || '—'}</p>
                </div>
            </div>
            <div class="course-assigned">
                <strong>Assigned instructors:</strong>
                <div style="margin-top:8px;">${chips}</div>
            </div>
            <div class="course-assign-row">
                <label style="font-size:13px;color:var(--text-secondary);">Assign/update:</label>
                <select class="course-user-select" data-subject="${subj.name}" onchange="applyCourseSelect(this, '${subj.name}')">
                    <option value="">— select instructor —</option>
                    ${options}
                </select>
            </div>
        </div>`;
    }).join('');
}

async function applyCourseSelect(select, subject) {
    const userId = select.value;
    if (!userId) return;
    setCourseForUser(userId, subject, true);
}

async function setCourseForUser(userId, subject, add) {
    const user = allUsers.find(u => u.id === userId);
    if (!user) return;
    const courses = new Set((user.courses || []).map(c => c.toLowerCase()));
    if (add) courses.add(subject.toLowerCase()); else courses.delete(subject.toLowerCase());
    await putUser(userId, { courses: Array.from(courses) });
    if (allSubjects.length) renderCourses();
}

// ── Subject Detail View ────────────────────────────────────────────
let currentSubjectFilesData = null;

async function openSubjectDetail(subjectName) {
    document.getElementById('subjectDetailTitle').textContent = `Subject: ${subjectName}`;
    const content = document.getElementById('subjectDetailContent');
    content.innerHTML = '<div style="color:var(--text-muted);text-align:center;">Loading files...</div>';
    document.getElementById('subjectFilesSearch').value = '';
    document.getElementById('subjectDetailModal').showModal();
    
    try {
        const resp = await fetch(`/api/admin/subjects/${subjectName}/files`);
        if (!resp.ok) throw new Error(await resp.text());
        currentSubjectFilesData = await resp.json();
        renderSubjectFiles();
    } catch (err) {
        content.innerHTML = `<div style="color:red;">Error: ${err.message}</div>`;
    }
}

function filterSubjectFiles() {
    renderSubjectFiles();
}

function renderSubjectFiles() {
    const content = document.getElementById('subjectDetailContent');
    if (!currentSubjectFilesData) return;
    
    const query = document.getElementById('subjectFilesSearch').value.toLowerCase();
    const folders = currentSubjectFilesData.folders;
    
    if (Object.keys(folders).length === 0) {
        content.innerHTML = '<div style="color:var(--text-muted);text-align:center;">No files found in this subject.</div>';
        return;
    }
    
    let html = '';
    let hasMatches = false;
    
    for (const [folderName, files] of Object.entries(folders).sort()) {
        const folderMatches = folderName.toLowerCase().includes(query);
        const matchingFiles = files.filter(f => f.toLowerCase().includes(query) || folderMatches);
        
        if (matchingFiles.length > 0) {
            hasMatches = true;
            html += `
                <div style="margin-bottom: 16px;">
                    <div style="font-weight: 600; color: var(--text-primary); margin-bottom: 8px; display: flex; align-items: center; gap: 6px;">
                        <span class="material-symbols-outlined" style="font-size: 18px; color: var(--accent);">folder</span>
                        ${escapeHtml(folderName)}
                    </div>
                    <div style="display: flex; flex-direction: column; gap: 4px; padding-left: 24px;">
                        ${matchingFiles.map(f => `
                            <div style="display: flex; align-items: center; gap: 6px; color: var(--text-secondary); font-size: 13px;">
                                <span class="material-symbols-outlined" style="font-size: 16px;">description</span>
                                ${escapeHtml(f)}
                            </div>
                        `).join('')}
                    </div>
                </div>
            `;
        }
    }
    
    if (!hasMatches) {
        html = '<div style="color:var(--text-muted);text-align:center;">No files match your search.</div>';
    }
    
    content.innerHTML = html;
}

// ── Settings ───────────────────────────────────────────────────────
async function loadConfig() {
    try {
        const resp = await fetch('/api/admin/config');
        if (!resp.ok) throw new Error(await resp.text());
        const cfg = await resp.json();
        const group = (title, rows) => `
            <div class="config-group">
                <h2>${title}</h2>
                ${rows.map(r => `
                    <div class="config-row">
                        <span class="config-key">${escapeHtml(r.k)}</span>
                        <input type="${r.type || 'text'}" name="${r.env_key}" value="${escapeHtml(r.v)}" class="admin-edit-row">
                    </div>`).join('')}
            </div>`;
        const sections = [
            group('Storage', [
                { k: 'Postgres Database URL', v: cfg.storage.database, env_key: 'DATABASE_URL' },
                { k: 'SQLite Database', v: cfg.storage.klyra_db, env_key: 'KLYRA_DB' },
                { k: 'Jobs Database', v: cfg.storage.jobs_db, env_key: 'KLYRA_JOBS_DB' },
            ]),
            group('Auth', [
                { k: 'Session secret', v: cfg.auth.session_secret, env_key: 'KLYRA_SESSION_SECRET', type: 'password' },
                { k: 'Google Client ID', v: cfg.auth.google_client_id, env_key: 'GOOGLE_CLIENT_ID' },
                { k: 'Google Client Secret', v: cfg.auth.google_client_secret, env_key: 'GOOGLE_CLIENT_SECRET', type: 'password' },
                { k: 'Auto-provision OAuth users', v: cfg.auth.oauth_auto_provision, env_key: 'KLYRA_OAUTH_AUTO_PROVISION' },
            ]),
            group('LLM', [
                { k: 'Agents model', v: cfg.llm.model, env_key: 'KLYRA_LLM_MODEL' },
                { k: 'Chat model', v: cfg.llm.chat_model, env_key: 'KLYRA_CHAT_MODEL' },
                { k: 'Voice model', v: cfg.llm.voice_model, env_key: 'KLYRA_VOICE_MODEL' },
            ]),
            group('Infrastructure', [
                { k: 'Qdrant URL', v: cfg.infra.qdrant_url, env_key: 'QDRANT_URL' },
                { k: 'Celery Broker', v: cfg.infra.celery_broker, env_key: 'CELERY_BROKER_URL' },
                { k: 'Redis password', v: cfg.infra.redis_key, env_key: 'REDIS_PASSWORD', type: 'password' },
            ]),
        ];
        document.getElementById('settingsConfig').innerHTML = sections.join('');
    } catch (err) {
        console.error('Failed to load config:', err);
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
        loadUsers();
    } catch (err) {
        errEl.textContent = err.message;
        errEl.style.display = 'block';
    }
});

// ── Edit user form ─────────────────────────────────────────────────
function openEditUserModal(user) {
    document.getElementById('editUserId').value = user.id;
    document.getElementById('editFullName').value = user.full_name || '';
    document.getElementById('editEmail').value = user.email || '';
    document.getElementById('editPassword').value = '';
    
    const roleSelect = document.getElementById('editRole');
    roleSelect.value = user.role || 'instructor';
    roleSelect.disabled = (currentUser && user.id === currentUser.id);
    
    document.getElementById('editCourses').value = (user.courses || []).join(', ');
    document.getElementById('editUserModal').showModal();
}

document.getElementById('editUserForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const userId = document.getElementById('editUserId').value;
    const roleSelect = document.getElementById('editRole');
    
    const payload = {
        full_name: document.getElementById('editFullName').value.trim() || null,
        email: document.getElementById('editEmail').value.trim() || null,
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
    const name = document.getElementById('newSubjectName').value.trim();
    
    const btn = e.target.querySelector('button[type="submit"]');
    btn.disabled = true;
    try {
        const resp = await fetch('/api/admin/subjects', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name }),
        });
        if (!resp.ok) {
            const data = await resp.json().catch(() => ({}));
            throw new Error(data.detail || 'Failed to create subject');
        }
        createSubjectForm.reset();
        document.getElementById('createSubjectModal').close();
        
        // Refresh subjects
        const subjectsResp = await fetch('/api/admin/subjects');
        if (subjectsResp.ok) {
            allSubjects = (await subjectsResp.json()).subjects || [];
            renderCourses();
        }
    } catch (err) {
        errEl.textContent = err.message;
        errEl.style.display = 'block';
    } finally {
        btn.disabled = false;
    }
});

// Search filters
document.getElementById('usersSearch').addEventListener('input', () => renderUsers(allUsers));
document.getElementById('coursesSearch').addEventListener('input', () => renderCourses());

// Logout
document.getElementById('adminLogoutBtn').addEventListener('click', async () => {
    try { await fetch('/api/auth/logout', { method: 'POST' }); } catch (err) { }
    window.location.href = '/';
});

// ── Settings Save ───────────────────────────────────────────────────
document.getElementById('updateSettingsForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const inputs = e.target.querySelectorAll('input[type="text"]');
    const settings = {};
    inputs.forEach(input => {
        if (input.name) {
            settings[input.name] = input.value.trim();
        }
    });
    
    const btn = e.target.querySelector('button[type="submit"]');
    btn.disabled = true;
    btn.textContent = 'Saving...';
    
    try {
        const resp = await fetch('/api/admin/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ settings }),
        });
        if (!resp.ok) throw new Error(await resp.text());
        
        btn.textContent = 'Saved!';
        setTimeout(() => { btn.disabled = false; btn.textContent = 'Save Settings'; }, 2000);
    } catch (err) {
        alert('Failed to save settings: ' + err.message);
        btn.disabled = false;
        btn.textContent = 'Save Settings';
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
function init() {
    loadStats();
}

requireAdmin();