// ─── Global State ───
let currentRole = 'sales_1';
let currentSandboxFilter = 0; // 0=all, 1-4=sandbox filter
const CONSOLE_SWITCHABLE_ROLES = ['sales_1'];
let allLogs = [];
let currentFilter = 'all';
let syncInterval = null;
let campaignWorkerActive = false;
let _sseConnection = null;

function getLivePollMs() {
    if (typeof isLiveStreamHealthy === 'function' && isLiveStreamHealthy()) return 30000;
    if (typeof window.__livePollMs === 'number' && window.__livePollMs > 0) {
        return window.__livePollMs;
    }
    return (typeof campaignWorkerActive !== 'undefined' && campaignWorkerActive) ? 5000 : 8000;
}

function resetLivePollInterval() {
    if (syncInterval) clearInterval(syncInterval);
    syncInterval = setInterval(function () {
        if (typeof quietSyncState === 'function') quietSyncState();
    }, getLivePollMs());
}

// ─── Theme Toggle ───
function toggleTheme() {
  var html = document.documentElement;
  var isDark = html.getAttribute('data-theme') === 'dark';
  html.setAttribute('data-theme', isDark ? '' : 'dark');
  localStorage.setItem('de_theme', isDark ? 'light' : 'dark');
  updateThemeUI(!isDark);
}

function updateThemeUI(isDark) {
  var lbl = document.getElementById('theme-toggle-label');
  var path = document.getElementById('theme-icon-path');
  if (!lbl || !path) return;
  lbl.textContent = isDark ? 'Light Mode' : 'Dark Mode';
  path.setAttribute('d', isDark
    ? 'M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z'
    : 'M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z');
}

function clearSessionSnapshots() {
    try {
        CONSOLE_SWITCHABLE_ROLES.forEach(function(role) {
            sessionStorage.removeItem('vernika_dash_snap_' + role);
            sessionStorage.removeItem('vernika_dash_snap_v2_' + role);
            sessionStorage.removeItem('vernika_leads_snap_' + role);
            sessionStorage.removeItem('vernika_leads_snap_v2_' + role);
        });
    } catch (_) {}
}

function _clearRoleSessionSnapshots(role) {
    try {
        sessionStorage.removeItem('vernika_dash_snap_' + role);
        sessionStorage.removeItem('vernika_dash_snap_v2_' + role);
        sessionStorage.removeItem('vernika_leads_snap_' + role);
        sessionStorage.removeItem('vernika_leads_snap_v2_' + role);
    } catch (_) {}
}

function _loginRole() {
    if (typeof loginRoleFromToken === 'function') {
        const fromJwt = loginRoleFromToken();
        if (fromJwt) return fromJwt;
    }
    return normalizeRole(localStorage.getItem('vernika_role') || 'sales_1');
}

/** After login, align UI + storage with JWT so a stale ``vernika_role`` cannot hijack the dashboard. */
function _applyLockedLoginRole() {
    const sess = window.__VERN_SESSION__;
    if (sess && sess.dashboard_role && (!sess.can_switch_roles || sess.locked)) {
        currentRole = normalizeRole(sess.dashboard_role);
        localStorage.setItem('vernika_role', currentRole);
        return;
    }
    const locked =
        typeof loginRoleFromToken === 'function' ? loginRoleFromToken() : null;
    if (!locked || typeof LOCKED_CONSOLE_ROLES === 'undefined') return;
    if (!LOCKED_CONSOLE_ROLES.includes(locked)) return;
    currentRole = locked;
    localStorage.setItem('vernika_role', locked);
}

/** Role for Configuration / greeting capture APIs. */
function tuningRoleForApi() {
    return (typeof currentRole !== 'undefined' && isSandboxRole(currentRole)) ? currentRole : 'sales_1';
}

function _initialConsoleRole() {
    const jwtRole =
        typeof loginRoleFromToken === 'function' ? loginRoleFromToken() : null;
    if (jwtRole && isSandboxRole(jwtRole)) return jwtRole;
    const stored = normalizeRole(localStorage.getItem('vernika_role') || '');
    if (stored && isSandboxRole(stored)) return stored;
    return 'sales_1';
}

function _updateRoleToggleVisibility() {
    const sw = document.getElementById('role-switch');
    if (sw) sw.style.display = 'none';
}

function roleFriendlyName(role) {
    if (role === 'sales_1') return 'Vernika (Technopolis \u2013 Solitaire Unity)';
    return 'Technopolis';
}

function isChannelPartnerRole(role) {
    return false;
}

