// --- Restored Missing Functions ---

function formatActionDateTime(iso) {
    if (!iso) return '';
    try {
        var s = String(iso);
        var hasTZ = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(s);
        if (!hasTZ && /\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(s)) s = s + 'Z';
        var d = new Date(s);
        if (isNaN(d.getTime())) return iso;
        return d.toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
    } catch (e) { return iso; }
}

function downloadFilteredCSV() {
    const search = (document.getElementById('search-input')?.value || '').toLowerCase().trim();
    let rows = allLeads.filter(isCalled);

    // Check if a chart filter is active (exclusive mode — overrides all other filters)
    var _chartFilterActive = (typeof getChartFilter === 'function') && getChartFilter() && getChartFilter().type;

    // Apply Chart Filters
    if (typeof isLeadMatchingChartFilter === 'function') {
        rows = rows.filter(isLeadMatchingChartFilter);
    }

    if (!_chartFilterActive) {
        // Apply Disposition Filters (only when no chart filter is active)
        if (currentFilter !== 'all') {
            if (currentFilter === 'failed') rows = rows.filter(isFailed);
            else if (currentFilter === 'dialing') rows = rows.filter(l => String(l.status || '').toLowerCase() === 'dialing');
            else if (currentFilter === 'star4') rows = rows.filter(l => (l.rating || 0) >= 4);
            else if (currentFilter === 'site_visit' || currentFilter === 'Site Visited') rows = rows.filter(hasSiteVisitWithParticularDate);
            else if (currentFilter === 'follow_up') rows = rows.filter(isFollowUpLead);
            else rows = rows.filter(l => effectiveDispo(l) === currentFilter);
        }
        
        const fromVal = document.getElementById('filter-date-from')?.value;
        const toVal = document.getElementById('filter-date-to')?.value;
        if (fromVal || toVal) {
            const fromMs = fromVal ? _istDayStartMs(fromVal) : 0;
            var effectiveTo = toVal;
            if (fromVal && !toVal) {
                effectiveTo = fromVal;
            }
            const toMs = effectiveTo ? _istDayEndMs(effectiveTo) : Infinity;
            rows = rows.filter(function (l) {
                return isLeadCalledInDateRange(l, { fromMs: fromMs, toMs: toMs });
            });
        }

        var locationFilter = (document.getElementById('filter-location')?.value || '').trim();
        var budgetFilter = (document.getElementById('filter-budget')?.value || '').trim();
        if (locationFilter || budgetFilter) {
            rows = rows.filter(function (l) {
                var ext = {};
                try { ext = typeof l.extra === 'string' ? JSON.parse(l.extra) : (l.extra || {}); } catch(e) {}
                if (locationFilter && (ext.location || '') !== locationFilter) return false;
                if (budgetFilter && (ext.budget || '') !== budgetFilter) return false;
                return true;
            });
        }

        const timeFromEl = document.getElementById('filter-time-from');
        const timeToEl = document.getElementById('filter-time-to');
        const timeFrom = timeFromEl ? timeFromEl.value : '';
        const timeTo = timeToEl ? timeToEl.value : '';
        if (timeFrom || timeTo) {
            rows = rows.filter(function (l) {
                if (!l.start_time) return false;
                const d = new Date(l.start_time * 1000);
                const h = d.getHours();
                const m = d.getMinutes();
                const mins = h * 60 + m;
                if (timeFrom) {
                    const [fh, fm] = timeFrom.split(':').map(Number);
                    if (mins < fh * 60 + fm) return false;
                }
                if (timeTo) {
                    const [th, tm] = timeTo.split(':').map(Number);
                    if (mins > th * 60 + tm) return false;
                }
                return true;
            });
        }

        if (search) {
            rows = rows.filter(function (l) {
                var p = typeof leadContactPrimary === 'function' ? leadContactPrimary(l) : (l.name || '');
                var s2 = typeof leadContactSecondary === 'function' ? leadContactSecondary(l) : (l.company || '');
                var ext = {};
                try { ext = typeof l.extra === 'string' ? JSON.parse(l.extra) : (l.extra || {}); } catch(e) {}
                return (l.name || '').toLowerCase().includes(search)
                    || (p || '').toLowerCase().includes(search)
                    || (s2 || '').toLowerCase().includes(search)
                    || (l.phone || '').toLowerCase().includes(search)
                    || (l.company || '').toLowerCase().includes(search)
                    || (l.summary || '').toLowerCase().includes(search)
                    || (ext.location || '').toLowerCase().includes(search)
                    || (ext.budget || '').toLowerCase().includes(search);
            });
        }
    }

    rows.sort((a, b) => (b.start_time || 0) - (a.start_time || 0));

    if (!rows.length) {
        showToast('No records to export for current filters.', 'info');
        return;
    }


    // Build CSV
    const headers = [
        'S.NO',
        'Date & Time',
        'Lead ID',
        'Name',
        'Phone Number',
        'Email ID',
        'Outcome',
        'Retake Attempt',
        'Source File Name',
        'Rating',
        'Budget',
        'Location',
        'Summary',
        'WhatsApp Sent',
        'Email Sent'
    ];
    const lines = rows.map((r, idx) => {
        const sNo = idx + 1;

        // Date & time — formatted as DD-MM-YYYY HH:MM:SS so Excel never shows ########
        let calledAt = '';
        let dateObj = null;
        if (r.called_at_iso) {
            let s = r.called_at_iso;
            if (!/[zZ]$|[+-]\d{2}:?\d{2}$/.test(s) && /\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(s)) s = s + 'Z';
            dateObj = new Date(s);
        } else if (r.start_time && Number(r.start_time) > 0) {
            dateObj = new Date(Number(r.start_time) * 1000);
        }
        if (dateObj && !isNaN(dateObj.getTime())) {
            const dd   = String(dateObj.getDate()).padStart(2, '0');
            const mm   = String(dateObj.getMonth() + 1).padStart(2, '0');
            const yyyy = dateObj.getFullYear();
            const hh   = String(dateObj.getHours()).padStart(2, '0');
            const min  = String(dateObj.getMinutes()).padStart(2, '0');
            const ss   = String(dateObj.getSeconds()).padStart(2, '0');
            calledAt = `${dd}-${mm}-${yyyy} ${hh}:${min}:${ss}`;
        }

        // Parse analysis blob
        let aj = {};
        if (r.analysis) {
            try { aj = typeof r.analysis === 'string' ? JSON.parse(r.analysis) : r.analysis; }
            catch (e) { aj = {}; }
        }

        const leadId = String(r.id || '');
        const name   = String(r.name || '');
        // Apostrophe prefix stops Excel from reformatting phone as a number
        const phone  = r.phone ? "'" + String(r.phone) : '';
        const email  = String(r.email || '');

        // Single Outcome column — mirrors what the dashboard shows
        const outcome = (typeof effectiveDispo === 'function') ? effectiveDispo(r) : (r.disposition || r.status || 'Unknown');

        // Rating — omit zeros
        let rating = '';
        const rVal = r.rating !== undefined && r.rating !== null && r.rating !== '' ? r.rating : aj.rating;
        if (rVal !== undefined && rVal !== null && rVal !== '') {
            const num = Number(rVal);
            if (!isNaN(num) && num > 0) rating = String(num);
        }

        // Budget & Location from analysis blob
        const budget   = String(aj.budget   || r.budget   || '');
        const location = String(aj.location || r.location || '');

        // Summary
        const stLc = String(r.status || '').toLowerCase();
        const connected = ['completed','site_visit','site_visited','callback_scheduled','callback_completed','not_interested'].includes(stLc);
        const summary = String(r.summary || aj.summary || r.error || (connected ? 'Call answered.' : 'Call did not connect.'));

        // WhatsApp & Email flags
        const whatsappSent = (r.whatsapp_sent === 1 || r.whatsapp_sent === true) ? 'Yes' : 'No';
        const emailSent    = (r.email_sent    === 1 || r.email_sent    === true) ? 'Yes' : 'No';

        // Retake status
        var ext = {};
        try { ext = typeof r.extra === 'string' ? JSON.parse(r.extra) : (r.extra || {}); } catch(e) {}
        const retriesVal = Number(ext.failed_call_retries) || 0;
        const retakeAttempt = retriesVal > 0 ? 'Retake ' + retriesVal : 'None';
        const sourceFileName = ext.upload_source || 'Unknown';

        const cells = [sNo, calledAt, leadId, name, phone, email, outcome, retakeAttempt, sourceFileName, rating, budget, location, summary, whatsappSent, emailSent];
        return cells.map(c => '"' + String(c).replace(/"/g, '""') + '"').join(',');
    });

    // UTF-8 BOM so Excel opens without garbled Hindi/special characters
    const BOM = '\uFEFF';
    const csv  = BOM + [headers.join(','), ...lines].join('\n');
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href = url;
    const today = new Date().toISOString().slice(0, 10);
    const filt  = currentFilter !== 'all' ? `-${currentFilter}` : '';
    a.download = `vernika-${currentRole}${filt}-${today}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    showToast(`Exported ${rows.length} rows.`, 'success');
}

// ─── Call Detail Modal ───

let _cdRecordingBlobUrl = null;

function revokeCdRecordingBlobUrl() {
    if (_cdRecordingBlobUrl) {
        URL.revokeObjectURL(_cdRecordingBlobUrl);
        _cdRecordingBlobUrl = null;
    }
}

async function prepCallDetailRecording(lead) {
    revokeCdRecordingBlobUrl();
    const block = document.getElementById('cd-recording-block');
    const audio = document.getElementById('cd-audio');
    if (!block || !audio) return;

    const media = typeof resolveLeadMediaContext === 'function'
        ? resolveLeadMediaContext(lead)
        : { leadId: lead && lead.id, logId: String((lead && (lead._log_id || lead.log_id)) || ''), hasMedia: !!(lead && (lead.recording_available || lead._log_id || lead.log_id)) };
    if (!media.hasMedia || media.leadId == null) {
        block.style.display = 'none';
        audio.removeAttribute('src');
        audio.style.display = 'none';
        return;
    }

    block.style.display = 'block';
    const msgId = 'cd-recording-msg';
    let msgEl = document.getElementById(msgId);
    if (!msgEl) {
        msgEl = document.createElement('p');
        msgEl.id = msgId;
        msgEl.style.cssText = 'margin:8px 0 0;font-size:12px;color:var(--text-secondary);';
        block.appendChild(msgEl);
    }
    msgEl.textContent = 'Loading recording…';
    audio.style.display = 'none';
    audio.removeAttribute('src');

    function showRecordingError(msg) {
        revokeCdRecordingBlobUrl();
        audio.removeAttribute('src');
        audio.style.display = 'none';
        block.style.display = 'block';
        msgEl.textContent = msg || 'Recording not available for this call.';
    }

    function attachStreamSrc() {
        if (typeof campaignRecordingStreamUrl !== 'function') return false;
        const streamUrl = campaignRecordingStreamUrl(media.leadId);
        return new Promise(function (resolve, reject) {
            let settled = false;
            const onReady = function () {
                if (settled) return;
                settled = true;
                audio.removeEventListener('loadedmetadata', onReady);
                audio.removeEventListener('canplay', onReady);
                audio.removeEventListener('error', onErr);
                if (audio.duration && Number.isFinite(audio.duration) && audio.duration > 0) {
                    resolve(true);
                } else {
                    reject(new Error('Recording loaded but duration is zero.'));
                }
            };
            const onErr = function () {
                if (settled) return;
                settled = true;
                audio.removeEventListener('loadedmetadata', onReady);
                audio.removeEventListener('canplay', onReady);
                audio.removeEventListener('error', onErr);
                reject(new Error('Browser could not play this recording.'));
            };
            audio.addEventListener('loadedmetadata', onReady);
            audio.addEventListener('canplay', onReady);
            audio.addEventListener('error', onErr);
            audio.preload = 'auto';
            audio.src = streamUrl;
            audio.style.display = 'block';
            audio.load();
            setTimeout(function () {
                if (!settled && audio.readyState >= 1 && audio.duration > 0) onReady();
            }, 12000);
        });
    }

    try {
        await attachStreamSrc();
        msgEl.textContent = '';
        return;
    } catch (_streamErr) {
        /* fall through to blob fetch */
    }

    try {
        const res = await fetch(apiUrl(`/api/campaign/lead/${media.leadId}/recording?role=${apiRoleQ()}`), {
            headers: { 'Authorization': `Bearer ${token()}` },
            credentials: 'same-origin',
        });
        if (!res.ok) {
            const errText = await res.text().catch(function () { return ''; });
            throw new Error(errText || ('Recording not available (HTTP ' + res.status + ')'));
        }
        let blob = await res.blob();
        if (!blob || !blob.size) throw new Error('Recording file is empty.');
        if (!blob.type || blob.type === 'application/octet-stream') {
            blob = new Blob([blob], { type: 'audio/wav' });
        }
        _cdRecordingBlobUrl = URL.createObjectURL(blob);
        audio.src = _cdRecordingBlobUrl;
        audio.style.display = 'block';
        audio.load();
        msgEl.textContent = '';
    } catch (err) {
        showRecordingError((err && err.message) ? String(err.message) : 'Recording not available for this call.');
    }
}

/** Load recording in call-detail modal (cdm-*) when _log_id exists even if recording_available is false. */
async function loadCdmRecording(lead, audioEl, waveformEl, playBtn, downloadBtn) {
    if (!audioEl) return;
    const media = typeof resolveLeadMediaContext === 'function'
        ? resolveLeadMediaContext(lead)
        : { leadId: lead && lead.id, logId: String((lead && (lead._log_id || lead.log_id)) || ''), hasMedia: !!(lead && (lead.recording_available || lead._log_id || lead.log_id)) };
    if (!media.hasMedia || media.leadId == null) {
        if (waveformEl) {
            waveformEl.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:40px;font-size:11px;color:var(--text-secondary);">No recording available for this call</div>';
        }
        if (playBtn) playBtn.style.display = 'none';
        if (downloadBtn) downloadBtn.style.display = 'none';
        audioEl.removeAttribute('src');
        return;
    }
    if (playBtn) playBtn.style.display = '';
    if (downloadBtn) downloadBtn.style.display = '';
    if (waveformEl) {
        var bars = '';
        for (var i = 0; i < 50; i++) {
            var h = Math.floor(Math.random() * 25) + 5;
            bars += '<div class="bar" style="height:' + h + 'px;background:' + (i < 15 ? 'var(--primary)' : 'var(--border)') + ';"></div>';
        }
        waveformEl.innerHTML = bars;
    }
    window._cdmAudioReady = false;
    if (typeof campaignRecordingStreamUrl === 'function') {
        var streamUrl = campaignRecordingStreamUrl(media.leadId, media.logId);
        if (streamUrl) {
            audioEl.src = streamUrl;
            audioEl.load();
            return;
        }
    }
    try {
        var recUrl = apiUrl('/api/campaign/lead/' + media.leadId + '/recording?role=' + apiRoleQ());
        if (media.logId) recUrl += '&log_id=' + encodeURIComponent(media.logId);
        var res = await fetch(recUrl, {
            headers: { 'Authorization': 'Bearer ' + token() },
            credentials: 'same-origin',
        });
        if (!res.ok) throw new Error('Recording not found');
        var blob = await res.blob();
        if (!blob || !blob.size) throw new Error('Recording empty');
        audioEl.src = URL.createObjectURL(blob);
        audioEl.load();
    } catch (_) {
        if (waveformEl) {
            waveformEl.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:40px;font-size:11px;color:var(--text-secondary);">Recording could not be loaded</div>';
        }
        if (playBtn) playBtn.style.display = 'none';
        if (downloadBtn) downloadBtn.style.display = 'none';
        audioEl.removeAttribute('src');
    }
}

async function loadCallDetailTranscript(lead, transEl) {
    var tOpts = { agentName: getAgentNameForRole(lead.role), callerName: lead.name || 'Caller' };
    // Prefer cached transcript on the lead row, otherwise fetch from API.
    if (typeof lead.transcript === 'string' && lead.transcript.trim()) {
        renderTranscript(lead.transcript, tOpts);
        return;
    }
    
    const legacyUrl = lead.transcript_url;
    const media = typeof resolveLeadMediaContext === 'function'
        ? resolveLeadMediaContext(lead)
        : { leadId: lead && lead.id, hasMedia: !!(lead && (lead.log_id || lead._log_id)) };
    const securedUrl =
        media.leadId != null && media.hasMedia
            ? apiUrl(`/api/campaign/lead/${media.leadId}/transcript?role=${apiRoleQ()}${media.logId ? '&log_id=' + encodeURIComponent(media.logId) : ''}`)
            : null;

    if (!securedUrl && !legacyUrl) {
        transEl.innerHTML = `<p style="font-size:12px;color:var(--text-secondary);text-align:center;margin:20px 0;">No transcript recorded for this call.</p>`;
        return;
    }
    try {
        const url = securedUrl || legacyUrl;
        const res = await fetch(url, {
            headers: { 'Authorization': `Bearer ${token()}` },
            credentials: 'same-origin',
        });
        if (!res.ok) throw new Error('Transcript not available (HTTP ' + res.status + ')');
        const text = await res.text();
        renderTranscript(text, tOpts);
    } catch (err) {
        const msg = (err && err.message) ? String(err.message) : 'Transcript not available.';
        transEl.innerHTML = `<p style="font-size:12px;color:var(--text-secondary);text-align:center;margin:20px 0;">${escapeHtml(msg)}</p>`;
    }
}

let currentCallLead = null;
async function openCallDetail(leadId) {
    let lead = typeof findLeadById === 'function' ? findLeadById(leadId) : allLeads.find(function (l) { return Number(l.id) === Number(leadId); });
    if (!lead) return;
    if (typeof ensureLeadMediaFromApi === 'function') {
        lead = await ensureLeadMediaFromApi(lead);
    }
    currentCallLead = lead;

    var dispo = (typeof dispositionDisplayLabel === 'function' ? dispositionDisplayLabel(lead) : effectiveDispo(lead) || '').trim();
    var dispoLower = dispo.toLowerCase();
    var dispoClass = 'other';
    var hasSiteVisit = lead.site_visit_agreed === true;
    if (!hasSiteVisit && lead.analysis) {
        try { var aj = typeof lead.analysis === 'string' ? JSON.parse(lead.analysis) : (lead.analysis || {}); hasSiteVisit = aj.site_visit_agreed === true; } catch(e) {}
    }
    var cpRole = typeof isChannelPartnerRole === 'function' && isChannelPartnerRole();
    if (!cpRole && (hasSiteVisit || lead.status === 'site_visit')) dispoClass = 'sitevisit';
    else if (dispoLower.indexOf('interested') >= 0 && dispoLower.indexOf('not') < 0) dispoClass = 'interested';
    else if (dispoLower.indexOf('not interested') >= 0 || dispoLower === 'not_interested' || dispoLower === 'site visited') dispoClass = 'notinterested';
    else if (dispoLower === 'failed' || dispoLower === 'error' || dispoLower === 'no answer') dispoClass = 'failed';
    else if (dispoLower === 'call later' || dispoLower === 'callback' || dispoLower === 'busy') dispoClass = 'callback';

    var badgeLabels = { sitevisit: 'Site Visit', interested: 'Interested', notinterested: 'Site Visited', failed: 'Failed', callback: 'Callback', other: dispo || 'Answered' };

    // Header
    document.getElementById('cdm-title').textContent = lead.name || 'Unknown';
    document.getElementById('cdm-subtitle').textContent = [lead.phone, lead.company, lead.email].filter(Boolean).join(' \u2022 ');

    // Outcome badge
    var extData = {};
    try { extData = typeof lead.extra === 'string' ? JSON.parse(lead.extra) : (lead.extra || {}); } catch(e) {}
    var retriesVal = Number(extData.failed_call_retries) || 0;
    var retryBadgeValHtml = '';
    if (retriesVal === 1) {
        retryBadgeValHtml = ' <span class="badge-tag tag-failed" style="font-weight:bold;color:var(--text-error-red, #ef4444);margin-left:4px;">Retake 1</span>';
    } else if (retriesVal === 2) {
        retryBadgeValHtml = ' <span class="badge-tag tag-failed" style="font-weight:bold;color:var(--text-error-red, #ef4444);margin-left:4px;">Retake 2</span>';
    }
    document.getElementById('cdm-outcome').innerHTML =
        '<span class="cdm-badge cdm-badge-' + dispoClass + '">' + badgeLabels[dispoClass] + '</span>' + retryBadgeValHtml;

    // Rating
    var ratingEl = document.getElementById('cdm-rating');
    if (lead.rating) {
        var stars = '';
        for (var i = 0; i < 5; i++) stars += i < lead.rating ? '\u2605' : '\u2606';
        ratingEl.innerHTML = '<span style="font-size:20px;color:#f59e0b;letter-spacing:2px;">' + stars + '</span>';
    } else {
        ratingEl.textContent = 'No data';
    }

    // Extract data
    var ext = {};
    try { ext = typeof lead.extra === 'string' ? JSON.parse(lead.extra) : (lead.extra || {}); } catch(e) {}
    var summaryText = lead.summary || '';
    if (!summaryText || summaryText === 'No summary generated for this call yet.') {
        try { var a = typeof lead.analysis === 'string' ? JSON.parse(lead.analysis) : (lead.analysis || {}); summaryText = a.summary || a.reason || ''; } catch(e) {}
    }
    if (!summaryText && dispoClass === 'notinterested') {
        if (ext.reason_not_interested) summaryText = ext.reason_not_interested;
        else if (ext.reason) summaryText = ext.reason;
    }
    document.getElementById('cdm-summary').textContent = summaryText || 'Analysis pending';
    try {
        var _ajPending = typeof lead.analysis === 'string' ? JSON.parse(lead.analysis) : (lead.analysis || {});
        if (_ajPending && _ajPending.analysis_pending) {
            var _sumEl = document.getElementById('cdm-summary');
            if (_sumEl && !document.getElementById('cdm-analysis-pending-hint')) {
                var _hint = document.createElement('div');
                _hint.id = 'cdm-analysis-pending-hint';
                _hint.style.cssText = 'font-size:11px;color:var(--text-secondary);margin-top:6px;font-weight:500;';
                _hint.textContent = 'Generating full analysis\u2026';
                _sumEl.parentNode.appendChild(_hint);
            }
        } else {
            var _oldHint = document.getElementById('cdm-analysis-pending-hint');
            if (_oldHint) _oldHint.remove();
        }
    } catch (_pe) {}

    // Emotion — API sends emotion_label as top-level field (analysis blob is stripped)
    var emotionText = 'Unknown';
    var emotionRationale = '';
    var emotionConfidence = null;
    if (lead.emotion_label) {
        emotionText = lead.emotion_label;
        emotionRationale = lead.emotion_rationale || '';
        emotionConfidence = lead.emotion_confidence;
    } else {
        try {
            var ea = typeof lead.analysis === 'string' ? JSON.parse(lead.analysis) : (lead.analysis || {});
            if (ea.emotion_label) emotionText = ea.emotion_label;
            else if (ea.emotion) emotionText = ea.emotion;
            emotionRationale = ea.emotion_rationale || '';
            emotionConfidence = ea.emotion_confidence;
        } catch(e) {}
    }
    var emotionEl = document.getElementById('cdm-emotion');
    var emotionHtml = '<span style="font-weight:700;">' + escapeHtml(emotionText) + '</span>';
    if (emotionRationale) {
        emotionHtml += '<div style="font-size:12px;color:var(--text-secondary);margin-top:4px;line-height:1.4;">' + escapeHtml(emotionRationale) + '</div>';
    }
    if (emotionConfidence !== null && emotionConfidence !== undefined) {
        var confPct = Math.round(emotionConfidence * 100);
        var confColor = confPct >= 70 ? '#16a34a' : confPct >= 40 ? '#ca8a04' : '#dc2626';
        emotionHtml += '<div style="font-size:11px;color:' + confColor + ';margin-top:3px;font-weight:600;">Confidence: ' + confPct + '%</div>';
    }
    emotionEl.innerHTML = emotionHtml;

    // Next steps
    var nextText = lead.next_steps || '';
    if (!nextText && dispoClass === 'interested') nextText = 'Follow up with project details and brochure.';
    else if (!nextText && dispoClass === 'notinterested') nextText = 'Lead not interested. No immediate follow-up required.';
    else if (!nextText) nextText = 'No next steps recorded.';
    document.getElementById('cdm-nextsteps').textContent = nextText;

    // Suggested Action (from next_action analysis)
    var saContainer = document.getElementById('cdm-suggested-action-container');
    var saBadge = document.getElementById('cdm-suggested-action-badge');
    var saDetails = document.getElementById('cdm-suggested-action-details');
    var saActionBtns = document.getElementById('cdm-sa-action-buttons');
    var reschedBtnContainer = document.getElementById('cdm-sa-reschedule-btn');
    var nextAction = lead.next_action || (lead.analysis && lead.analysis.next_action);
    if (nextAction && nextAction.action_type && nextAction.action_type !== 'None') {
        if (saContainer) saContainer.style.display = '';
        var type = nextAction.action_type;
        var badgeHtml = '';
        var actionBtnHtml = '';
        var phone = (lead.phone || '').replace(/[^0-9+]/g, '');
        var details = nextAction.details || '';
        if (type === 'WhatsApp') {
            badgeHtml = '<span style="display:inline-flex;align-items:center;padding:4px 10px;background:rgba(37,211,102,0.12);color:#075e54;border:1px solid rgba(37,211,102,0.25);border-radius:20px;font-size:11px;font-weight:700;letter-spacing:0.05em;text-transform:uppercase;"><svg style="margin-right:4px;" width="14" height="14" viewBox="0 0 24 24" fill="#25D366"><path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413z"/></svg> WhatsApp</span>';
            var waMsg = encodeURIComponent(details || 'Hi, following up on our conversation.');
            var waPhone = phone.replace('+', '');
            actionBtnHtml = '<a href="https://wa.me/' + waPhone + '?text=' + waMsg + '" target="_blank" onclick="markLeadWhatsAppSent(' + lead.id + ')" style="display:inline-flex;align-items:center;gap:6px;padding:8px 16px;background:#25D366;color:#fff;border:none;border-radius:10px;cursor:pointer;font-size:13px;font-weight:700;text-decoration:none;margin-top:8px;"><svg width="16" height="16" viewBox="0 0 24 24" fill="#fff"><path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413z"/></svg> Send on WhatsApp</a>';
        } else if (type === 'Email') {
            badgeHtml = '<span style="display:inline-flex;align-items:center;padding:4px 10px;background:rgba(59,130,246,0.1);color:#1d4ed8;border:1px solid rgba(59,130,246,0.2);border-radius:20px;font-size:11px;font-weight:700;letter-spacing:0.05em;text-transform:uppercase;"><svg style="margin-right:4px;" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#1d4ed8" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="16" rx="3"/><path d="M22 4L12 13 2 4"/></svg> Email</span>';
            var emailTo = lead.email || '';
            var emailSubject = encodeURIComponent('Follow up: ' + (details || 'Property Information'));
            var emailBody = encodeURIComponent(details || 'Hi, following up on our conversation.');
            var mailtoUrl = 'mailto:' + emailTo + '?subject=' + emailSubject + '&body=' + emailBody;
            actionBtnHtml = '<a href="' + mailtoUrl + '" style="display:inline-flex;align-items:center;gap:6px;padding:8px 16px;background:#1d4ed8;color:#fff;border:none;border-radius:10px;cursor:pointer;font-size:13px;font-weight:700;text-decoration:none;margin-top:8px;"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="16" rx="3"/><path d="M22 4L12 13 2 4"/></svg> Send Email</a>';
        } else if (type === 'Call Again') {
            var cbDtText = formatActionDateTime(nextAction.datetime_iso);
            badgeHtml = '<span style="display:inline-flex;align-items:center;padding:4px 10px;background:rgba(249,115,22,0.1);color:#c2410c;border:1px solid rgba(249,115,22,0.2);border-radius:20px;font-size:11px;font-weight:700;letter-spacing:0.05em;text-transform:uppercase;"><svg style="margin-right:4px;" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#c2410c" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z"></path></svg> Call Again' + (cbDtText ? '  ·  ' + cbDtText : '') + '</span>';
            actionBtnHtml = '<button onclick="dialLeadViaAI(' + lead.id + ', \'' + phone + '\', \'' + escapeHtml(lead.name || '') + '\')" class="btn btn-primary" style="display:inline-flex;align-items:center;gap:6px;padding:8px 16px;background:#c2410c;color:#fff;border:none;border-radius:10px;cursor:pointer;font-size:13px;font-weight:700;margin-top:8px;"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z"></path></svg> Call Now (via AI)</button>';
        } else if (type === 'Reschedule') {
            var rsDtText = formatActionDateTime(nextAction.datetime_iso);
            badgeHtml = '<span style="display:inline-flex;align-items:center;padding:4px 10px;background:rgba(139,92,246,0.1);color:#7c3aed;border:1px solid rgba(139,92,246,0.2);border-radius:20px;font-size:11px;font-weight:700;letter-spacing:0.05em;text-transform:uppercase;"><svg style="margin-right:4px;" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#7c3aed" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"></rect><line x1="16" y1="2" x2="16" y2="6"></line><line x1="8" y1="2" x2="8" y2="6"></line><line x1="3" y1="10" x2="21" y2="10"></line></svg> Reschedule' + (rsDtText ? '  ·  ' + rsDtText : '') + '</span>';
            actionBtnHtml = '<button onclick="openLeadReschedule(' + lead.id + ')" style="display:inline-flex;align-items:center;gap:6px;padding:8px 16px;background:rgba(139,92,246,0.12);color:#7c3aed;border:1px solid rgba(139,92,246,0.25);border-radius:10px;cursor:pointer;font-size:13px;font-weight:700;margin-top:8px;"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg> Reschedule Now</button>';
        } else if (type === 'Virtual Meet') {
            var meetDt = formatActionDateTime(nextAction.datetime_iso);
            badgeHtml = '<span style="display:inline-flex;align-items:center;padding:4px 10px;background:rgba(139,92,246,0.1);color:#7c3aed;border:1px solid rgba(139,92,246,0.2);border-radius:20px;font-size:11px;font-weight:700;letter-spacing:0.05em;text-transform:uppercase;"><svg style="margin-right:4px;" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#7c3aed" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="23 7 16 12 23 17 23 7"></polygon><rect x="1" y="5" width="15" height="14" rx="2" ry="2"></rect></svg> Virtual Meet' + (meetDt ? '  ·  ' + meetDt : '') + '</span>';
            var meetDetails = details || 'Virtual walkthrough scheduled';
            var vmRescheduleBtn = '<button onclick="openVirtualMeetReschedule(' + lead.id + ')" style="display:inline-flex;align-items:center;gap:6px;padding:8px 16px;background:rgba(139,92,246,0.12);color:#7c3aed;border:1px solid rgba(139,92,246,0.25);border-radius:10px;cursor:pointer;font-size:13px;font-weight:700;margin-top:8px;"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg> Reschedule Virtual Meet</button>';
            actionBtnHtml = '<div style="display:flex;flex-direction:column;gap:4px;margin-top:8px;"><span style="display:inline-flex;align-items:center;gap:6px;padding:8px 16px;background:rgba(139,92,246,0.08);color:#7c3aed;border:1px solid rgba(139,92,246,0.15);border-radius:10px;font-size:13px;font-weight:600;">' + escapeHtml(meetDetails) + '</span>' + vmRescheduleBtn + '</div>';
        } else {
            badgeHtml = '<span style="display:inline-flex;align-items:center;padding:4px 10px;background:rgba(100,116,139,0.1);color:#475569;border:1px solid rgba(100,116,139,0.2);border-radius:20px;font-size:11px;font-weight:700;letter-spacing:0.05em;text-transform:uppercase;">' + escapeHtml(type) + '</span>';
        }
        if (saBadge) saBadge.innerHTML = badgeHtml;
        if (saDetails) saDetails.textContent = details;
        if (saActionBtns) { saActionBtns.innerHTML = actionBtnHtml; saActionBtns.style.display = actionBtnHtml ? 'block' : 'none'; }
        if (reschedBtnContainer) reschedBtnContainer.style.display = 'none';
    } else {
        if (saContainer) saContainer.style.display = 'none';
        if (saActionBtns) { saActionBtns.innerHTML = ''; saActionBtns.style.display = 'none'; }
        if (reschedBtnContainer) reschedBtnContainer.style.display = 'none';
    }

    // Callback / Follow-Up Card (second suggested action card)
    var cbContainer = document.getElementById('cdm-callback-action-container');
    var cbBadge = document.getElementById('cdm-callback-badge');
    var cbDetails = document.getElementById('cdm-callback-details');
    var cbButtons = document.getElementById('cdm-callback-buttons');
    var cbDt = lead.requested_callback_datetime_iso || (lead.analysis && lead.analysis.requested_callback_datetime_iso);
    if (cbDt && nextAction && nextAction.action_type !== 'Call Again') {
        var cbFormatted = formatActionDateTime(cbDt);
        if (cbContainer) cbContainer.style.display = '';
        if (cbBadge) cbBadge.innerHTML = '<span style="display:inline-flex;align-items:center;padding:4px 10px;background:rgba(249,115,22,0.1);color:#c2410c;border:1px solid rgba(249,115,22,0.2);border-radius:20px;font-size:11px;font-weight:700;letter-spacing:0.05em;text-transform:uppercase;"><svg style="margin-right:4px;" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#c2410c" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z"></path></svg> Call Again' + (cbFormatted ? '  ·  ' + cbFormatted : '') + '</span>';
        if (cbDetails) cbDetails.textContent = 'Customer requested callback';
        var cbPhone = (lead.phone || '').replace(/[^0-9+]/g, '');
        if (cbButtons) { cbButtons.innerHTML = '<button onclick="dialLeadViaAI(' + lead.id + ', \'' + cbPhone + '\', \'' + escapeHtml(lead.name || '') + '\')" class="btn btn-primary" style="display:inline-flex;align-items:center;gap:6px;padding:8px 16px;background:#c2410c;color:#fff;border:none;border-radius:10px;cursor:pointer;font-size:13px;font-weight:700;margin-top:8px;"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z"></path></svg> Call Now (via AI)</button>'; cbButtons.style.display = 'block'; }
    } else {
        if (cbContainer) cbContainer.style.display = 'none';
        if (cbButtons) { cbButtons.innerHTML = ''; cbButtons.style.display = 'none'; }
    }

    // Date, Location, Budget
    var dateEl = document.getElementById('cdm-date');
    var locationEl = document.getElementById('cdm-location');
    var budgetEl = document.getElementById('cdm-budget');
    if (lead.start_time) {
        var d = new Date(lead.start_time * 1000);
        dateEl.textContent = d.toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' }) + ' \u2022 ' + d.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' });
    } else if (lead.called_at_iso) {
        var d2 = new Date(lead.called_at_iso);
        dateEl.textContent = d2.toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' }) + ' \u2022 ' + d2.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' });
    } else {
        dateEl.textContent = '—';
    }
    locationEl.textContent = lead.preferred_location || ext.location || '—';
    budgetEl.textContent = lead.preferred_budget || ext.budget || '—';

    // Generate waveform bars
    var waveformEl = document.getElementById('cdm-waveform');
    var bars = '';
    for (var i = 0; i < 50; i++) {
        var h = Math.floor(Math.random() * 25) + 5;
        var barColor = i < 15 ? 'var(--primary)' : 'var(--border)';
        bars += '<div class="bar" style="height:' + h + 'px;background:' + barColor + ';"></div>';
    }
    waveformEl.innerHTML = bars;

    // Reset play button
    var playBtn = document.getElementById('cdm-play-btn');
    playBtn.innerHTML = '<span class="material-symbols-rounded" style="font-size:22px;">play_arrow</span>';
    window._cdmAudioPlaying = false;

    // Show modal
    document.getElementById('modal-call-detail').classList.add('active');

    // Load recording — use _log_id when present, not only recording_available flag
    var audioEl = document.getElementById('cdm-audio');
    var downloadBtn = document.getElementById('cdm-download-btn');
    await loadCdmRecording(lead, audioEl, waveformEl, playBtn, downloadBtn);
    var totalBars = waveformEl ? waveformEl.children.length : 0;
    audioEl.onloadedmetadata = function() {
        var d = audioEl.duration || 0;
        var spans = document.querySelectorAll('#modal-call-detail .cdm-rec-time');
        if (spans.length >= 2) {
            spans[0].textContent = '0:00';
            var m = Math.floor(d / 60);
            var s = Math.floor(d % 60);
            spans[1].textContent = m + ':' + (s < 10 ? '0' : '') + s;
        }
    };
    audioEl.ontimeupdate = function() {
        if (!audioEl.duration || audioEl.duration === Infinity) return;
        var pct = audioEl.currentTime / audioEl.duration;
        var playedBars = Math.floor(pct * totalBars);
        for (var i = 0; i < totalBars; i++) {
            var bar = waveformEl.children[i];
            if (bar) bar.style.background = i <= playedBars ? 'var(--primary)' : 'var(--border)';
        }
        var spans = document.querySelectorAll('#modal-call-detail .cdm-rec-time');
        if (spans.length >= 1) {
            var cur = audioEl.currentTime;
            var m = Math.floor(cur / 60);
            var s = Math.floor(cur % 60);
            spans[0].textContent = m + ':' + (s < 10 ? '0' : '') + s;
        }
    };
    audioEl.onended = function() {
        window._cdmAudioPlaying = false;
        var btn = document.getElementById('cdm-play-btn');
        if (btn) btn.innerHTML = '<span class="material-symbols-rounded" style="font-size:22px;">play_arrow</span>';
        for (var i = 0; i < totalBars; i++) {
            var bar = waveformEl.children[i];
            if (bar) bar.style.background = i < 15 ? 'var(--primary)' : 'var(--border)';
        }
        var spans = document.querySelectorAll('#modal-call-detail .cdm-rec-time');
        if (spans.length >= 1) spans[0].textContent = '0:00';
    };
    audioEl.onerror = function() {
        window._cdmAudioPlaying = false;
        var btn = document.getElementById('cdm-play-btn');
        if (btn) btn.style.display = 'none';
        var downloadBtn = document.getElementById('cdm-download-btn');
        if (downloadBtn) downloadBtn.style.display = 'none';
        waveformEl.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:40px;font-size:11px;color:var(--text-secondary);">Recording could not be loaded</div>';
        var spans = document.querySelectorAll('#modal-call-detail .cdm-rec-time');
        if (spans.length >= 1) spans[0].textContent = '—';
        if (spans.length >= 2) spans[1].textContent = '—';
        audioEl.removeAttribute('src');
    };

    // Load transcript
    var transEl = document.getElementById('cdm-transcript');
    await loadCallDetailTranscript(lead, transEl);

    // Load call history (retakes)
    loadCallHistory(lead.id);
    loadFollowUpLifecycle(lead);
}

async function loadInlineTranscript(leadId, logId, containerId) {
    var el = document.getElementById(containerId);
    if (!el || !logId) return;
    el.innerHTML = '<div style="font-size:11px;color:var(--text-tertiary);padding:8px;">Loading transcript…</div>';
    try {
        var url = apiUrl('/api/campaign/lead/' + leadId + '/transcript?role=' + apiRoleQ() + '&log_id=' + encodeURIComponent(logId));
        var res = await fetch(url, { headers: authHeaders(), credentials: 'same-origin' });
        if (!res.ok) { el.innerHTML = '<div style="font-size:11px;color:var(--text-tertiary);padding:8px;">Transcript unavailable</div>'; return; }
        var text = await res.text();
        el.innerHTML = '<pre style="font-size:11px;white-space:pre-wrap;margin:0;padding:8px;max-height:200px;overflow:auto;background:var(--card-hover);border-radius:6px;">' + escapeHtml(text.slice(0, 8000)) + '</pre>';
    } catch (e) {
        el.innerHTML = '<div style="font-size:11px;color:var(--text-tertiary);padding:8px;">Transcript load failed</div>';
    }
}

function loadFollowUpLifecycle(lead) {
    var container = document.getElementById('cdm-follow-up-lifecycle');
    if (!container) return;
    var plan = lead.follow_up_plan || [];
    if (!plan.length && !lead.lifecycle_stage) {
        container.style.display = 'none';
        return;
    }
    container.style.display = '';
    var stage = lead.lifecycle_stage || '';
    var visitDt = lead.site_visit_datetime_iso || '';
    var pending = lead.follow_up_pending_label || '';
    var header = '<div style="padding:12px 18px;border-bottom:1px solid var(--border);font-weight:700;font-size:13px;">Site Visit Lifecycle</div>';
    var sub = '';
    if (visitDt) sub += '<div style="font-size:12px;color:var(--text-secondary);">Visit: ' + escapeHtml(String(visitDt).slice(0, 40)) + '</div>';
    if (pending) sub += '<div style="font-size:11px;color:var(--primary);margin-top:4px;">' + escapeHtml(pending) + '</div>';
    if (lead.site_visit_headcount) sub += '<div style="font-size:11px;color:var(--text-tertiary);margin-top:2px;">Headcount: ' + escapeHtml(String(lead.site_visit_headcount)) + '</div>';
    if (lead.site_visit_arrival_time_iso) sub += '<div style="font-size:11px;color:var(--text-tertiary);margin-top:2px;">Arrival: ' + escapeHtml(String(lead.site_visit_arrival_time_iso).slice(0, 30)) + '</div>';
    var rows = plan.map(function(entry, idx) {
        if (!entry || typeof entry !== 'object') return '';
        var st = entry.status || 'scheduled';
        var icon = st === 'completed' ? 'check_circle' : 'schedule';
        var label = entry.label || ('Follow-up ' + (entry.follow_up_number || (idx + 1)));
        return '<div style="display:flex;gap:10px;padding:10px 18px;border-bottom:1px solid var(--border);">' +
            '<span class="material-symbols-rounded" style="font-size:16px;color:var(--text-tertiary);">' + icon + '</span>' +
            '<div><div style="font-size:12px;font-weight:600;">' + escapeHtml(label) + '</div>' +
            '<div style="font-size:10px;color:var(--text-tertiary);">' + escapeHtml(st) + '</div></div></div>';
    }).join('');
    container.innerHTML = header + (sub ? '<div style="padding:10px 18px;border-bottom:1px solid var(--border);">' + sub + '</div>' : '') + rows;
}

function toggleCallHistory(headerEl) {
    var body = document.getElementById('cdm-ch-body');
    var chevron = headerEl.querySelector('.ch-chevron');
    if (!body) return;
    var isHidden = body.style.display === 'none';
    body.style.display = isHidden ? 'block' : 'none';
    if (chevron) chevron.style.transform = isHidden ? 'rotate(0deg)' : 'rotate(180deg)';
}

async function loadCallHistory(leadId) {
    var container = document.getElementById('cdm-call-history');
    var body = document.getElementById('cdm-ch-body');
    var countEl = document.getElementById('cdm-ch-count');
    if (!container || !body) return;
    try {
        var res = await fetch(apiUrl('/api/campaign/lead/' + leadId + '/attempts?role=' + apiRoleQ()), {
            headers: authHeaders(),
            credentials: 'same-origin',
        });
        if (!res.ok) { container.style.display = 'none'; return; }
        var data = await res.json();
        var attempts = data.attempts || [];
        if (!attempts.length) { container.style.display = 'none'; return; }
        container.style.display = '';
        if (countEl) countEl.textContent = '(' + attempts.length + ' attempt' + (attempts.length > 1 ? 's' : '') + ')';
        body.innerHTML = attempts.map(function(a, idx) {
            var num = a.attempt_number || (idx + 1);
            var cat = a.call_category || 'initial';
            var fuNum = a.follow_up_number;
            var attemptLabel = cat === 'follow_up' && fuNum ? ('Follow-up #' + fuNum) : ('Attempt #' + num);
            if (a.is_best) attemptLabel += ' · Best';
            var status = a.status || 'completed';
            var disposition = attemptDispositionLabel(a.disposition || '', status);
            var summary = a.summary || '';
            var rating = a.rating;
            var logId = a.log_id || '';
            var attemptKey = 'attempt-' + leadId + '-' + (a.id || idx);
            var createdAt = a.created_at ? formatTimeIST(a.created_at) : '—';
            var durSec = a.duration_sec;
            var durStr = durSec ? Math.floor(durSec / 60) + 'm ' + Math.floor(durSec % 60) + 's' : '—';

            var dispBadge = '';
            if (disposition) {
                var cls = dispoTagClass(disposition);
                dispBadge = '<span class="badge-tag ' + cls + '" style="font-size:10px;padding:2px 8px;">' + escapeHtml(disposition) + '</span>';
            }

            var starsHtml = '';
            if (rating != null && rating > 0) {
                var f = Math.round(rating);
                starsHtml = '<span style="margin-left:6px;color:var(--text-secondary);font-size:11px;">' + '★'.repeat(f) + '☆'.repeat(Math.max(0, 5 - f)) + '</span>';
            }

            var mediaHtml = '';
            if (logId && (a.recording_available || a.recording_url)) {
                var streamUrl = typeof campaignRecordingStreamUrl === 'function'
                    ? campaignRecordingStreamUrl(leadId, logId)
                    : (a.recording_url || '');
                mediaHtml += '<audio controls preload="none" style="width:100%;height:32px;margin-top:6px;" src="' + escapeHtml(streamUrl) + '"></audio>';
                mediaHtml += '<div id="tx-' + attemptKey + '" style="margin-top:6px;"></div>';
                mediaHtml += '<button type="button" onclick="loadInlineTranscript(' + leadId + ',\'' + String(logId).replace(/'/g, '') + '\',\'tx-' + attemptKey + '\')" style="background:none;border:1px solid var(--border);border-radius:6px;padding:2px 8px;font-size:10px;cursor:pointer;color:var(--primary);margin-top:4px;"><span class="material-symbols-rounded" style="font-size:12px;vertical-align:middle;">description</span> Show transcript</button>';
            }

            var statusIcon = status === 'completed' ? 'check_circle' :
                status === 'failed' ? 'error' : 'call';

            return '<div style="display:flex;gap:12px;padding:12px 18px;border-bottom:' + (idx < attempts.length - 1 ? '1px solid var(--border)' : 'none') + ';' + (a.is_best ? 'background:var(--card-hover);' : '') + '">' +
                '<div style="width:32px;height:32px;border-radius:50%;background:var(--card-hover);display:flex;align-items:center;justify-content:center;flex-shrink:0;border:1px solid var(--border);">' +
                    '<span class="material-symbols-rounded" style="font-size:16px;color:var(--text-tertiary);">' + statusIcon + '</span>' +
                '</div>' +
                '<div style="flex:1;min-width:0;">' +
                    '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">' +
                        '<span style="font-weight:700;font-size:13px;">' + escapeHtml(attemptLabel) + '</span>' +
                        dispBadge +
                        starsHtml +
                    '</div>' +
                    '<div style="font-size:11px;color:var(--text-tertiary);margin-top:3px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;">' +
                        '<span><span class="material-symbols-rounded" style="font-size:11px;vertical-align:middle;">schedule</span> ' + createdAt + '</span>' +
                        '<span><span class="material-symbols-rounded" style="font-size:11px;vertical-align:middle;">timer</span> ' + durStr + '</span>' +
                    '</div>' +
                    (summary ? '<div style="font-size:12px;color:var(--text-secondary);margin-top:4px;line-height:1.4;">' + escapeHtml(summary) + '</div>' : '') +
                    mediaHtml +
                '</div>' +
            '</div>';
        }).join('');
    } catch(e) {
        console.warn('Call history load failed', e);
        container.style.display = 'none';
    }
}

function downloadCallRecording(leadId) {
    if (leadId == null) {
        if (typeof showToast === 'function') showToast('No recording available for this lead.', 'error');
        return;
    }
    var url = apiUrl('/api/campaign/lead/' + leadId + '/recording?role=' + apiRoleQ());
    _downloadRecordingBlob(url, 'lead-' + leadId + '-recording.mp3');
}

function downloadManualCallRecording(callId) {
    var url = apiUrl('/api/manual/calls/' + callId + '/recording?role=' + apiRoleQ());
    _downloadRecordingBlob(url, 'manual-call-' + callId + '-recording.mp3');
}

function downloadIncomingCallRecording(callId) {
    var url = apiUrl('/api/incoming/calls/' + callId + '/recording?role=' + apiRoleQ());
    _downloadRecordingBlob(url, 'incoming-call-' + callId + '-recording.mp3');
}

function _downloadRecordingBlob(url, filename) {
    fetch(url, {
        headers: { 'Authorization': 'Bearer ' + token() },
        credentials: 'same-origin',
    }).then(function (res) {
        if (!res.ok) throw new Error('Recording not available (HTTP ' + res.status + ')');
        return res.blob();
    }).then(function (blob) {
        if (!blob || !blob.size) throw new Error('Recording file is empty.');
        var ext = filename.endsWith('.wav') ? '.wav' : '.mp3';
        if (blob.type && blob.type.includes('mpeg')) ext = '.mp3';
        var finalName = filename.replace(/\.\w+$/, '') + ext;
        var blobUrl = URL.createObjectURL(blob);
        var a = document.createElement('a');
        a.href = blobUrl;
        a.download = finalName;
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(blobUrl);
        if (typeof showToast === 'function') showToast('Downloaded ' + finalName, 'success');
    }).catch(function (err) {
        if (typeof showToast === 'function') showToast((err && err.message) || 'Failed to download recording.', 'error');
    });
}

function closeCallDetailModal() {
    var audio = document.getElementById('cdm-audio');
    if (audio) { audio.pause(); audio.removeAttribute('src'); audio.load(); }
    window._cdmAudioPlaying = false;
    var wf = document.getElementById('cdm-waveform');
    if (wf) {
        wf.innerHTML = '';
        for (var i = 0; i < 50; i++) {
            var h = Math.floor(Math.random() * 25) + 5;
            var d = document.createElement('div');
            d.className = 'bar';
            d.style.cssText = 'height:' + h + 'px;background:' + (i < 15 ? 'var(--primary)' : 'var(--border)') + ';';
            wf.appendChild(d);
        }
    }
    var playBtn = document.getElementById('cdm-play-btn');
    if (playBtn) playBtn.style.display = '';
    var downloadBtn = document.getElementById('cdm-download-btn');
    if (downloadBtn) downloadBtn.style.display = '';
    var spans = document.querySelectorAll('#modal-call-detail .cdm-rec-time');
    if (spans.length >= 1) spans[0].textContent = '0:00';
    if (spans.length >= 2) spans[1].textContent = '0:00';
    document.getElementById('modal-call-detail').classList.remove('active');
}

function toggleCdmPlay() {
    var audio = document.getElementById('cdm-audio');
    if (!audio || !audio.src) return;
    var btn = document.getElementById('cdm-play-btn');
    if (!window._cdmAudioPlaying) {
        audio.play().catch(function(e) { console.warn('Audio play failed', e); });
        window._cdmAudioPlaying = true;
        btn.innerHTML = '<span class="material-symbols-rounded" style="font-size:22px;">pause</span>';
    } else {
        audio.pause();
        window._cdmAudioPlaying = false;
        btn.innerHTML = '<span class="material-symbols-rounded" style="font-size:22px;">play_arrow</span>';
    }
}

function renderTranscript(rawText, opts) {
    const transEl = document.getElementById('cdm-transcript');
    renderTranscriptTo(transEl, rawText, opts);
}

function getAgentNameForRole(role) {
    var r = String(role || '').toLowerCase();
    if (r === 'sales_1') return 'Vernika';
    return 'AI Assistant';
}

function renderTranscriptTo(transEl, rawText, opts) {
    const agentName = (opts && opts.agentName) || 'AI Assistant';
    const callerName = (opts && opts.callerName) || 'Caller';
    const lines = String(rawText || '').split('\n').filter(l => l.trim());
    const turns = [];
    let t0Ms = null;
    for (const line of lines) {
        try {
            const obj = JSON.parse(line);
            const role = obj.role || obj.type || '';
            const content = obj.content || obj.text || obj.message || '';
            const note = obj.note || '';
            const synthetic = obj.synthetic === '1' || obj.synthetic === 1 || obj.synthetic === true;
            if ((role === 'user' || role === 'assistant') && content) {
                let tsMs = null;
                if (obj.ts) {
                    const parsed = Date.parse(obj.ts);
                    if (!isNaN(parsed)) tsMs = parsed;
                }
                if (tsMs != null && t0Ms == null) t0Ms = tsMs;
                turns.push({ role, content: String(content).trim(), tsMs, note, synthetic });
            }
        } catch {}
    }
    if (!turns.length) {
        const plain = lines.map(function (l) { return l.trim(); }).filter(Boolean);
        if (plain.length) {
            var parsedTurns = [];
            for (var p of plain) {
                var m = p.match(/^(Assistant|User|assistant|user)\s*[:]\s*(.+)/);
                if (m) {
                    parsedTurns.push({ role: m[1].toLowerCase() === 'assistant' ? 'assistant' : 'user', content: m[2].trim() });
                }
            }
            if (parsedTurns.length) {
                turns.push(...parsedTurns);
            } else {
                transEl.innerHTML = '<pre style="margin:0;font-size:12px;line-height:1.45;white-space:pre-wrap;word-break:break-word;color:var(--text);">' +
                    escapeHtml(plain.join('\n')) + '</pre>';
                return;
            }
        } else {
            transEl.innerHTML = '<p style="font-size:13px;color:var(--text-secondary);text-align:center;margin:30px 0;">Transcript is empty.</p>';
            return;
        }
    }
    transEl.innerHTML = turns.map((t, i) => {
        var isAgent = t.role === 'assistant';
        var roleLabel = isAgent ? agentName : callerName;
        var seconds = 0;
        if (t.tsMs != null && t0Ms != null) {
            seconds = Math.max(0, Math.floor((t.tsMs - t0Ms) / 1000));
        } else {
            seconds = Math.floor(i * 8);
        }
        var mins = Math.floor(seconds / 60);
        var secs = seconds % 60;
        var ts = mins + ':' + String(secs).padStart(2, '0');
        var badge = '';
        if (t.note === 'scripted_opening') {
            badge = '<span style="font-size:10px;margin-left:6px;padding:1px 6px;border-radius:4px;background:var(--border);color:var(--text-secondary);">Scripted greeting</span>';
        } else if (t.synthetic) {
            badge = '<span style="font-size:10px;margin-left:6px;padding:1px 6px;border-radius:4px;background:var(--border);color:var(--text-secondary);">Audio only</span>';
        } else {
            badge = '<span style="font-size:10px;margin-left:6px;padding:1px 6px;border-radius:4px;background:var(--border);color:var(--text-secondary);">Live STT</span>';
        }
        return '<div class="cdm-msg ' + (isAgent ? 'cdm-msg-agent' : 'cdm-msg-caller') + '">' +
            '<div class="cdm-msg-meta">' +
            '<span class="cdm-msg-timestamp">' + ts + '</span>' +
            '<span class="cdm-msg-speaker">' + escapeHtml(roleLabel) + badge + '</span>' +
            '</div>' +
            '<div class="cdm-msg-bubble">' + escapeHtml(t.content) + '</div>' +
            '</div>';
    }).join('') +
    '<p style="margin:12px 0 0;font-size:11px;color:var(--text-secondary);">Summary and disposition are AI analysis of this transcript.</p>';
}

async function reanalyzeCall() {
    if (!currentCallLead) return;
    const btn = document.getElementById('cdm-reanalyze-btn');
    if (!btn) return;
    btn.disabled = true;
    const oldHtml = btn.innerHTML;
    btn.innerHTML = '<span class="material-symbols-rounded" style="font-size:13px;animation:spin 1s linear infinite;">autorenew</span> Analyzing...';
    try {
        const res = await fetch(apiUrl(`/api/campaign/lead/${currentCallLead.id}/analyze?role=${apiRoleQ()}`), {
            method: 'POST',
            headers: authHeaders(),
            credentials: 'same-origin',
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            const d = data.detail;
            let msg = 'Analysis failed';
            if (typeof d === 'string') msg = d;
            else if (Array.isArray(d)) {
                msg = d.map(function (x) { return x && x.msg ? x.msg : JSON.stringify(x); }).join('; ') || msg;
            } else if (d) msg = String(d);
            throw new Error(msg);
        }
        if (data.lead && data.lead.id != null) {
            const idx = allLeads.findIndex(l => Number(l.id) === Number(data.lead.id));
            if (idx >= 0) {
                allLeads[idx] = Object.assign({}, allLeads[idx], data.lead);
                currentCallLead = allLeads[idx];
            }
        }
        await syncState();
        openCallDetail(currentCallLead.id);
        btn.innerHTML = '<span class="material-symbols-rounded" style="font-size:13px;">check</span> Done';
        setTimeout(() => { btn.innerHTML = oldHtml; btn.disabled = false; }, 1500);
    } catch (e) {
        alert('Re-analyze failed: ' + (e.message || e));
        btn.innerHTML = oldHtml;
        btn.disabled = false;
    }
}

// ─── Render Lead Manifest ───
const MANIFEST_TABLE_ROW_CAP = 5000;

function renderManifest() {
    const tbody = document.getElementById('manifest-tbody');
    if (!tbody) return;
    const dataset = typeof getLeadDatasetForFilters === 'function' ? getLeadDatasetForFilters() : allLeads;
    const dateFiltered = typeof getDateFilteredLeads === 'function' ? getDateFilteredLeads(dataset) : dataset;
    const countEl = document.getElementById('manifest-count');
    if (countEl) {
        var totalDb = typeof getAuthoritativeLeadTotal === 'function' ? getAuthoritativeLeadTotal() : dataset.length;
        var calledLoaded = dataset.length;
        var calledTotal = typeof getAuthoritativeCalledTotal === 'function' ? getAuthoritativeCalledTotal() : calledLoaded;
        if (window.__vizDateFilterActive) {
            countEl.textContent = dateFiltered.length.toLocaleString() + ' lead' + (dateFiltered.length === 1 ? '' : 's') + ' on selected date(s)';
        } else if (window._lastManifestScope === 'called') {
            var truncNote = window._lastManifestTruncated ? ' (load more for full list)' : '';
            countEl.textContent = calledLoaded.toLocaleString() + ' called loaded'
                + (calledTotal > calledLoaded ? ' of ' + calledTotal.toLocaleString() : '')
                + truncNote;
        } else {
            countEl.textContent = totalDb.toLocaleString() + ' lead' + (totalDb === 1 ? '' : 's');
        }
    }
    if (!dateFiltered.length) {
        tbody.innerHTML = `<tr><td colspan="7" style="text-align:center;padding:40px;color:var(--text-secondary);">No leads for selected date</td></tr>`;
        return;
    }
    const cap = typeof window.__VERN_MANIFEST_CAP === 'number' ? window.__VERN_MANIFEST_CAP : MANIFEST_TABLE_ROW_CAP;
    const slice = dateFiltered
        .filter(function (l) { return l && l.id != null && Number.isFinite(Number(l.id)); })
        .slice()
        .sort(function (a, b) {
            var ta = Number(a.start_time) || 0;
            var tb = Number(b.start_time) || 0;
            if (tb !== ta) return tb - ta;
            return Number(b.id || 0) - Number(a.id || 0);
        })
        .slice(0, cap);
    if (!slice.length && allLeads.length) {
        tbody.innerHTML = `<tr><td colspan="7" style="text-align:center;padding:40px;color:var(--text-secondary);">Leads loaded but missing row IDs — try a hard refresh (Ctrl+Shift+R).</td></tr>`;
        return;
    }
    let rowsHtml = slice.map(function (l) {
        const id = l.id;
        const meta = manifestStatusMeta(l);
        const failureHtml = formatFailureCell(l);
        const pname = escapeHtml(typeof leadContactPrimary === 'function' ? leadContactPrimary(l) : (l.name || '—'));
        const canView = l.status && ['completed','failed','not_interested','interested','callback','callback_scheduled','callback_completed'].includes((l.status||'').toLowerCase());
        const rowCursor = canView ? 'cursor:pointer;' : '';
        const rowClick = canView ? `onclick="openCallDetail(${Number(id)})"` : '';
        const na = l.next_action || null;
        const naType = na && na.action_type ? String(na.action_type).trim().toLowerCase() : '';
        let naHtml = '<span style="color:var(--text-tertiary);font-size:10px;">-</span>';
        if (naType === 'whatsapp') {
            naHtml = '<span class="next-action-tag na-whatsapp">WhatsApp</span>';
        } else if (naType === 'email') {
            naHtml = '<span class="next-action-tag na-email">Email</span>';
        } else if (naType === 'virtual meet' || naType === 'virtual') {
            naHtml = '<span class="next-action-tag na-virtual">Virtual Meet</span>';
        } else if (naType === 'call again' || naType === 'callback') {
            naHtml = '<span class="next-action-tag na-callback">Call Back</span>';
        } else if (naType && naType !== 'none' && naType !== 'other') {
            naHtml = '<span class="next-action-tag na-none">' + escapeHtml(na.action_type) + '</span>';
        }
        let ext = {};
        try { ext = typeof l.extra === 'string' ? JSON.parse(l.extra) : (l.extra || {}); } catch(e) {}
        const retries = Number(ext.failed_call_retries) || 0;
        const maxAtt = Number(l.failed_max_attempts) || 3;
        let retryBadgeHtml = '';
        if (retries > 0) {
            retryBadgeHtml = ' <span class="badge-tag tag-failed" style="font-weight:bold;margin-left:4px;">Retake ' + retries + '/' + (maxAtt - 1) + '</span>';
        }
        if (l.next_retake_at_iso && isFailed(l)) {
            retryBadgeHtml += ' <span class="badge-tag tag-cbk" style="font-size:9px;margin-left:4px;">Next: ' + (typeof formatTimeIST === 'function' ? formatTimeIST(l.next_retake_at_iso) : l.next_retake_at_iso) + '</span>';
        }
        return `<tr ${rowClick} style="${rowCursor}">
            <td style="padding-left:20px;font-weight:600;">${pname}</td>
            <td style="font-family:var(--font-mono);font-size:12px;">${escapeHtml(l.phone || '—')}</td>
            <td style="color:var(--text-secondary);">${escapeHtml(String(l.company || '—'))}</td>
            <td><span class="badge-tag ${escapeHtml(meta.cls)}">${escapeHtml(meta.label)}</span>${retryBadgeHtml}</td>
            <td>${naHtml}</td>
            <td style="max-width:220px;">${failureHtml}</td>
            <td style="text-align:right;padding-right:20px;" onclick="event.stopPropagation();">
                <button type="button" class="btn btn-ghost btn-sm" style="font-size:11px;color:var(--success);border:1px solid var(--success);" onclick="markStatus(${Number(id)},'completed')">Right</button>
                <button type="button" class="btn btn-ghost btn-sm" style="font-size:11px;margin-left:4px;color:var(--danger);border:1px solid var(--danger);" onclick="markStatus(${Number(id)},'not_interested')">Wrong</button>
            </td>
        </tr>`;
    }).join('');
    if (dateFiltered.length > cap) {
        rowsHtml += `<tr><td colspan="7" style="padding:14px;color:var(--text-secondary);font-size:12px;text-align:center;">Showing first <strong>${cap.toLocaleString()}</strong> of <strong>${dateFiltered.length.toLocaleString()}</strong> in this browser view.</td></tr>`;
    }
    const sig = slice.map(l => l.id + ':' + l.status + ':' + l.disposition).join('|') + '|' + slice.length;
    if (tbody.dataset.sig !== sig) {
        const temp = document.createElement('tbody');
        temp.innerHTML = rowsHtml;
        if (tbody.children.length === temp.children.length) {
            const oldChildren = Array.from(tbody.children);
            const newChildren = Array.from(temp.children);
            for (let i = 0; i < oldChildren.length; i++) {
                if (oldChildren[i].outerHTML !== newChildren[i].outerHTML) {
                    oldChildren[i].outerHTML = newChildren[i].outerHTML;
                }
            }
        } else {
            tbody.innerHTML = rowsHtml;
        }
        tbody.dataset.sig = sig;
    }
}

/** Map raw lead.status + disposition to a friendly badge for the manifest. */
function manifestStatusMeta(lead) {
    if (typeof dispositionDisplayMeta === 'function') {
        return dispositionDisplayMeta(lead);
    }
    const raw = (lead.status || '').trim();
    const s = raw.toLowerCase();
    if (s === 'callback_scheduled') {
        const iso = (lead.callback_reminder_at_iso || '').trim();
        const lbl = iso ? ('Callback scheduled · ' + formatTimeIST(iso)) : 'Callback scheduled';
        return { label: lbl, cls: 'tag-cbk' };
    }
    if (s === 'pending' || !s) return { label: 'Pending', cls: '' };
    if (s === 'dialing') return { label: 'Dialing…', cls: 'tag-cbk' };
    const dispo = (typeof dispositionDisplayLabel === 'function' ? dispositionDisplayLabel(lead) : effectiveDispo(lead)).trim();
    if (dispo) return { label: dispo, cls: dispoTagClass(dispo) };
    return { label: raw || 'pending', cls: '' };
}

async function parseApiErrorMessage(res) {
    const raw = await res.text().catch(() => '');
    if (!raw) return `Request failed (${res.status})`;
    try {
        const j = JSON.parse(raw);
        const d = j.detail;
        if (typeof d === 'string') return d;
        if (Array.isArray(d) && d.length && typeof d[0] === 'object' && d[0].msg) {
            return d.map((x) => x.msg || '').filter(Boolean).join(' ') || `Request failed (${res.status})`;
        }
        if (j.message) return String(j.message);
    } catch (_) {}
    return raw.slice(0, 500);
}

// ─── Campaign Controls ───
let _campaignSubmitting = false;
let _stopCampaignSubmitting = false;
let _uploadLeadsSubmitting = false;
let _saveTuningSubmitting = false;

async function startCampaign() {
    if (_campaignSubmitting) return;
    _campaignSubmitting = true;
    const btn = document.getElementById('btn-start');
    if (btn && btn.disabled && btn.getAttribute('title')) {
        showToast(btn.getAttribute('title') || 'Campaign cannot start outside calling hours.', 'error');
        _campaignSubmitting = false;
        return;
    }
    if (btn) btn.disabled = true;
    try {
        if (campaignWorkerActive) {
            showToast('Campaign is already running for this role.', 'info');
            return;
        }
        // Use /start (idempotent) instead of /toggle so a stale "alive" task
        // never silently stops the campaign when the user clicks Start.
        const res = await fetch(apiUrl(`/api/campaign/start?role=${apiRoleQ()}`), {
            method: 'POST',
            headers: authHeaders(),
            credentials: 'same-origin',
        });
        if (!res.ok) {
            showToast(await parseApiErrorMessage(res), 'error');
            return;
        }
        const data = await res.json().catch(() => ({}));
        if ((data.status === 'started' || data.status === 'already_running') && data.active) {
            campaignWorkerActive = true;
            const p = data.pending;
            const prefix = data.status === 'already_running' ? 'Campaign already running' : 'Campaign started';
            showToast(
                typeof p === 'number' ? `${prefix} — ${p} lead(s) in the queue.` : `${prefix}.`,
                'success'
            );
        } else {
            showToast('Campaign could not be started. Try again or check pending leads.', 'error');
        }
    } catch (e) {
        showToast((e && e.message) ? e.message : 'Network error', 'error');
    } finally {
        if (btn) btn.disabled = false;
        await syncState();
        _campaignSubmitting = false;
    }
}
async function stopCampaign() {
    if (_stopCampaignSubmitting) return;
    _stopCampaignSubmitting = true;
    const btn = document.getElementById('btn-stop');
    if (btn) btn.disabled = true;
    try {
        const res = await fetch(apiUrl(`/api/campaign/stop?role=${apiRoleQ()}`), {
            method: 'POST',
            headers: authHeaders(),
            credentials: 'same-origin',
        });
        if (!res.ok) {
            showToast(await parseApiErrorMessage(res), 'error');
            return;
        }
        const data = await res.json().catch(() => ({}));
        campaignWorkerActive = false;
        if (data.status === 'stopped' || data.active === false) {
            showToast('Campaign stopped.', 'success');
        } else {
            showToast('Stop acknowledged.', 'info');
        }
    } catch (e) {
        showToast((e && e.message) ? e.message : 'Network error', 'error');
    } finally {
        if (btn) btn.disabled = false;
        await syncState();
        _stopCampaignSubmitting = false;
    }
}
async function executeWipe() {
    if (!confirm('This will permanently delete all leads, call data, memory, and messages. Continue?')) return;
    try {
        const res = await fetch(apiUrl(`/api/campaign/wipe?role=${apiRoleQ()}`), {
            method: 'POST',
            headers: authHeaders(),
            credentials: 'same-origin',
        });
        if (!res.ok) throw new Error('Wipe failed: HTTP ' + res.status);
        if (typeof clearSessionSnapshots === 'function') clearSessionSnapshots();
        if (typeof _clearRoleSessionSnapshots === 'function') _clearRoleSessionSnapshots(apiRoleQ());
        if (typeof allLeads !== 'undefined') { allLeads = []; allLeadsFull = []; }
        showToast('All leads cleared.', 'success');
        if (typeof clearCategoryOverviewCache === 'function') clearCategoryOverviewCache();
        const uploadWrap = document.getElementById('upload-sources-list');
        if (uploadWrap) delete uploadWrap.dataset.sig;
        if (typeof syncState === 'function') await syncState();
        if (typeof loadCategoryOverview === 'function') await loadCategoryOverview();
        if (typeof loadUploadSources === 'function') await loadUploadSources();
        setTimeout(() => window.location.reload(), 800);
    } catch (e) {
        showToast(String(e.message || e), 'error');
    }
}
async function markStatus(idx, status) {
    const leadId = Number(idx);
    if (!Number.isFinite(leadId)) return;
    function patchLead(arr) {
        if (!Array.isArray(arr)) return;
        const lead = arr.find(l => Number(l.id) === leadId);
        if (lead) {
            lead.status = status;
            if (status === 'completed') lead.disposition = 'Interested';
            else if (status === 'not_interested') lead.disposition = 'Not Interested';
        }
    }
    patchLead(allLeads);
    patchLead(allLeadsFull);
    renderManifest();
    try {
        const res = await fetch(apiUrl(`/api/campaign/lead/${leadId}/status?role=${apiRoleQ()}`), {
            method: 'POST',
            headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
            credentials: 'same-origin',
            body: JSON.stringify({ status }),
        });
        if (res.status === 401 && typeof logout === 'function') { logout(); return; }
        if (!res.ok) {
            showToast(await parseApiErrorMessage(res), 'error');
            if (typeof syncState === 'function') syncState(true);
            return;
        }
        showToast(status === 'completed' ? 'Marked Right (interested).' : 'Marked Wrong (not interested).', 'success');
        if (typeof syncState === 'function') syncState(true);
    } catch (e) {
        showToast(String(e.message || e), 'error');
    }
}

async function dialLeadViaAI(leadId, phone, name) {
    const confirmCall = confirm(`Start immediate AI call to ${name || 'Unknown'} (${phone})?`);
    if (!confirmCall) return;

    showToast('Initiating AI Call...', 'info');

    try {
        const lead = allLeads.find(l => String(l.id) === String(leadId));
        if (lead) {
            lead.status = 'pending';
            lead.disposition = 'dialing';
        }
        renderManifest();
        
        await fetch(apiUrl(`/api/campaign/lead/${leadId}/status?role=${apiRoleQ()}`), {
            method: 'POST',
            headers: authHeaders(),
            credentials: 'same-origin',
            body: JSON.stringify({ status: 'pending' })
        });
        
        const res = await fetch(apiUrl(`/api/manual/call?role=${apiRoleQ()}`), {
            method: 'POST',
            headers: authHeaders(),
            credentials: 'same-origin',
            body: JSON.stringify({ to: phone, callee_name: name })
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            throw new Error(data.detail || data.message || 'Call failed to initiate');
        }
        showToast('AI call queued successfully. An outbound call has been placed.', 'success');
        
        const modal = document.getElementById('modal-call-detail');
        if (modal) modal.classList.remove('active');
        
        if (typeof syncCampaignState === 'function') syncCampaignState();
    } catch(err) {
        console.error(err);
        showToast(err.message || 'Failed to initiate AI call', 'error');
    }
}
async function markLeadWhatsAppSent(idx) {
    try {
        await fetch(apiUrl('/api/campaign/lead/' + idx + '/whatsapp-sent?role=' + apiRoleQ()), {
            method: 'POST',
            headers: authHeaders(),
            credentials: 'same-origin'
        });
    } catch (_) {}
}
async function uploadLeads(input) {
    if (_uploadLeadsSubmitting) return;
    _uploadLeadsSubmitting = true;
    const file = input.files && input.files[0];
    if (!file) {
        _uploadLeadsSubmitting = false;
        return;
    }
    var roleName = typeof roleFriendlyName === 'function' ? roleFriendlyName(apiRoleQ()) : apiRoleQ();
    showToast(`Uploading "${file.name}" for ${roleName}…`, 'info');
    const fd = new FormData(); fd.append('file', file);
    try {
        const res = await fetch(apiUrl(`/api/campaign/upload?role=${apiRoleQ()}`), {
            method: 'POST',
            headers: { Authorization: `Bearer ${token()}` },
            credentials: 'same-origin',
            body: fd,
        });
        if (res.status === 401 && typeof logout === 'function') logout();
        if (!res.ok) {
            showToast(await parseApiErrorMessage(res), 'error');
            return;
        }
        const data = await res.json().catch(() => ({}));
        const count = Number(data.count || 0);
        const cleaning = data.cleaning || {};
        const skipped = Number(data.skipped_duplicates || cleaning.file_duplicates + cleaning.db_duplicates || 0);
        const invalid = Number(cleaning.invalid_phones || 0);
        const dncBlocked = Number(cleaning.dnc_blocked || 0);
        const totalRows = Number(cleaning.total_rows || 0);
        if (Array.isArray(data.recent) && data.recent.length > 0) {
            allLeads = data.recent;
            renderManifest();
            renderCalls();
            if (typeof persistLeadTablesToSession === 'function') persistLeadTablesToSession();
        }
        const pv = document.getElementById('upload-preview-summary');
        if (pv) {
            if (count > 0 || totalRows > 0) {
                pv.style.display = 'block';
                let summary = count > 0
                    ? `${count.toLocaleString()} lead${count === 1 ? '' : 's'} saved after cleaning.`
                    : 'No new leads saved after cleaning.';
                if (skipped > 0) summary += ` ${skipped.toLocaleString()} duplicate${skipped === 1 ? '' : 's'} removed.`;
                if (invalid > 0) summary += ` ${invalid.toLocaleString()} invalid phone${invalid === 1 ? '' : 's'} skipped.`;
                if (dncBlocked > 0) summary += ` ${dncBlocked.toLocaleString()} DNC blocked.`;
                pv.textContent = summary + ' Dashboard updating…';
            } else {
                pv.style.display = 'none';
                pv.textContent = '';
            }
        }

        function buildCleaningToast() {
            if (count > 0) {
                let msg = `${count.toLocaleString()} lead${count === 1 ? '' : 's'} saved to ${roleName} after cleaning.`;
                if (skipped > 0) msg += ` (${skipped.toLocaleString()} duplicate${skipped === 1 ? '' : 's'} removed)`;
                if (invalid > 0) msg += ` (${invalid.toLocaleString()} invalid skipped)`;
                if (dncBlocked > 0) msg += ` (${dncBlocked.toLocaleString()} DNC blocked)`;
                return msg;
            }
            if (skipped > 0) {
                return `All ${skipped.toLocaleString()} row${skipped === 1 ? '' : 's'} were duplicates — nothing new added.`;
            }
            if (invalid > 0 && totalRows > 0) {
                return `No valid leads found — ${invalid.toLocaleString()} invalid phone${invalid === 1 ? '' : 's'} in file.`;
            }
            return '';
        }

        const cleaningMsg = buildCleaningToast();
        if (count > 0) {
            showToast(cleaningMsg, 'success');
            if (data.auto_started) {
                const pending = Number(data.pending);
                const status = data.campaign_status || 'started';
                const startMsg = status === 'already_running'
                    ? 'Campaign already running — new leads added to queue.'
                    : (Number.isFinite(pending)
                        ? `Campaign started — ${pending.toLocaleString()} lead${pending === 1 ? '' : 's'} in queue.`
                        : 'Campaign started automatically.');
                showToast(startMsg, 'success');
                campaignWorkerActive = true;
            } else if (data.campaign_status === 'blocked') {
                const blockedDetail = data.detail || 'Outside calling hours (9:30 AM – 7:30 PM IST). Leads are saved — click Start Campaign tomorrow morning.';
                showToast(blockedDetail, 'warning', 8000);
                if (typeof startCampaign === 'function') await startCampaign();
            } else if (data.campaign_status === 'error') {
                showToast(data.detail || 'Leads saved but campaign could not start.', 'error');
            } else {
                showToast('Leads saved — starting campaign…', 'info');
                if (typeof startCampaign === 'function') await startCampaign();
            }
        } else if (cleaningMsg) {
            showToast(cleaningMsg, skipped > 0 ? 'info' : 'error');
        } else if (data.error) {
            showToast(`Upload finished, but: ${data.error}`, 'info');
        } else {
            showToast('Upload finished — no new valid leads found.', 'info');
        }

        if (typeof clearCategoryOverviewCache === 'function') clearCategoryOverviewCache();
        await syncState();
        if (typeof loadUploadSources === 'function') await loadUploadSources();
        if (typeof loadCategoryOverview === 'function') await loadCategoryOverview();
        if (typeof refreshCampaignManifest === 'function') {
            await refreshCampaignManifest({ keepStaleVisible: false });
        }
        if (pv && count > 0) {
            pv.textContent = `${count.toLocaleString()} lead${count === 1 ? '' : 's'} ready. Campaign ${data.auto_started ? 'running' : 'updated'}.`;
        }


        requestAnimationFrame(function () {
            const a = document.getElementById('lead-manifest-anchor');
            if (a && count > 0) {
                try {
                    a.scrollIntoView({ behavior: 'smooth', block: 'start' });
                } catch (_) {
                    a.scrollIntoView(true);
                }
            }
        });
    } catch (e) {
        showToast((e && e.message) ? e.message : 'Network error during upload', 'error');
    } finally {
        input.value = '';
        _uploadLeadsSubmitting = false;
    }
}
function downloadCSV() {
    downloadFilteredCSV();
}

// ─── Manual Call ───

let _manualCallModalBlobUrl = null;

function revokeManualCallModalBlobUrl() {
    if (_manualCallModalBlobUrl) {
        URL.revokeObjectURL(_manualCallModalBlobUrl);
        _manualCallModalBlobUrl = null;
    }
}

function closeManualCallModal() {
    const m = document.getElementById('manual-call-modal');
    const audio = document.getElementById('manual-call-modal-audio');
    if (audio) {
        audio.pause();
        audio.removeAttribute('src');
        audio.style.display = 'none';
    }
    revokeManualCallModalBlobUrl();
    if (!m) return;
    m.style.display = 'none';
    m.setAttribute('aria-hidden', 'true');
}

async function prepManualCallModalRecording(callId, recordingAvailable) {
    const audio = document.getElementById('manual-call-modal-audio');
    const msg = document.getElementById('manual-call-recording-msg');
    if (!audio || !msg) return;

    revokeManualCallModalBlobUrl();
    audio.pause();
    audio.removeAttribute('src');
    audio.style.display = 'none';
    msg.textContent = '';

    if (!callId) {
        msg.textContent = '';
        return;
    }
    if (!recordingAvailable) {
        msg.textContent = 'No recording saved for this call (recording may be off, still processing, or files rotated).';
        return;
    }
    msg.textContent = 'Loading audio…';

    // Try streaming via <audio src> with access_token first (supports range requests, progressive playback)
    if (typeof manualCallRecordingStreamUrl === 'function') {
        const streamUrl = manualCallRecordingStreamUrl(callId);
        try {
            await new Promise(function (resolve, reject) {
                var settled = false;
                function onReady() { if (!settled) { settled = true; resolve(); } }
                function onErr() { if (!settled) { settled = true; reject(new Error('Stream failed')); } }
                audio.addEventListener('loadedmetadata', onReady);
                audio.addEventListener('canplay', onReady);
                audio.addEventListener('error', onErr);
                audio.preload = 'auto';
                audio.src = streamUrl;
                audio.style.display = 'block';
                audio.load();
                setTimeout(function () {
                    if (!settled && audio.readyState >= 1 && audio.duration > 0) onReady();
                }, 12000);
            });
            msg.textContent = '';
            return;
        } catch (_e) {
            /* fall through to blob fetch */
            audio.pause();
            audio.removeAttribute('src');
            audio.style.display = 'none';
        }
    }

    // Fallback: fetch entire blob (works with Bearer token)
    try {
        const res = await fetch(apiUrl(`/api/manual/calls/${callId}/recording?role=${apiRoleQ()}`), {
            headers: { 'Authorization': `Bearer ${token()}` },
            credentials: 'same-origin',
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            msg.textContent = (typeof err.detail === 'string' && err.detail) || 'Recording could not be loaded.';
            return;
        }
        const blob = await res.blob();
        _manualCallModalBlobUrl = URL.createObjectURL(blob);
        audio.src = _manualCallModalBlobUrl;
        audio.style.display = 'block';
        msg.textContent = '';
    } catch (e) {
        msg.textContent = (e && e.message) ? e.message : 'Could not load recording.';
    }
}

async function manualCallModalReanalyze() {
    const id = window.__manualModalCallId;
    if (id == null || id === '') return;
    const btn = document.getElementById('manual-call-reanalyze-btn');
    const oldLabel = btn ? btn.textContent : '';
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Analyzing…';
    }
    try {
        const res = await fetch(apiUrl(`/api/manual/calls/${id}/reanalyze?role=${apiRoleQ()}`), {
            method: 'POST',
            headers: authHeaders(),
            credentials: 'same-origin',
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            if (res.status === 401 && typeof logout === 'function') logout();
            throw new Error(
                typeof data.detail === 'string'
                    ? data.detail
                    : (data.detail?.[0]?.msg || res.statusText || 'Re-analyze failed')
            );
        }
        openManualCallModal(data);
        showToast('Summary updated.', 'success');
        loadRecentManualCalls();
    } catch (e) {
        showToast((e && e.message) ? e.message : 'Re-analyze failed', 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = oldLabel || 'Re-analyze';
        }
    }
}

function renderShareOfVoiceGraph(lines) {
    const container = document.getElementById('manual-call-modal-graph-container');
    const barAssistant = document.getElementById('manual-graph-bar-assistant');
    const barUser = document.getElementById('manual-graph-bar-user');
    const pctAssistant = document.getElementById('manual-graph-pct-assistant');
    const pctUser = document.getElementById('manual-graph-pct-user');
    
    if (!lines || lines.length === 0) {
        container.style.display = 'none';
        return;
    }
    
    let userTokens = 0;
    let assistantTokens = 0;
    
    for (let line of lines) {
        if (line.toLowerCase().startsWith('user:')) {
            userTokens += line.length;
        } else if (line.toLowerCase().startsWith('assistant:')) {
            assistantTokens += line.length;
        }
    }
    
    const total = userTokens + assistantTokens;
    if (total === 0) {
        container.style.display = 'none';
        return;
    }
    
    container.style.display = 'block';
    
    const asstPct = Math.round((assistantTokens / total) * 100);
    const userPct = 100 - asstPct;
    
    barAssistant.style.width = '0%';
    barUser.style.width = '0%';
    
    setTimeout(() => {
        barAssistant.style.width = asstPct + '%';
        barUser.style.width = userPct + '%';
        pctAssistant.textContent = asstPct + '%';
        pctUser.textContent = userPct + '%';
    }, 50);
}

async function openManualCallModal(payload) {
    const m = document.getElementById('manual-call-modal');
    if (!m || !payload) return;
    const sub = document.getElementById('manual-call-modal-sub');
    const sum = document.getElementById('manual-call-modal-summary');
    const pre = document.getElementById('manual-call-modal-transcript');
    
    if (sub) sub.textContent = `${escapeHtml(payload.callee_name || '—')} · ${escapeHtml(payload.to_phone || '')} · ${escapeHtml(payload.status || '')}`;
    if (sum) sum.textContent = payload.summary || '—';
    
    // Graph
    renderShareOfVoiceGraph(payload.transcript_lines || []);
    
    // Recording - we rely on prepManualCallModalRecording for this
    var _recRow = document.getElementById('manual-call-recording-row');
    if (_recRow) _recRow.style.display = 'block';

    // Next Action
    const naContainer = document.getElementById('manual-call-next-action-container');
    const naBadge = document.getElementById('manual-call-next-action-badge');
    const naTime = document.getElementById('manual-call-next-action-time');
    const naDetails = document.getElementById('manual-call-next-action-details');
    
    let analysisObj = payload.analysis || {};
    let next_action = analysisObj.next_action;
    let next_steps = payload.next_steps;
    
    if (next_action && next_action.action_type && next_action.action_type !== 'None') {
        if (naContainer) naContainer.style.display = 'block';
        const type = next_action.action_type;
        let badgeHtml = '';
        if (type === 'WhatsApp') {
            badgeHtml = `<span style="display:inline-flex;align-items:center;padding:4px 10px;background:rgba(37,211,102,0.12);color:#075e54;border:1px solid rgba(37,211,102,0.25);border-radius:20px;font-size:11px;font-weight:700;letter-spacing:0.05em;text-transform:uppercase;"><svg style="margin-right:4px;" width="14" height="14" viewBox="0 0 24 24" fill="#25D366"><path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413z"/></svg> WhatsApp</span>`;
        } else if (type === 'Email') {
            badgeHtml = `<span style="display:inline-flex;align-items:center;padding:4px 10px;background:rgba(59,130,246,0.1);color:#1d4ed8;border:1px solid rgba(59,130,246,0.2);border-radius:20px;font-size:11px;font-weight:700;letter-spacing:0.05em;text-transform:uppercase;"><svg style="margin-right:4px;" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#1d4ed8" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="16" rx="3"/><path d="M22 4L12 13 2 4"/></svg> Email</span>`;
        } else if (type === 'Call Again') {
            badgeHtml = `<span style="display:inline-flex;align-items:center;padding:4px 10px;background:rgba(249,115,22,0.1);color:#c2410c;border:1px solid rgba(249,115,22,0.2);border-radius:20px;font-size:11px;font-weight:700;letter-spacing:0.05em;text-transform:uppercase;"><svg style="margin-right:4px;" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#c2410c" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z"></path></svg> Call Again</span>`;
        } else if (type === 'Reschedule') {
            badgeHtml = `<span style="display:inline-flex;align-items:center;padding:4px 10px;background:rgba(139,92,246,0.1);color:#7c3aed;border:1px solid rgba(139,92,246,0.2);border-radius:20px;font-size:11px;font-weight:700;letter-spacing:0.05em;text-transform:uppercase;"><svg style="margin-right:4px;" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#7c3aed" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg> Reschedule</span>`;
        } else if (type === 'Virtual Meet') {
            badgeHtml = `<span style="display:inline-flex;align-items:center;padding:4px 10px;background:rgba(139,92,246,0.1);color:#7c3aed;border:1px solid rgba(139,92,246,0.2);border-radius:20px;font-size:11px;font-weight:700;letter-spacing:0.05em;text-transform:uppercase;"><svg style="margin-right:4px;" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#7c3aed" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="23 7 16 12 23 17 23 7"></polygon><rect x="1" y="5" width="15" height="14" rx="2" ry="2"></rect></svg> Virtual Meet</span>`;
        } else {
            badgeHtml = `<span style="display:inline-flex;align-items:center;padding:4px 10px;background:rgba(100,116,139,0.1);color:#475569;border:1px solid rgba(100,116,139,0.2);border-radius:20px;font-size:11px;font-weight:700;letter-spacing:0.05em;text-transform:uppercase;">${escapeHtml(type)}</span>`;
        }
        if (naBadge) naBadge.innerHTML = badgeHtml;
        if (naTime) {
            naTime.textContent = next_action.datetime_iso
                ? 'Scheduled: ' + (window.formatTime ? formatTime(next_action.datetime_iso) : next_action.datetime_iso)
                : '';
        }
        if (naDetails) naDetails.textContent = next_action.details || '';

    } else if (next_steps && next_steps !== 'N/A') {
        if (naContainer) naContainer.style.display = 'block';
        if (naBadge) naBadge.innerHTML = `<span style="display:inline-flex;align-items:center;padding:4px 10px;background:rgba(100,116,139,0.1);color:#475569;border:1px solid rgba(100,116,139,0.2);border-radius:20px;font-size:11px;font-weight:700;letter-spacing:0.05em;text-transform:uppercase;">Action Needed</span>`;
        if (naTime) naTime.textContent = '';
        if (naDetails) naDetails.textContent = next_steps;
    } else {
        if (naContainer) naContainer.style.display = 'none';
    }

    if (pre) {
        var transcriptText = (payload.transcript_readable || payload.transcript_raw || '').trim();
        if (transcriptText) {
            renderTranscriptTo(pre, transcriptText, { agentName: getAgentNameForRole(payload.role), callerName: payload.callee_name || 'Caller' });
        } else {
            pre.innerHTML = '<p style="font-size:13px;color:#94a3b8;text-align:center;margin:20px 0;">No transcript available.</p>';
        }
    }
    window.__manualModalCallId = payload.id ?? null;
    const rbtn = document.getElementById('manual-call-reanalyze-btn');
    if (rbtn) {
        rbtn.disabled = !((payload.log_id || '').trim());
        rbtn.title = rbtn.disabled ? 'No transcript session id — nothing to analyze yet.' : '';
    }

    // Emotion block
    const emoBlock = document.getElementById('manual-call-modal-emotion-block');
    const emoLabel = document.getElementById('manual-call-modal-emotion');
    const emoRat = document.getElementById('manual-call-modal-emotion-rationale');
    const emoConf = document.getElementById('manual-call-modal-emotion-confidence');
    const emotionLabel = (payload.emotion_label || '').trim();
    const emotionRat = (payload.emotion_rationale || '').trim();
    if (emoBlock && emotionLabel) {
        emoBlock.style.display = 'block';
        if (emoLabel) emoLabel.textContent = emotionLabel;
        if (emoRat) emoRat.textContent = emotionRat;
        if (emoConf) {
            const c = Number(payload.emotion_confidence || 0);
            emoConf.textContent = c > 0 ? `${Math.round(c * 100)}% confidence` : '';
        }
    } else if (emoBlock) {
        emoBlock.style.display = 'none';
    }

    // Rating block
    const ratingBlock = document.getElementById('manual-call-modal-rating-block');
    const ratingEl = document.getElementById('manual-call-modal-rating');
    const rating = payload.rating;
    if (ratingBlock && rating != null && rating !== '') {
        ratingBlock.style.display = 'block';
        if (ratingEl) ratingEl.textContent = String(rating);
    } else if (ratingBlock) {
        ratingBlock.style.display = 'none';
    }

    // Recommended actions list
    const actionsBlock = document.getElementById('manual-call-modal-actions-block');
    const actionsEl = document.getElementById('manual-call-modal-actions');
    const actions = Array.isArray(payload.recommended_actions) ? payload.recommended_actions : [];
    if (actionsBlock && actionsEl && actions.length) {
        actionsEl.innerHTML = actions.map(a => `<li>${escapeHtml(String(a))}</li>`).join('');
        actionsBlock.style.display = 'block';
    } else if (actionsBlock) {
        actionsBlock.style.display = 'none';
    }

    void prepManualCallModalRecording(payload.id, !!payload.recording_available);
    m.style.display = 'flex';
    m.setAttribute('aria-hidden', 'false');

    // Auto-retry analysis if it failed or shows parsing errors
    const summaryText = (payload.summary || '').toLowerCase();
    const hasFailedAnalysis = summaryText.includes('analysis parsing failed')
        || summaryText.includes('analyzer error')
        || summaryText.includes('analysis temporarily unavailable')
        || summaryText.includes('retry analysis')
        || summaryText.includes('retry later')
        || (payload.rating === 0 && !payload.emotion_label);
    if (hasFailedAnalysis && payload.id != null && (payload.log_id || '').trim()) {
        const st = document.getElementById('manual-poll-status');
        if (st) st.textContent = 'Analysis incomplete — auto-retrying…';
        try {
            const retryRes = await fetch(apiUrl(`/api/manual/calls/${payload.id}/reanalyze?role=${apiRoleQ()}`), {
                method: 'POST',
                headers: authHeaders(),
                credentials: 'same-origin',
            });
            if (retryRes.ok) {
                const retryData = await retryRes.json().catch(() => ({}));
                if (retryData && retryData.summary && !retryData.summary.toLowerCase().includes('parsing failed')) {
                    openManualCallModal(retryData);
                    showToast('Analysis completed successfully.', 'success');
                    loadRecentManualCalls();
                }
            }
        } catch (_) { /* silent retry failure */ }
        if (st) st.textContent = '';
    }
}

