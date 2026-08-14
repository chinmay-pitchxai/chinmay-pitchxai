// ─── Campaign State Sync ───
let campaignStateFetchWarned = false;
let _campaignSyncGen = 0;
/** Inbound calls for dashboard filter + SSE refresh. */
var incomingCallsList = [];

async function fetchIncomingCallsForDashboard() {
    try {
        var res = await fetch(apiUrl('/api/incoming/calls/recent?role=' + apiRoleQ() + '&limit=500'), {
            headers: authHeaders(),
            credentials: 'same-origin',
        });
        if (!res.ok) return;
        var data = await res.json();
        var items = data.items || [];
        incomingCallsList = items.map(function (c) {
            var st = 0;
            if (c.started_at) {
                var raw = String(c.started_at);
                var d = new Date(raw.indexOf('T') >= 0 ? raw : raw.replace(' ', 'T') + 'Z');
                if (!isNaN(d.getTime())) st = d.getTime() / 1000;
            }
            var rating = 0;
            try {
                var aj = typeof c.analysis_json === 'string' ? JSON.parse(c.analysis_json) : (c.analysis_json || c.analysis || {});
                rating = aj.rating || 0;
            } catch (e) { /* ignore */ }
            return {
                id: c.id,
                caller_name: c.caller_name,
                callee_name: c.caller_name,
                from_phone: c.from_phone,
                status: c.status,
                summary: c.summary || '',
                log_id: c.log_id || '',
                recording_available: !!c.recording_available,
                start_time: st,
                started_at: c.started_at,
                rating: rating,
                camp_id: c.camp_id,
            };
        });
    } catch (e) {
        console.warn('fetchIncomingCallsForDashboard failed', e);
    }
}
/** Last successful dashboard snapshot for replay when a refresh fails (avoids flashing all zeros). */
let lastCampaignSnapshot = null;

/** Initial table preview — KPI totals come from /api/campaign/state, not row count. */
const DEFAULT_MANIFEST_FETCH_LIMIT = 2500;
const MANIFEST_KPI_COUNT_LIMIT = 2500;

/** Server-side chart aggregates (full outbound cohort, not the chart row sample). */
function buildChartExtrasFromState(data) {
    data = data || {};
    return {
        calledCount: typeof data.called_count === 'number' ? data.called_count : null,
        calledBreakdown: data.called_breakdown || null,
        progressCounts: data.progress_counts || null,
        weekdayCounts: data.weekday_counts || null,
        hourlyCounts: Array.isArray(data.hourly_counts) && data.hourly_counts.length === 24 ? data.hourly_counts : null,
        dispositionCounts: data.disposition_counts || {},
    };
}

/** Reflect ``campaign_hours`` from ``/api/campaign/state`` (quiet-hours hard block). */
function applyCampaignHoursUI(hours) {
    const banner = document.getElementById('campaign-quiet-hours-banner');
    const btnStart = document.getElementById('btn-start');
    if (!hours || !hours.enabled) {
        if (banner) banner.style.display = 'none';
        if (btnStart) {
            btnStart.disabled = false;
            btnStart.removeAttribute('title');
        }
        return;
    }
    const blocked = !!hours.in_quiet_hours;
    if (banner) {
        if (blocked) {
            const msg = hours.block_message
                || ('Calling allowed ' + (hours.allowed_start || '') + '–' + (hours.allowed_end || '') + ' ' + (hours.tz || ''));
            banner.textContent = msg + (hours.local_time ? ' (now ' + hours.local_time + ').' : '.');
            banner.style.display = 'block';
        } else {
            banner.style.display = 'none';
        }
    }
    if (btnStart) {
        btnStart.disabled = blocked;
        if (blocked) {
            btnStart.setAttribute('title', hours.block_message || 'Outside calling hours');
        } else {
            btnStart.removeAttribute('title');
        }
    }
}

function applyCampaignPausedUI(paused) {
    const banner = document.getElementById('campaign-quiet-hours-banner');
    if (!paused) return;
    if (banner) {
        banner.style.display = 'block';
        banner.textContent =
            'Campaign is paused — outbound dialing is off. Re-analyze and the lead list still work; no new calls will be placed until you Start during calling hours (9:30 AM – 7:30 PM IST).';
    }
    const btnStart = document.getElementById('btn-start');
    if (btnStart) {
        btnStart.disabled = true;
        btnStart.setAttribute('title', 'Campaign paused');
    }
}

const LEAD_SESSION_KEY_PREFIX = 'vernika_leads_snap_v2_';
const DASH_SNAP_KEY_PREFIX = 'vernika_dash_snap_v2_';
const LEAD_SESSION_MAX_ROWS = 3500;
/** Stay under typical ~5 MB ``sessionStorage`` limits. */
const LEAD_SESSION_MAX_CHARS = 4_200_000;

function slimLeadForSession(l) {
    if (!l || l.id == null) return null;
    return {
        id: l.id,
        role: l.role,
        name: l.name,
        phone: l.phone,
        company: l.company,
        email: l.email,
        status: l.status,
        disposition: l.disposition,
        summary: l.summary,
        rating: l.rating,
        start_time: l.start_time,
        called_at_iso: l.called_at_iso,
        _log_id: l._log_id,
        log_id: l.log_id,
        recording_available: l.recording_available,
        recording_url: l.recording_url,
        transcript_url: l.transcript_url,
        outcome_from_transcript: l.outcome_from_transcript,
        contact_display_primary: l.contact_display_primary,
        contact_display_secondary: l.contact_display_secondary,
        failure_title: l.failure_title,
        failure_detail: l.failure_detail,
        failure_reason: l.failure_reason,
        failure_severity: l.failure_severity,
        next_steps: l.next_steps,
        emotion_label: l.emotion_label,
        emotion_rationale: l.emotion_rationale,
        emotion_confidence: l.emotion_confidence,
        callback_reminder_at_iso: l.callback_reminder_at_iso,
        next_action: l.next_action || null,
        preferred_location: l.preferred_location || null,
        preferred_budget: l.preferred_budget || null,
        extra: l.extra || null,
    };
}

function persistLeadTablesToSession() {
    try {
        const role = typeof currentRole !== 'undefined' ? currentRole : 'sales_1';
        if (!Array.isArray(allLeads) || !allLeads.length) {
            sessionStorage.removeItem(LEAD_SESSION_KEY_PREFIX + role);
            return;
        }
        let slice = allLeads.map(slimLeadForSession).filter(Boolean).slice(0, LEAD_SESSION_MAX_ROWS);
        let json = null;
        while (slice.length > 0) {
            json = JSON.stringify({ v: 2, ts: Date.now(), leads: slice });
            if (json.length <= LEAD_SESSION_MAX_CHARS || slice.length <= 1) break;
            slice = slice.slice(0, Math.max(1, Math.floor(slice.length * 0.7)));
        }
        if (!json || json.length > LEAD_SESSION_MAX_CHARS) return;
        sessionStorage.setItem(LEAD_SESSION_KEY_PREFIX + role, json);
    } catch (_) {}
}

/** Hydrate Lead Manifest + Recent Calls from cache so a hard refresh does not wipe tables. */
function restoreLeadTablesFromSession() {
    try {
        const role = typeof currentRole !== 'undefined' ? currentRole : 'sales_1';
        const raw = sessionStorage.getItem(LEAD_SESSION_KEY_PREFIX + role);
        if (!raw) return false;
        const o = JSON.parse(raw);
        if (!o || !Array.isArray(o.leads) || !o.leads.length) return false;
        allLeads = o.leads;
        if (typeof renderManifest === 'function') renderManifest();
        if (typeof renderCalls === 'function') renderCalls();
        return true;
    } catch (_) {
        return false;
    }
}

function setCampaignTotalsIndeterminate(busy) {
    const dash = '\u2013'; // en dash — reads as "waiting", not zero
    if (busy) {
        updateStat('stat-total', dash);
        updateStat('stat-called', dash);
        updateStat('stat-interested-count', dash);
        updateStat('stat-not-interested', dash);
        updateStat('stat-callbacks', dash);
        updateStat('stat-failed', dash);
        updateStat('stat-attempts', dash);
        updateStat('camp-total', 'Loading…');
        updateStat('perf-avg-rating', dash);
        updateStat('perf-total-called', dash);
        updateStat('perf-callback-rate', dash);
        updateStat('perf-fail-rate', dash);
        const bar = document.getElementById('progress-bar');
        if (bar) {
            bar.classList.add('vern-progress-indeterminate');
            bar.parentElement?.classList.add('vern-loading-pulse');
        }
        return;
    }
    const bar = document.getElementById('progress-bar');
    if (bar) {
        bar.classList.remove('vern-progress-indeterminate');
        bar.parentElement?.classList.remove('vern-loading-pulse');
    }
}

/** Lead Manifest skeleton (spinner row). Caller must eventually call ``renderManifest`` or overwrite tbody. */
function showLeadManifestSkeleton(message, opts) {
    const tb = document.getElementById('manifest-tbody');
    if (!tb) return;
    const spinner = !(opts && opts.spinner === false);
    const m = escapeHtml(message || 'Loading leads…');
    const spinHtml = spinner
        ? '<span class="vern-campaign-spinner"></span>'
        : '';
    tb.innerHTML = '<tr><td colspan="7" style="padding:42px;">'
        + '<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;color:var(--text-secondary);">'
        + spinHtml
        + '<span style="font-size:13px;font-weight:600;text-align:center;max-width:320px;line-height:1.4;">' + m + '</span>'
        + '</div></td></tr>';
}

function stashCampaignSnapshot(patch) {
    lastCampaignSnapshot = Object.assign({}, lastCampaignSnapshot || {}, patch);
    try {
        const role = typeof currentRole !== 'undefined' ? currentRole : 'sales_1';
        const s = lastCampaignSnapshot;
        if (!s || !s.texts) return;
        const slim = {
            texts: s.texts,
            progressPct: s.progressPct,
            disposition_counts: s.disposition_counts || {},
            callback_dates: s.callback_dates || {},
            timeline_week_labels: s.timeline_week_labels,
            timeline_total_calls: s.timeline_total_calls,
            timeline_interested: s.timeline_interested,
            timeline_dates_iso: s.timeline_dates_iso,
            workerActive: s.workerActive,
            activeCalls: s.activeCalls,
        };
        sessionStorage.setItem(DASH_SNAP_KEY_PREFIX + role, JSON.stringify(slim));
    } catch (_) {}
}

/** After hard refresh, paint last known stats/charts from sessionStorage until /state returns. */
function restoreDashboardSnapshotFromSession() {
    try {
        const role = typeof currentRole !== 'undefined' ? currentRole : 'sales_1';
        const raw = sessionStorage.getItem(DASH_SNAP_KEY_PREFIX + role);
        if (!raw) return;
        const slim = JSON.parse(raw);
        if (!slim || !slim.texts) return;
        lastCampaignSnapshot = Object.assign({}, slim, { chartSample: [] });
        replayLastCampaignSnapshot();
    } catch (_) {}
}

function replayLastCampaignSnapshot() {
    if (!lastCampaignSnapshot || !lastCampaignSnapshot.texts) return false;
    const s = lastCampaignSnapshot;
    campaignWorkerActive = !!s.workerActive;
    Object.keys(s.texts).forEach(function (id) {
        updateStat(id, s.texts[id]);
    });
    const bar = document.getElementById('progress-bar');
    if (bar && typeof s.progressPct === 'number') {
        bar.style.width = s.progressPct + '%';
    }
    const dot = document.getElementById('active-dot');
    const sample = Array.isArray(s.chartSample) ? s.chartSample : [];
    updateCharts(sample, s.disposition_counts || {}, s.callback_dates || {}, {
        timeline_week_labels: s.timeline_week_labels,
        timeline_total_calls: s.timeline_total_calls,
        timeline_interested: s.timeline_interested,
        timeline_dates_iso: s.timeline_dates_iso,
    }, {
        calledCount: s.texts && s.texts['stat-called'] ? parseInt(String(s.texts['stat-called']).replace(/,/g, ''), 10) : null,
        progressCounts: s.progress_counts,
        weekdayCounts: s.weekday_counts,
        dispositionCounts: s.disposition_counts || {},
    });
    updateCampaignRunnerChrome();
    return true;
}