async function switchRole(role) {
    if (!CONSOLE_SWITCHABLE_ROLES.includes(role)) return;
    var prevRole = currentRole;
    currentRole = role;
    localStorage.setItem('vernika_role', role);
    updateRoleSwitchUI();
    updateRoleLabels();

    // Close old SSE connection for previous role
    if (_sseConnection) { _sseConnection.close(); _sseConnection = null; }

    // Only clear the OUTGOING role's session snapshots (preserve the target role's cache)
    if (typeof _clearRoleSessionSnapshots === 'function' && prevRole !== role) {
        _clearRoleSessionSnapshots(prevRole);
    }
    allLeads = [];
    if (typeof lastCampaignSnapshot !== 'undefined') lastCampaignSnapshot = null;

    // Reset dashboard stats immediately (no stale numbers)
    setCampaignTotalsIndeterminate(true);
    updateStat('stat-total', '0');
    updateStat('stat-called', '0');
    updateStat('stat-interested-count', '0');
    updateStat('stat-site-visit', '0');
    updateStat('stat-not-interested', '0');
    updateStat('stat-callbacks', '0');
    updateStat('stat-failed', '0');
    updateStat('stat-conversion-rate', '0%');
    updateStat('stat-attempts', '0');
    updateStat('camp-total', '0 leads');
    updatePct('pct-called', 0);
    updatePct('pct-interested', 0);
    updatePct('pct-site-visit', 0);
    updatePct('pct-not-interested', 0);
    updatePct('pct-callbacks', 0);
    updatePct('pct-failed', 0);
    setProgressWidth('bar-total', 0);
    setProgressWidth('bar-called', 0);
    setProgressWidth('bar-interested', 0);
    setProgressWidth('bar-site-visit', 0);
    setProgressWidth('bar-not-interested', 0);
    setProgressWidth('bar-callbacks', 0);
    setProgressWidth('bar-conversion', 0);
    setProgressWidth('bar-followups', 0);
    setProgressWidth('bar-failed', 0);
    updateCharts([], {}, {}, {}, {});
    updateStat('perf-avg-rating', '\u2014');
    updateStat('perf-total-called', '0');
    updateStat('perf-callback-rate', '0%');
    updateStat('perf-fail-rate', '0%');

    // Show loading in lead table
    var tbody = document.getElementById('manifest-tbody');
    if (tbody) tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:40px;color:var(--text-secondary);">Switching role\u2026</td></tr>';
    var cbody = document.getElementById('calls-tbody');
    if (cbody) cbody.innerHTML = '';

    // syncState() already loads /state + manifest — avoid duplicate 20k-row fetch
    try {
        if (typeof syncState === 'function') await syncState();
    } catch (_) {}
    if (typeof loadPhoneNumbers === 'function') loadPhoneNumbers();
    if (typeof loadTuning === 'function') loadTuning();


    // Connect live dashboard (WebSocket primary, SSE fallback)
    if (typeof connectLiveDashboard === 'function') {
        connectLiveDashboard();
    } else if (typeof connectSSE === 'function') {
        _sseConnection = connectSSE();
    }
    if (typeof startInboundDashboardSelfHeal === 'function') {
        startInboundDashboardSelfHeal();
    }
}

function updateRoleSwitchUI() {
    var friendly = roleFriendlyName(currentRole);
    var labels = document.querySelectorAll('#role-label-dash');
    for (var i = 0; i < labels.length; i++) {
        labels[i].textContent = friendly;
    }
}

function toggleDashRole() {
    switchRole('sales_1');
}

// ─── Date Filter Helpers (IST) ───
function getIstDateStr(d) {
    if (!d) d = new Date();
    return new Intl.DateTimeFormat('en-CA', {
        timeZone: 'Asia/Kolkata',
        year: 'numeric',
        month: '2-digit',
        day: '2-digit'
    }).format(d);
}

function setDefaultDateToToday() {
    // Don't set dates or activate filter — let the API serve all-data stats.
    // User can manually pick a date range if they want to filter.
    var fromEl = document.getElementById('filter-date-from');
    var toEl = document.getElementById('filter-date-to');
    if (fromEl) fromEl.value = '';
    if (toEl) toEl.value = '';
    var vizFrom = document.getElementById('viz-date-from');
    var vizTo = document.getElementById('viz-date-to');
    if (vizFrom) vizFrom.value = '';
    if (vizTo) vizTo.value = '';
    window.__vizDateFilterActive = false;
}

function syncDateInputsToAll(sourceFromId, sourceToId) {
    var fromVal = document.getElementById(sourceFromId)?.value || '';
    var toVal = document.getElementById(sourceToId)?.value || '';
    var ids = [
        ['filter-date-from', 'filter-date-to'],
        ['viz-date-from', 'viz-date-to']
    ];
    ids.forEach(function(pair) {
        var f = document.getElementById(pair[0]);
        var t = document.getElementById(pair[1]);
        if (f && f.id !== sourceFromId) f.value = fromVal;
        if (t && t.id !== sourceToId) t.value = toVal;
    });
}

function getSelectedDateRange() {
    var fromEl = document.getElementById('filter-date-from');
    var toEl = document.getElementById('filter-date-to');
    var fromStr = fromEl ? fromEl.value : '';
    var toStr = toEl ? toEl.value : '';
    if (!fromStr && !toStr) return null;
    
    // If only from is selected, default to to same date (single day filter)
    var effectiveTo = toStr;
    if (fromStr && !toStr) {
        effectiveTo = fromStr;
    }
    
    return {
        fromMs: fromStr ? _istDayStartMs(fromStr) : 0,
        toMs: effectiveTo ? _istDayEndMs(effectiveTo) : Infinity
    };
}

