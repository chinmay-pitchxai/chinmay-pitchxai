// ─── Campaign Cases (per-role) — SQLite via /api/cases; used by templates/console.html ───

function escapeHtmlSafe(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, (c) =>
        ({
            '&': '&amp;',
            '<': '&lt;',
            '>': '&gt;',
            '"': '&quot;',
            "'": '&#39;',
        }[c])
    );
}

function escapeAttr(v) {
    return String(v).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

let _casesLastLoaded = [];

async function loadCases() {
    const list = document.getElementById('cases-list');
    const pill = document.getElementById('cases-active-pill');
    if (!list) return;
    list.innerHTML = `<div style="font-size:13px;color:var(--text-secondary);text-align:center;padding:18px;">Loading cases…</div>`;
    try {
        const res = await fetch(apiUrl(`/api/cases?role=${apiRoleQ()}`), {
            headers: { Authorization: `Bearer ${token()}` },
            credentials: 'same-origin',
        });
        if (!res.ok) {
            list.innerHTML = `<div style="font-size:13px;color:var(--danger);text-align:center;padding:18px;">Could not load cases (${escapeHtmlSafe(res.status)}).</div>`;
            _casesLastLoaded = [];
            return;
        }
        const data = await res.json();
        const cases = Array.isArray(data.cases) ? data.cases : [];
        _casesLastLoaded = cases;
        const activeId = data.active_case_id != null ? data.active_case_id : null;
        const active = cases.find((c) => Number(c.id) === Number(activeId));
        if (pill) {
            if (active) {
                pill.textContent = `Active · ${active.name}`;
                pill.style.background = 'rgba(52,199,89,.14)';
                pill.style.color = '#34C759';
            } else {
                pill.textContent = 'No active case';
                pill.style.background = 'rgba(0,0,0,.05)';
                pill.style.color = 'var(--text-secondary)';
            }
        }
        if (!cases.length) {
            list.innerHTML = `
                <div style="font-size:13px;color:var(--text-secondary);text-align:center;padding:30px;border:1px dashed var(--border);border-radius:12px;">
                    No cases yet for this role.<br>
                    <button class="btn btn-primary btn-sm" style="margin-top:14px;" onclick="openModal('modal-case')">+ Create your first case</button>
                </div>`;
            return;
        }
        list.innerHTML = cases
            .map((c) => {
                const isActive = !!c.active;
                const desc = c.description || '';
                const descPreview = desc.length > 240 ? desc.slice(0, 240) + '…' : desc;
                const updated = c.updated_at ? formatTime(c.updated_at) : '';
                return `
                <div class="case-row" data-case-id="${escapeAttr(c.id)}" style="border:1px solid ${isActive ? 'rgba(52,199,89,.4)' : 'var(--border)'};border-radius:12px;padding:14px 16px;background:${isActive ? 'rgba(52,199,89,.06)' : 'var(--card)'};display:flex;flex-direction:column;gap:10px;">
                    <div style="display:flex;align-items:flex-start;gap:14px;">
                        <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:13px;font-weight:700;flex:1;min-width:0;">
                            <input type="checkbox" ${isActive ? 'checked' : ''} onchange="toggleCase(${escapeAttr(c.id)}, this.checked)" style="width:18px;height:18px;cursor:pointer;accent-color:var(--primary);">
                            <span style="font-weight:700;">${escapeHtmlSafe(c.name)}</span>
                            ${isActive ? '<span style="font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.06em;background:rgba(52,199,89,.15);color:#34C759;padding:3px 8px;border-radius:999px;">ACTIVE</span>' : ''}
                        </label>
                        <div style="display:flex;gap:6px;">
                            <button type="button" class="btn btn-ghost btn-sm" onclick="openCaseEdit(${escapeAttr(c.id)})" style="font-size:11px;padding:4px 12px;">Edit</button>
                            <button type="button" class="btn btn-ghost btn-sm" onclick="deleteCaseUI(${escapeAttr(c.id)})" style="font-size:11px;padding:4px 12px;color:var(--danger);border-color:var(--danger);">Delete</button>
                        </div>
                    </div>
                    ${desc ? `<div style="font-size:12px;color:var(--text-secondary);line-height:1.5;white-space:pre-wrap;">${escapeHtmlSafe(descPreview)}</div>` : '<div style="font-size:11px;color:var(--text-secondary);font-style:italic;">No instructions yet — click Edit to add them.</div>'}
                    ${updated ? `<div style="font-size:10px;color:var(--text-secondary);text-transform:uppercase;letter-spacing:.06em;">Updated · ${escapeHtmlSafe(updated)}</div>` : ''}
                </div>`;
            })
            .join('');
    } catch (_) {
        _casesLastLoaded = [];
        list.innerHTML = `<div style="font-size:13px;color:var(--danger);text-align:center;padding:18px;">Network error loading cases.</div>`;
    }
}

function _findCaseById(id) {
    return _casesLastLoaded.find((c) => Number(c.id) === Number(id));
}

async function _refreshCasesCache() {
    try {
        const res = await fetch(apiUrl(`/api/cases?role=${apiRoleQ()}`), {
            headers: { Authorization: `Bearer ${token()}` },
            credentials: 'same-origin',
        });
        if (!res.ok) return;
        const data = await res.json();
        _casesLastLoaded = Array.isArray(data.cases) ? data.cases : [];
    } catch (_) {}
}

async function openCaseEdit(id) {
    await _refreshCasesCache();
    const c = _findCaseById(id);
    if (!c) {
        showToast('Case not found.', 'error');
        return;
    }
    document.getElementById('case-modal-title').textContent = 'Edit Case';
    document.getElementById('case-edit-id').value = String(c.id);
    document.getElementById('case-name').value = c.name || '';
    document.getElementById('case-description').value = c.description || '';
    document.getElementById('case-save-btn').textContent = 'Save Changes';
    openModal('modal-case');
}

function _resetCaseModal() {
    document.getElementById('case-modal-title').textContent = 'New Case';
    document.getElementById('case-edit-id').value = '';
    document.getElementById('case-name').value = '';
    document.getElementById('case-description').value = '';
    document.getElementById('case-save-btn').textContent = 'Save Case';
}

async function submitCaseModal() {
    const idVal = (document.getElementById('case-edit-id').value || '').trim();
    const name = (document.getElementById('case-name').value || '').trim();
    const description = (document.getElementById('case-description').value || '').trim();
    if (!name) {
        showToast('Case name is required.', 'error');
        return;
    }
    const btn = document.getElementById('case-save-btn');
    const original = btn ? btn.textContent : 'Save Case';
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Saving…';
    }
    try {
        let res;
        if (idVal) {
            res = await fetch(apiUrl(`/api/cases/${encodeURIComponent(idVal)}`), {
                method: 'PATCH',
                headers: authHeaders(),
                credentials: 'same-origin',
                body: JSON.stringify({ name, description }),
            });
        } else {
            res = await fetch(apiUrl(`/api/cases?role=${apiRoleQ()}`), {
                method: 'POST',
                headers: authHeaders(),
                credentials: 'same-origin',
                body: JSON.stringify({ name, description }),
            });
        }
        if (!res.ok) {
            showToast(await parseApiErrorMessage(res), 'error');
            return;
        }
        closeModal('modal-case');
        _resetCaseModal();
        showToast(idVal ? 'Case updated.' : 'Case created.', 'success');
        await loadCases();
    } catch (e) {
        showToast(e && e.message ? e.message : 'Network error', 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = original || 'Save Case';
        }
    }
}