/** Full rows for Lead Manifest + call list live here (small JSON). ``/state`` only carries counts + chart sample. */
async function refreshCampaignManifest(opts) {
    opts = opts || {};
    const staleVisibleKeep = !!(opts.keepStaleVisible && Array.isArray(allLeads) && allLeads.length > 0);
    try {
        if (typeof fetchIncomingCallsForDashboard === 'function') {
            await fetchIncomingCallsForDashboard();
        }
        if (!staleVisibleKeep) {
            showLeadManifestSkeleton('Loading lead preview…');
        }

        const rawLimit = opts.limit != null ? Number(opts.limit) : (
            typeof window.__VERN_MANIFEST_FETCH_LIMIT !== 'undefined' && window.__VERN_MANIFEST_FETCH_LIMIT != null
                ? Number(window.__VERN_MANIFEST_FETCH_LIMIT)
                : DEFAULT_MANIFEST_FETCH_LIMIT
        );
        const ml = Number.isFinite(rawLimit) ? Math.min(70000, Math.max(50, Math.floor(rawLimit))) : DEFAULT_MANIFEST_FETCH_LIMIT;
        const manifestLimit = typeof calledManifestLimitHint === 'function'
            ? calledManifestLimitHint(window._lastApiData || ml)
            : ml;
        const manifestUrl = typeof buildCalledManifestUrl === 'function'
            ? buildCalledManifestUrl(manifestLimit)
            : apiUrl(`/api/campaign/manifest?role=${apiRoleQ()}&scope=called&limit=${manifestLimit}`);

        const res = await fetch(manifestUrl, {
            headers: { 'Authorization': `Bearer ${token()}` },
            credentials: 'same-origin',
        });
        if (res.status === 401) {
            logout();
            return;
        }
        if (!res.ok) {
            console.warn('Campaign manifest fetch failed', res.status);
            if (typeof showToast === 'function' && !campaignStateFetchWarned) {
                campaignStateFetchWarned = true;
                showToast(
                    'Could not load lead preview (HTTP ' + res.status + '). Stats may load; try reload.',
                    'error',
                    7000,
                );
            }
            if (!allLeads.length) {
                showLeadManifestSkeleton('Preview could not be loaded.', { spinner: false });
            } else {
                renderManifest();
                renderCalls();
            }
            return;
        }
        const m = await res.json().catch(() => ({}));
        if (typeof applyCalledManifestPayload === 'function') {
            applyCalledManifestPayload(m, window._lastApiData || null);
        } else {
            var manifestLeads = Array.isArray(m.leads) ? m.leads : [];
            window._lastManifestTotalInDb = Number(m.total_in_db);
            window._lastManifestTruncated = !!m.lead_list_truncated;
            allLeadsFull = manifestLeads;
            manifestPage = 1;
            allLeads = manifestLeads;
        }
        campaignStateFetchWarned = false;
        renderManifest();
        renderCalls();
        persistLeadTablesToSession();
        showLoadMoreButton();
        if (typeof applyManifestDispositionStats === 'function' && !opts.skipStats) {
            applyManifestDispositionStats();
        }
    } catch (e) {
        console.error('Campaign manifest failed', e);
        if (!allLeads.length) {
            showLeadManifestSkeleton('Something went wrong while loading leads.', { spinner: false });
        } else {
            renderManifest();
            renderCalls();
        }
    }
}

// Pagination state for manifest
let manifestPage = 1;
const MANIFEST_PAGE_SIZE = 100;
var allLeadsFull = [];

function showLoadMoreButton() {
    var container = document.getElementById('load-more-container');
    if (container) {
        var calledTotal = typeof getAuthoritativeCalledTotal === 'function'
            ? getAuthoritativeCalledTotal()
            : (allLeadsFull.length || 0);
        var loaded = allLeadsFull.length || 0;
        var hasMore = !!window._lastManifestTruncated || calledTotal > loaded;
        container.style.display = hasMore && loaded > 0 ? 'block' : 'none';
        var btn = container.querySelector('button');
        if (btn && hasMore) {
            btn.textContent = 'Load all called (' + loaded.toLocaleString() + ' of ' + calledTotal.toLocaleString() + ')';
        }
    }
}

async function loadMoreLeads() {
    await ensureFullLeadManifest();
    renderManifest();
    if (typeof renderCalls === 'function') renderCalls();
    showLoadMoreButton();
}

/** Upsert one lead into manifest lists (live row updates without full refresh). */
function upsertLeadInManifestLists(lead) {
    if (!lead || lead.id == null) return false;
    var id = Number(lead.id);
    var found = false;
    function patch(arr) {
        if (!Array.isArray(arr)) return;
        for (var i = 0; i < arr.length; i++) {
            if (Number(arr[i].id) === id) {
                arr[i] = lead;
                found = true;
                return;
            }
        }
    }
    patch(allLeads);
    patch(allLeadsFull);
    if (!found) {
        allLeads.unshift(lead);
        allLeadsFull.unshift(lead);
    }
    return true;
}

var _applyStateQueued = null;
var _applyLeadQueued = null;
var _applyOptsQueued = null;
var _lastChartStatsKey = '';
var _lastInboundInterestCount = null;
var _lastWorkerActive = null;
var _manifestQuietTimer = null;
var _manifestSyncTimer = null;
window._sseHealthy = false;
window._wsHealthy = false;
window._wsReconnectAttempts = 0;
window._lastLiveUpdateAt = 0;
window._dashboardInitialLoadDone = false;
window.__livePollMs = 5000;
window._lastManifestQuietAt = 0;
var _renderCallsTimer = null;

function isLiveStreamHealthy() {
    return !!(window._wsHealthy || window._sseHealthy);
}

function scheduleRenderCalls() {
    if (_renderCallsTimer) return;
    _renderCallsTimer = setTimeout(function () {
        _renderCallsTimer = null;
        if (typeof renderCalls === 'function') renderCalls();
    }, 180);
}

function buildChartStatsKey(data) {
    if (!data) return '';
    return [
        data.called_count,
        data.total,
        data.dialing,
        data.pending,
        JSON.stringify(data.disposition_counts || {}),
        JSON.stringify(data.timeline_total_calls || []),
        JSON.stringify(data.timeline_interested || []),
        JSON.stringify(data.timeline_dates_iso || []),
        JSON.stringify(data.progress_counts || {}),
        JSON.stringify(data.weekday_counts || []),
        JSON.stringify(data.hourly_counts || []),
    ].join('|');
}

function scheduleBackgroundManifestSync() {
    if (!campaignWorkerActive || !isLiveStreamHealthy()) return;
    if (_manifestSyncTimer) return;
    _manifestSyncTimer = setTimeout(function () {
        _manifestSyncTimer = null;
        refreshManifestQuiet();
    }, 12000);
}

function updateLiveFeedIndicator(connected) {
    var el = document.getElementById('live-feed-indicator');
    if (!el) return;
    window._lastLiveUpdateAt = Date.now();
    if (connected === false || !isLiveStreamHealthy()) {
        el.textContent = 'Syncing…';
        el.className = 'live-feed-indicator live-feed-paused';
        return;
    }
    el.textContent = window._wsHealthy ? 'Live (WS)' : 'Live';
    el.className = 'live-feed-indicator live-feed-active';
}

function scheduleApplyStateData(data, changedLead, opts) {
    _applyStateQueued = data;
    if (changedLead) _applyLeadQueued = changedLead;
    if (opts) _applyOptsQueued = opts;
    if (window._applyStateRaf) return;
    window._applyStateRaf = requestAnimationFrame(function () {
        window._applyStateRaf = 0;
        var d = _applyStateQueued;
        var l = _applyLeadQueued;
        var o = _applyOptsQueued || {};
        _applyStateQueued = null;
        _applyLeadQueued = null;
        _applyOptsQueued = null;
        if (d) applyStateDataImpl(d, l, o);
    });
}

function applyStateData(data, changedLead, opts) {
    scheduleApplyStateData(data, changedLead, opts);
}