function isLeadInDateRange(l) {
    if (!l) return false;
    var range = getSelectedDateRange();
    if (!range) return true;
    
    var timestamps = [];
    
    // 1. Call attempt timestamps
    if (typeof getLeadTimestampMs === 'function') {
        var t1 = getLeadTimestampMs(l);
        if (Number.isFinite(t1)) timestamps.push(t1);
    }
    if (l.start_time) {
        var t2 = parseFloat(l.start_time) * 1000;
        if (Number.isFinite(t2)) timestamps.push(t2);
    }
    if (l.called_at_iso) {
        var t3 = new Date(l.called_at_iso).getTime();
        if (Number.isFinite(t3)) timestamps.push(t3);
    }
    if (l.created_at) {
        var cStr = String(l.created_at).trim();
        if (cStr && !cStr.includes('T')) cStr = cStr.replace(' ', 'T');
        if (cStr && !/[zZ]|[+-]\d{2}/.test(cStr)) cStr += '+05:30';
        var tc = new Date(cStr).getTime();
        if (Number.isFinite(tc) && !isNaN(tc)) timestamps.push(tc);
    }

    
    // 2. Callback / scheduled / retry timestamps
    if (l.callback_reminder_at_iso) {
        var c1 = new Date(l.callback_reminder_at_iso).getTime();
        if (Number.isFinite(c1)) timestamps.push(c1);
    }
    if (l.requested_callback_datetime_iso) {
        var c2 = new Date(l.requested_callback_datetime_iso).getTime();
        if (Number.isFinite(c2)) timestamps.push(c2);
    }
    if (l.analysis) {
        try {
            var aj = typeof l.analysis === 'string' ? JSON.parse(l.analysis) : l.analysis;
            if (aj) {
                if (aj.callback_reminder_epoch) {
                    var c3 = parseFloat(aj.callback_reminder_epoch) * 1000;
                    if (Number.isFinite(c3)) timestamps.push(c3);
                }
                if (aj.requested_callback_datetime_iso) {
                    var c4 = new Date(aj.requested_callback_datetime_iso).getTime();
                    if (Number.isFinite(c4)) timestamps.push(c4);
                }
            }
        } catch(e) {}
    }
    
    if (timestamps.length === 0) return false;
    
    for (var i = 0; i < timestamps.length; i++) {
        var ts = timestamps[i];
        if (ts >= range.fromMs && ts <= range.toMs) {
            return true;
        }
    }
    return false;
}

function getDateFilteredLeads(leads) {
    var range = getSelectedDateRange();
    if (!range) return leads || [];
    return (leads || []).filter(isLeadInDateRange);
}

function refreshAllDateScopedViews() {
    var fromEl = document.getElementById('filter-date-from') || document.getElementById('viz-date-from');
    var toEl = document.getElementById('filter-date-to') || document.getElementById('viz-date-to');
    var hasDate = (fromEl && fromEl.value) || (toEl && toEl.value);
    window.__vizDateFilterActive = !!hasDate;
    if (typeof applyVizDateFilter === 'function') applyVizDateFilter();
    if (window._lastApiData && typeof applyStateData === 'function') {
        applyStateData(window._lastApiData, null, {
            source: hasDate ? 'date-filter' : 'date-clear',
            silent: true,
        });
    } else if (typeof applyManifestDispositionStats === 'function') {
        applyManifestDispositionStats();
    }
    if (typeof renderCalls === 'function') renderCalls();
    if (typeof renderManifest === 'function') renderManifest();
}

function onDateFilterChange(sourceFromId) {
    if (sourceFromId === 'filter-date-from' || sourceFromId === 'filter-date-to') {
        syncDateInputsToAll('filter-date-from', 'filter-date-to');
    } else if (sourceFromId === 'viz-date-from' || sourceFromId === 'viz-date-to') {
        syncDateInputsToAll('viz-date-from', 'viz-date-to');
    }
    refreshAllDateScopedViews();
}