async function toggleCase(id, checked) {
    try {
        let res;
        if (checked) {
            res = await fetch(apiUrl(`/api/cases/${id}/activate?role=${apiRoleQ()}`), {
                method: 'POST',
                headers: authHeaders(),
                credentials: 'same-origin',
            });
        } else {
            res = await fetch(apiUrl(`/api/cases/deactivate?role=${apiRoleQ()}`), {
                method: 'POST',
                headers: authHeaders(),
                credentials: 'same-origin',
            });
        }
        if (!res.ok) {
            showToast(await parseApiErrorMessage(res), 'error');
        } else {
            showToast(checked ? 'Case activated for this role.' : 'No case is active.', 'success');
        }
    } catch (e) {
        showToast(e && e.message ? e.message : 'Network error', 'error');
    } finally {
        await loadCases();
    }
}

async function deleteCaseUI(id) {
    if (!confirm('Delete this case? This cannot be undone.')) return;
    try {
        const res = await fetch(apiUrl(`/api/cases/${id}`), {
            method: 'DELETE',
            headers: authHeaders(),
            credentials: 'same-origin',
        });
        if (!res.ok) {
            showToast(await parseApiErrorMessage(res), 'error');
            return;
        }
        showToast('Case deleted.', 'success');
        await loadCases();
    } catch (e) {
        showToast(e && e.message ? e.message : 'Network error', 'error');
    }
}