async function fetchManualCallDetail(id) {
    const res = await fetch(apiUrl(`/api/manual/calls/${id}?role=${apiRoleQ()}`), {
        headers: authHeaders(),
        credentials: 'same-origin',
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    return res.json();
}

async function pollManualCallComplete(manualCallId, opts) {
    const maxMs = (opts && opts.maxMs) || 180000;
    const intervalMs = (opts && opts.intervalMs) || 2000;
    const t0 = Date.now();
    while (Date.now() - t0 < maxMs) {
        try {
            const row = await fetchManualCallDetail(manualCallId);
            if (row.status === 'completed' || row.status === 'failed') {
                return row;
            }
        } catch (_) { /* network blip */ }
        await new Promise(r => setTimeout(r, intervalMs));
    }
    return null;
}

async function loadRecentManualCalls() {
    const listEl = document.getElementById('manual-recent-list');
    const emptyEl = document.getElementById('manual-recent-empty');
    if (!listEl) return;
    try {
        const res = await fetch(apiUrl(`/api/manual/calls/recent?role=${apiRoleQ()}&limit=12`), {
            headers: authHeaders(),
            credentials: 'same-origin',
        });
        const data = await res.json();
        const items = data.items || [];
        if (emptyEl) {
            emptyEl.style.display = items.length ? 'none' : 'block';
        }
        listEl.innerHTML = items.map(r => {
            const st = escapeHtml(r.status || '');
            const sum = escapeHtml((r.summary || '').slice(0, 80) + ((r.summary || '').length > 80 ? '…' : ''));
            const btn = (r.status === 'completed' || r.status === 'failed')
                ? `<button type="button" class="btn btn-ghost btn-sm" onclick="viewManualCallOutcome(${escapeHtml(r.id)})">View result</button>`
                : `<span style="color:var(--text-secondary);font-size:11px;">${st}</span>`;
            return `<div class="manual-recent-row">
                <div><span style="font-weight:600;">${escapeHtml(r.callee_name || '—')}</span>
                <span style="color:var(--text-secondary);margin-left:8px;">${escapeHtml(r.to_phone || '')}</span>
                <div style="color:var(--text-secondary);margin-top:2px;">${sum || st}</div></div>
                <div>${btn}</div>
            </div>`;
        }).join('');
    } catch (e) {
        listEl.innerHTML = '<div style="font-size:12px;color:var(--text-secondary);">Could not load recent calls.</div>';
    }
}

async function viewManualCallOutcome(id) {
    try {
        const row = await fetchManualCallDetail(id);
        openManualCallModal(row);
    } catch (e) {
        showToast((e && e.message) ? e.message : 'Failed to load call', 'error');
    }
}

/** Value sent to `/api/manual/call`: strips leading `0` and returns digits for India-first server norm, or full `+country…`. */
function composeManualDialPayload(raw) {
    const v = String(raw || '').trim();
    if (!v) return '';
    const tight = v.replace(/[^\d+]/g, '');
    if (!tight) return '';
    if (tight.startsWith('+')) return tight;
    const digits = tight.replace(/\+/g, '');
    if (digits.length > 10 && digits.startsWith('0')) return digits.slice(1);
    return digits;
}

/** Human-readable label for the result card (best-effort; server is canonical). */
function manualDialPreviewLabel(payloadTo) {
    if (!payloadTo) return '';
    const p = String(payloadTo);
    if (p.startsWith('+')) return p;
    let d = p.replace(/\D/g, '');
    if (!d) return '';
    if (d.length > 10 && d.startsWith('0')) d = d.slice(1);
    return '+91 ' + d.slice(-10);
}

function refreshManualPhoneCcBadge() {
    const label = document.getElementById('manual-phone-cc');
    const inp = document.getElementById('manual-phone-local');
    if (!label || !inp) return;
    const intl = inp.value.trimStart().startsWith('+');
    label.style.opacity = intl ? '0.35' : '1';
    label.textContent = '+91';
}

function manualCallRoleQ() {
    if (typeof tuningRoleForApi === 'function') {
        return encodeURIComponent(tuningRoleForApi());
    }
    return typeof apiRoleQ === 'function' ? apiRoleQ() : encodeURIComponent('sales_1');
}

async function triggerManualTest() {
    const name = document.getElementById('manual-name')?.value?.trim() || '';
    const localEl = document.getElementById('manual-phone-local');
    const rawLocal = localEl ? localEl.value.trim() : '';
    const phone = composeManualDialPayload(rawLocal);
    if (!phone) {
        if (typeof showToast === 'function') showToast('Enter a phone number.', 'error');
        else alert('Enter a phone number.');
        return;
    }
    const btn = document.getElementById('manual-initiate-btn')
        || document.querySelector('#page-manual button.btn-primary.btn-lg');
    if (!btn) {
        if (typeof showToast === 'function') showToast('Initiate Call button not found — reload the page.', 'error');
        return;
    }
    btn.disabled = true;
    btn.textContent = 'Calling...';
    const abort = new AbortController();
    const abortTimer = setTimeout(function () { abort.abort(); }, 45000);
    try {
        const res = await fetch(apiUrl(`/api/manual/call?role=${manualCallRoleQ()}`), {
            method: 'POST',
            headers: authHeaders(),
            credentials: 'same-origin',
            signal: abort.signal,
            body: JSON.stringify({ to: phone, callee_name: name }),
        });
        let data = {};
        try { data = await res.json(); } catch (_) {}
        if (!res.ok) {
            if (res.status === 401 && typeof logout === 'function') {
                alert('Your session expired or the server was restarted. Signing you in again…');
                logout();
                return;
            }
            throw new Error(
                typeof data.detail === 'string'
                    ? data.detail
                    : (Array.isArray(data.detail)
                        ? (data.detail[0]?.msg || res.statusText)
                        : (data.detail || data.message || res.statusText))
            );
        }
        const card = document.getElementById('call-result-card');
        card.style.borderStyle = 'solid';
        const mid = data.manual_call_id;
        card.innerHTML = `<div style="text-align:left;padding:16px;width:100%;">
            <p style="font-size:13px;font-weight:700;color:var(--success);margin:0 0 8px;">Call initiated</p>
            <p style="font-size:12px;color:var(--text-secondary);margin:0 0 12px;">Dialing ${escapeHtml(manualDialPreviewLabel(phone))}… Outcome appears when the call ends (typically 30–90s after hangup).</p>
            <p id="manual-poll-status" style="font-size:12px;color:var(--text-secondary);margin:0;">Analyzing transcript…</p>
            <button type="button" class="btn btn-ghost btn-sm" style="margin-top:12px;" onclick="loadRecentManualCalls()">Refresh recent list</button>
        </div>`;
        loadRecentManualCalls();
        if (mid) {
            pollManualCallComplete(mid).then(row => {
                const st = document.getElementById('manual-poll-status');
                if (!row) {
                    if (st) st.textContent = 'Still processing — retrying in 10s…';
                    setTimeout(() => {
                        fetchManualCallDetail(mid).then(row2 => {
                            if (row2 && (row2.status === 'completed' || row2.status === 'failed')) {
                                openManualCallModal(row2);
                                loadRecentManualCalls();
                            } else {
                                if (st) st.textContent = 'Open Recent manual calls → View result when ready.';
                            }
                        }).catch(() => {
                            if (st) st.textContent = 'Open Recent manual calls → View result when ready.';
                        });
                    }, 10000);
                    return;
                }
                if (st) st.textContent = row.status === 'failed' ? 'Ended with errors — see summary.' : 'Ready — opening summary…';
                openManualCallModal(row);
                if (row.status === 'failed') {
                    showToast((row.error || 'Manual call failed') + '', 'error');
                }
                loadRecentManualCalls();
            });
        }
    } catch (e) {
        const msg = (e && e.name === 'AbortError')
            ? 'Call request timed out (45s). Server may be busy — try again in a few seconds.'
            : ((e && e.message) ? e.message : String(e));
        if (typeof showToast === 'function') showToast('Error: ' + msg, 'error');
        else alert('Error: ' + msg);
    } finally {
        clearTimeout(abortTimer);
        btn.disabled = false;
        btn.textContent = 'Initiate Call';
    }
}

// ─── Incoming Calls ───

function closeIncomingCallModal() {
    const m = document.getElementById('incoming-call-modal');
    const audio = document.getElementById('incoming-call-modal-audio');
    if (audio) {
        audio.pause();
        audio.removeAttribute('src');
        audio.style.display = 'none';
    }
    if (!m) return;
    m.style.display = 'none';
    m.setAttribute('aria-hidden', 'true');
}

async function fetchIncomingCallDetail(id) {
    const res = await fetch(apiUrl(`/api/incoming/calls/${id}?role=${apiRoleQ()}`), {
        headers: authHeaders(),
        credentials: 'same-origin',
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    return res.json();
}

async function loadRecentIncomingCalls() {
    const listEl = document.getElementById('incoming-recent-list');
    const emptyEl = document.getElementById('incoming-recent-empty');
    if (!listEl) return;
    try {
        const res = await fetch(apiUrl(`/api/incoming/calls/recent?role=${apiRoleQ()}&limit=12`), {
            headers: authHeaders(),
            credentials: 'same-origin',
        });
        const data = await res.json();
        const items = data.items || [];
        if (emptyEl) {
            emptyEl.style.display = items.length ? 'none' : 'block';
        }
        listEl.innerHTML = items.map(r => {
            const st = escapeHtml(r.status || '');
            const sum = escapeHtml((r.summary || '').slice(0, 80) + ((r.summary || '').length > 80 ? '…' : ''));
            const btn = (r.status === 'completed' || r.status === 'failed')
                ? `<button type="button" class="btn btn-ghost btn-sm" onclick="viewIncomingCallOutcome(${escapeHtml(r.id)})">View result</button>`
                : `<span style="color:var(--text-secondary);font-size:11px;">${st}</span>`;
            return `<div class="manual-recent-row">
                <div><span style="font-weight:600;">${escapeHtml(r.callee_name || '—')}</span>
                <span style="color:var(--text-secondary);margin-left:8px;">${escapeHtml(r.from_phone || '')}</span>
                <div style="color:var(--text-secondary);margin-top:2px;">${sum || st}</div></div>
                <div>${btn}</div>
            </div>`;
        }).join('');
    } catch (e) {
        listEl.innerHTML = '<div style="font-size:12px;color:var(--text-secondary);">Could not load incoming calls.</div>';
    }
}

async function viewIncomingCallOutcome(id) {
    try {
        const row = await fetchIncomingCallDetail(id);
        openIncomingCallModal(row);
    } catch (e) {
        showToast((e && e.message) ? e.message : 'Failed to load call', 'error');
    }
}

async function openIncomingCallModal(payload) {
    const m = document.getElementById('incoming-call-modal');
    if (!m) return;
    m.style.display = 'flex';
    m.removeAttribute('aria-hidden');

    const titleEl = document.getElementById('incoming-call-modal-title');
    const subEl = document.getElementById('incoming-call-modal-sub');
    const summaryEl = document.getElementById('incoming-call-modal-summary');
    const transcriptEl = document.getElementById('incoming-call-modal-transcript');
    const audioEl = document.getElementById('incoming-call-modal-audio');
    const recordingRow = document.getElementById('incoming-call-recording-row');
    const recordingMsg = document.getElementById('incoming-call-recording-msg');

    const name = escapeHtml(payload.callee_name || payload.lead_name || 'Unknown Caller');
    const phone = escapeHtml(payload.from_phone || '');
    const status = escapeHtml(payload.status || '');
    const summary = escapeHtml(payload.summary || 'No summary available.');
    const transcript = payload.transcript_readable || payload.transcript || '';

    titleEl.textContent = 'Incoming Call — ' + name;
    subEl.textContent = phone + (status ? ' · ' + status : '');
    summaryEl.textContent = summary;

    if (transcript) {
        transcriptEl.innerHTML = transcript.split('\n').filter(l => l.trim()).map(line => {
            const esc = escapeHtml(line.trim());
            if (esc.startsWith('**')) {
                const bold = esc.replace(/^\*\*|\*\*$/g, '');
                return '<div style="font-weight:700;margin-top:10px;color:#0f172a;">' + bold + '</div>';
            }
            return '<div style="padding:2px 0;">' + esc + '</div>';
        }).join('');
    } else {
        transcriptEl.innerHTML = '<em style="color:var(--text-secondary);">No transcript available.</em>';
    }

    window.__incomingModalCallId = payload.id ?? null;

    // Recording
    if (audioEl) { audioEl.pause(); audioEl.removeAttribute('src'); audioEl.style.display = 'none'; }
    if (recordingMsg) recordingMsg.textContent = '';
    if (recordingRow) recordingRow.style.display = 'none';

    if (payload.id && (payload.recording_available || payload.log_id)) {
        async function loadIncomingRecording(retry) {
            try {
                const recRes = await fetch(apiUrl(`/api/incoming/calls/${payload.id}/recording?role=${apiRoleQ()}`), {
                    headers: authHeaders(),
                    credentials: 'same-origin',
                });
                if (recRes.ok) {
                    const blob = await recRes.blob();
                    if (blob.size > 0) {
                        const url = URL.createObjectURL(blob);
                        audioEl.src = url;
                        audioEl.style.display = 'block';
                        if (recordingRow) recordingRow.style.display = 'block';
                        return true;
                    }
                }
            } catch (_) {}
            if (retry) {
                setTimeout(function () { loadIncomingRecording(false); }, 5000);
                if (recordingMsg) recordingMsg.textContent = 'Recording finalizing…';
                if (recordingRow) recordingRow.style.display = 'block';
                return false;
            }
            if (recordingMsg) recordingMsg.textContent = 'No recording available.';
            if (recordingRow) recordingRow.style.display = 'block';
            return false;
        }
        await loadIncomingRecording(true);
    }
}

// ─── Tuning (Configuration) ───
// Critical invariant: the textareas must reflect ``currentRole`` at all times.
// If we ever leave stale content from another role visible, the user could click
// Save and POST it under the wrong role — corrupting that role's prompt file.
// So we (1) blank the fields first, (2) lock Save during the fetch, and
// (3) always assign the API response (even if empty).
async function loadTuning() {
    const promptEl   = document.getElementById('tuning-prompt');
    const ragEl      = document.getElementById('tuning-rag');
    const greetingEl = document.getElementById('tuning-greeting');
    const saveBtn    = document.querySelector('#page-tuning button.btn-primary');

    // 1) Blank everything so a stale (cross-role) value can never be re-saved.
    promptEl.value = '';
    ragEl.value = '';
    greetingEl.value = '';

    // 2) Lock Save while the role's real content is in-flight.
    let originalLabel = '';
    if (saveBtn) {
        originalLabel = saveBtn.textContent;
        saveBtn.disabled = true;
        saveBtn.textContent = 'Loading…';
    }

    try {
        const roleQ =
            typeof tuningRoleForApi === 'function'
                ? encodeURIComponent(tuningRoleForApi())
                : apiRoleQ();
        const res = await fetch(apiUrl(`/api/tuning?role=${roleQ}`), {
            headers: { 'Authorization': `Bearer ${token()}` },
            credentials: 'same-origin',
        });
        if (!res.ok) return;
        const d = await res.json();
        // Always assign — even if empty — so the UI is the source of truth
        // for ``currentRole`` and Save can never mix roles.
        promptEl.value   = d.prompt != null ? String(d.prompt) : '';
        ragEl.value      = d.rag    != null ? String(d.rag)    : '';
        greetingEl.value = d.greeting_text != null ? String(d.greeting_text) : '';
    } catch {}
    finally {
        if (saveBtn) {
            saveBtn.disabled = false;
            saveBtn.textContent = originalLabel || 'Save Changes';
        }
        // Fetch active version number after loading
        fetchActiveVersion();
    }
}

function stopLiveTest() {
    if (liveWs) {
        liveWs.close();
        liveWs = null;
    }
    teardownLiveAudio();
    resetVoiceTest();
}

async function addSchedule() {
    const btn = document.getElementById('btn-schedule');
    const whenEl = document.getElementById('schedule-when');
    const stopEl = document.getElementById('schedule-stop');
    const nameEl = document.getElementById('schedule-name');
    const rawStart = (whenEl && whenEl.value) ? whenEl.value.trim() : '';
    const rawStop  = (stopEl && stopEl.value) ? stopEl.value.trim() : '';
    if (!rawStart) {
        showToast('Pick a start date and time first.', 'error');
        return;
    }
    // ``datetime-local`` returns ``YYYY-MM-DDTHH:MM`` without timezone, which
    // ``new Date(...)`` parses as **local time** — exactly what the operator
    // sees in the picker. We then convert to ISO with offset so the backend
    // gets an unambiguous instant.
    const startLocal = new Date(rawStart);
    if (isNaN(startLocal.getTime())) {
        showToast('That doesn\'t look like a valid start date / time.', 'error');
        return;
    }
    if (startLocal.getTime() < Date.now() - 15000) {
        showToast('Pick a future start time — that moment has already passed.', 'error');
        return;
    }

    let stopLocal = null;
    if (rawStop) {
        stopLocal = new Date(rawStop);
        if (isNaN(stopLocal.getTime())) {
            showToast('That doesn\'t look like a valid stop date / time.', 'error');
            return;
        }
        if (stopLocal.getTime() <= startLocal.getTime()) {
            showToast('Stop time must be after the start time.', 'error');
            return;
        }
    }

    if (btn) { btn.disabled = true; btn.textContent = 'Scheduling…'; }
    try {
        const body = {
            run_at_iso: startLocal.toISOString(),
            name: nameEl ? nameEl.value.trim() : '',
        };
        if (stopLocal) body.stop_at_iso = stopLocal.toISOString();

        const res = await fetch(apiUrl(`/api/schedules?role=${apiRoleQ()}`), {
            method: 'POST',
            headers: authHeaders(),
            credentials: 'same-origin',
            body: JSON.stringify(body),
        });
        if (!res.ok) {
            showToast(await parseApiErrorMessage(res), 'error');
            return;
        }
        const startTxt = _formatScheduleWhen(startLocal.getTime() / 1000);
        const msg = stopLocal
            ? `Scheduled ${startTxt} → ${_formatScheduleWhen(stopLocal.getTime() / 1000)}.`
            : `Scheduled for ${startTxt}.`;
        showToast(msg, 'success');
        if (nameEl) nameEl.value = '';
        if (stopEl) stopEl.value = '';
        await loadSchedules();
    } catch (e) {
        showToast((e && e.message) ? e.message : 'Network error', 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Schedule'; }
    }
}

function clearScheduleStop() {
    const el = document.getElementById('schedule-stop');
    if (el) el.value = '';
}

async function saveTuning() {
    if (_saveTuningSubmitting) return;
    _saveTuningSubmitting = true;
    const btn = document.querySelector('#page-tuning button.btn-primary');
    const targetRole = currentRole;
    const prompt = document.getElementById('tuning-prompt').value;
    const rag = document.getElementById('tuning-rag').value;
    const greeting_text = document.getElementById('tuning-greeting').value;

    btn.disabled = true; btn.textContent = 'Saving...';
    try {
        const res = await fetch(apiUrl(`/api/tuning?role=${encodeURIComponent(targetRole)}`), {
            method: 'POST',
            headers: authHeaders(),
            credentials: 'same-origin',
            body: JSON.stringify({ prompt, rag, greeting_text }),
        });
        if (res.ok) {
            btn.textContent = '✓ Saved';
            showToast('Configuration saved.', 'success');
            if (currentRole === targetRole) await loadTuning();
            setTimeout(() => { btn.textContent = 'Save Changes'; btn.disabled = false; }, 2000);
        } else {
            const err = await parseApiErrorMessage(res);
            showToast(err, 'error');
            btn.disabled = false; btn.textContent = 'Save Changes';
        }
    } catch (e) {
        showToast(e.message || 'Save failed', 'error');
        btn.disabled = false; btn.textContent = 'Save Changes';
    } finally {
        _saveTuningSubmitting = false;
    }
}

// ─── Prompt Versioning ───

async function loadVersionHistory() {
    const panel = document.getElementById('version-history-panel');
    const list = document.getElementById('version-history-list');
    if (panel.style.display === 'block') { panel.style.display = 'none'; return; }
    panel.style.display = 'block';
    list.innerHTML = '<div style="padding:12px;color:var(--text-secondary);">Loading...</div>';
    const targetRole = currentRole;
    try {
        const res = await fetch(apiUrl(`/api/tuning/versions?role=${encodeURIComponent(targetRole)}`), {
            headers: authHeaders(), credentials: 'same-origin',
        });
        if (!res.ok) throw new Error('Failed to load versions');
        const data = await res.json();
        const versions = data.versions || [];
        const activeId = data.active_version_id;
        if (!versions.length) {
            list.innerHTML = '<div style="padding:12px;color:var(--text-secondary);">No versions yet. Save &amp; Publish to create the first version.</div>';
            return;
        }
        list.innerHTML = versions.map(v => {
            const isActive = v.id === activeId;
            const badge = isActive ? '<span style="color:#16a34a;font-weight:700;">● ACTIVE</span>' : '<span style="color:var(--text-secondary);">Archived</span>';
            const restoreBtn = isActive ? '' : `<button class="btn btn-sm" onclick="restoreVersion(${v.id})" style="font-size:10px;padding:2px 8px;">↩ Restore</button>`;
            return `<div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--border);">
                <div>
                    <strong>v${v.version_number}</strong> ${badge}
                    <span style="color:var(--text-secondary);font-size:10px;margin-left:8px;">${(v.created_at||'').slice(0,16)}</span>
                </div>
                <div>${restoreBtn}</div>
            </div>`;
        }).join('');
    } catch (e) {
        list.innerHTML = `<div style="padding:12px;color:#dc2626;">Error: ${e.message}</div>`;
    }
}

async function restoreVersion(versionId) {
    if (!confirm('Restore this version? It will become the active prompt for all new calls.')) return;
    const targetRole = currentRole;
    try {
        const res = await fetch(apiUrl(`/api/tuning/versions/${versionId}/restore?role=${encodeURIComponent(targetRole)}`), {
            method: 'POST', headers: authHeaders(), credentials: 'same-origin',
        });
        if (!res.ok) throw new Error(await parseApiErrorMessage(res));
        showToast('Version restored! New calls will use this prompt.', 'success');
        await loadTuning();
        await loadVersionHistory();
    } catch (e) {
        showToast(e.message || 'Restore failed', 'error');
    }
}

async function fetchActiveVersion() {
    const targetRole = currentRole;
    try {
        const res = await fetch(apiUrl(`/api/tuning/versions?role=${encodeURIComponent(targetRole)}`), {
            headers: authHeaders(), credentials: 'same-origin',
        });
        if (!res.ok) return;
        const data = await res.json();
        const vn = data.active_version_number;
        const el = document.getElementById('tuning-version-num');
        if (el) el.textContent = vn || '—';
        const badge = document.getElementById('tuning-status-badge');
        if (badge) {
            if (vn) { badge.textContent = `● v${vn} LIVE`; badge.style.background = '#dcfce7'; badge.style.color = '#16a34a'; }
        }
    } catch (e) { /* silent */ }
}

// ─── Scheduled Callback UI Integration ───
function _initScheduledCallbackDefaults() {
    const whenEl = document.getElementById('callback-schedule-at');
    if (whenEl && !whenEl.value) {
        const future = new Date(Date.now() + 10 * 60 * 1000);
        future.setSeconds(0);
        future.setMilliseconds(0);
        const pad = (n) => String(n).padStart(2, '0');
        const formatted = `${future.getFullYear()}-${pad(future.getMonth() + 1)}-${pad(future.getDate())}T${pad(future.getHours())}:${pad(future.getMinutes())}`;
        whenEl.value = formatted;
    }
}

// ─── Campaign Schedules UI Integration ───
function _initScheduleDefaults() {
    const tzEl = document.getElementById('schedule-tz-pill');
    if (tzEl) tzEl.textContent = 'IST';
    const whenEl = document.getElementById('schedule-when');
    if (whenEl && !whenEl.value) {
        const now = new Date();
        const istOffset = 5.5 * 60 * 60 * 1000;
        const future = new Date(Date.now() + istOffset + 10 * 60 * 1000);
        future.setSeconds(0);
        future.setMilliseconds(0);
        const pad = (n) => String(n).padStart(2, '0');
        const formatted = `${future.getUTCFullYear()}-${pad(future.getUTCMonth() + 1)}-${pad(future.getUTCDate())}T${pad(future.getUTCHours())}:${pad(future.getUTCMinutes())}`;
        whenEl.value = formatted;
    }
}

function _formatScheduleWhen(epoch) {
    if (!epoch) return '—';
    try {
        const d = new Date(epoch * 1000);
        return formatTimeIST(d.toISOString());
    } catch (_) {
        return '—';
    }
}

async function loadSchedules() {
    const listEl = document.getElementById('schedules-list');
    if (!listEl) return;
    try {
        const res = await fetch(apiUrl(`/api/schedules?role=${apiRoleQ()}`), {
            headers: authHeaders(),
            credentials: 'same-origin',
        });
        if (res.status === 401 && typeof logout === 'function') { logout(); return; }
        if (!res.ok) {
            listEl.innerHTML = `<div style="text-align:center;padding:18px;color:var(--text-secondary);">Could not load schedules.</div>`;
            return;
        }
        const data = await res.json().catch(() => ({}));
        const list = Array.isArray(data.schedules) ? data.schedules : [];
        if (!list.length) {
            listEl.innerHTML = `<div style="text-align:center;padding:18px;color:var(--text-secondary);">No schedules yet.</div>`;
            return;
        }
        
        listEl.innerHTML = list.map(s => {
            const id = s.id;
            const status = s.status || 'scheduled';
            const name = escapeHtml(s.name || 'Unnamed Schedule');
            
            let whenText = s.run_at ? formatTimeIST(new Date(s.run_at * 1000).toISOString()) : '—';
            if (s.stop_at) {
                const stopText = formatTimeIST(new Date(s.stop_at * 1000).toISOString());
                whenText += ` → ${stopText}`;
            }
            
            let badgeClass = 'badge-tag-neutral';
            if (status === 'scheduled') badgeClass = 'badge-tag-warning';
            else if (status === 'running') badgeClass = 'badge-tag-success';
            else if (status === 'completed') badgeClass = 'badge-tag-info';
            else if (status === 'cancelled') badgeClass = 'badge-tag-neutral';
            else if (status === 'failed') badgeClass = 'badge-tag-danger';
            
            const cancelBtn = (status === 'scheduled') 
                ? `<button type="button" class="btn btn-ghost btn-sm" style="color:var(--danger);font-size:11px;padding:2px 8px;border:1px solid var(--danger);" onclick="cancelSchedule(${escapeHtml(id)})">Cancel</button>`
                : '';
                
            return `
                <div style="display:flex;justify-content:space-between;align-items:center;padding:10px 14px;border-bottom:1px solid var(--border);gap:12px;">
                    <div style="flex:1;">
                        <div style="font-weight:600;display:flex;align-items:center;gap:8px;">
                            <span>${name}</span>
                            <span class="badge-tag ${badgeClass}" style="font-size:10px;padding:2px 6px;text-transform:uppercase;">${escapeHtml(status)}</span>
                        </div>
                        <div style="font-size:11px;color:var(--text-secondary);margin-top:4px;">${escapeHtml(whenText)}</div>
                    </div>
                    <div>${cancelBtn}</div>
                </div>
            `;
        }).join('');
    } catch (e) {
        listEl.innerHTML = `<div style="text-align:center;padding:18px;color:var(--text-secondary);">Could not load schedules.</div>`;
    }
}

async function cancelSchedule(id) {
    if (!confirm('Are you sure you want to cancel this scheduled campaign?')) return;
    try {
        const res = await fetch(apiUrl(`/api/schedules/${id}`), {
            method: 'DELETE',
            headers: authHeaders(),
            credentials: 'same-origin',
        });
        if (res.status === 401 && typeof logout === 'function') logout();
        if (!res.ok) {
            showToast(await parseApiErrorMessage(res), 'error');
            return;
        }
        showToast('Schedule cancelled successfully.', 'success');
        await loadSchedules();
    } catch (e) {
        showToast((e && e.message) ? e.message : 'Failed to cancel schedule', 'error');
    }
}

// ─── Scheduled Individual Callbacks ───

async function addScheduledCallback() {
    const btn = document.getElementById('btn-schedule-callback');
    const phoneEl = document.getElementById('callback-phone');
    const nameEl = document.getElementById('callback-name');
    const leadIdEl = document.getElementById('callback-lead-id');
    const whenEl = document.getElementById('callback-schedule-at');

    const rawPhone = (phoneEl && phoneEl.value) ? phoneEl.value.trim() : '';
    const rawName = (nameEl && nameEl.value) ? nameEl.value.trim() : '';
    const rawLeadId = (leadIdEl && leadIdEl.value) ? leadIdEl.value.trim() : '';
    const rawWhen = (whenEl && whenEl.value) ? whenEl.value.trim() : '';

    if (!rawPhone) {
        showToast('Enter a phone number.', 'error');
        return;
    }
    if (!rawWhen) {
        showToast('Pick a date and time for the callback.', 'error');
        return;
    }

    const localDt = new Date(rawWhen);
    if (isNaN(localDt.getTime())) {
        showToast('Invalid date/time.', 'error');
        return;
    }
    if (localDt.getTime() < Date.now() - 15000) {
        showToast('Pick a future time — that moment has already passed.', 'error');
        return;
    }

    if (btn) { btn.disabled = true; btn.textContent = 'Scheduling…'; }
    try {
        const body = {
            phone: rawPhone,
            name: rawName,
            scheduled_at_iso: localDt.toISOString(),
        };
        if (rawLeadId) body.lead_id = parseInt(rawLeadId, 10);

        const res = await fetch(apiUrl(`/api/callbacks?role=${apiRoleQ()}`), {
            method: 'POST',
            headers: authHeaders(),
            credentials: 'same-origin',
            body: JSON.stringify(body),
        });
        if (!res.ok) {
            showToast(await parseApiErrorMessage(res), 'error');
            return;
        }
        showToast('Callback scheduled successfully.', 'success');
        if (phoneEl) phoneEl.value = '';
        if (nameEl) nameEl.value = '';
        if (leadIdEl) leadIdEl.value = '';
        if (whenEl) whenEl.value = '';
        await loadScheduledCallbacks();
    } catch (e) {
        showToast((e && e.message) ? e.message : 'Network error', 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Schedule Callback'; }
    }
}

function renderCallbacksList(list) {
    const listEl = document.getElementById('scheduled-callbacks-list');
    const countEl = document.getElementById('callback-count');
    if (!listEl) return;
    if (!list.length) {
        listEl.innerHTML = `<div style="text-align:center;padding:var(--space-xl);color:var(--text-secondary);font-size:13px;">No scheduled callbacks found.</div>`;
        if (countEl) countEl.textContent = '';
        return;
    }
    if (countEl) countEl.textContent = '(' + list.length + ')';

    const rows = list.map(cb => {
        const id = cb.id;
        const status = cb.status || 'scheduled';
        const name = escapeHtml(cb.name || 'Unknown');
        const phone = escapeHtml(cb.phone || '—');
        const scheduledAt = cb.scheduled_at ? formatTimeIST(new Date(cb.scheduled_at * 1000).toISOString()) : '—';
        const errorTxt = cb.error ? escapeHtml(cb.error) : '';
        const outbound = escapeHtml(cb.outbound_phone || '—');
        const disposition = cb.disposition || '';
        const summary = cb.summary ? escapeHtml(cb.summary) : '—';
        const userReview = (cb.user_review || '').trim().toLowerCase();

        let badgeClass = 'badge-tag-warning';
        let badgeLabel = status;
        if (status === 'queued') { badgeClass = 'badge-tag-info'; badgeLabel = 'Queued'; }
        else if (status === 'calling') { badgeClass = 'badge-tag-success'; badgeLabel = 'Calling'; }
        else if (status === 'completed') { badgeClass = 'badge-tag-info'; badgeLabel = 'Completed'; }
        else if (status === 'cancelled') { badgeClass = 'badge-tag-neutral'; badgeLabel = 'Cancelled'; }
        else if (status === 'failed') { badgeClass = 'badge-tag-danger'; badgeLabel = 'Failed'; }
        else if (status === 'scheduled') { badgeClass = 'badge-tag-warning'; badgeLabel = 'Scheduled'; }

        const cancelBtn = (status === 'scheduled' || status === 'queued')
            ? `<button type="button" class="btn btn-ghost btn-sm" style="color:var(--danger);font-size:11px;padding:2px 8px;border:1px solid var(--danger);border-radius:6px;" onclick="cancelScheduledCallback(${escapeHtml(id)})">Cancel</button>`
            : '—';

        const reviewInterestedStyle = userReview === 'interested'
            ? 'background:var(--success);color:#fff;border-color:var(--success);'
            : 'color:var(--success);border:1px solid var(--success);';
        const reviewNotStyle = userReview === 'not_interested'
            ? 'background:var(--danger);color:#fff;border-color:var(--danger);'
            : 'color:var(--danger);border:1px solid var(--danger);';
        const reviewBtns = `<div style="display:flex;gap:6px;justify-content:flex-end;align-items:center;">
            <button type="button" title="Right number / interested" class="btn btn-ghost btn-sm" style="font-size:11px;padding:2px 10px;border-radius:6px;${reviewInterestedStyle}" onclick="reviewScheduledCallback(${Number(id)}, 'interested')">Right</button>
            <button type="button" title="Wrong number / not interested" class="btn btn-ghost btn-sm" style="font-size:11px;padding:2px 10px;border-radius:6px;${reviewNotStyle}" onclick="reviewScheduledCallback(${Number(id)}, 'not_interested')">Wrong</button>
        </div>`;

        const outcomeCell = status === 'completed' && disposition
            ? `<span class="badge-tag" style="font-size:10px;padding:2px 6px;">${escapeHtml(disposition)}</span>`
            : (status === 'failed' && errorTxt ? `<span style="color:var(--danger);font-size:11px;">${errorTxt}</span>` : '—');

        return `<tr>
            <td style="padding:10px 14px;font-weight:600;border-bottom:1px solid var(--border);">${name}</td>
            <td style="padding:10px 14px;font-family:var(--font-mono);font-size:12px;border-bottom:1px solid var(--border);">${phone}</td>
            <td style="padding:10px 14px;font-family:var(--font-mono);font-size:11px;border-bottom:1px solid var(--border);">${outbound}</td>
            <td style="padding:10px 14px;font-size:12px;border-bottom:1px solid var(--border);">${scheduledAt}</td>
            <td style="padding:10px 14px;border-bottom:1px solid var(--border);"><span class="badge-tag ${badgeClass}" style="font-size:10px;padding:2px 8px;text-transform:uppercase;">${badgeLabel}</span></td>
            <td style="padding:10px 14px;font-size:12px;border-bottom:1px solid var(--border);max-width:200px;">${outcomeCell}</td>
            <td style="padding:10px 14px;font-size:11px;color:var(--text-secondary);border-bottom:1px solid var(--border);max-width:240px;">${summary}</td>
            <td style="padding:10px 14px;text-align:center;border-bottom:1px solid var(--border);">${reviewBtns}</td>
            <td style="padding:10px 14px;text-align:right;border-bottom:1px solid var(--border);">${cancelBtn}</td>
        </tr>`;
    }).join('');

    listEl.innerHTML = `
        <div class="table-wrap" style="border:1px solid var(--border);border-radius:10px;overflow:hidden;">
            <table style="width:100%;border-collapse:collapse;font-size:13px;">
                <thead>
                    <tr style="background:var(--surface);">
                        <th style="padding:10px 14px;text-align:left;border-bottom:1px solid var(--border);">Name</th>
                        <th style="padding:10px 14px;text-align:left;border-bottom:1px solid var(--border);">Phone</th>
                        <th style="padding:10px 14px;text-align:left;border-bottom:1px solid var(--border);">Outbound Line</th>
                        <th style="padding:10px 14px;text-align:left;border-bottom:1px solid var(--border);">Scheduled</th>
                        <th style="padding:10px 14px;text-align:left;border-bottom:1px solid var(--border);">Status</th>
                        <th style="padding:10px 14px;text-align:left;border-bottom:1px solid var(--border);">Outcome</th>
                        <th style="padding:10px 14px;text-align:left;border-bottom:1px solid var(--border);">Summary</th>
                        <th style="padding:10px 14px;text-align:center;border-bottom:1px solid var(--border);">Right / Wrong</th>
                        <th style="padding:10px 14px;text-align:right;border-bottom:1px solid var(--border);">Action</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        </div>`;
}

function filterScheduledCallbacks() {
    const searchVal = (document.getElementById('callback-search-input')?.value || '').trim().toLowerCase();
    const list = window.lastLoadedCallbacks || [];
    const filtered = list.filter(cb => {
        const phone = (cb.phone || '').toLowerCase();
        const name = (cb.name || '').toLowerCase();
        return phone.includes(searchVal) || name.includes(searchVal);
    });
    renderCallbacksList(filtered);
}

async function loadHealthAgents() {
    const listEl = document.getElementById('health-agents-list');
    const badge = document.getElementById('agents-overall-badge');
    if (!listEl) return;
    try {
        const res = await fetch(apiUrl('/health/agents'), { headers: authHeaders(), credentials: 'same-origin' });
        if (!res.ok) throw new Error('Failed to load agents');
        const data = await res.json();
        const agents = Array.isArray(data.agents) ? data.agents : [];
        const boss = data.boss || null;
        const overall = data.overall_health || 'ok';
        if (badge) {
            badge.textContent = overall.toUpperCase();
            badge.className = 'badge-tag ' + (overall === 'ok' ? 'badge-tag-success' : overall === 'critical' ? 'badge-tag-danger' : 'badge-tag-warning');
        }
        const cards = [];
        if (boss && boss.agent_id) {
            const bh = (boss.health || 'ok').toLowerCase();
            const bcolor = bh === 'ok' ? 'var(--success)' : bh === 'critical' ? 'var(--danger)' : 'var(--warning)';
            const childNote = (boss.children_monitored != null)
                ? `Watching ${boss.children_monitored} agents · ${boss.children_critical || 0} critical · ${boss.children_warn || 0} warn`
                : 'Parent supervisor';
            const decision = boss.last_decision_detail || boss.last_decision || '';
            cards.push(`<div style="grid-column:1/-1;border:2px solid var(--primary);border-radius:10px;padding:12px 14px;background:linear-gradient(135deg,rgba(59,130,246,.08),rgba(16,185,129,.06));">
                <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;">
                    <div>
                        <div style="font-weight:800;font-size:13px;">👑 ${escapeHtml(boss.agent_name || 'Super Boss')}</div>
                        <div style="font-size:10px;color:var(--text-tertiary);text-transform:uppercase;letter-spacing:.05em;margin-top:2px;">Parent Supervisor</div>
                    </div>
                    <div style="font-size:11px;color:${bcolor};font-weight:700;">${bh.toUpperCase()}</div>
                </div>
                <div style="font-size:11px;color:var(--text-secondary);margin-top:8px;line-height:1.4;">${escapeHtml(childNote)}</div>
                ${decision ? `<div style="font-size:10px;color:var(--text-secondary);margin-top:6px;font-style:italic;">${escapeHtml(decision)}</div>` : ''}
            </div>`);
        }
        if (!agents.length && !cards.length) {
            listEl.innerHTML = '<div style="color:var(--text-secondary);">No agents running.</div>';
            return;
        }
        cards.push(...agents.map(a => {
            const h = (a.health || 'ok').toLowerCase();
            const color = h === 'ok' ? 'var(--success)' : h === 'critical' ? 'var(--danger)' : 'var(--warning)';
            const issues = (a.findings || []).map(f => f.message).slice(0, 2).join('; ');
            const healed = a.last_heal && a.last_heal.healed ? ' · auto-healed' : '';
            return `<div style="border:1px solid var(--border);border-radius:8px;padding:10px 12px;background:var(--card);">
                <div style="font-weight:600;font-size:12px;margin-bottom:4px;">${escapeHtml(a.agent_name || a.agent_id)}</div>
                <div style="font-size:10px;color:var(--text-tertiary);text-transform:uppercase;letter-spacing:.05em;">${escapeHtml(a.domain || '')}</div>
                <div style="font-size:11px;margin-top:6px;color:${color};font-weight:600;">${h.toUpperCase()}${healed}</div>
                ${issues ? `<div style="font-size:10px;color:var(--text-secondary);margin-top:4px;line-height:1.35;">${escapeHtml(issues)}</div>` : ''}
            </div>`;
        }));
        listEl.innerHTML = cards.join('');
    } catch (e) {
        listEl.innerHTML = '<div style="color:var(--danger);">Could not load health agents.</div>';
    }
}

// Refresh agents every 30s when console is open
setInterval(() => { if (document.getElementById('health-agents-list')) loadHealthAgents(); }, 30000);

async function loadScheduledCallbacks() {
    const listEl = document.getElementById('scheduled-callbacks-list');
    if (!listEl) return;
    try {
        const res = await fetch(apiUrl(`/api/callbacks?role=${apiRoleQ()}`), {
            headers: authHeaders(),
            credentials: 'same-origin',
        });
        if (res.status === 401 && typeof logout === 'function') { logout(); return; }
        if (!res.ok) {
            listEl.innerHTML = `<div style="text-align:center;padding:18px;color:var(--text-secondary);">Could not load callbacks.</div>`;
            return;
        }
        const data = await res.json().catch(() => ({}));
        const list = Array.isArray(data.callbacks) ? data.callbacks : [];
        window.lastLoadedCallbacks = list;
        
        const searchVal = (document.getElementById('callback-search-input')?.value || '').trim().toLowerCase();
        if (searchVal) {
            filterScheduledCallbacks();
        } else {
            renderCallbacksList(list);
        }
    } catch (e) {
        listEl.innerHTML = `<div style="text-align:center;padding:18px;color:var(--text-secondary);">Could not load callbacks.</div>`;
    }
}

async function reviewScheduledCallback(id, review) {
    try {
        const res = await fetch(apiUrl(`/api/callbacks/${id}/review?role=${encodeURIComponent(typeof currentRole !== 'undefined' ? currentRole : 'sales_1')}`), {
            method: 'PATCH',
            headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
            credentials: 'same-origin',
            body: JSON.stringify({ review: review }),
        });
        if (res.status === 401 && typeof logout === 'function') logout();
        if (!res.ok) {
            showToast(await parseApiErrorMessage(res), 'error');
            return;
        }
        const data = await res.json().catch(() => ({}));
        const updated = data.callback;
        if (updated && Array.isArray(window.lastLoadedCallbacks)) {
            window.lastLoadedCallbacks = window.lastLoadedCallbacks.map(function (cb) {
                return cb.id === id ? Object.assign({}, cb, updated) : cb;
            });
            filterScheduledCallbacks();
        } else {
            await loadScheduledCallbacks();
        }
        showToast(review === 'interested' ? 'Marked Right.' : 'Marked Wrong.', 'success');
    } catch (e) {
        showToast((e && e.message) ? e.message : 'Failed to update mark', 'error');
    }
}

async function cancelScheduledCallback(id) {
    if (!confirm('Cancel this scheduled callback?')) return;
    try {
        const res = await fetch(apiUrl(`/api/callbacks/${id}`), {
            method: 'DELETE',
            headers: authHeaders(),
            credentials: 'same-origin',
        });
        if (res.status === 401 && typeof logout === 'function') logout();
        if (!res.ok) {
            showToast(await parseApiErrorMessage(res), 'error');
            return;
        }
        showToast('Callback cancelled.', 'success');
        await loadScheduledCallbacks();
    } catch (e) {
        showToast((e && e.message) ? e.message : 'Failed to cancel callback', 'error');
    }
}

// ─── Campaign Start / Schedule Modal ───
function showCampaignStartModal() {
    var now = new Date();
    var pad = function (n) { return String(n).padStart(2, '0'); };
    var localStr = now.getFullYear() + '-' + pad(now.getMonth() + 1) + '-' + pad(now.getDate()) + 'T' + pad(now.getHours()) + ':' + pad(now.getMinutes());
    document.getElementById('campaign-start-datetime').value = localStr;
    document.getElementById('campaign-stop-datetime').value = '';
    document.getElementById('campaign-start-label').value = '';
    openModal('modal-campaign-start');
}

function startCampaignFromModal() {
    var label = document.getElementById('campaign-start-label').value.trim();
    closeModal('modal-campaign-start');
    if (label) {
        var nameEl = document.getElementById('schedule-name');
        if (nameEl) nameEl.value = label;
    }
    if (typeof startCampaign === 'function') startCampaign();
}

function scheduleCampaignFromModal() {
    var label = document.getElementById('campaign-start-label').value.trim();
    var startVal = document.getElementById('campaign-start-datetime').value;
    var stopVal = document.getElementById('campaign-stop-datetime').value;
    if (!startVal) { showToast('Please select a start time.', 'error'); return; }
    closeModal('modal-campaign-start');
    var nameEl = document.getElementById('schedule-name');
    var whenEl = document.getElementById('schedule-when');
    var stopEl = document.getElementById('schedule-stop');
    if (nameEl && label) nameEl.value = label;
    if (whenEl) whenEl.value = startVal;
    if (stopEl) stopEl.value = stopVal;
    if (typeof addSchedule === 'function') addSchedule();
    if (typeof showPageNav === 'function') showPageNav('campaigns', document.getElementById('nav-campaigns'));
}

// ─── Individual Lead Reschedule (from Suggested Action) ───
function openLeadReschedule(leadId) {
    showToast('Opening reschedule modal…', 'info');
    openRescheduleModal();
}

// ─── Reschedule Campaign Calls Modal ───
function openRescheduleModal() {
    const modal = document.getElementById('reschedule-modal');
    if (!modal) return;

    // Default dates: last 7 days to today
    const today = new Date();
    const lastWeek = new Date(today.getTime() - 7 * 24 * 60 * 60 * 1000);
    const fmtDate = (d) => d.toISOString().slice(0, 10);

    const fromEl = document.getElementById('reschedule-from-date');
    const toEl = document.getElementById('reschedule-to-date');
    const targetEl = document.getElementById('reschedule-target-datetime');

    if (fromEl && !fromEl.value) fromEl.value = fmtDate(lastWeek);
    if (toEl && !toEl.value) toEl.value = fmtDate(today);
    if (targetEl && !targetEl.value) {
        // Default to tomorrow 10:00 AM local
        const tomorrow = new Date(today.getTime() + 24 * 60 * 60 * 1000);
        tomorrow.setHours(10, 0, 0, 0);
        targetEl.value = tomorrow.toISOString().slice(0, 16);
    }

    modal.style.display = 'flex';
}

function closeRescheduleModal() {
    const modal = document.getElementById('reschedule-modal');
    if (modal) modal.style.display = 'none';
}

async function submitReschedule() {
    const fromDate = (document.getElementById('reschedule-from-date')?.value || '').trim();
    const toDate = (document.getElementById('reschedule-to-date')?.value || '').trim();
    const targetDatetime = (document.getElementById('reschedule-target-datetime')?.value || '').trim();
    const outcomeEls = document.querySelectorAll('input[name="reschedule-outcome"]:checked');
    const outcomes = Array.from(outcomeEls).map(el => el.value);

    if (!fromDate || !toDate) {
        showToast('Please select from and to dates.', 'error');
        return;
    }
    if (fromDate > toDate) {
        showToast('From date cannot be after to date.', 'error');
        return;
    }
    if (!targetDatetime) {
        showToast('Please select target date and time.', 'error');
        return;
    }
    if (!outcomes.length) {
        showToast('Please select at least one outcome.', 'error');
        return;
    }

    const btn = document.getElementById('reschedule-submit-btn');
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Rescheduling…';
    }

    try {
        const res = await fetch(apiUrl('/api/campaign/reschedule?role=' + apiRoleQ()), {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': 'Bearer ' + (token() || '')
            },
            body: JSON.stringify({
                from_date: fromDate,
                to_date: toDate,
                outcomes: outcomes,
                target_datetime: targetDatetime
            })
        });

        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            throw new Error(data.detail || data.message || 'Reschedule failed');
        }

        showToast(`Rescheduled ${data.rescheduled_count || 0} call(s).`, 'success');
        closeRescheduleModal();
        if (typeof syncCampaignState === 'function') syncCampaignState();
    } catch (err) {
        console.error(err);
        showToast(err.message || 'Reschedule failed', 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = 'Confirm Reschedule';
        }
    }
}

// ── Virtual Meet Reschedule ───────────────────────────────────────

function openVirtualMeetReschedule(leadId) {
    const dateStr = prompt('Enter new date for Virtual Meet (YYYY-MM-DD):');
    if (!dateStr || !dateStr.trim()) return;
    const timeStr = prompt('Enter new time (e.g. 10:30 AM or 14:30):');
    if (!timeStr || !timeStr.trim()) return;
    const notesStr = prompt('Any additional notes (optional):') || '';

    (async function () {
        const btn = document.getElementById('cdm-sa-action-buttons');
        try {
            const res = await fetch(apiUrl('/api/campaign/lead/' + leadId + '/virtual-meet/reschedule?role=' + apiRoleQ()), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token() },
                credentials: 'same-origin',
                body: JSON.stringify({ meet_date: dateStr.trim(), meet_time: timeStr.trim(), notes: notesStr.trim() }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.detail || data.message || 'Failed to reschedule');
            showToast('Virtual Meet ' + (data.action || 'rescheduled') + ' successfully.', 'success');
            if (typeof syncCampaignState === 'function') syncCampaignState();
        } catch (err) {
            console.error(err);
            showToast(err.message || 'Failed to reschedule Virtual Meet', 'error');
        }
    })();
}

// Close modal on backdrop click
window.addEventListener('click', function (e) {
    const modal = document.getElementById('reschedule-modal');
    if (modal && e.target === modal) closeRescheduleModal();
});

async function loadInboundInterest(scrollIntoView) {
    const listEl = document.getElementById('inbound-interest-list');
    const countEl = document.getElementById('inbound-interest-count');
    const statEl = document.getElementById('stat-inbound-interest');
    if (!listEl) return;
    try {
        const res = await fetch(apiUrl('/api/campaign/inbound-interest?role=' + apiRoleQ()), {
            headers: { Authorization: 'Bearer ' + token() },
            credentials: 'same-origin',
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || 'Failed to load inbound interest');
        const leads = data.leads || [];
        const count = Number(data.count) || leads.length;
        if (countEl) countEl.textContent = '(' + count + ')';
        if (statEl) statEl.textContent = count;
        if (!leads.length) {
            listEl.innerHTML = '<div style="text-align:center;padding:var(--space-xl);color:var(--text-secondary);">No inbound interest replies yet.</div>';
            return;
        }
        listEl.innerHTML = '<table style="width:100%;border-collapse:collapse;"><thead><tr style="background:var(--card-hover);">' +
            '<th style="padding:10px 14px;text-align:left;font-size:10px;text-transform:uppercase;">Lead</th>' +
            '<th style="padding:10px 14px;text-align:left;font-size:10px;text-transform:uppercase;">Phone</th>' +
            '<th style="padding:10px 14px;text-align:left;font-size:10px;text-transform:uppercase;">Source</th>' +
            '<th style="padding:10px 14px;text-align:left;font-size:10px;text-transform:uppercase;">Message</th>' +
            '<th style="padding:10px 14px;text-align:left;font-size:10px;text-transform:uppercase;">When</th>' +
            '</tr></thead><tbody>' +
            leads.map(function (l) {
                const rtype = (l.inbound_reply_type || '').toLowerCase();
                let src = (l.inbound_interest_source || 'whatsapp').replace('email_whatsapp', 'Email → WhatsApp');
                if (rtype === 'callback' || (l.inbound_interest_source || '').includes('callback')) {
                    src = 'Callback Request';
                } else if (rtype === 'interested') {
                    src = src.indexOf('Email') >= 0 ? src : 'Interested';
                }
                const badgeCls = rtype === 'callback' ? 'tag-cbk' : 'tag-int';
                const msg = escapeHtml((l.inbound_interest_message || '').substring(0, 120));
                const when = l.inbound_interest_at ? (typeof formatTimeIST === 'function' ? formatTimeIST(l.inbound_interest_at) : l.inbound_interest_at) : '—';
                const pname = escapeHtml(typeof leadContactPrimary === 'function' ? leadContactPrimary(l) : (l.name || '—'));
                return '<tr class="clickable-row" onclick="openCallDetail(' + Number(l.id) + ')" style="border-top:1px solid var(--border);cursor:pointer;">' +
                    '<td style="padding:10px 14px;font-weight:600;">' + pname + '</td>' +
                    '<td style="padding:10px 14px;font-family:var(--font-mono);font-size:12px;">' + escapeHtml(l.phone || '—') + '</td>' +
                    '<td style="padding:10px 14px;"><span class="badge-tag ' + badgeCls + '">' + escapeHtml(src) + '</span></td>' +
                    '<td style="padding:10px 14px;max-width:280px;font-size:12px;color:var(--text-secondary);">' + msg + '</td>' +
                    '<td style="padding:10px 14px;font-size:12px;">' + escapeHtml(when) + '</td></tr>';
            }).join('') +
            '</tbody></table>';
        if (scrollIntoView) {
            const panel = document.getElementById('inbound-interest-panel');
            if (panel) panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
    } catch (err) {
        console.error(err);
        listEl.innerHTML = '<div style="text-align:center;padding:var(--space-xl);color:var(--danger);">' + escapeHtml(err.message || 'Load failed') + '</div>';
    }
}

// Load inbound interest panel on dashboard init
document.addEventListener('DOMContentLoaded', function () {
    if (typeof loadInboundInterest === 'function') loadInboundInterest(false);
});

async function triggerRetryAllFailedCalls() {
    const confirmRetry = confirm('Are you sure you want to retry all failed/unanswered calls for this campaign immediately?');
    if (!confirmRetry) return;

    const btn = document.getElementById('stat-detail-retry-failed-btn');
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Queueing…';
    }

    try {
        const res = await fetch(apiUrl('/api/campaign/retry-failed?role=' + apiRoleQ()), {
            method: 'POST',
            headers: {
                'Authorization': 'Bearer ' + (token() || '')
            }
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            throw new Error(data.detail || data.message || 'Retry failed');
        }
        showToast(data.message || 'Successfully queued failed calls for redialing.', 'success');
        
        // Close modal
        const modalEl = document.getElementById('modal-stat-detail');
        if (modalEl) {
            modalEl.classList.remove('open');
            modalEl.classList.remove('active');
        }
        if (typeof closeModal === 'function') {
            closeModal('modal-stat-detail');
        }
        
        if (typeof syncCampaignState === 'function') syncCampaignState();
    } catch (err) {
        console.error(err);
        showToast(err.message || 'Failed to retry calls', 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = `
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" style="width:15px;height:15px;stroke-width:2;"><path d="M23 4v6h-6M1 20v-6h6M3.51 9a9 9 0 0114.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0020.49 15"/></svg>
                Redial All Failed Calls
            `;
        }
    }
}