// ─── Stat Card Popups ───
function showStatPopup(type) {
    var dataset = typeof getLeadDatasetForFilters === 'function' ? getLeadDatasetForFilters() : allLeads;
    var filteredLeads = getDateFilteredLeads(dataset);
    var range = typeof getSelectedDateRange === 'function' ? getSelectedDateRange() : null;
    var called = filteredLeads.filter(function(l) { return isLeadCalledInDateRange(l, range); });

    var total = window.__vizDateFilterActive ? filteredLeads.length : (typeof getAuthoritativeLeadTotal === 'function' ? getAuthoritativeLeadTotal() : filteredLeads.length);
    var calledCount = called.length;

    var failed = 0;
    var noAnswer = 0;
    var busy = 0;
    var voicemail = 0;
    var noResponse = 0;
    var siteVisitCount = 0;
    var callbacks = 0;
    var notInterested = 0;
    var interested = 0;
    var plainAnswered = 0;

    called.forEach(function(l) {
        var d = (typeof effectiveDispo === 'function') ? effectiveDispo(l) : (l.disposition || '');
        var dl = d.trim().toLowerCase();
        var st = String(l.status || '').trim().toLowerCase();

        // 1. Failed / Error → count as No Response (dial / no-connect)
        if (st === 'failed' || st === 'error' || dl === 'failed') {
            noResponse++;
            return;
        }
        // 2. No Answer
        if (st === 'no answer' || st === 'no-answer' || dl === 'no answer' || dl === 'no-answer') {
            noAnswer++;
            return;
        }
        // 3. Busy
        if (st === 'busy' || dl === 'busy') {
            busy++;
            return;
        }
        // 4. Voice Mail
        if (dl === 'voicemail' || dl === 'voice mail' || dl.includes('voice mail') || dl.includes('voicemail')) {
            voicemail++;
            return;
        }
        // 5. No Response
        if (st === 'no response' || st === 'no_response' || dl === 'no response' || dl === 'no_response' || dl.includes('no response') || dl.includes('no_response')) {
            noResponse++;
            return;
        }

        // Connected/Answered Call partitioning:
        if (typeof hasSiteVisitWithParticularDate === 'function' && hasSiteVisitWithParticularDate(l)) {
            siteVisitCount++;
        } else if (typeof isFollowUpLead === 'function' && isFollowUpLead(l)) {
            callbacks++;
        } else if (d === 'Not Interested' || dl.includes('not interested')) {
            notInterested++;
        } else if (d === 'Interested' || dl.includes('interested')) {
            interested++;
        } else {
            plainAnswered++;
        }
    });

    var cb = (!window.__vizDateFilterActive && window._lastApiData && window._lastApiData.called_breakdown)
        ? window._lastApiData.called_breakdown : null;
    if (cb && typeof cb.total_called === 'number' && cb.total_called >= 0) {
        calledCount = cb.total_called;
        interested = Number(cb.interested) || 0;
        notInterested = Number(cb.not_interested) || 0;
        callbacks = Number(cb.callbacks) || 0;
        siteVisitCount = Number(cb.site_visit) || 0;
        plainAnswered = Number(cb.plain_answered) || 0;
        failed = Number(cb.failed) || 0;
        noAnswer = Number(cb.no_answer) || 0;
        busy = Number(cb.busy) || 0;
        voicemail = Number(cb.voicemail) || 0;
        noResponse = Number(cb.no_response) || 0;
    }

    var answered = plainAnswered + interested + notInterested + callbacks + siteVisitCount;
    var followUpCount = callbacks;
    var dc = {
        'Interested': interested,
        'Not Interested': notInterested,
        'Failed': failed,
        'No Answer': noAnswer,
        'Busy': busy,
        'Voicemail': voicemail,
        'Voice Mail': voicemail,
        'No Response': noResponse,
        'Answered': answered
    };
    // Other = any disposition not in a known bucket
    var knownSum = interested + notInterested + failed + noAnswer + busy + answered + callbacks;
    var other = Math.max(0, calledCount - knownSum);

    var sum = 0, cnt = 0;
    called.forEach(function(l) {
        var r = l.rating || null;
        if (r && r >= 1 && r <= 5) { sum += r; cnt++; }
    });
    var avgRating = cnt > 0 ? (sum / cnt).toFixed(1) : '—';
    var convRate = calledCount > 0 ? Math.round((interested / calledCount) * 100) : 0;
    var cbRate = calledCount > 0 ? Math.round((callbacks / calledCount) * 100) : 0;
    var failRate = calledCount > 0 ? Math.round((failed / calledCount) * 100) : 0;

    var details = {
        'total': {
            title: 'Total Leads',
            value: total.toLocaleString(),
            rows: [
                ['Total leads in database', total.toLocaleString()],
                ['Called', calledCount.toLocaleString()],
                ['Pending', (total - calledCount).toLocaleString()]
            ]
        },
        'called': {
            title: 'Calls Made',
            value: calledCount.toLocaleString(),
            rows: [
                ['Total called', calledCount.toLocaleString()],
                ['Answered', answered.toLocaleString()],
                ['No Answer', noAnswer.toLocaleString()],
                ['Busy', busy.toLocaleString()],
                ['Interested', interested.toLocaleString()],
                ['Not Interested', notInterested.toLocaleString()],
                ['Callbacks', callbacks.toLocaleString()],
                ['Failed Calls', failed.toLocaleString()],
                ['Conversion rate', convRate + '%']
            ]
        },
        'interested': {
            title: 'Interested Leads',
            value: interested,
            rows: [
                ['Interested', interested],
                ['Conversion rate', convRate + '%'],
                ['Out of ' + calledCount + ' calls', '']
            ]
        },
        'site-visit': {
            title: 'Site Visit',
            value: siteVisitCount,
            rows: [
                ['Site Visit', siteVisitCount],
                ['Out of ' + calledCount + ' calls', '']
            ]
        },
        'not-interested': {
            title: 'Not Interested',
            value: notInterested,
            rows: [
                ['Not Interested', notInterested],
                ['Out of ' + calledCount + ' calls', '']
            ]
        },
        'callback': {
            title: 'Inbound Calls',
            value: callbacks,
            rows: [
                ['Inbound Calls', callbacks],
                ['Callback rate', cbRate + '%'],
                ['Out of ' + calledCount + ' calls', '']
            ],
            _isInboundCallbacks: true
        },
        'conversion': {
            title: 'Conversion Rate',
            value: convRate + '%',
            rows: [
                ['Interested / Called', interested + ' / ' + calledCount],
                ['Conversion rate', convRate + '%']
            ]
        },
        'attempts': {
            title: 'Callbacks',
            value: followUpCount,
            rows: [
                ['Total callbacks', followUpCount.toLocaleString()],
                ['Answered', answered.toLocaleString()],
                ['Interested', interested.toLocaleString()],
                ['Callback rate', cbRate + '%']
            ]
        },
        'failed': {
            title: 'Failed Calls',
            value: failed,
            rows: [
                ['Failed Calls', failed],
                ['Fail rate', failRate + '%'],
                ['Out of ' + calledCount + ' calls', '']
            ]
        },
        'avg-rating': {
            title: 'Average Rating',
            value: avgRating + ' ★',
            rows: [
                ['Average rating', avgRating + ' ★'],
                ['Rated calls', cnt.toLocaleString()],
                ['Total called', calledCount.toLocaleString()],
                ['Rating distribution', '']
            ].concat((function() {
                var dist = [0,0,0,0,0];
                called.forEach(function(l) {
                    var r = l.rating || 0;
                    if (r >= 1 && r <= 5) dist[r-1]++;
                });
                return dist.map(function(n, i) {
                    return ['★'.repeat(i+1) + '☆'.repeat(4-i), String(n)];
                });
            })())
        },
        'total-called': {
            title: 'Total Called',
            value: calledCount.toLocaleString(),
            rows: (function() {
                var voicemail = Number(dc['Voicemail'] || dc['Voice Mail'] || 0);

                var noResponse = dc['No Response'] || dc['no_response'] || 0;
                var plainAnswered = answered - interested - notInterested - callbacks - siteVisitCount;
                if (plainAnswered < 0) plainAnswered = 0;
                return [
                    ['Total called', calledCount.toLocaleString()],
                    ['✅ Answered (picked up)', answered.toLocaleString()],
                    ['   ↳ Connected (no specific outcome)', plainAnswered.toLocaleString()],
                    ['   ↳ Interested', interested.toLocaleString()],
                    ['   ↳ Not Interested', notInterested.toLocaleString()],
                    ['   ↳ Callbacks', callbacks.toLocaleString()],
                    ['   ↳ Site Visit', siteVisitCount.toLocaleString()],
                    ['❓ No Response', noResponse.toLocaleString()],
                    ['🔊 Voice Mail', voicemail.toLocaleString()],
                    ['🚫 No Answer', noAnswer.toLocaleString()],
                    ['📞 Busy', busy.toLocaleString()],
                    ['⚠️ Failed Calls', failed.toLocaleString()]
                ];
            })()
        },
        'callback-rate': {
            title: 'Callback Rate',
            value: cbRate + '%',
            rows: [
                ['Callbacks', callbacks],
                ['Total called', calledCount],
                ['Callback rate', cbRate + '%']
            ]
        },
        'fail-rate': {
            title: 'Fail Rate',
            value: failRate + '%',
            rows: [
                ['Failed Calls', failed],
                ['Total called', calledCount],
                ['Fail rate', failRate + '%']
            ]
        },
        'whatsapp': {
            title: 'WhatsApp Sent',
            value: (typeof window._lastApiData !== 'undefined' && window._lastApiData) ? (Number(window._lastApiData.whatsapp_sent_count) || 0) : 0,
            rows: [['Leads with WhatsApp sent', 'See table below']],
            _filterType: 'whatsapp_sent'
        },
        'email': {
            title: 'Email Sent',
            value: (typeof window._lastApiData !== 'undefined' && window._lastApiData) ? (Number(window._lastApiData.email_sent_count) || 0) : 0,
            rows: [['Leads with email sent', 'See table below']],
            _filterType: 'email_sent'
        }
    };

    var info = details[type];
    if (!info) return;

    var rowsHtml = info.rows.map(function(r) {
        return '<div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border);">' +
            '<span style="color:var(--text-secondary);font-size:13px;">' + r[0] + '</span>' +
            '<span style="font-weight:600;font-size:13px;">' + r[1] + '</span>' +
            '</div>';
    }).join('');

    var existing = document.getElementById('stat-popup-overlay');
    if (existing) existing.remove();

    var overlay = document.createElement('div');
    overlay.id = 'stat-popup-overlay';
    overlay.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.4);z-index:9999;display:flex;align-items:center;justify-content:center;animation:fadeIn .2s ease;';
    overlay.onclick = function(e) { if (e.target === overlay) overlay.remove(); };

    overlay.innerHTML = '<div style="background:var(--card);border:1px solid var(--border);border-radius:var(--radius-lg);padding:24px;max-width:520px;width:90%;box-shadow:var(--shadow-lg);animation:fadeIn .2s ease;">' +
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">' +
            '<h3 style="margin:0;font-size:16px;font-weight:700;">' + info.title + '</h3>' +
            '<button onclick="document.getElementById(\'stat-popup-overlay\').remove()" style="background:none;border:none;cursor:pointer;font-size:18px;color:var(--text-secondary);padding:4px 8px;">&times;</button>' +
        '</div>' +
        '<div style="font-size:28px;font-weight:800;margin-bottom:16px;color:var(--accent);">' + info.value + '</div>' +
        '<div>' + rowsHtml + '</div>' +
        '<div id="stat-popup-leads-table"></div>' +
    '</div>';

    document.body.appendChild(overlay);

    if (type === 'callback') {
        _loadInboundCallbackLeads();
    } else if (info._filterType === 'whatsapp_sent' || info._filterType === 'email_sent') {
        _loadStatPopupLeads(info._filterType);
    }
}

