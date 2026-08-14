// ─── Agent Management ───
let allAgents = [];

function escapeAttr(v) {
    return String(v).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

async function loadAgents() {
    try {
        const res = await fetch(apiUrl(`/api/factory/agents?role=${apiRoleQ()}`), {
            headers: authHeaders(),
            credentials: 'same-origin',
        });
        if (res.ok) {
            const data = await res.json();
            allAgents = data.agents || [];
            renderAgentsList();
        }
    } catch (err) {
        console.error('Load agents failed:', err);
    }
}

function renderAgentsList() {
    const tbody = document.getElementById('agents-tbody');
    if (!tbody) return;
    
    if (allAgents.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;padding:40px;color:var(--text-secondary);">No AI agents found for this sandbox. Create your first agent below.</td></tr>';
        return;
    }
    
    tbody.innerHTML = allAgents.map(a => `
        <tr>
            <td style="font-weight:700;color:var(--text);">${escapeHtml(a.name)}</td>
            <td><span class="badge-tag tag-cbk">${escapeHtml(a.voice)}</span></td>
            <td style="font-size:12px;color:var(--text-secondary);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(a.prompt)}</td>
            <td>${escapeHtml(formatTime(a.created_at))}</td>
            <td style="text-align:right;">
                <button class="btn btn-ghost btn-sm" onclick="editAgent('${escapeAttr(a.id)}')">Edit</button>
                <button class="btn btn-ghost btn-sm" style="color:var(--danger);" onclick="deleteAgentConfirm('${escapeAttr(a.id)}')">Delete</button>
            </td>
        </tr>
    `).join('');
}

async function createAgent() {
    const name = document.getElementById('new-agent-name').value;
    const prompt = document.getElementById('new-agent-prompt').value;
    const voice = document.getElementById('new-agent-voice').value;
    
    if (!name || !prompt) {
        showToast('Please provide a name and prompt', 'error');
        return;
    }
    
    try {
        const res = await fetch(apiUrl(`/api/factory/agents?role=${apiRoleQ()}`), {
            method: 'POST',
            headers: authHeaders(),
            credentials: 'same-origin',
            body: JSON.stringify({ name, prompt, voice }),
        });
        if (res.ok) {
            showToast('Agent created successfully');
            closeModal('modal-new-agent');
            loadAgents();
        } else {
            showToast('Failed to create agent', 'error');
        }
    } catch (err) {
        showToast('Connection error', 'error');
    }
}

async function deleteAgentConfirm(id) {
    if (confirm('Are you sure you want to delete this agent?')) {
        try {
            const res = await fetch(apiUrl(`/api/factory/agent/${id}`), {
                method: 'DELETE',
                headers: authHeaders(),
                credentials: 'same-origin',
            });
            if (res.ok) {
                showToast('Agent deleted');
                loadAgents();
            }
        } catch (err) {
            showToast('Delete failed', 'error');
        }
    }
}