function applyStateDataImpl(data, changedLead, opts) {
    opts = opts || {};
    var source = opts.source || 'poll';
    var silent = !!opts.silent || source === 'sse-tick';
    if (!data) return;
    if (typeof data.live_poll_ms === 'number' && data.live_poll_ms > 0) {
        window.__livePollMs = data.live_poll_ms;
        if (typeof resetLivePollInterval === 'function') resetLivePollInterval();
    }
    if (!silent) updateLiveFeedIndicator(true);

    campaignWorkerActive = !!data.active;
    applyCampaignHoursUI(data.campaign_hours);
    if (data.campaign_paused) {
        applyCampaignPausedUI(true);
        campaignWorkerActive = false;
    }
    var chartSample = Array.isArray(data.chart_sample) ? data.chart_sample : [];
    if (!chartSample.length && Array.isArray(data.leads) && data.leads.length) {
        chartSample = data.leads.slice(0, 900);
    }

    var _leadsForStats = typeof getLeadDatasetForFilters === 'function' ? getLeadDatasetForFilters() : [];
    if (!_leadsForStats.length && Array.isArray(chartSample) && chartSample.length) {
        _leadsForStats = chartSample;
    }
    var _apiTotal = Number(data.total);
    if (!Number.isFinite(_apiTotal) || _apiTotal < 0) _apiTotal = 0;
    data.total = _apiTotal;
    window._lastApiData = data;

    // Upsert changed lead into manifest lists (live table update)
    if (changedLead && changedLead.id != null) {
        upsertLeadInManifestLists(changedLead);
    }

    var dateFilteredLeads = typeof getDateFilteredLeads === 'function' ? getDateFilteredLeads(_leadsForStats) : _leadsForStats;
    var range = typeof getSelectedDateRange === 'function' ? getSelectedDateRange() : null;
    var called = dateFilteredLeads.filter(function(l) { return isLeadCalledInDateRange(l, range); });
    var dc, interested, notInterested, siteVisit, failed;
    if (window.__vizDateFilterActive) {
        dc = typeof countDispositionFromLeads === 'function' ? countDispositionFromLeads(called) : {};
        interested = Number(dc['Interested']) || 0;
        notInterested = Number(dc['Not Interested']) || 0;
        siteVisit = typeof countSiteVisitFromLeads === 'function' ? countSiteVisitFromLeads(called) : 0;
        failed = Number(dc['Failed']) || 0;
    } else {
        var resolved = typeof resolveDashboardCounts === 'function'
            ? resolveDashboardCounts(data, called)
            : null;
        if (resolved) {
            interested = Number(resolved.interested) || 0;
            notInterested = Number(resolved.notInterested) || 0;
            siteVisit = Number(resolved.siteVisit) || 0;
            failed = Number(resolved.failed) || 0;
            dc = resolved.dispositionCounts || data.disposition_counts || {};
        } else {
            dc = data.disposition_counts || {};
            var cb = data.called_breakdown || {};
            interested = Number(cb.interested) || Number(dc['Interested']) || 0;
            notInterested = Number(cb.not_interested) || Number(dc['Not Interested']) || 0;
            siteVisit = Number(cb.site_visit) || Number(dc['Site Visit']) || 0;
            failed = Number(cb.failed) || Number(dc['Failed']) || 0;
        }
    }
    var chartExtras = buildChartExtrasFromState(data);
    var active = data.active_calls ?? 0;
    var calledCount = window.__vizDateFilterActive
        ? called.length
        : (Number((data.called_breakdown || {}).total_called) || data.called_count || called.length);
    var nTotal = window.__vizDateFilterActive ? dateFilteredLeads.length : getAuthoritativeLeadTotal();
    var conversionRate = calledCount > 0 ? Math.round((interested / calledCount) * 100) : 0;

    var callbackLeads = window.__vizDateFilterActive
        ? called.filter(isFollowUpLead).length
        : (Number((data.called_breakdown || {}).callbacks) || called.filter(isFollowUpLead).length);
    var callbacksCount = typeof incomingCallsList !== 'undefined' ? incomingCallsList.length : 0;

    setCampaignTotalsIndeterminate(false);

    updateStat('stat-total', nTotal.toLocaleString());
    updateStat('stat-called', Number.isFinite(calledCount) ? calledCount.toLocaleString() : String(calledCount));
    updateStat('stat-interested-count', interested);
    updateStat('stat-site-visit', siteVisit);
    updateStat('stat-not-interested', notInterested);
    updateStat('stat-callbacks', callbacksCount);
    updateStat('stat-failed', failed);
    updateStat('stat-conversion-rate', conversionRate + '%');
    updateStat('stat-attempts', callbackLeads);

    var pctCalled = nTotal > 0 ? Math.round((calledCount / nTotal) * 100) : 0;
    updatePct('pct-called', pctCalled);
    var pctInterested = calledCount > 0 ? Math.round((interested / calledCount) * 100) : 0;
    updatePct('pct-interested', pctInterested);
    var pctSiteVisit = calledCount > 0 ? Math.round((siteVisit / calledCount) * 100) : 0;
    updatePct('pct-site-visit', pctSiteVisit);
    var pctNotInterested = calledCount > 0 ? Math.round((notInterested / calledCount) * 100) : 0;
    updatePct('pct-not-interested', pctNotInterested);
    var pctCallbacks = calledCount > 0 ? Math.round((callbacksCount / calledCount) * 100) : 0;
    updatePct('pct-callbacks', pctCallbacks);
    var pctAttempts = calledCount > 0 ? Math.round((callbackLeads / calledCount) * 100) : 0;
    updatePct('pct-attempts', pctAttempts);
    var pctFailed = calledCount > 0 ? Math.round((failed / calledCount) * 100) : 0;
    updatePct('pct-failed', pctFailed);

    // WhatsApp & Email sent stats
    var waSent = Number(data.whatsapp_sent_count) || 0;
    var emSent = Number(data.email_sent_count) || 0;
    var followupCount = Number(data.followup_count) || 0;
    updateStat('stat-whatsapp-sent', waSent);
    updateStat('stat-email-sent', emSent);
    updateStat('stat-followup', followupCount.toLocaleString());
    updateStat('stat-inbound-interest', Number(data.inbound_interest_count) || 0);
    
    var pctFollowup = nTotal > 0 ? Math.round((followupCount / nTotal) * 100) : 0;
    updatePct('pct-whatsapp', nTotal > 0 ? Math.round((waSent / nTotal) * 100) : 0);
    updatePct('pct-email', nTotal > 0 ? Math.round((emSent / nTotal) * 100) : 0);
    updatePct('pct-followup', pctFollowup);

    setProgressWidth('bar-total', 100);
    setProgressWidth('bar-called', pctCalled);
    setProgressWidth('bar-interested', pctInterested);
    setProgressWidth('bar-site-visit', pctSiteVisit);
    setProgressWidth('bar-not-interested', pctNotInterested);
    setProgressWidth('bar-callbacks', pctCallbacks);
    setProgressWidth('bar-conversion', conversionRate);
    setProgressWidth('bar-followups', pctAttempts);
    setProgressWidth('bar-failed', pctFailed);
    setProgressWidth('bar-followup', pctFollowup);

    var perfAvgEl = document.getElementById('perf-avg-rating');
    var perfCalledEl = document.getElementById('perf-total-called');
    var perfCallbackEl = document.getElementById('perf-callback-rate');
    var perfFailEl = document.getElementById('perf-fail-rate');
    if (perfCalledEl) perfCalledEl.textContent = calledCount;
    var perfCallbackLeads = called.filter(isFollowUpLead).length;
    if (perfCallbackEl) {
        var cbRate = calledCount > 0 ? Math.round((perfCallbackLeads / calledCount) * 100) : 0;
        perfCallbackEl.textContent = cbRate + '%';
    }
    if (perfFailEl) {
        var fRate = calledCount > 0 ? Math.round((failed / calledCount) * 100) : 0;
        perfFailEl.textContent = fRate + '%';
    }
    if (perfAvgEl) {
        var sum = 0, count = 0;
        called.forEach(function (l) {
            var r = l.rating || (l.analysis ? parseInt(l.analysis.rating, 10) : null);
            if (Number.isFinite(r) && r >= 1 && r <= 5) { sum += r; count++; }
        });
        perfAvgEl.textContent = count > 0 ? (sum / count).toFixed(1) + ' ★' : '—';
    }

    var campLabel = nTotal.toLocaleString() + ' leads';
    if (data.lead_list_truncated && typeof data.leads_returned === 'number') {
        campLabel += ' (' + String(data.chart_sample?.length ?? data.leads_returned) + '-row chart sample)';
    }
    updateStat('camp-total', campLabel);

    var progressBar = document.getElementById('progress-bar');
    var pct = 0;
    if (progressBar && nTotal > 0) {
        var progressCalled = dateFilteredLeads.filter(function (l) { return l.status && l.status !== 'pending'; }).length;
        pct = Math.min(100, (progressCalled / nTotal) * 100);
        var pw = pct + '%';
        if (progressBar.style.width !== pw) progressBar.style.width = pw;
    }

    var chartStatsKey = buildChartStatsKey(data);
    var chartStatsChanged = chartStatsKey !== _lastChartStatsKey || source === 'initial';
    if (chartStatsChanged) {
        _lastChartStatsKey = chartStatsKey;
        updateCharts(called, dc, data.callback_counts_by_date || {}, {
            timeline_week_labels: data.timeline_week_labels,
            timeline_total_calls: data.timeline_total_calls,
            timeline_interested: data.timeline_interested,
            timeline_dates_iso: data.timeline_dates_iso,
        }, chartExtras);
    }

    stashCampaignSnapshot({
        texts: {
            'stat-total': nTotal.toLocaleString(),
            'stat-called': Number.isFinite(calledCount) ? calledCount.toLocaleString() : String(calledCount),
            'stat-interested-count': String(interested),
            'stat-site-visit': String(siteVisit),
            'stat-not-interested': String(notInterested),
            'stat-callbacks': String(typeof incomingCallsList !== 'undefined' ? incomingCallsList.length : 0),
            'stat-failed': String(failed),
            'stat-conversion-rate': conversionRate + '%',
            'stat-attempts': String(callbackLeads),
            'camp-total': campLabel,
        },
        progressPct: pct,
        chartSample: chartSample,
        disposition_counts: data.disposition_counts || {},
        callback_dates: data.callback_counts_by_date || {},
        timeline_week_labels: data.timeline_week_labels,
        timeline_total_calls: data.timeline_total_calls,
        timeline_interested: data.timeline_interested,
        timeline_dates_iso: data.timeline_dates_iso,
        progress_counts: data.progress_counts,
        weekday_counts: data.weekday_counts,
        hourly_counts: data.hourly_counts,
        workerActive: campaignWorkerActive,
        activeCalls: active,
    });

    campaignStateFetchWarned = false;

    if (!silent) {
        loadCategoryOverview();
        loadUploadSources();
    }

    var gapEl = document.getElementById('campaign-inter-call-gap');
    if (gapEl && data.inter_call_gap_sec != null && document.activeElement !== gapEl) {
        var n = Math.round(Number(data.inter_call_gap_sec));
        gapEl.value = Number.isFinite(n) ? String(n) : '5';
    }
    if (!silent) updateInterCallGapControls(data);

    var inboundCount = Number(data.inbound_interest_count) || 0;
    var inboundChanged = inboundCount !== _lastInboundInterestCount;
    if (inboundChanged) _lastInboundInterestCount = inboundCount;
    if ((!silent || source === 'sse-event') && inboundChanged && typeof loadInboundInterest === 'function') {
        loadInboundInterest(false);
    }

    // Tables: patch only the row that changed — renderManifest diffs rows, no full-table flash.
    if (changedLead && changedLead.id != null) {
        renderManifest();
        if (typeof renderCalls === 'function') renderCalls();
    } else if (silent && source === 'sse-event' && campaignWorkerActive) {
        scheduleBackgroundManifestSync();
    }

    setCampaignTotalsIndeterminate(false);
    if (_lastWorkerActive !== campaignWorkerActive || source === 'initial') {
        _lastWorkerActive = campaignWorkerActive;
        updateCampaignRunnerChrome();
    }
}

function _debugApi(msg) {
    var el = document.getElementById('api-debug-content');
    if (el) el.textContent += (el.textContent ? '\n' : '') + '[' + new Date().toLocaleTimeString() + '] ' + msg;
}
function _clearDebugApi() {
    var el = document.getElementById('api-debug-content');
    if (el) el.textContent = '';
}

/** Fetch initial state from API (called once on page load). */
async function syncState() {
    console.log('[syncState] starting, allLeads len=' + (Array.isArray(allLeads) ? allLeads.length : 'n/a'));
    _clearDebugApi();
    setCampaignTotalsIndeterminate(true);
    showLeadManifestSkeleton('Fetching campaign summary…');

    try {
        var stateUrl = apiUrl('/api/campaign/state?role=' + apiRoleQ());
        var fetchOpts = {
            headers: { 'Authorization': 'Bearer ' + token() },
            credentials: 'same-origin',
        };

        var stateRes = await fetch(stateUrl, fetchOpts);

        if (stateRes.status === 401) { setCampaignTotalsIndeterminate(false); logout(); return; }
        if (!stateRes.ok) {
            console.warn('Campaign state fetch failed', stateRes.status);
            setCampaignTotalsIndeterminate(false);
            if (!replayLastCampaignSnapshot()) {
                updateStat('stat-total', '0');
                updateStat('stat-called', '0');
                updateStat('stat-interested-count', '0');
                updateStat('stat-not-interested', '0');
                updateStat('stat-callbacks', '0');
                updateStat('stat-failed', '0');
                updateStat('stat-conversion-rate', '0%');
                updateStat('stat-attempts', '0');
                updateStat('camp-total', '0 leads');
            }
            renderManifest();
            if (typeof renderCalls === 'function') renderCalls();
            return;
        }

        var stateData = await stateRes.json();
        applyStateData(stateData, null, { source: 'initial', silent: false });
        if (typeof loadInboundInterest === 'function') loadInboundInterest(false);

        var manifestLimit = typeof calledManifestLimitHint === 'function'
            ? calledManifestLimitHint(stateData)
            : MANIFEST_KPI_COUNT_LIMIT;
        var manifestUrl = typeof buildCalledManifestUrl === 'function'
            ? buildCalledManifestUrl(manifestLimit)
            : apiUrl('/api/campaign/manifest?role=' + apiRoleQ() + '&scope=called&limit=' + manifestLimit);
        var manifestRes = await fetch(manifestUrl, fetchOpts);

        if (manifestRes.ok) {
            var m = await manifestRes.json().catch(function () { return {}; });
            if (typeof applyCalledManifestPayload === 'function') {
                applyCalledManifestPayload(m, stateData);
            } else {
                var manifestLeads = Array.isArray(m.leads) ? m.leads : [];
                allLeadsFull = manifestLeads;
                manifestPage = 1;
                allLeads = manifestLeads;
                window._lastManifestTotalInDb = Number(m.total_in_db);
                window._lastManifestTruncated = !!m.lead_list_truncated;
            }
            if (typeof m.total_in_db === 'number' && m.total_in_db >= 0) {
                updateStat('stat-total', Number(m.total_in_db).toLocaleString());
            }
            campaignStateFetchWarned = false;
            renderManifest();
            renderCalls();
            persistLeadTablesToSession();
            showLoadMoreButton();
            if (typeof applyManifestDispositionStats === 'function') {
                applyManifestDispositionStats();
            }
            if (typeof fetchIncomingCallsForDashboard === 'function') {
                fetchIncomingCallsForDashboard();
            }
        } else {
            if (!allLeads.length) {
                showLeadManifestSkeleton('Preview could not be loaded.', { spinner: false });
            } else {
                renderManifest();
                renderCalls();
            }
        }
    } catch (e) {
        console.error('Initial sync failed', e);
        setCampaignTotalsIndeterminate(false);
        if (!replayLastCampaignSnapshot()) {
            updateStat('stat-total', '0');
            updateStat('stat-called', '0');
            updateStat('stat-interested-count', '0');
            updateStat('stat-not-interested', '0');
            updateStat('stat-callbacks', '0');
            updateStat('stat-failed', '0');
            updateStat('stat-conversion-rate', '0%');
            updateStat('stat-attempts', '0');
            updateStat('camp-total', '0 leads');
            updateCharts([], {}, {}, {}, {});
        }
    } finally {
        setCampaignTotalsIndeterminate(false);
        updateCampaignRunnerChrome();
        window._dashboardInitialLoadDone = true;
    }
}