function showInboundCallToast(message, kind) {
    var existing = document.getElementById('inbound-toast');
    if (existing) existing.remove();
    var el = document.createElement('div');
    el.id = 'inbound-toast';
    var bg = kind === 'incoming_call' ? '#2563eb'
        : kind === 'whatsapp_sent' ? '#0d9488'
        : kind === 'email_sent' ? '#7c3aed'
        : '#16a34a';
    el.style.cssText = 'position:fixed;top:20px;right:20px;z-index:10000;padding:14px 18px;border-radius:10px;background:' + bg + ';color:#fff;font-size:13px;font-weight:600;box-shadow:0 8px 24px rgba(0,0,0,.2);cursor:pointer;max-width:320px;';
    el.textContent = message;
    el.onclick = function () {
        if (kind === 'incoming_call' && typeof openStatDetail === 'function') openStatDetail('callbacks');
        else if (kind === 'whatsapp_sent' && typeof openStatDetail === 'function') openStatDetail('whatsapp_sent');
        else if (kind === 'email_sent' && typeof openStatDetail === 'function') openStatDetail('email_sent');
        else if (kind === 'inbound_interest' && typeof loadInboundInterest === 'function') loadInboundInterest(true);
        el.remove();
    };
    document.body.appendChild(el);
    setTimeout(function () { if (el.parentNode) el.remove(); }, 12000);
}

function _loadStatPopupLeads(filterType) {
    var tableEl = document.getElementById('stat-popup-leads-table');
    if (!tableEl) return;
    var leads = (typeof allLeads !== 'undefined' && allLeads.length) ? allLeads : [];
    var filtered = leads.filter(function (l) {
        if (filterType === 'whatsapp_sent') return !!(l.whatsapp_sent || (l.extra && String(l.extra).indexOf('whatsapp_sent') >= 0));
        if (filterType === 'email_sent') return !!(l.email_sent || (l.email && String(l.email).indexOf('@') >= 0 && l.email_sent));
        return false;
    });
    if (!filtered.length) {
        tableEl.innerHTML = '<div style="margin-top:12px;color:var(--text-secondary);font-size:12px;">No matching leads in current view. Load more leads or refresh.</div>';
        return;
    }
    var html = '<table style="width:100%;margin-top:12px;font-size:12px;border-collapse:collapse;"><thead><tr style="text-align:left;color:var(--text-secondary);"><th style="padding:6px;">Name</th><th>Phone</th><th>Status</th></tr></thead><tbody>';
    filtered.slice(0, 50).forEach(function (l) {
        var pname = (typeof leadContactPrimary === 'function') ? leadContactPrimary(l) : (l.name || '—');
        html += '<tr style="border-top:1px solid var(--border);cursor:pointer;" onclick="openCallDetail(' + Number(l.id) + ')"><td style="padding:8px;font-weight:600;">' + escapeHtml(pname) + '</td><td style="padding:8px;">' + escapeHtml(l.phone || '') + '</td><td style="padding:8px;">' + escapeHtml(l.status || '') + '</td></tr>';
    });
    html += '</tbody></table>';
    tableEl.innerHTML = html;
}

async function _loadInboundCallbackLeads() {
    var tableEl = document.getElementById('stat-popup-leads-table');
    if (!tableEl) return;
    tableEl.innerHTML = '<div style="text-align:center;color:var(--text-secondary);padding:12px;font-size:13px;">Loading incoming calls…</div>';
    try {
        var roleQ = typeof apiRoleQ === 'function' ? apiRoleQ() : encodeURIComponent(currentRole || 'sales_1');
        var res = await fetch(apiUrl('/api/incoming/calls/recent?role=' + roleQ + '&limit=50'), {
            headers: authHeaders(),
            credentials: 'same-origin',
        });
        if (!res.ok) throw new Error('Failed to load');
        var data = await res.json();
        var items = data.items || [];
        if (!items.length) {
            tableEl.innerHTML = '<div style="text-align:center;color:var(--text-secondary);padding:20px;font-size:13px;">No incoming calls recorded yet.</div>';
            return;
        }
        var html = '<table style="width:100%;border-collapse:collapse;margin-top:12px;font-size:12px;">' +
            '<thead><tr style="border-bottom:2px solid var(--border);text-align:left;">' +
            '<th style="padding:8px 6px;font-weight:700;color:var(--text-secondary);">NAME</th>' +
            '<th style="padding:8px 6px;font-weight:700;color:var(--text-secondary);">PHONE</th>' +
            '<th style="padding:8px 6px;font-weight:700;color:var(--text-secondary);">LOCATION</th>' +
            '<th style="padding:8px 6px;font-weight:700;color:var(--text-secondary);">BUDGET</th>' +
            '<th style="padding:8px 6px;font-weight:700;color:var(--text-secondary);">STATUS</th>' +
            '<th style="padding:8px 6px;font-weight:700;color:var(--text-secondary);">SUMMARY</th>' +
            '</tr></thead><tbody>';
        items.forEach(function(r) {
            var name = escapeHtml(r.callee_name || r.lead_name || '—');
            var phone = escapeHtml(r.from_phone || r.to_phone || '');
            var loc = escapeHtml(r.preferred_location || '—');
            var budget = escapeHtml(r.preferred_budget || '—');
            var status = escapeHtml(r.status || '—');
            var sum = escapeHtml((r.summary || '').slice(0, 60) + ((r.summary || '').length > 60 ? '…' : ''));
            html += '<tr style="border-bottom:1px solid var(--border);cursor:pointer;" onclick="document.getElementById(\'stat-popup-overlay\').remove();viewIncomingCallOutcome(' + escapeHtml(r.id) + ');">' +
                '<td style="padding:8px 6px;font-weight:600;">' + name + '</td>' +
                '<td style="padding:8px 6px;color:var(--text-secondary);">' + phone + '</td>' +
                '<td style="padding:8px 6px;">' + loc + '</td>' +
                '<td style="padding:8px 6px;">' + budget + '</td>' +
                '<td style="padding:8px 6px;">' + status + '</td>' +
                '<td style="padding:8px 6px;color:var(--text-secondary);">' + sum + '</td>' +
                '</tr>';
        });
        html += '</tbody></table>';
        tableEl.innerHTML = html;
        var overlay = document.getElementById('stat-popup-overlay');
        if (overlay) {
            var valEl = overlay.querySelector('[style*="font-size:28px"]');
            if (valEl) valEl.textContent = items.length;
        }
    } catch (e) {
        tableEl.innerHTML = '<div style="text-align:center;color:var(--text-secondary);padding:12px;font-size:13px;">Could not load incoming calls.</div>';
    }
}