/** Shared handler for WebSocket + SSE live dashboard messages. */
function handleDashboardLiveMessage(msg) {
    if (!msg || !msg.type) return;
    if (msg.type === 'upload_started' || msg.type === 'upload_progress') {
        var pv = document.getElementById('upload-preview-summary');
        if (pv) {
            pv.style.display = 'block';
            var saved = Number(msg.saved || 0);
            var total = Number(msg.total || msg.valid_rows || msg.total_rows || 0);
            var processed = Number(msg.processed || saved);
            if (msg.type === 'upload_started') {
                pv.textContent = 'Uploading ' + (msg.filename || 'file') + '… (' + total.toLocaleString() + ' rows)';
            } else {
                pv.textContent = 'Saving leads… ' + processed.toLocaleString() + ' / ' + total.toLocaleString()
                    + ' (' + saved.toLocaleString() + ' saved)';
            }
        }
        return;
    }
    if (msg.type === 'upload_complete') {
        if (typeof refreshManifestQuiet === 'function') {
            refreshManifestQuiet().then(function () {
                var total = (typeof getAuthoritativeLeadTotal === 'function') ? getAuthoritativeLeadTotal() : 0;
                if (typeof updateStat === 'function' && total > 0) updateStat('stat-total', total.toLocaleString());
            });
        }
        return;
    }
    if (msg.type === 'incoming_call') {
        if (typeof fetchIncomingCallsForDashboard === 'function') fetchIncomingCallsForDashboard().then(function () {
            scheduleRenderCalls();
        });
        if (typeof showInboundCallToast === 'function') {
            var who = msg.caller_name || msg.from_phone || 'Unknown';
            showInboundCallToast('Inbound call: ' + who, 'incoming_call');
        }
        return;
    }
    if (msg.type === 'incoming_call_connected' || msg.type === 'incoming_call_completed') {
        if (typeof fetchIncomingCallsForDashboard === 'function') {
            fetchIncomingCallsForDashboard().then(function () {
                scheduleRenderCalls();
                if (msg.type === 'incoming_call_completed' && typeof loadRecentIncomingCalls === 'function') {
                    loadRecentIncomingCalls();
                }
            });
        }
        if (msg.type === 'incoming_call_completed' && typeof showInboundCallToast === 'function') {
            showInboundCallToast('Inbound call ended — summary ready', 'incoming_call');
        }
        return;
    }
    if (msg.type === 'whatsapp_sent') {
        if (typeof showInboundCallToast === 'function') showInboundCallToast('WhatsApp details sent', 'whatsapp_sent');
        if (msg.state) applyStateData(msg.state, msg.changed_lead || null, { silent: true, source: 'live-event' });
        return;
    }
    if (msg.type === 'email_sent') {
        if (typeof showInboundCallToast === 'function') showInboundCallToast('Email details sent', 'email_sent');
        if (msg.state) applyStateData(msg.state, msg.changed_lead || null, { silent: true, source: 'live-event' });
        return;
    }
    if (msg.type === 'inbound_interest') {
        if (typeof loadInboundInterest === 'function') loadInboundInterest(false);
        if (typeof showInboundCallToast === 'function') showInboundCallToast('New WhatsApp / email reply', 'inbound_interest');
        if (msg.state) applyStateData(msg.state, msg.changed_lead || null, { silent: true, source: 'live-event' });
        return;
    }
    if (msg.type === 'state' || msg.type === 'lead_updated' || msg.type === 'tick') {
        if (!msg.state) return;
        var opts = { silent: true };
        if (msg.type === 'tick') {
            opts.source = 'live-tick';
        } else if (msg.type === 'lead_updated') {
            opts.source = 'live-event';
        } else if (window._dashboardInitialLoadDone) {
            opts.source = 'live-tick';
        } else {
            opts.source = 'initial';
            opts.silent = false;
        }
        applyStateData(msg.state, msg.changed_lead || null, opts);
    }
}

function _dashboardWsUrl() {
    var path = apiUrl('/ws/dashboard?role=' + apiRoleQ() + '&access_token=' + encodeURIComponent(token()));
    if (path.indexOf('ws://') === 0 || path.indexOf('wss://') === 0) return path;
    if (path.indexOf('http://') === 0 || path.indexOf('https://') === 0) {
        try {
            var u = new URL(path);
            var wsProto = u.protocol === 'https:' ? 'wss:' : 'ws:';
            return wsProto + '//' + u.host + u.pathname + u.search;
        } catch (_) {}
    }
    var proto = (window.location.protocol === 'https:') ? 'wss:' : 'ws:';
    if (path.charAt(0) !== '/') path = '/' + path;
    return proto + '//' + window.location.host + path;
}

/** Connect WebSocket for live dashboard (primary). */
function connectDashboardWS() {
    if (window._dashboardWs && (window._dashboardWs.readyState === WebSocket.OPEN || window._dashboardWs.readyState === WebSocket.CONNECTING)) {
        return window._dashboardWs;
    }
    try {
        var ws = new WebSocket(_dashboardWsUrl());
        window._dashboardWs = ws;
        ws.onopen = function () {
            window._wsHealthy = true;
            window._wsReconnectAttempts = 0;
            updateLiveFeedIndicator(true);
            if (typeof resetLivePollInterval === 'function') resetLivePollInterval();
        };
        ws.onmessage = function (event) {
            try {
                handleDashboardLiveMessage(JSON.parse(event.data));
            } catch (e) {
                console.error('WS parse error', e);
            }
        };
        ws.onerror = function () {
            window._wsHealthy = false;
            updateLiveFeedIndicator(false);
        };
        ws.onclose = function () {
            window._wsHealthy = false;
            updateLiveFeedIndicator(false);
            window._wsReconnectAttempts = (window._wsReconnectAttempts || 0) + 1;
            if (window._wsReconnectAttempts <= 3) {
                var delay = Math.min(10000, 2000 * window._wsReconnectAttempts);
                setTimeout(function () {
                    if (!isLiveStreamHealthy()) connectDashboardWS();
                }, delay);
            } else if (typeof connectSSE === 'function' && !window._sseHealthy) {
                window._sseConnection = connectSSE();
            }
            if (typeof quietSyncState === 'function') quietSyncState();
        };
        return ws;
    } catch (e) {
        console.warn('WebSocket unavailable, falling back to SSE', e);
        return null;
    }
}

function connectLiveDashboard() {
    connectDashboardWS();
    setTimeout(function () {
        if (!window._wsHealthy && typeof connectSSE === 'function') {
            window._sseConnection = connectSSE();
        }
    }, 2500);
}

/** Connect to SSE for live updates (fallback). Returns the EventSource. */
function connectSSE() {
    var url = apiUrl('/api/events/stream?role=' + apiRoleQ() + '&access_token=' + encodeURIComponent(token()));
    var es = new EventSource(url);

    es.onopen = function () {
        window._sseHealthy = true;
        updateLiveFeedIndicator(true);
        if (typeof resetLivePollInterval === 'function') resetLivePollInterval();
    };

    es.onmessage = function (event) {
        try {
            handleDashboardLiveMessage(JSON.parse(event.data));
        } catch (e) {
            console.error('SSE parse error', e);
        }
    };

    es.onerror = function () {
        window._sseHealthy = false;
        updateLiveFeedIndicator(false);
        console.warn('SSE connection glitched. Triggering quietSyncState for self-healing.');
        if (typeof quietSyncState === 'function') quietSyncState();
        if (typeof fetchIncomingCallsForDashboard === 'function') {
            fetchIncomingCallsForDashboard().then(function () {
                scheduleRenderCalls();
            });
        }
    };

    return es;
}

/** Periodic inbound refresh when dashboard tab is visible. */
function startInboundDashboardSelfHeal() {
    if (window._inboundSelfHealStarted) return;
    window._inboundSelfHealStarted = true;
    setInterval(function () {
        if (document.visibilityState !== 'visible') return;
        if (typeof fetchIncomingCallsForDashboard !== 'function') return;
        fetchIncomingCallsForDashboard().then(function () {
            if (typeof renderCalls === 'function') renderCalls();
        });
    }, 60000);
}

async function quietSyncState() {
    if (isLiveStreamHealthy()) return;
    try {
        var stateRes = await fetch(apiUrl('/api/campaign/state?role=' + apiRoleQ()), {
            headers: { 'Authorization': 'Bearer ' + token() },
            credentials: 'same-origin',
        });

        if (stateRes.status === 401) { logout(); return; }
        if (!stateRes.ok) return;

        var s = await stateRes.json().catch(function () { return {}; });
        applyStateData(s, null, { silent: true, source: 'poll-fallback' });

        var now = Date.now();
        if (now - (window._lastManifestQuietAt || 0) >= 20000) {
            window._lastManifestQuietAt = now;
            if (typeof refreshManifestQuiet === 'function') refreshManifestQuiet();
        }
    } catch (e) {
        console.warn('[QuietSync] Failed', e);
    }
}

/** Manifest-only refresh — no loading skeletons, row-level diff in renderManifest. */
async function refreshManifestQuiet() {
    if (_manifestQuietTimer) return;
    _manifestQuietTimer = setTimeout(function () { _manifestQuietTimer = null; }, 350);
    try {
        var quietLimit = typeof calledManifestLimitHint === 'function'
            ? calledManifestLimitHint(window._lastApiData || DEFAULT_MANIFEST_FETCH_LIMIT)
            : DEFAULT_MANIFEST_FETCH_LIMIT;
        var quietUrl = typeof buildCalledManifestUrl === 'function'
            ? buildCalledManifestUrl(quietLimit)
            : apiUrl('/api/campaign/manifest?role=' + apiRoleQ() + '&scope=called&limit=' + quietLimit);
        var manifestRes = await fetch(
            quietUrl,
            { headers: { 'Authorization': 'Bearer ' + token() }, credentials: 'same-origin' },
        );
        if (!manifestRes.ok) return;
            var m = await manifestRes.json().catch(function () { return {}; });
            if (typeof applyCalledManifestPayload === 'function') {
                applyCalledManifestPayload(m, window._lastApiData || null);
            } else {
                var manifestLeads = Array.isArray(m.leads) ? m.leads : [];
                window._lastManifestTotalInDb = Number(m.total_in_db);
                window._lastManifestTruncated = !!m.lead_list_truncated;
                allLeadsFull = manifestLeads;
                allLeads = manifestLeads;
            }
            var sig = (allLeadsFull || []).map(function (l) { return l.id + ':' + (l.status || ''); }).join('|');
            if (sig === window._lastManifestSig) return;
            window._lastManifestSig = sig;
        renderManifest();
        if (typeof renderCalls === 'function') renderCalls();
        showLoadMoreButton();
    } catch (e) {
        console.warn('[ManifestQuiet] Failed', e);
    }
}

/** Refresh KPIs from manifest; reconcile when server Interested is 0 but leads say otherwise. */
function applyManifestDispositionStats() {
    var dataset = typeof getLeadDatasetForFilters === 'function' ? getLeadDatasetForFilters() : allLeads;
    if (!Array.isArray(dataset) || !dataset.length) { console.log('[DEBUG] applyManifestDispositionStats: lead dataset empty'); return; }
    var dateFilteredLeads = typeof getDateFilteredLeads === 'function' ? getDateFilteredLeads(dataset) : dataset;
    var range = typeof getSelectedDateRange === 'function' ? getSelectedDateRange() : null;
    var called = dateFilteredLeads.filter(function(l) { return isLeadCalledInDateRange(l, range); });
    var computed = countDispositionFromLeads(called);
    var leadInterested = Number(computed.Interested) || 0;
    var serverInterested = 0;
    if (window._lastApiData) {
        var _cb = window._lastApiData.called_breakdown || {};
        var _dc = window._lastApiData.disposition_counts || {};
        serverInterested = Number(_cb.interested) || Number(_dc['Interested']) || 0;
    }
    // Skip overwrite only when server already has a positive Interested count and no date filter
    if (
        !window.__vizDateFilterActive
        && serverInterested > 0
        && leadInterested <= serverInterested
    ) {
        return;
    }
    var interested = (!window.__vizDateFilterActive && serverInterested > 0)
        ? serverInterested
        : leadInterested;
    var notInterested = Number(computed['Not Interested']) || 0;
    var siteVisit = countSiteVisitFromLeads(called);
    var failed = Number(computed.Failed) || 0;
    // Use API called_count if available (authoritative DB count, not capped sample)
    var calledCount = window.__vizDateFilterActive
        ? called.length
        : ((window._lastApiData && typeof window._lastApiData.called_count === 'number')
            ? window._lastApiData.called_count
            : called.length);
    var nTotal = window.__vizDateFilterActive
        ? dateFilteredLeads.length
        : (typeof getAuthoritativeLeadTotal === 'function' ? getAuthoritativeLeadTotal() : dateFilteredLeads.length);
    var callbackLeads = called.filter(isFollowUpLead).length;
    var conversionRate = calledCount > 0 ? Math.round((interested / calledCount) * 100) : 0;

    updateStat('stat-total', nTotal.toLocaleString());
    updateStat('stat-called', calledCount.toLocaleString());
    updateStat('stat-interested-count', interested);
    updateStat('stat-site-visit', siteVisit);
    updateStat('stat-not-interested', notInterested);
    updateStat('stat-callbacks', callbackLeads);
    updateStat('stat-failed', failed);
    updateStat('stat-conversion-rate', conversionRate + '%');
    updateStat('stat-attempts', callbackLeads);

    var pctCalled = nTotal > 0 ? Math.round((calledCount / nTotal) * 100) : 0;
    updatePct('pct-called', pctCalled);
    var pctInterested = calledCount > 0 ? Math.round((interested / calledCount) * 100) : 0;
    updatePct('pct-interested', pctInterested);
    var pctSiteVisit = calledCount > 0 ? Math.round((siteVisit / calledCount) * 100) : 0;
    updatePct('pct-site-visit', pctSiteVisit);
    var pctNotInterested = calledCount > 0 ? Math.round((notInterested / calledCount) * 100) : 0;
    updatePct('pct-not-interested', pctNotInterested);
    var pctCallbacks = calledCount > 0 ? Math.round((callbackLeads / calledCount) * 100) : 0;
    updatePct('pct-callbacks', pctCallbacks);
    var pctAttempts = calledCount > 0 ? Math.round((callbackLeads / calledCount) * 100) : 0;
    updatePct('pct-attempts', pctAttempts);
    var pctFailed = calledCount > 0 ? Math.round((failed / calledCount) * 100) : 0;
    updatePct('pct-failed', pctFailed);

    setProgressWidth('bar-total', 100);
    setProgressWidth('bar-called', pctCalled);
    setProgressWidth('bar-interested', pctInterested);
    setProgressWidth('bar-site-visit', pctSiteVisit);
    setProgressWidth('bar-not-interested', pctNotInterested);
    setProgressWidth('bar-callbacks', pctCallbacks);
    setProgressWidth('bar-conversion', conversionRate);
    setProgressWidth('bar-followups', pctAttempts);
    setProgressWidth('bar-failed', pctFailed);

    var perfCalledEl = document.getElementById('perf-total-called');
    if (perfCalledEl) perfCalledEl.textContent = calledCount;
    var perfCallbackEl = document.getElementById('perf-callback-rate');
    if (perfCallbackEl) {
        var cbRate = calledCount > 0 ? Math.round((callbackLeads / calledCount) * 100) : 0;
        perfCallbackEl.textContent = cbRate + '%';
    }
    var perfFailEl = document.getElementById('perf-fail-rate');
    if (perfFailEl) {
        var fRate = calledCount > 0 ? Math.round((failed / calledCount) * 100) : 0;
        perfFailEl.textContent = fRate + '%';
    }
    var perfAvgEl = document.getElementById('perf-avg-rating');
    if (perfAvgEl) {
        var sum = 0, count = 0;
        called.forEach(function (l) {
            var r = l.rating || (l.analysis ? parseInt(l.analysis.rating, 10) : null);
            if (Number.isFinite(r) && r >= 1 && r <= 5) { sum += r; count++; }
        });
        perfAvgEl.textContent = count > 0 ? (sum / count).toFixed(1) + ' ★' : '—';
    }

    if (typeof updateCharts === 'function') {
        var chartSample = dateFilteredLeads.slice(0, 900);
        updateCharts(chartSample, computed, {}, {});
    }
}

function updateStat(id, val) {
    const el = document.getElementById(id);
    if (!el) return;
    const s = String(val);
    if (el.textContent !== s) el.textContent = s;
}

function updatePct(id, val) {
    const el = document.getElementById(id);
    if (!el) return;
    const s = (val === null || val === undefined || val === '') ? '' : val + '%';
    if (el.textContent !== s) el.textContent = s;
}

function setProgressWidth(id, pct) {
    const el = document.getElementById(id);
    if (!el) return;
    const w = Math.min(100, Math.max(0, pct)) + '%';
    if (el.style.width !== w) el.style.width = w;
}

/** Campaign Control chrome: status pill + Start button (``/api/campaign/state.active``). */
function updateCampaignRunnerChrome() {
    const pill = document.getElementById('campaign-status-pill');
    const startBtn = document.getElementById('btn-start');
    const stopBtn = document.getElementById('btn-stop');
    const running = typeof campaignWorkerActive !== 'undefined' && !!campaignWorkerActive;

    if (pill) {
        pill.textContent = running
            ? 'Outbound: RUNNING (auto-resumes after deploy/restart while pending leads remain)'
            : 'Outbound: idle — click Start to dial pending leads';
        pill.className = running ? 'badge-tag tag-int' : 'badge-tag tag-cbk';
        pill.style.fontSize = '11px';
        pill.style.fontWeight = '700';
    }
    if (startBtn) {
        startBtn.disabled = !!running;
        startBtn.textContent = running ? 'Running…' : 'Start Campaign';
    }
    if (stopBtn) {
        stopBtn.disabled = false;
    }
}

function _incomingRowsForDashboard() {
    return (typeof incomingCallsList !== 'undefined' ? incomingCallsList : []).map(function (c) {
        return {
            id: 'incoming_' + c.id,
            _incoming_id: c.id,
            name: c.caller_name || c.callee_name || c.name || 'Inbound Call',
            phone: c.from_phone || c.phone || '',
            status: c.status || 'completed',
            disposition: 'Inbound',
            _call_id: c.camp_id || ('incoming_' + c.id),
            _incoming_call: true,
            start_time: c.start_time || c.created_at || 0,
            started_at: c.started_at,
            analysis: c.analysis || null,
            summary: c.summary || '',
            rating: c.rating || 0,
            log_id: c.log_id || '',
            recording_available: !!c.recording_available,
        };
    });
}

// ─── Table Rendering ───
function renderCalls() {
    const tbody = document.getElementById('calls-tbody');
    if (!tbody) return;

    const search = (document.getElementById('search-input')?.value || '').toLowerCase().trim();
    let rows = (typeof getLeadDatasetForFilters === 'function' ? getLeadDatasetForFilters() : allLeads).filter(isCalled);

    // Check if a chart filter is active (exclusive mode — overrides all other filters)
    var _chartFilterActive = (typeof getChartFilter === 'function') && getChartFilter() && getChartFilter().type;

    // Apply Chart Filters
    if (typeof isLeadMatchingChartFilter === 'function') {
        rows = rows.filter(isLeadMatchingChartFilter);
    }

    if (!_chartFilterActive) {
        // Apply Disposition Filters (only when no chart filter is active)
        if (currentFilter !== 'all') {
            if (currentFilter === 'inbound') {
                rows = _incomingRowsForDashboard();
            } else if (currentFilter === 'failed') rows = rows.filter(isFailed);
            else if (currentFilter === 'dialing') rows = rows.filter(l => String(l.status || '').toLowerCase() === 'dialing');
            else if (currentFilter === 'star4') rows = rows.filter(l => (l.rating || 0) >= 4);
            else if (currentFilter === 'site_visit' || currentFilter === 'Site Visited') rows = rows.filter(hasSiteVisitWithParticularDate);
            else if (currentFilter === 'follow_up') rows = rows.filter(isFollowUpLead);
            else if (currentFilter === 'Answered') rows = rows.filter(isPlainAnsweredLead);
            else rows = rows.filter(l => effectiveDispo(l) === currentFilter);
        } else {
            // All calls: prepend recent inbound legs so every attempt is visible
            var _inboundAll = _incomingRowsForDashboard();
            if (_inboundAll.length) {
                rows = _inboundAll.concat(rows);
            }
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
                    || (l.source_file || '').toLowerCase().includes(search)
                    || (l.segment || '').toLowerCase().includes(search)
                    || (ext.upload_source || '').toLowerCase().includes(search)
                    || (ext.location || '').toLowerCase().includes(search)
                    || (ext.budget || '').toLowerCase().includes(search);
            });
        }
    }

    rows.sort((a, b) => (b.start_time || 0) - (a.start_time || 0));

    if (!rows.length) {
        tbody.innerHTML = `<tr><td colspan="10" style="text-align:center;padding:40px;color:var(--text-secondary);">No matching calls</td></tr>`;
        return;
    }

    const newHtml = rows.map(r => renderCallRow(r)).join('');
    const sig = rows.map(r => r.id + ':' + r.status + ':' + effectiveDispo(r) + ':' + (r.summary || '')).join('|') + '|' + rows.length;
    if (tbody.dataset.sig !== sig) {
        const temp = document.createElement('tbody');
        temp.innerHTML = newHtml;
        if (tbody.children.length === temp.children.length) {
            const oldChildren = Array.from(tbody.children);
            const newChildren = Array.from(temp.children);
            for (let i = 0; i < oldChildren.length; i++) {
                if (oldChildren[i].outerHTML !== newChildren[i].outerHTML) {
                    oldChildren[i].outerHTML = newChildren[i].outerHTML;
                }
            }
        } else {
            tbody.innerHTML = newHtml;
        }
        tbody.dataset.sig = sig;
    }

    if (typeof updateHourlyChartForLeads === 'function') {
        updateHourlyChartForLeads(rows);
    }
}

function dataSourceCellHtml(r) {
    let src = '';
    if (r && r.source_file) src = r.source_file;
    else if (r && r.segment) src = r.segment;
    else if (r && r.extra) {
        try {
            var ex = typeof r.extra === 'string' ? JSON.parse(r.extra) : (r.extra || {});
            src = ex.upload_source || ex.source_file || ex.filename || '';
        } catch (e) {}
    }
    if (!src) return '<span style="font-size:11px;color:var(--text-secondary);">—</span>';
    var isDigital = String(r && r.source || '').toLowerCase() === 'digital'
        || String(r && r.source || '').toLowerCase() === 'digital_marketing';
    var badge = isDigital
        ? '<span class="badge-tag" style="background:#7c3aed;color:white;padding:2px 6px;font-size:9px;border-radius:4px;margin-left:4px;text-transform:uppercase;">Digital</span>'
        : '<span class="badge-tag" style="background:#2563eb;color:white;padding:2px 6px;font-size:9px;border-radius:4px;margin-left:4px;text-transform:uppercase;">Cold</span>';
    return '<div style="font-size:11px;font-weight:600;max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="' + escapeHtml(src) + '">' + escapeHtml(src) + badge + '</div>';
}