// ─── Initialization ───
document.addEventListener('DOMContentLoaded', () => {
    if (!token()) { window.location.href = '/login'; return; }

    (async function initConsole() {
        try {
            if (typeof bootstrapConsoleSession === 'function') {
                await bootstrapConsoleSession();
            }
        } catch (err) {
            console.error('Session bootstrap failed:', err);
            _applyLockedLoginRole();
            currentRole = _initialConsoleRole();
        }
        _applyLockedLoginRole();
        if (!window.__VERN_SESSION__) {
            currentRole = _initialConsoleRole();
            _applyLockedLoginRole();
        }
        if (!currentRole) {
            currentRole = 'sales_1';
            localStorage.setItem('vernika_role', currentRole);
        }

        updateRoleLabels();
        updateRoleSwitchUI();
        _updateRoleToggleVisibility();
        if (typeof loadPhoneNumbers === 'function') loadPhoneNumbers();

        var savedTheme = localStorage.getItem('de_theme') || 'light';
        document.documentElement.setAttribute('data-theme', savedTheme === 'dark' ? 'dark' : '');
        updateThemeUI(savedTheme === 'dark');

        if (typeof restoreDashboardSnapshotFromSession === 'function') {
            restoreDashboardSnapshotFromSession();
        }
        if (typeof restoreLeadTablesFromSession === 'function') {
            restoreLeadTablesFromSession();
        }

        if (typeof setDefaultDateToToday === 'function') {
            setDefaultDateToToday();
        }

        try {
            initCharts();
        } catch (err) {
            console.error('Chart init failed — stats will still load:', err);
            if (typeof showToast === 'function') {
                showToast('Charts failed to load (check network/CDN). KPI numbers will still sync.', 'warning', 6500);
            }
        }

        // Real data will be loaded by syncState(); keep tables empty until then
        // so the UI never shows fabricated leads.
        if (typeof renderCalls === 'function') renderCalls();
        if (typeof renderManifest === 'function') renderManifest();

        loadTuning();
        loadCases();
        _initScheduleDefaults();
        loadSchedules();
        _initScheduledCallbackDefaults();
        loadScheduledCallbacks();
        if (typeof loadHealthAgents === 'function') loadHealthAgents();

        syncState();
        resetLivePollInterval();
        // Poll lightweight endpoints less aggressively — WS/SSE pushes lead state changes
        setInterval(loadSchedules, 60000);
        setInterval(loadScheduledCallbacks, 60000);
        setInterval(loadPhoneNumbers, 60000);
        if (typeof connectLiveDashboard === 'function') {
            setTimeout(function () { connectLiveDashboard(); }, 200);
        } else if (typeof connectSSE === 'function') {
            setTimeout(function () { _sseConnection = connectSSE(); }, 200);
        }
        if (typeof startInboundDashboardSelfHeal === 'function') {
            startInboundDashboardSelfHeal();
        }


        const settingsUrlEl = document.getElementById('settings-url');
        if (settingsUrlEl) {
            settingsUrlEl.textContent = window.location.origin;
        }

        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') closeMobileSidebar();
        });
        window.addEventListener('resize', () => {
            if (window.innerWidth > 900) closeMobileSidebar();
        });
    })();
});

// ─── Sidebar & Navigation ───
/** Scroll to greeting + prerecord controls (Configuration tab). */
function openPreRecordSetup() {
    showPageNav('tuning', document.getElementById('nav-tuning'));
    closeMobileSidebar();
    setTimeout(() => {
        const g = document.getElementById('tuning-greeting');
        const card = document.getElementById('tuning-greeting-card');
        (card || g)?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }, 80);
}

function closeMobileSidebar() {
    const shell = document.getElementById('app-shell');
    const backdrop = document.getElementById('sidebar-backdrop');
    if (shell) shell.classList.remove('sidebar-open');
    if (backdrop) backdrop.classList.remove('visible');
    document.body.classList.remove('nav-drawer-open');
}

function toggleMobileSidebar(ev) {
    if (ev) ev.stopPropagation();
    const shell = document.getElementById('app-shell');
    if (!shell) return;
    const open = !shell.classList.contains('sidebar-open');
    shell.classList.toggle('sidebar-open', open);
    const backdrop = document.getElementById('sidebar-backdrop');
    if (backdrop) backdrop.classList.toggle('visible', open);
    document.body.classList.toggle('nav-drawer-open', open);
}

function showPageNav(pageId, navEl) {
    showPage(pageId, navEl);
    closeMobileSidebar();
    if (pageId === 'agents') loadAgents();
}

function showPage(pageId, navEl) {
    document.querySelectorAll('.page-section').forEach(s => s.classList.remove('active'));
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    const el = document.getElementById('page-' + pageId);
    if (el) el.classList.add('active');
    if (navEl) navEl.classList.add('active');
    if (pageId === 'manual') {
        if (typeof loadRecentManualCalls === 'function') loadRecentManualCalls();
        if (typeof loadRecentIncomingCalls === 'function') loadRecentIncomingCalls();
    }
    if (pageId === 'tuning') {
        if (typeof loadTuning === 'function') loadTuning();
    }
    if (pageId === 'whatsapp') {
        if (typeof loadWhatsAppStatus === 'function') loadWhatsAppStatus();
    }
}

// ─── WhatsApp (OpenWA gateway) ───
async function loadWhatsAppStatus() {
    const placeholder = document.getElementById('wa-status-placeholder');
    const body = document.getElementById('wa-status-body');
    const qrImg = document.getElementById('wa-qr-img');
    const qrPlaceholder = document.getElementById('wa-qr-placeholder');
    const qrWrap = document.getElementById('wa-qr-wrap');
    if (!placeholder || !body) return;
    try {
        const res = await fetch(apiUrl('/api/whatsapp/proxy/status'), { headers: authHeaders(), credentials: 'same-origin' });
        const s = await res.json();
        placeholder.style.display = 'none';
        body.style.display = 'block';
        let html = '';
        if (!s.enabled) {
            html = statusRow('OpenWA gateway', 'Disabled', 'red');
        } else if (s.error) {
            html = statusRow('OpenWA gateway', s.error, 'red');
        } else if (s.connected || s.authenticated) {
            html = statusRow('Status', 'Connected', 'green');
            if (s.phone) html += statusRow('Phone', escapeHtml(s.phone), '');
            if (s.pushname) html += statusRow('Name', escapeHtml(s.pushname), '');
        } else if (s.has_qr) {
            html = statusRow('Status', 'QR ready — scan below', 'amber');
        } else {
            html = statusRow('Status', 'Not linked', 'red');
        }
        body.innerHTML = html + '<div style="font-size:12px;color:var(--text-tertiary);margin-top:10px;">Gateway: OpenWA · auto-refreshes every 10s.</div>';

        const showQr = s.enabled && !s.connected && !s.authenticated;
        if (qrImg && qrPlaceholder && qrWrap) {
            qrImg.style.display = 'none';
            qrImg.src = '';
            if (showQr) {
                const qrRes = await fetch(apiUrl('/api/whatsapp/proxy/qr'), { headers: authHeaders(), credentials: 'same-origin' });
                if (qrRes.ok) {
                    const blob = await qrRes.blob();
                    qrImg.src = URL.createObjectURL(blob);
                    qrImg.style.display = 'block';
                    qrPlaceholder.style.display = 'none';
                } else {
                    qrPlaceholder.textContent = 'QR not available yet.';
                    qrPlaceholder.style.display = 'block';
                }
            } else {
                qrPlaceholder.textContent = s.connected ? 'Linked &amp; ready — no QR needed.' : 'Waiting for QR…';
                qrPlaceholder.style.display = 'block';
            }
        }
    } catch (e) {
        placeholder.style.display = 'none';
        body.style.display = 'block';
        body.innerHTML = statusRow('OpenWA gateway', 'Could not reach gateway.', 'red');
    }
}