function renderCallRow(r) {
    const dispo = effectiveDispo(r) || '—';
    const tagClass = dispoTagClass(dispo);
    const isIncoming = !!(r._incoming_call || r._incoming_id || (r._call_id && String(r._call_id).startsWith('incoming_')) || String(r.id || '').startsWith('incoming_'));
    const incomingId = r._incoming_id || (String(r.id || '').startsWith('incoming_') ? String(r.id).replace(/^incoming_/, '') : null);
    const summaryHtml = isFailed(r) ? failureSummaryHtml(r) : escapeHtml(r.summary || (isIncoming && r.status === 'connected' ? 'Call in progress…' : 'No summary yet'));
    const mayRec = r.recording_available || r._log_id || r.log_id;
    const recHtml = mayRec
        ? `<span style="font-size:11px;color:var(--accent);cursor:pointer;" onclick="event.stopPropagation();${isIncoming && incomingId ? 'viewIncomingCallOutcome(' + incomingId + ')' : 'openCallDetail(' + r.id + ')'}">Listen</span>`
        : '<span style="font-size:11px;color:var(--text-secondary);">—</span>';
    
    const pname = escapeHtml(typeof leadContactPrimary === 'function' ? leadContactPrimary(r) : (r.name || '—'));
    const ps2 = escapeHtml(typeof leadContactSecondary === 'function' ? leadContactSecondary(r) : (r.company || ''));
    
    const dateHtml = r.start_time 
        ? `<div style="font-size:11px;color:var(--text-secondary);font-weight:500;">${new Date(r.start_time * 1000).toLocaleString(undefined, {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'})}</div>`
        : '<span style="font-size:11px;color:var(--text-secondary);">—</span>';

    const rowStyle = isIncoming ? ' style="background-color: rgba(99, 102, 241, 0.1); border-left: 4px solid #6366f1;"' : '';
    const badgeHtml = isIncoming ? ' <span class="badge-tag" style="background-color: #6366f1; color: white; padding: 2px 6px; font-size: 9px; border-radius: 4px; margin-left: 6px; text-transform: uppercase;">Inbound</span>' : '';
    const rowClick = isIncoming && incomingId
        ? `viewIncomingCallOutcome(${incomingId})`
        : `openCallDetail(${r.id})`;

    return `<tr class="clickable-row" onclick="${rowClick}"${rowStyle}>
        <td style="padding-left:20px;font-weight:600;">${pname}${badgeHtml}<div style="font-size:11px;color:var(--text-secondary);font-weight:400;">${ps2}</div></td>
        <td style="font-family:var(--font-mono);font-size:12px;">${escapeHtml(r.phone || '—')}</td>
        <td>${dataSourceCellHtml(r)}</td>
        <td>${dateHtml}</td>
        <td style="font-size:12px;max-width:320px;">${summaryHtml}</td>
        <td>${r.rating ? starsHtml(r.rating) : '—'}</td>
        <td><span class="badge-tag ${tagClass}">${escapeHtml(dispo)}</span></td>
        <td>${formatFailureCell(r)}</td>
        <td>${recHtml}</td>
        <td style="text-align:right;padding-right:20px;white-space:nowrap;">
            ${isIncoming && incomingId ? `<button class="btn btn-sm" style="background:#6366f1;color:white;border:none;padding:4px 10px;border-radius:6px;font-size:11px;cursor:pointer;margin-right:4px;" onclick="event.stopPropagation();dialInboundBack(${incomingId})">Call Back</button>` : ''}
            <button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();${rowClick}">View</button>
        </td>
    </tr>`;
}

function getFilterDate(id, startOfDay) {
    const val = document.getElementById(id)?.value;
    return val ? new Date(val + (startOfDay ? 'T00:00:00' : 'T23:59:59')) : null;
}

/** Recent Calls disposition tabs — must stay in sync with ``onclick`` in ``console.html``. */
function setFilter(f, btn) {
    currentFilter = f;
    document.querySelectorAll('.btn-filter').forEach(function (b) {
        b.classList.remove('active');
    });
    if (btn) btn.classList.add('active');
    renderCalls();
}

function clearDateFilters() {
    const ids = ['filter-date-from', 'filter-date-to', 'viz-date-from', 'viz-date-to'];
    ids.forEach(function(id) {
        var el = document.getElementById(id);
        if (el) el.value = '';
    });
    var labelEl = document.getElementById('viz-date-range-label');
    if (labelEl) labelEl.textContent = '';
    window.__vizDateFilterActive = false;
    if (typeof refreshAllDateScopedViews === 'function') {
        refreshAllDateScopedViews();
    } else {
        renderCalls();
        if (typeof renderManifest === 'function') renderManifest();
    }
}

function clearTimeFilters() {
    const fromEl = document.getElementById('filter-time-from');
    const toEl = document.getElementById('filter-time-to');
    if (fromEl) fromEl.value = '';
    if (toEl) toEl.value = '';
    renderCalls();
}

const STRICT_GAP_CORE_ROLES = new Set([]);

function updateInterCallGapControls(state) {
    const gapEl = document.getElementById('campaign-inter-call-gap');
    const saveBtn = document.getElementById('campaign-inter-call-gap-save');
    const noteEl = document.getElementById('campaign-inter-call-gap-note');
    if (!gapEl) return;

    const role = typeof apiRoleQ === 'function' ? apiRoleQ() : (typeof currentRole !== 'undefined' ? currentRole : 'sales_1');
    const strict = !!(state && state.inter_call_gap_strict) || STRICT_GAP_CORE_ROLES.has(role);
    const sec = state && state.inter_call_gap_sec != null
        ? Math.round(Number(state.inter_call_gap_sec))
        : (strict ? 150 : 5);

    gapEl.value = Number.isFinite(sec) ? String(sec) : (strict ? '150' : '5');
    gapEl.readOnly = strict;
    gapEl.disabled = strict;
    gapEl.style.opacity = strict ? '0.65' : '1';
    if (saveBtn) saveBtn.disabled = strict;

    if (noteEl) {
        if (strict) {
            const lo = (state && state.inter_call_gap_min_sec) || 135;
            const hi = (state && state.inter_call_gap_max_sec) || 165;
            noteEl.innerHTML =
                'Wait time after each outbound call before dialing the next lead for <strong>this role</strong>. '
                + `A fixed <strong>${sec}s</strong> carrier-safety pause is enforced `
                + `(${lo}–${hi}s band; not configurable).`;
        } else {
            noteEl.innerHTML =
                'Wait time after each outbound call before dialing the next lead for <strong>this role</strong>. '
                + 'Enter 0 for no gap, or up to 1200 seconds, then save.';
        }
    }
}