function statusRow(label, value, tone) {
    const color = tone === 'green' ? '#16a34a' : tone === 'red' ? '#dc2626' : tone === 'amber' ? '#ca8a04' : 'var(--text)';
    return '<div style="display:flex;justify-content:space-between;gap:10px;padding:10px 0;border-bottom:1px solid var(--border);font-size:13px;">' +
        '<span style="color:var(--text-secondary);">' + escapeHtml(label) + '</span>' +
        '<span style="font-weight:700;color:' + color + ';">' + escapeHtml(value) + '</span></div>';
}

// Auto-refresh WhatsApp status only while its page is visible.
setInterval(function () {
    const page = document.getElementById('page-whatsapp');
    if (page && page.classList.contains('active') && typeof loadWhatsAppStatus === 'function') {
        loadWhatsAppStatus();
    }
}, 10000);

function updateRoleLabels() {
    var friendly = roleFriendlyName(currentRole);

    const elIds = ['role-label-dash', 'tuning-role-label', 'test-role-label', 'manual-role-label', 'cases-role-label'];
    elIds.forEach(id => {
        const el = document.getElementById(id);
        if (el) el.textContent = friendly;
    });

    const badge = document.getElementById('role-badge');
    const sess = window.__VERN_SESSION__;
    if (badge) {
        badge.innerHTML = '<span style="font-size:10px;opacity:0.7;">Technopolis</span>';
        if (sess && sess.email) badge.title = sess.email;
    }

    updateRoleSwitchUI();

    const agentsNav = document.getElementById('nav-agents');
    if (agentsNav) {
        agentsNav.style.display = 'none';
    }
}

function logout() {
    if (_sseConnection) _sseConnection.close();
    if (syncInterval) clearInterval(syncInterval);
    localStorage.removeItem('vernika_token');
    localStorage.removeItem('vernika_role');
    window.location.href = '/login';
}

function openModal(id) {
    const el = document.getElementById(id);
    if (el) {
        el.classList.add('active');
        el.classList.add('open');
    }
}
function closeModal(id) {
    const el = document.getElementById(id);
    if (el) {
        el.classList.remove('active');
        el.classList.remove('open');
    }
}

async function uploadRagDocs(files) {
    if (!files || files.length === 0) return;
    const statusEl = document.getElementById('rag-status-label');
    if (statusEl) {
        statusEl.textContent = 'Uploading…';
        statusEl.style.color = 'var(--text-secondary)';
    }
    const roleQ = typeof apiRoleQ === 'function' ? apiRoleQ() : encodeURIComponent(currentRole || 'sales_1');
    for (const file of files) {
        const form = new FormData();
        form.append('file', file);
        try {
            const res = await fetch(apiUrl(`/api/tuning/upload-doc?role=${roleQ}`), {
                method: 'POST',
                headers: { 'Authorization': `Bearer ${token()}` },
                credentials: 'same-origin',
                body: form,
            });
            if (!res.ok) {
                const err = await res.text();
                showToast(`Upload failed for ${file.name}: ${err}`, 'error', 5000);
                continue;
            }
            showToast(`Uploaded ${file.name}`, 'success', 3000);
        } catch (e) {
            showToast(`Upload error for ${file.name}: ${e.message}`, 'error', 5000);
        }
    }
    if (typeof loadTuning === 'function') await loadTuning();
    if (statusEl) {
        statusEl.textContent = 'READY';
        statusEl.style.color = 'var(--primary)';
    }
}

(function patchCaseModalOpen() {
    const base = typeof openModal === 'function' ? openModal : null;
    if (!base) return;
    window.openModal = function patchedOpenModal(id) {
        if (id === 'modal-case') {
            const hid = document.getElementById('case-edit-id');
            if (hid && !String(hid.value || '').trim()) {
                try {
                    _resetCaseModal();
                } catch (_) {}
            }
        }
        return base(id);
    };
})();

// ─── Sandbox Filter ───
// Set by sandbox tab clicks in the console. 0 = show all sandboxes.
function setSandboxFilter(sandbox) {
    currentSandboxFilter = (sandbox || 0);
    // Refresh the lead manifest with the new sandbox filter
    if (typeof ensureFullLeadManifest === 'function') {
        ensureFullLeadManifest();
    } else if (typeof refreshCampaignManifest === 'function') {
        refreshCampaignManifest();
    }
    // Also refresh the state/KPIs
    if (typeof loadCampaignState === 'function') {
        loadCampaignState();
    }
}