async function saveInterCallGap() {
    const gapEl = document.getElementById('campaign-inter-call-gap');
    if (!gapEl || typeof apiRoleQ !== 'function' || typeof token !== 'function') return;
    const role = apiRoleQ();
    if (STRICT_GAP_CORE_ROLES.has(role)) {
        showToast('This role uses a fixed 150s carrier-safety pause and cannot be changed.', 'error');
        return;
    }
    const raw = Number(gapEl.value);
    if (!Number.isFinite(raw) || raw < 0 || raw > 1200) {
        showToast('Enter a pause between 0 and 1200 seconds.', 'error');
        return;
    }
    try {
        const res = await fetch(apiUrl(`/api/campaign/inter-call-gap?role=${apiRoleQ()}`), {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token()}`, 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify({ seconds: raw }),
        });
        const data = await res.json().catch(() => ({}));
        if (res.status === 401 && typeof logout === 'function') logout();
        if (!res.ok) {
            const detail = typeof data.detail === 'string' ? data.detail : (Array.isArray(data.detail) && data.detail[0]?.msg) || res.statusText;
            throw new Error(detail || 'Save failed');
        }
        const sec = data.inter_call_gap_sec != null ? data.inter_call_gap_sec : raw;
        showToast(`Pause between calls: ${sec}s for this role`, 'success');
        await syncState();
    } catch (e) {
        showToast((e && e.message) ? e.message : 'Could not save pause', 'error');
    }
}

// ─── Re-analyze All ───
let _reanalyzePollTimer = null;

function showReanalyzeModal() {
    const m = document.getElementById('modal-reanalyze-all');
    if (m) {
        m.classList.add('modal-open');
        // Reset UI
        document.getElementById('reanalyze-progress-bar').style.width = '0%';
        document.getElementById('reanalyze-progress-text').textContent = '0 / 0';
        document.getElementById('reanalyze-status').textContent = 'Starting...';
        document.getElementById('reanalyze-current').textContent = '\u00a0';
        const errEl = document.getElementById('reanalyze-errors');
        errEl.style.display = 'none';
        errEl.innerHTML = '';
        document.getElementById('btn-reanalyze-cancel').style.display = 'inline-flex';
        document.getElementById('btn-reanalyze-close').style.display = 'none';
    }
}

function startReanalyzeAll() {
    const btn = document.getElementById('btn-reanalyze-all');
    if (btn) btn.disabled = true;
    showReanalyzeModal();
        fetch(apiUrl('/api/campaign/reanalyze-all?role=' + apiRoleQ()), {
        method: 'POST',
        headers: { 'Authorization': `Bearer ${token()}`, 'Content-Type': 'application/json' },
        credentials: 'same-origin',
    }).then(r => r.json()).then(data => {
        if (data.total) {
            document.getElementById('reanalyze-progress-text').textContent = `0 / ${data.total}`;
            document.getElementById('reanalyze-status').textContent = `Processing ${data.total} leads...`;
            _pollReanalyzeProgress();
        } else {
            document.getElementById('reanalyze-status').textContent = 'No eligible leads found.';
            _finishReanalyzeAll();
        }
    }).catch(err => {
        try {
            const resp = err.response;
            if (resp && resp.status === 409) {
                document.getElementById('reanalyze-status').textContent = 'Already running.';
            } else {
                document.getElementById('reanalyze-status').textContent = 'Error: ' + (err.message || 'unknown');
            }
        } catch (_) {
            document.getElementById('reanalyze-status').textContent = 'Error starting re-analysis.';
        }
        _finishReanalyzeAll();
    });
}

function _pollReanalyzeProgress() {
    if (_reanalyzePollTimer) clearTimeout(_reanalyzePollTimer);
    fetch(apiUrl('/api/campaign/reanalyze-all/progress?role=' + apiRoleQ()), {
        headers: { 'Authorization': `Bearer ${token()}` },
        credentials: 'same-origin',
    }).then(r => r.json()).then(state => {
        const total = state.total || 0;
        const done = state.completed || 0;
        const pct = total > 0 ? Math.round((done / total) * 100) : 0;
        document.getElementById('reanalyze-progress-bar').style.width = pct + '%';
        document.getElementById('reanalyze-progress-text').textContent = `${done} / ${total}`;
        document.getElementById('reanalyze-current').textContent = state.current || '\u00a0';
        if (state.errors && state.errors.length) {
            const errEl = document.getElementById('reanalyze-errors');
            errEl.style.display = 'block';
            errEl.innerHTML = state.errors.map(e => '<div>' + escapeHtml(e) + '</div>').join('');
        }
        if (state.running) {
            _reanalyzePollTimer = setTimeout(_pollReanalyzeProgress, 2000);
        } else {
            document.getElementById('reanalyze-status').textContent = 'Done! Refreshing...';
            _finishReanalyzeAll();
        }
    }).catch(() => {
        _reanalyzePollTimer = setTimeout(_pollReanalyzeProgress, 3000);
    });
}

function _finishReanalyzeAll() {
    if (_reanalyzePollTimer) { clearTimeout(_reanalyzePollTimer); _reanalyzePollTimer = null; }
    const btn = document.getElementById('btn-reanalyze-all');
    if (btn) btn.disabled = false;
    document.getElementById('btn-reanalyze-cancel').style.display = 'none';
    document.getElementById('btn-reanalyze-close').style.display = 'inline-flex';
    // refresh state
    syncState();
}

function cancelReanalyzeAll() {
    if (_reanalyzePollTimer) { clearTimeout(_reanalyzePollTimer); _reanalyzePollTimer = null; }
    fetch(apiUrl('/api/campaign/reanalyze-all/cancel?role=' + apiRoleQ()), {
        method: 'POST',
        headers: { 'Authorization': `Bearer ${token()}`, 'Content-Type': 'application/json' },
        credentials: 'same-origin',
    }).catch(() => {});
    const btn = document.getElementById('btn-reanalyze-all');
    if (btn) btn.disabled = false;
    document.getElementById('reanalyze-status').textContent = 'Cancelled.';
    document.getElementById('btn-reanalyze-cancel').style.display = 'none';
    document.getElementById('btn-reanalyze-close').style.display = 'inline-flex';
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.appendChild(document.createTextNode(str));
    return div.innerHTML;
}

function escapeAttr(str) {
    return String(str || '')
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;')
        .replace(/</g, '&lt;');
}

function clearCategoryOverviewCache() {
    const tbody = document.getElementById('category-overview-tbody');
    if (!tbody) return;
    delete tbody.dataset.sig;
    tbody.innerHTML = '<tr><td colspan="13" style="padding:24px;text-align:center;color:var(--text-secondary);font-size:12px;">Loading category data…</td></tr>';
}

var _uploadSourcesClickBound = false;

function initUploadSourceActions() {
    const wrap = document.getElementById('upload-sources-list');
    if (!wrap || _uploadSourcesClickBound) return;
    _uploadSourcesClickBound = true;
    wrap.addEventListener('click', function (e) {
        const btn = e.target.closest('[data-upload-action]');
        if (!btn || btn.disabled) return;
        e.preventDefault();
        e.stopPropagation();
        const action = btn.getAttribute('data-upload-action');
        const source = btn.getAttribute('data-upload-source') || '';
        if (action === 'toggle') toggleUploadSource(source, btn);
        else if (action === 'delete') deleteUploadSource(source, btn);
        else if (action === 'run-only') runOnlySource(source, btn);
        else if (action === 'sandbox-exit') runOnlySource('', btn);
    });
}

// ─── Phone Numbers ───
async function loadPhoneNumbers() {
    const section = document.getElementById('phone-numbers-section');
    const list = document.getElementById('phone-numbers-list');
    const stats = document.getElementById('phone-number-stats');
    
    if (!section || !list || !stats) return;
    
    if (currentRole !== 'sales_1') {
        section.style.display = 'none';
        return;
    }
    
    section.style.display = 'block';
    
    try {
        const res = await fetch(apiUrl(`/api/campaign/phone-numbers?role=${apiRoleQ()}`), {
            headers: { 'Authorization': `Bearer ${token()}` },
            credentials: 'same-origin',
        });
        
        if (!res.ok) {
            _showDefaultPhoneNumbers();
            return;
        }
        
        const data = await res.json();
        const numbers = data.phone_numbers || [];
        
        if (numbers.length === 0) {
            _showDefaultPhoneNumbers();
            return;
        }
        
        list.innerHTML = numbers.map((num, idx) => {
            const phoneLabel = currentRole === 'sales_1' 
                ? `Phone ${idx + 1}` 
                : `Phone ${idx + 3}`;
            
            return `
                <div style="display:flex;align-items:center;justify-content:space-between;padding:10px 12px;background:rgba(52,199,89,.08);border:1px solid rgba(52,199,89,.25);border-radius:8px;">
                    <div style="display:flex;align-items:center;gap:10px;">
                        <div style="width:8px;height:8px;border-radius:50%;background:var(--success);"></div>
                        <div>
                            <div style="font-size:12px;font-weight:600;color:var(--text);">${phoneLabel}</div>
                            <div style="font-size:11px;color:var(--text-secondary);font-family:var(--font-mono);">${num}</div>
                        </div>
                    </div>
                    <div style="text-align:right;">
                        <div style="font-size:11px;font-weight:600;color:var(--success);">
                            Active
                        </div>
                    </div>
                </div>
            `;
        }).join('');
        
        stats.innerHTML = `
            <div style="display:flex;justify-content:space-between;align-items:center;">
                <span>Call Rate: ${data.total_calls_this_hour}/${data.max_calls_per_hour} per hour</span>
                <span style="font-weight:600;color:var(--success);">Parallel dialing active</span>
            </div>
        `;
        
    } catch (e) {
        console.error('Failed to load phone numbers:', e);
        _showDefaultPhoneNumbers();
    }
}

function _showDefaultPhoneNumbers() {
    const list = document.getElementById('phone-numbers-list');
    const stats = document.getElementById('phone-number-stats');
    
    if (!list || !stats) return;
    
    if (currentRole === 'sales_1') {
        list.innerHTML = `
            <div style="display:flex;align-items:center;justify-content:space-between;padding:10px 12px;background:rgba(0,0,0,.03);border:1px solid var(--border);border-radius:8px;">
                <div style="display:flex;align-items:center;gap:10px;">
                    <div style="width:8px;height:8px;border-radius:50%;background:var(--text-secondary);"></div>
                    <div>
                        <div style="font-size:12px;font-weight:600;color:var(--text);">Phone 1</div>
                        <div style="font-size:11px;color:var(--text-secondary);font-family:var(--font-mono);">Not configured</div>
                    </div>
                </div>
                <div style="text-align:right;">
                    <div style="font-size:11px;font-weight:600;color:var(--text-secondary);">Standby</div>
                </div>
            </div>
            <div style="display:flex;align-items:center;justify-content:space-between;padding:10px 12px;background:rgba(0,0,0,.03);border:1px solid var(--border);border-radius:8px;">
                <div style="display:flex;align-items:center;gap:10px;">
                    <div style="width:8px;height:8px;border-radius:50%;background:var(--text-secondary);"></div>
                    <div>
                        <div style="font-size:12px;font-weight:600;color:var(--text);">Phone 2</div>
                        <div style="font-size:11px;color:var(--text-secondary);font-family:var(--font-mono);">Not configured</div>
                    </div>
                </div>
                <div style="text-align:right;">
                    <div style="font-size:11px;font-weight:600;color:var(--text-secondary);">Standby</div>
                </div>
            </div>
        `;
    }

    stats.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center;">
            <span>Call Rate: 0/42 per hour</span>
            <span style="font-weight:600;color:var(--primary);">20-22 calls per number</span>
        </div>
    `;
}

// ─── Category Overview Table ───
let _lastCategoryOverviewLoad = 0;
async function loadCategoryOverview(force) {
    const tbody = document.getElementById('category-overview-tbody');
    if (!tbody) return;
    if (!force && (Date.now() - _lastCategoryOverviewLoad) < 60000) return;
    const hasCachedRows = !!(tbody.dataset.sig && tbody.dataset.sig.length > 0);
    if (!hasCachedRows && (!tbody.innerHTML || tbody.innerHTML.includes('Loading category data…') || tbody.innerHTML.includes('Failed to load') || tbody.innerHTML.includes('No category data available'))) {
        tbody.innerHTML = '<tr><td colspan="13" style="padding:24px;text-align:center;color:var(--text-secondary);font-size:12px;">Loading category data…</td></tr>';
    }
    try {
        const res = await fetch(apiUrl(`/api/campaign/kpi-summary?role=${apiRoleQ()}`), {
            headers: { 'Authorization': `Bearer ${token()}` },
            credentials: 'same-origin',
        });
        if (!res.ok) {
            tbody.innerHTML = '<tr><td colspan="13" style="padding:24px;text-align:center;color:var(--text-secondary);font-size:12px;">Failed to load</td></tr>';
            return;
        }
        _lastCategoryOverviewLoad = Date.now();
        const data = await res.json();
        const rows = data.kpi || [];
        const totals = data.totals || null;
        if (!rows.length) {
            const emptyHtml = '<tr><td colspan="13" style="padding:24px;text-align:center;color:var(--text-secondary);font-size:12px;">No category data yet. Upload leads to see breakdown by file.</td></tr>';
            if (tbody.dataset.sig !== emptyHtml) {
                tbody.innerHTML = emptyHtml;
                tbody.dataset.sig = emptyHtml;
            }
            return;
        }
        let html = '';
        rows.forEach(function(r) {
            html += '<tr style="border-bottom:1px solid var(--border);">';
            html += '<td style="padding:10px 14px;font-weight:600;color:var(--text);text-align:left;position:sticky;left:0;background:var(--surface);">' + escapeHtml(r.category) + '</td>';
            html += '<td style="padding:10px 14px;text-align:right;color:var(--text);font-weight:600;">' + (r.total_leads || 0) + '</td>';
            html += '<td style="padding:10px 14px;text-align:right;color:var(--text);">' + (r.calls_made || 0) + '</td>';
            html += '<td style="padding:10px 14px;text-align:right;color:var(--text);">' + (r.connected || 0) + '</td>';
            html += '<td style="padding:10px 14px;text-align:right;color:var(--danger);">' + (r.failed_calls || 0) + '</td>';
            html += '<td style="padding:10px 14px;text-align:right;color:var(--success);font-weight:600;">' + (r.interested || 0) + '</td>';
            html += '<td style="padding:10px 14px;text-align:right;color:var(--danger);">' + (r.not_interested || 0) + '</td>';
            html += '<td style="padding:10px 14px;text-align:right;color:var(--text-secondary);">' + (r.no_response || 0) + '</td>';
            html += '<td style="padding:10px 14px;text-align:right;color:var(--text-secondary);">' + (r.voicemail || 0) + '</td>';
            html += '<td style="padding:10px 14px;text-align:right;color:var(--primary);font-weight:600;">' + (r.site_visit || 0) + '</td>';
            html += '<td style="padding:10px 14px;text-align:right;color:var(--warning);">' + (r.callback || 0) + '</td>';
            html += '<td style="padding:10px 14px;text-align:right;color:var(--text);">' + (r.whatsapp_sent || 0) + '</td>';
            html += '<td style="padding:10px 14px;text-align:right;color:var(--text);">' + (r.email_sent || 0) + '</td>';
            html += '</tr>';
        });
        if (totals) {
            html += '<tr style="background:rgba(59,130,246,.08);border-top:2px solid var(--primary);font-weight:700;">';
            html += '<td style="padding:10px 14px;font-weight:800;color:var(--text);text-align:left;position:sticky;left:0;background:rgba(59,130,246,.08);">TOTAL</td>';
            html += '<td style="padding:10px 14px;text-align:right;color:var(--text);font-weight:700;">' + (totals.total_leads || 0) + '</td>';
            html += '<td style="padding:10px 14px;text-align:right;color:var(--text);font-weight:700;">' + (totals.calls_made || 0) + '</td>';
            html += '<td style="padding:10px 14px;text-align:right;color:var(--text);font-weight:700;">' + (totals.connected || 0) + '</td>';
            html += '<td style="padding:10px 14px;text-align:right;color:var(--danger);font-weight:700;">' + (totals.failed_calls || 0) + '</td>';
            html += '<td style="padding:10px 14px;text-align:right;color:var(--success);font-weight:700;">' + (totals.interested || 0) + '</td>';
            html += '<td style="padding:10px 14px;text-align:right;color:var(--danger);font-weight:700;">' + (totals.not_interested || 0) + '</td>';
            html += '<td style="padding:10px 14px;text-align:right;color:var(--text-secondary);font-weight:700;">' + (totals.no_response || 0) + '</td>';
            html += '<td style="padding:10px 14px;text-align:right;color:var(--text-secondary);font-weight:700;">' + (totals.voicemail || 0) + '</td>';
            html += '<td style="padding:10px 14px;text-align:right;color:var(--primary);font-weight:700;">' + (totals.site_visit || 0) + '</td>';
            html += '<td style="padding:10px 14px;text-align:right;color:var(--warning);font-weight:700;">' + (totals.callback || 0) + '</td>';
            html += '<td style="padding:10px 14px;text-align:right;color:var(--text);font-weight:700;">' + (totals.whatsapp_sent || 0) + '</td>';
            html += '<td style="padding:10px 14px;text-align:right;color:var(--text);font-weight:700;">' + (totals.email_sent || 0) + '</td>';
            html += '</tr>';
        }
        // Only re-render when the data actually changed — prevents 4s poll flicker.
        if (tbody.dataset.sig !== html) {
            tbody.innerHTML = html;
            tbody.dataset.sig = html;
        }
    } catch (e) {
        tbody.innerHTML = '<tr><td colspan="13" style="padding:24px;text-align:center;color:var(--text-secondary);font-size:12px;">Error loading data</td></tr>';
    }
}

// ─── Uploaded Sources (Pause / Play) ───
async function loadUploadSources() {
    const wrap = document.getElementById('upload-sources-list');
    if (!wrap) return;
    if (!wrap.innerHTML || wrap.innerHTML.includes('Loading…') || wrap.innerHTML.includes('No uploaded files yet') || wrap.innerHTML.includes('Failed to load') || wrap.innerHTML.includes('Error loading')) {
        wrap.innerHTML = '<div style="text-align:center;padding:var(--space-lg);color:var(--text-secondary);font-size:13px;">Loading…</div>';
    }
    try {
        const res = await fetch(apiUrl(`/api/campaign/sources?role=${apiRoleQ()}`), {
            headers: { 'Authorization': `Bearer ${token()}` },
            credentials: 'same-origin',
        });
        if (!res.ok) {
            wrap.innerHTML = '<div style="text-align:center;padding:var(--space-lg);color:var(--text-secondary);font-size:13px;">Failed to load sources</div>';
            return;
        }
        const data = await res.json();
        const sources = data.sources || [];
        if (!sources.length) {
            wrap.innerHTML = '<div style="text-align:center;padding:var(--space-lg);color:var(--text-secondary);font-size:13px;">No uploaded files yet.</div>';
            return;
        }

        // Detect sandbox mode: exactly one source is running, all others paused
        const pausedNames = data.paused_sources || [];
        const runningCount = sources.filter(s => !s.paused).length;
        const isSandbox = runningCount === 1 && pausedNames.length > 0;
        const sandboxSource = isSandbox ? sources.find(s => !s.paused) : null;

        let html = '';

        if (data.mode === 'site_visit_followup') {
            html += '<div style="display:flex;align-items:center;justify-content:space-between;gap:10px;'
                  + 'padding:10px 14px;border-radius:10px;margin-bottom:10px;'
                  + 'background:rgba(99,102,241,0.12);border:1px solid rgba(99,102,241,0.4);">'
                  + '<div style="font-size:12px;font-weight:600;color:#6366f1;">'
                  + '📞 Site Visit Follow-Up Mode Active — Dialing Site Visit Candidate Leads'
                  + '</div>'
                  + '</div>';
        }

        // Sandbox banner
        if (isSandbox && sandboxSource) {
            html += '<div style="display:flex;align-items:center;justify-content:space-between;gap:10px;'
                  + 'padding:10px 14px;border-radius:10px;margin-bottom:10px;'
                  + 'background:rgba(251,191,36,0.12);border:1px solid rgba(251,191,36,0.4);">'
                  + '<div style="font-size:12px;font-weight:600;color:#f59e0b;">'
                  + '⚡ Sandbox mode — dialing only: <span style="font-style:italic;">' + escapeHtml(sandboxSource.name) + '</span>'
                  + '</div>'
                  + '<button type="button" data-upload-action="sandbox-exit" data-upload-source="" style="background:#f59e0b;color:#000;border:none;border-radius:6px;padding:5px 12px;font-size:11px;font-weight:700;cursor:pointer;">Resume All</button>'
                  + '</div>';
        }

        sources.forEach(function (s) {
            const paused = !!s.paused;
            const isActive = isSandbox && !paused;
            const accent = paused ? 'var(--danger)' : 'var(--success)';
            const borderColor = isActive ? '#f59e0b' : 'var(--border)';
            const bg = isActive ? 'rgba(251,191,36,0.06)' : 'var(--surface)';

            html += '<div style="display:flex;align-items:center;justify-content:space-between;gap:10px;padding:12px 14px;'
                  + 'border:1px solid ' + borderColor + ';border-radius:10px;background:' + bg + ';margin-bottom:6px;">';
            html += '<div style="min-width:0;flex:1;">';
            html += '<div style="font-size:13px;font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">' + escapeHtml(s.name) + '</div>';
            html += '<div style="font-size:11px;color:var(--text-secondary);margin-top:2px;">';
            html += (s.total || 0) + ' leads · ' + (s.pending || 0) + ' pending · ' + (s.called || 0) + ' called';
            html += '</div>';
            html += '</div>';
            const srcAttr = escapeAttr(s.name);
            // Buttons row
            html += '<div style="display:flex;gap:6px;flex:none;">';
            html += '<button type="button" data-upload-action="toggle" data-upload-source="' + srcAttr + '" '
                  + 'style="border:1px solid ' + accent + ';color:' + accent + ';background:transparent;'
                  + 'padding:5px 11px;border-radius:6px;font-size:11px;font-weight:600;cursor:pointer;">'
                  + (paused ? '▶ Play' : '⏸ Pause') + '</button>';
            if (!isActive) {
                html += '<button type="button" data-upload-action="run-only" data-upload-source="' + srcAttr + '" '
                      + 'style="border:1px solid #f59e0b;color:#f59e0b;background:transparent;'
                      + 'padding:5px 11px;border-radius:6px;font-size:11px;font-weight:600;cursor:pointer;">⚡ Run Only</button>';
            }
            html += '<button type="button" data-upload-action="delete" data-upload-source="' + srcAttr + '" '
                  + 'style="border:1px solid var(--danger);color:var(--danger);background:transparent;'
                  + 'padding:5px 11px;border-radius:6px;font-size:11px;font-weight:600;cursor:pointer;">🗑 Delete</button>';
            html += '</div>';
            html += '</div>';
        });

        // Only re-render when the data actually changed — prevents 4s poll flicker.
        if (wrap.dataset.sig !== html) {
            wrap.innerHTML = html;
            wrap.dataset.sig = html;
        }
    } catch (e) {
        wrap.innerHTML = '<div style="text-align:center;padding:var(--space-lg);color:var(--text-secondary);font-size:13px;">Error loading sources</div>';
    }
}

async function toggleUploadSource(sourceName, btn) {
    if (!sourceName) return;
    const prevText = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = '…'; }
    try {
        const res = await fetch(apiUrl(`/api/campaign/sources/toggle?role=${apiRoleQ()}`), {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token()}`, 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify({ source: sourceName }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            if (typeof showToast === 'function') showToast(data.detail || 'Failed to toggle source', 'error');
            return;
        }
        const wrap = document.getElementById('upload-sources-list');
        if (wrap) delete wrap.dataset.sig;
        await loadUploadSources();
        if (typeof showToast === 'function') showToast((data.toggled || sourceName) + ' toggled', 'success');
    } catch (e) {
        if (typeof showToast === 'function') showToast('Failed to toggle source', 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = prevText; }
    }
}

async function deleteUploadSource(sourceName, btn) {
    if (!sourceName) return;
    if (!confirm('Delete all leads from "' + sourceName + '"? This cannot be undone.')) return;
    const prevText = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = '…'; }
    try {
        const res = await fetch(apiUrl(`/api/campaign/sources?role=${apiRoleQ()}&source=${encodeURIComponent(sourceName)}`), {
            method: 'DELETE',
            headers: { 'Authorization': `Bearer ${token()}` },
            credentials: 'same-origin',
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            if (typeof showToast === 'function') showToast(data.detail || 'Failed to delete source', 'error');
            return;
        }
        const wrap = document.getElementById('upload-sources-list');
        if (wrap) delete wrap.dataset.sig;
        clearCategoryOverviewCache();
        await loadUploadSources();
        if (typeof loadCategoryOverview === 'function') await loadCategoryOverview();
        if (typeof quietSyncState === 'function') quietSyncState();
        if (typeof showToast === 'function') showToast('Deleted ' + (data.deleted || 0) + ' leads from ' + sourceName, 'success');
    } catch (e) {
        if (typeof showToast === 'function') showToast('Failed to delete source', 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = prevText; }
    }
}

async function runOnlySource(sourceName, btn) {
    /**
     * Sandbox mode: pause all other sources and run only the selected one.
     * Pass sourceName="" to exit sandbox and resume all.
     */
    const prevText = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = '…'; }
    try {
        const label = sourceName ? 'Activating sandbox for: ' + sourceName : 'Resuming all sources…';
        if (typeof showToast === 'function') showToast(label, 'info');
        const res = await fetch(apiUrl(`/api/campaign/sources/run-only?role=${apiRoleQ()}`), {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token()}`, 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify({ source: sourceName }),
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            if (typeof showToast === 'function') showToast(err.detail || 'Failed to set sandbox mode', 'error');
            return;
        }
        const data = await res.json();
        const wrap = document.getElementById('upload-sources-list');
        if (wrap) delete wrap.dataset.sig;
        await loadUploadSources();
        if (typeof quietSyncState === 'function') quietSyncState();
        if (data.mode === 'sandbox') {
            if (typeof showToast === 'function') showToast('Sandbox: running only "' + sourceName + '"', 'success');
        } else {
            if (typeof showToast === 'function') showToast('All sources resumed', 'success');
        }
    } catch (e) {
        if (typeof showToast === 'function') showToast('Failed to set sandbox mode', 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = prevText; }
    }
}

// ─── Manual Call ───
// triggerManualTest, modal, and recent list live in restored.js (loaded after this file).

// ─── Inbound Call Back ───
async function dialInboundBack(inboundId) {
    if (typeof incomingCallsList === 'undefined' || !incomingCallsList.length) {
        if (typeof showToast === 'function') showToast('No inbound calls data available', 'error');
        return;
    }
    var numericId = parseInt(inboundId);
    if (isNaN(numericId)) {
        if (typeof showToast === 'function') showToast('Invalid inbound call ID', 'error');
        return;
    }
    var callData = incomingCallsList.find(function(c) { return c.id === numericId; });
    if (!callData) {
        if (typeof showToast === 'function') showToast('Inbound call not found', 'error');
        return;
    }
    var phone = callData.phone || callData.from || '';
    var name = callData.name || 'Inbound Call';
    if (!phone) {
        if (typeof showToast === 'function') showToast('No phone number for this inbound call', 'error');
        return;
    }
    try {
        var resp = await fetch(apiUrl('/api/manual/call?role=' + (typeof manualCallRoleQ === 'function' ? manualCallRoleQ() : encodeURIComponent('sales_1'))), {
            method: 'POST',
            headers: authHeaders(),
            credentials: 'same-origin',
            body: JSON.stringify({ to: phone, callee_name: name }),
        });
        if (!resp.ok) {
            var errText = (await resp.json().catch(function(){return{};})).detail || resp.statusText;
            throw new Error(errText);
        }
        if (typeof showToast === 'function') showToast('Calling ' + name + ' back...', 'success');
    } catch (e) {
        if (typeof showToast === 'function') showToast('Failed to call back: ' + e.message, 'error');
    }
}

function manualSendDetails() {
    var phone = (document.getElementById('manual-send-phone')?.value || '').trim();
    var email = (document.getElementById('manual-send-email')?.value || '').trim();
    var name = (document.getElementById('manual-send-name')?.value || '').trim();
    var channel = document.getElementById('manual-send-channel')?.value || 'both';
    if (!phone && !email) {
        if (typeof showToast === 'function') showToast('Enter at least phone or email.', 'error');
        return;
    }
    if ((channel === 'email' || channel === 'both') && email && email.indexOf('@') < 0) {
        if (typeof showToast === 'function') showToast('Invalid email address.', 'error');
        return;
    }
    if (typeof showToast === 'function') showToast('Sending details...', 'info');
    var body = { channel: channel };
    if (phone) body.phone = phone;
    if (email) body.email = email;
    if (name) body.name = name;
    fetch(apiUrl('/api/leads/send-details?role=' + apiRoleQ()), {
        method: 'POST',
        headers: authHeaders(),
        credentials: 'same-origin',
        body: JSON.stringify(body),
    }).then(function (r) {
        if (!r.ok) throw new Error('Send failed');
        return r.json();
    }).then(function (d) {
        if (typeof showToast === 'function') showToast('Details sent successfully!', 'success');
        document.getElementById('manual-send-card').style.display = 'none';
        document.getElementById('manual-send-phone').value = '';
        document.getElementById('manual-send-email').value = '';
        document.getElementById('manual-send-name').value = '';
    }).catch(function (e) {
        if (typeof showToast === 'function') showToast('Failed: ' + e.message, 'error');
    });
}

async function triggerFollowupCalls() {
    try {
        var role = (typeof currentConsoleRole === 'function') ? currentConsoleRole() : 'sales_1';
        if (typeof showToast === 'function') showToast('Stopping regular campaign and triggering Site Visit Follow-Up calls...', 'info');
        
        var resp = await fetch('/api/campaign/trigger-followup?role=' + encodeURIComponent(role), {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': typeof getToken === 'function' ? 'Bearer ' + getToken() : ''
            }
        });
        
        if (!resp.ok) {
            var err = await resp.json();
            throw new Error(err.detail || 'Failed to trigger follow-up calls');
        }
        
        var data = await resp.json();
        if (typeof showToast === 'function') {
            showToast(data.message || 'Follow-Up calls started successfully!', 'success');
        }
        
        if (typeof fetchCampaignState === 'function') {
            fetchCampaignState();
        }
    } catch (e) {
        console.error('triggerFollowupCalls error:', e);
        if (typeof showToast === 'function') {
            showToast('Error: ' + e.message, 'error');
        } else {
            alert('Error starting follow-up calls: ' + e.message);
        }
    }
}

function openFollowupDetailModal() {
    if (typeof openStatDetail === 'function') {
        openStatDetail('followup_site_visit');
    }
}

// Expose upload-source handlers globally and bind delegated clicks once.
window.toggleUploadSource = toggleUploadSource;
window.deleteUploadSource = deleteUploadSource;
window.runOnlySource = runOnlySource;
window.loadUploadSources = loadUploadSources;
window.loadCategoryOverview = loadCategoryOverview;
window.clearCategoryOverviewCache = clearCategoryOverviewCache;
window.triggerFollowupCalls = triggerFollowupCalls;
window.openFollowupDetailModal = openFollowupDetailModal;

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initUploadSourceActions);
} else {
    initUploadSourceActions();
}
