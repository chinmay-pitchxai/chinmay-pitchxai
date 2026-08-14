let chartTimeline = null;
let chartPie = null;
let chartConversion = null;
let chartProgress = null;
let chartWeekday = null;
let chartHourly = null;
let sparkCharts = {};

const CHART_COLORS = ['#DC2626', '#16A34A', '#D97706', '#9333EA', '#DB2777', '#EA580C', '#0891B2', '#65A30D'];
const CHART_WEEKDAY_SHORT = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

// ─── Chart Click-to-Filter State ───
let chartFilterState = {
    type: null,
    value: null,
    label: '',
};

function getChartFilter() { return chartFilterState; }

function setChartFilter(type, value, label) {
    chartFilterState = { type: type, value: value, label: label };
    updateChartFilterBanner();
    updateActivePills();
    if (typeof renderCalls === 'function') renderCalls();
}

function clearChartFilter() {
    chartFilterState = { type: null, value: null, label: '' };
    updateChartFilterBanner();
    updateActivePills();
    if (typeof renderCalls === 'function') renderCalls();
}

function updateActivePills() {
    document.querySelectorAll('.chart-pill').forEach(function(p) { p.classList.remove('active'); });
    if (!chartFilterState.type) return;
    var selectors = {
        'pie': '.chart-pill[data-filter-type="pie"][data-value="' + chartFilterState.value + '"]',
        'weekday': '.chart-pill[data-filter-type="weekday"][data-value="' + chartFilterState.value + '"]',
        'progress': '.chart-pill[data-filter-type="progress"][data-value="' + chartFilterState.value + '"]',
        'hourly-range': '.chart-pill[data-filter-type="hourly"][data-value="' + chartFilterState.value + '"]',
    };
    var sel = selectors[chartFilterState.type];
    if (sel) document.querySelectorAll(sel).forEach(function(p) { p.classList.add('active'); });
}

function updateChartFilterBanner() {
    var banner = document.getElementById('chart-filter-banner');
    var text = document.getElementById('chart-filter-text');
    if (!banner || !text) return;
    if (chartFilterState.type && chartFilterState.label) {
        banner.style.display = 'flex';
        text.textContent = 'Filtered by: ' + chartFilterState.label;
    } else {
        banner.style.display = 'none';
        text.textContent = '';
    }
    var chartIds = {
        'timeline': 'chart-timeline', 'pie': 'chart-pie', 'weekday': 'chart-weekday',
        'hourly': 'chart-hourly', 'hourly-range': 'chart-hourly', 'progress': 'chart-progress',
    };
    document.querySelectorAll('.card').forEach(function(c) { c.classList.remove('chart-filter-active'); });
    if (chartFilterState.type && chartIds[chartFilterState.type]) {
        var el = document.getElementById(chartIds[chartFilterState.type]);
        if (el && el.parentElement) el.parentElement.classList.add('chart-filter-active');
    }
}

// ─── Master filter function used by renderCalls ───
function isLeadMatchingChartFilter(lead) {
    if (!chartFilterState.type) return true;
    var type = chartFilterState.type;
    var val = chartFilterState.value;

    // PIE / OUTCOME filter
    if (type === 'pie') {
        var d = effectiveDispo(lead);
        if (val === 'Interested') return d === 'Interested';
        if (val === 'Site Visited') return hasSiteVisitWithParticularDate(lead);
        if (val === 'Call Later') return isFollowUpLead(lead);
        if (val === 'Failed') return d === 'Failed' || isFailed(lead);
        if (val === 'Answered') return d === 'Answered';
        if (val === 'No Response') return d === 'No Response';
        if (val === 'Voice Mail') return d === 'Voice Mail';
        return false;
    }
    // WEEKDAY filter
    if (type === 'weekday') {
        var ms = leadTimelineMs(lead);
        if (isNaN(ms)) return false;
        var dayIdx = new Date(ms).getDay();
        var mapped = dayIdx === 0 ? 6 : dayIdx - 1;
        return mapped === val;
    }
    // HOURLY (single hour) filter
    if (type === 'hourly') {
        var ms2 = leadTimelineMs(lead);
        if (isNaN(ms2)) return false;
        return new Date(ms2).getHours() === val;
    }
    // HOURLY RANGE filter
    if (type === 'hourly-range') {
        var ms3 = leadTimelineMs(lead);
        if (isNaN(ms3)) return false;
        var h = new Date(ms3).getHours();
        var parts = String(val).split('-');
        return h >= parseInt(parts[0], 10) && h < parseInt(parts[1], 10);
    }
    // TIMELINE (specific date) filter
    if (type === 'timeline') {
        var ms4 = leadTimelineMs(lead);
        if (isNaN(ms4)) return false;
        return _istDateStr(ms4) === val;
    }
    // PROGRESS (status) filter
    if (type === 'progress') {
        var st = (lead.status || '').toLowerCase();
        if (val === 'Connected') return st === 'completed';
        if (val === 'Failed') return st === 'failed' || st === 'error';
        if (val === 'No Answer') return st === 'no answer' || st === 'busy';
        if (val === 'Pending') return st === 'pending' || !lead.status;
        return false;
    }
    return true;
}

// ─── Pill Click Handlers ───
function togglePieFilter(btn) {
    var val = btn.getAttribute('data-value');
    if (chartFilterState.type === 'pie' && chartFilterState.value === val) { clearChartFilter(); }
    else { setChartFilter('pie', val, 'Outcome: ' + val); }
}

function toggleWeekdayFilter(btn) {
    var val = parseInt(btn.getAttribute('data-value'), 10);
    var days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
    if (chartFilterState.type === 'weekday' && chartFilterState.value === val) { clearChartFilter(); }
    else { setChartFilter('weekday', val, 'Day: ' + days[val]); }
}

// ─── Viz Date Range Filter ───
var _vizLastLeads = [];
var _vizLastDc = {};
var _vizLastCb = {};
var _vizLastTimeline = {};
var _vizLastExtras = {};

function _storeVizData(leads, dc, cb, timeline, extras) {
    _vizLastLeads = leads || [];
    _vizLastDc = dc || {};
    _vizLastCb = cb || {};
    _vizLastTimeline = timeline || {};
    _vizLastExtras = extras || {};
}

function applyVizDateFilter() {
    var fromEl = document.getElementById('viz-date-from');
    var toEl = document.getElementById('viz-date-to');
    var labelEl = document.getElementById('viz-date-range-label');
    var fromVal = fromEl ? fromEl.value : '';
    var toVal = toEl ? toEl.value : '';

    if (!fromVal && !toVal) {
        if (labelEl) labelEl.textContent = '';
        window.__vizDateFilterActive = false;
        updateCharts(_vizLastLeads, _vizLastDc, _vizLastCb, _vizLastTimeline, _vizLastExtras);
        return;
    }

    window.__vizDateFilterActive = true;

    var fromMs = fromVal ? _istDayStartMs(fromVal) : 0;
    var toMs = toVal ? _istDayEndMs(toVal) : Infinity;

    if (labelEl) {
        var parts = [];
        if (fromVal) parts.push('From ' + fromVal);
        if (toVal) parts.push('To ' + toVal);
        labelEl.textContent = parts.join(' — ');
    }

    var filtered = _vizLastLeads.filter(function(l) {
        var ms = leadTimelineMs(l);
        if (isNaN(ms)) return false;
        return ms >= fromMs && ms <= toMs;
    });

    var fDc = {};
    filtered.forEach(function(l) {
        var d = typeof effectiveDispo === 'function' ? effectiveDispo(l) : (l.disposition || '');
        fDc[d] = (fDc[d] || 0) + 1;
    });

    var fCb = {};
    filtered.forEach(function(l) {
        var ms = leadTimelineMs(l);
        if (isNaN(ms)) return;
        var key = _istDateStr(ms);
        var d = typeof effectiveDispo === 'function' ? effectiveDispo(l) : (l.disposition || '');
        if (d === 'Call Later' || d === 'Callback' || d === 'Busy' || d === 'Callback scheduled') {
            fCb[key] = (fCb[key] || 0) + 1;
        }
    });

    var fTimeline = {};
    var dates = [];
    if (fromVal || toVal) {
        var s = fromVal ? _istDayStartMs(fromVal) : (_istDayEndMs(toVal) - 6 * 86400000);
        var e = toVal ? _istDayStartMs(toVal) : Date.now();
        var cur = new Date(s);
        while (cur <= new Date(e)) {
            dates.push(_istDateStr(cur.getTime()));
            cur.setDate(cur.getDate() + 1);
            if (dates.length > 31) break;
        }
        if (dates.length > 14) {
            var step = Math.ceil(dates.length / 14);
            var sampled = [];
            for (var i = 0; i < dates.length; i += step) sampled.push(dates[i]);
            dates = sampled;
        }
    }

    fTimeline.timeline_dates_iso = dates;
    fTimeline.timeline_total_calls = dates.map(function(d) {
        var count = 0;
        filtered.forEach(function(l) {
            var ms = leadTimelineMs(l);
            if (isNaN(ms)) return;
            var ld = _istDateStr(ms);
            if (ld === d) count++;
        });
        return count;
    });
    fTimeline.timeline_interested = dates.map(function(d) {
        var count = 0;
        filtered.forEach(function(l) {
            var ms = leadTimelineMs(l);
            if (isNaN(ms)) return;
            if ((typeof effectiveDispo === 'function' ? effectiveDispo(l) : l.disposition) === 'Interested') {
                var ld = _istDateStr(ms);
                if (ld === d) count++;
            }
        });
        return count;
    });
    fTimeline.timeline_week_labels = dates;

    var fExtras = Object.assign({}, _vizLastExtras);
    fExtras.progressCounts = undefined;
    fExtras.weekdayCounts = undefined;
    fExtras.calledCount = filtered.filter(typeof isCalled === 'function' ? isCalled : function(l) { return !!l.start_time; }).length;

    updateCharts(filtered, fDc, fCb, fTimeline, fExtras);

    updateVizStatCards(filtered);
}

function updateVizStatCards(filtered) {
    var serverTotal = (!window.__vizDateFilterActive &&
        window._lastApiData && typeof window._lastApiData.total === 'number')
        ? window._lastApiData.total : null;
    var nTotal = serverTotal !== null ? serverTotal : filtered.length;
    var called = filtered.filter(typeof isCalled === 'function' ? isCalled : function(l) { return !!l.start_time; });
    var calledCount = (!window.__vizDateFilterActive &&
        window._lastApiData && typeof window._lastApiData.called_count === 'number')
        ? window._lastApiData.called_count : called.length;
    var cb = (!window.__vizDateFilterActive && window._lastApiData && window._lastApiData.called_breakdown)
        ? window._lastApiData.called_breakdown : null;
    var interested;
    var siteVisit;
    var notInterested;
    var failed;
    var callbackCount;
    if (cb && !window.__vizDateFilterActive) {
        interested = Number(cb.interested) || 0;
        siteVisit = Number(cb.site_visit) || 0;
        notInterested = Number(cb.not_interested) || 0;
        failed = Number(cb.failed) || 0;
        callbackCount = Number(cb.callbacks) || 0;
        if (Number.isFinite(Number(cb.total_called)) && Number(cb.total_called) >= 0) {
            calledCount = Number(cb.total_called);
        }
    } else {
        var dc = typeof countDispositionFromLeads === 'function' ? countDispositionFromLeads(filtered) : {};
        interested = Number(dc['Interested'] || 0);
        siteVisit = typeof countSiteVisitFromLeads === 'function' ? countSiteVisitFromLeads(filtered) : 0;
        notInterested = Number(dc['Not Interested'] || 0);
        failed = Number(dc['Failed'] || 0);
        callbackCount = called.filter(isFollowUpLead).length;
    }
    var inboundCallbacksCount = typeof incomingCallsList !== 'undefined' ? incomingCallsList.length : 0;
    var conversionRate = calledCount > 0 ? Math.round((interested / calledCount) * 100) : 0;

    var pctSiteVisit = calledCount > 0 ? Math.round((siteVisit / calledCount) * 100) : 0;
    var pctNotInterested = calledCount > 0 ? Math.round((notInterested / calledCount) * 100) : 0;

    if (typeof updateStat === 'function') {
        updateStat('stat-total', nTotal.toLocaleString());
        updateStat('stat-called', calledCount.toLocaleString());
        updateStat('stat-interested-count', interested);
        updateStat('stat-site-visit', siteVisit);
        updateStat('stat-not-interested', notInterested);
        updateStat('stat-callbacks', inboundCallbacksCount);
        updateStat('stat-failed', failed);
        updateStat('stat-conversion-rate', conversionRate + '%');
        updateStat('stat-attempts', callbackCount.toLocaleString());
    }
    if (typeof updatePct === 'function') {
        updatePct('pct-called', nTotal > 0 ? Math.round((calledCount / nTotal) * 100) : 0);
        updatePct('pct-interested', calledCount > 0 ? Math.round((interested / calledCount) * 100) : 0);
        updatePct('pct-site-visit', pctSiteVisit);
        updatePct('pct-not-interested', pctNotInterested);
        updatePct('pct-callbacks', calledCount > 0 ? Math.round((inboundCallbacksCount / calledCount) * 100) : 0);
        updatePct('pct-attempts', calledCount > 0 ? Math.round((callbackCount / calledCount) * 100) : 0);
        updatePct('pct-failed', calledCount > 0 ? Math.round((failed / calledCount) * 100) : 0);
    }
    if (typeof setProgressWidth === 'function') {
        setProgressWidth('bar-total', 100);
        setProgressWidth('bar-called', nTotal > 0 ? Math.round((calledCount / nTotal) * 100) : 0);
        setProgressWidth('bar-interested', calledCount > 0 ? Math.round((interested / calledCount) * 100) : 0);
        setProgressWidth('bar-site-visit', pctSiteVisit);
        setProgressWidth('bar-not-interested', pctNotInterested);
        setProgressWidth('bar-callbacks', calledCount > 0 ? Math.round((inboundCallbacksCount / calledCount) * 100) : 0);
        setProgressWidth('bar-conversion', conversionRate);
        setProgressWidth('bar-followups', calledCount > 0 ? Math.round((callbackCount / calledCount) * 100) : 0);
        setProgressWidth('bar-failed', calledCount > 0 ? Math.round((failed / calledCount) * 100) : 0);
    }
    var perfAvgEl = document.getElementById('perf-avg-rating');
    var perfCalledEl = document.getElementById('perf-total-called');
    var perfCallbackEl = document.getElementById('perf-callback-rate');
    var perfFailEl = document.getElementById('perf-fail-rate');
    if (perfCalledEl) perfCalledEl.textContent = calledCount;
    if (perfCallbackEl) perfCallbackEl.textContent = (calledCount > 0 ? Math.round((callbackCount / calledCount) * 100) : 0) + '%';
    if (perfFailEl) perfFailEl.textContent = (calledCount > 0 ? Math.round((failed / calledCount) * 100) : 0) + '%';
    if (perfAvgEl) {
        var sum = 0, cnt = 0;
        called.forEach(function(l) {
            var r = l && l.analysis ? parseInt(l.analysis.rating, 10) : (l.rating || null);
            if (Number.isFinite(r) && r >= 1 && r <= 5) { sum += r; cnt++; }
        });
        perfAvgEl.textContent = cnt > 0 ? (sum / cnt).toFixed(1) + ' ★' : '\u2014';
    }
}

function clearVizDateFilter() {
    var ids = ['viz-date-from', 'viz-date-to', 'filter-date-from', 'filter-date-to'];
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
        updateCharts(_vizLastLeads, _vizLastDc, _vizLastCb, _vizLastTimeline, _vizLastExtras);
    }
}

function toggleProgressFilter(btn) {
    var val = btn.getAttribute('data-value');
    if (chartFilterState.type === 'progress' && chartFilterState.value === val) { clearChartFilter(); }
    else { setChartFilter('progress', val, 'Status: ' + val); }
}

function toggleHourlyRangeFilter(btn) {
    var val = btn.getAttribute('data-value');
    var labels = { '6-9': '6AM\u20139AM', '9-12': '9AM\u201312PM', '12-15': '12PM\u20133PM', '15-18': '3PM\u20136PM', '18-21': '6PM\u20139PM' };
    if (chartFilterState.type === 'hourly-range' && chartFilterState.value === val) { clearChartFilter(); }
    else { setChartFilter('hourly-range', val, 'Time: ' + (labels[val] || val)); }
}

function toggleTimelineSeries(btn) {
    if (!chartTimeline) return;
    var name = btn.getAttribute('data-value');
    chartTimeline.toggleSeries(name);
    var collapsed = chartTimeline.w.globals.collapsedSeries || [];
    var series = chartTimeline.w.config.series;
    var idx = -1;
    for (var i = 0; i < series.length; i++) { if (series[i].name === name) { idx = i; break; } }
    if (idx >= 0 && collapsed.indexOf(idx) >= 0) { btn.classList.add('active'); }
    else { btn.classList.remove('active'); }
}



function coerceNumArray7(raw) {
  if (!Array.isArray(raw)) return [];
  return raw.map(function (x) { var n = Number(x); return Number.isFinite(n) ? n : 0; });
}

function formatTimelineAxisLabel(iso) {
  if (!iso || !/^\d{4}-\d{2}-\d{2}$/.test(String(iso).trim())) return String(iso || '');
  var t = String(iso).trim();
  var ms = Date.parse(t + 'T06:30:00.000Z');
  if (isNaN(ms)) return t;
  return new Date(ms).toLocaleDateString('en-IN', { timeZone: 'Asia/Kolkata', weekday: 'short', day: 'numeric', month: 'short' });
}

function getLastSevenPlaceholderCategories() {
  var labels = [], now = new Date();
  for (var i = 0; i < 7; i++) {
    var ms = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - (6 - i));
    labels.push(CHART_WEEKDAY_SHORT[new Date(ms).getUTCDay()]);
  }
  return labels;
}

function getLast7DaysUtc() {
  var out = [], now = new Date(), i;
  for (i = 0; i < 7; i++) {
    var ms = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - (6 - i));
    out.push(CHART_WEEKDAY_SHORT[new Date(ms).getUTCDay()]);
  }
  return out;
}

function leadTimelineMs(lead) {
  if (!lead || typeof lead !== 'object') return NaN;
  var st = lead.start_time;
  if (st != null && Number(st) > 0) return Number(st) * 1000;
  var iso = lead.called_at_iso;
  if (!iso) return NaN;
  try {
    var s = String(iso);
    var hasTZ = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(s);
    if (!hasTZ && /\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(s)) s = s + 'Z';
    var t = Date.parse(s);
    return isNaN(t) ? NaN : t;
  } catch (_) { return NaN; }
}

function weekdayShortUtc(ms) { return CHART_WEEKDAY_SHORT[new Date(ms).getUTCDay()]; }

function getHourFromLead(lead) {
  var ms = leadTimelineMs(lead);
  if (isNaN(ms)) return -1;
  return new Date(ms).getHours();
}

function getRating(lead) {
  if (!lead || !lead.analysis) return null;
  var r = parseInt(lead.analysis.rating, 10);
  return Number.isFinite(r) && r >= 1 && r <= 5 ? r : null;
}

function initCharts() {
  var isDark = document.documentElement.getAttribute('data-theme') === 'dark';
  var c = isDark
    ? { text: '#9C9CA0', bg: '#0D0D0D', border: '#1A1A1A' }
    : { text: '#6C6C70', bg: '#FFFFFF', border: '#E5E5E7' };
  var RED = '#DC2626', RED_LIGHT = isDark ? 'rgba(220,38,38,0.15)' : 'rgba(220,38,38,0.08)', GREEN = '#16A34A', ORANGE = '#EA580C', AMBER = '#D97706', GRAY = '#9CA3AF', PURPLE = '#9333EA';

  // Engagement Timeline
  var timelineEl = document.getElementById('chart-timeline');
  if (timelineEl) {
    chartTimeline = new ApexCharts(timelineEl, {
      series: [
        { name: 'Total Calls', data: Array(7).fill(0) },
        { name: 'Interested', data: Array(7).fill(0) },
      ],
      chart: {
        type: 'area', height: 220, toolbar: { show: false }, background: 'transparent', fontFamily: 'inherit',
        events: {
          dataPointSelection: function(event, ctx) {
            var cat = chartTimeline && chartTimeline.w && chartTimeline.w.config && chartTimeline.w.config.xaxis && chartTimeline.w.config.xaxis.categories;
            if (!cat || !cat[ctx.dataPointIndex]) return;
            var isoDates = chartTimeline && chartTimeline.w && chartTimeline.w.globals && chartTimeline.w.globals.categoryLabels;
            var rawDates = window.__timelineDatesIso || [];
            var selected = rawDates[ctx.dataPointIndex] || '';
            if (selected && chartFilterState.type === 'timeline' && chartFilterState.value === selected) {
              clearChartFilter();
            } else {
              setChartFilter('timeline', selected, 'Timeline: ' + cat[ctx.dataPointIndex]);
            }
          }
        }
      },
      colors: [GRAY, GREEN, RED],
      fill: { type: 'solid', opacity: [0.08, 0.08, 0.08] },
      stroke: { curve: 'smooth', width: [2, 2, 2] },
      markers: { size: [3, 3, 3], strokeWidth: 1.5, hover: { sizeOffset: 2 } },
      xaxis: { categories: getLastSevenPlaceholderCategories(), labels: { style: { colors: Array(7).fill(c.text), fontSize: '10px' }, rotate: -35, trim: false } },
      yaxis: { min: 0, labels: { style: { colors: [c.text], fontSize: '9px' } } },
      grid: { borderColor: c.border, strokeDashArray: 4 },
      legend: { show: false },
      tooltip: { shared: true, intersect: false, theme: 'light' },
      dataLabels: { enabled: false },
    });
    chartTimeline.render();
  }

  // Outcome Distribution
  var pieEl = document.getElementById('chart-pie');
  if (pieEl) {
    chartPie = new ApexCharts(pieEl, {
      series: [0, 0, 0, 0, 0, 0, 0],
      chart: {
        type: 'donut', height: 220, background: 'transparent', fontFamily: 'inherit',
        events: {
          dataPointSelection: function(event, ctx) {
            var labels = ['Interested', 'Site Visited', 'Call Later', 'Failed', 'Answered', 'No Response', 'Voice Mail'];
            var selectedLabel = labels[ctx.dataPointIndex] || '';
            if (!selectedLabel) return;
            if (chartFilterState.type === 'pie' && chartFilterState.value === selectedLabel) {
              clearChartFilter();
            } else {
              setChartFilter('pie', selectedLabel, 'Outcome: ' + selectedLabel);
            }
          }
        }
      },
      labels: ['Interested', 'Site Visited', 'Call Later', 'Failed', 'Answered', 'No Response', 'Voice Mail'],
      colors: ['#16A34A', '#EAB308', '#3B82F6', '#DC2626', '#9CA3AF', '#F59E0B', '#A855F7'],
      legend: { position: 'bottom', labels: { colors: Array(7).fill(c.text) }, fontSize: '10px', itemMargin: { horizontal: 8 } },
      dataLabels: { enabled: false },
      stroke: { show: false },
      plotOptions: { pie: { donut: { size: '60%', labels: { show: true, name: { show: false }, value: { show: true, fontSize: '20px', fontWeight: 700, color: isDark ? '#E5E5E7' : '#1C1C1E', offsetY: 2 }, total: { show: true, showAlways: true, label: 'Total', fontSize: '10px', fontWeight: 600, color: c.text, formatter: function () { return '0'; } } } } } },
    });
    chartPie.render();
  }

  // Conversion Gauge
  var convEl = document.getElementById('chart-conversion');
  if (convEl) {
    chartConversion = new ApexCharts(convEl, {
      series: [0],
      chart: { type: 'radialBar', height: 220, background: 'transparent', fontFamily: 'inherit' },
      colors: [RED],
      plotOptions: {
        radialBar: {
          hollow: { size: '60%' },
          track: { background: c.border, strokeWidth: '100%' },
          dataLabels: {
            show: true,
            name: { show: true, fontSize: '11px', fontWeight: 600, color: c.text, offsetY: -8, formatter: function () { return 'Conversion'; } },
            value: { show: true, fontSize: '24px', fontWeight: 800, color: isDark ? '#E5E5E7' : '#1C1C1E', offsetY: 2, formatter: function (v) { return v.toFixed(1) + '%'; } },
          },
        },
      },
      stroke: { lineCap: 'round' },
      labels: ['Conversion Rate'],
    });
    chartConversion.render();
  }

  // Campaign Progress (horizontal stacked bar)
  var progressEl = document.getElementById('chart-progress');
  if (progressEl) {
    chartProgress = new ApexCharts(progressEl, {
      series: [
        { name: 'Connected', data: [0] },
        { name: 'Failed', data: [0] },
        { name: 'No Answer', data: [0] },
        { name: 'Pending', data: [0] },
      ],
      chart: {
        type: 'bar', height: 200, stacked: true, stackType: '100%', background: 'transparent', fontFamily: 'inherit', toolbar: { show: false },
        events: {
          dataPointSelection: function(event, ctx) {
            var seriesName = chartProgress && chartProgress.w && chartProgress.w.config && chartProgress.w.config.series && chartProgress.w.config.series[ctx.seriesIndex];
            var name = seriesName ? (seriesName.name || '') : '';
            if (!name) return;
            if (chartFilterState.type === 'progress' && chartFilterState.value === name) {
              clearChartFilter();
            } else {
              setChartFilter('progress', name, 'Progress: ' + name);
            }
          }
        }
      },
      colors: ['#16A34A', RED, '#D97706', '#9CA3AF'],
      plotOptions: { bar: { borderRadius: 4, horizontal: true, barHeight: '60%' } },
      xaxis: { categories: ['Campaign'], labels: { show: false } },
      yaxis: { show: false },
      grid: { show: false },
      legend: { position: 'bottom', fontSize: '10px', labels: { colors: [c.text, c.text, c.text, c.text] }, itemMargin: { horizontal: 6 } },
      tooltip: { theme: 'light', y: { formatter: function (v) { return v + ' calls'; } } },
      dataLabels: { enabled: false },
    });
    chartProgress.render();
  }

  // Day of Week Distribution
  var weekdayEl = document.getElementById('chart-weekday');
  if (weekdayEl) {
    chartWeekday = new ApexCharts(weekdayEl, {
      series: [{ name: 'Calls', data: [0, 0, 0, 0, 0, 0, 0] }],
      chart: {
        type: 'bar', height: 200, background: 'transparent', fontFamily: 'inherit', toolbar: { show: false },
        events: {
          dataPointSelection: function(event, ctx) {
            var days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
            var selectedDay = days[ctx.dataPointIndex] || '';
            if (!selectedDay) return;
            if (chartFilterState.type === 'weekday' && chartFilterState.value === ctx.dataPointIndex) {
              clearChartFilter();
            } else {
              setChartFilter('weekday', ctx.dataPointIndex, 'Day of Week: ' + selectedDay);
            }
          }
        }
      },
      colors: [RED],
      plotOptions: { bar: { borderRadius: 3, columnWidth: '60%', distributed: false } },
      xaxis: {
        categories: ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'],
        labels: { style: { colors: Array(7).fill(c.text), fontSize: '10px' }, rotate: 0 },
      },
      yaxis: { min: 0, labels: { style: { colors: [c.text], fontSize: '9px' } } },
      grid: { borderColor: c.border, strokeDashArray: 4 },
      legend: { show: false },
      tooltip: { theme: 'light' },
      dataLabels: { enabled: false },
    });
    chartWeekday.render();
  }

  // Hourly Distribution
  var hourlyEl = document.getElementById('chart-hourly');
  if (hourlyEl) {
    chartHourly = new ApexCharts(hourlyEl, {
      series: [{ name: 'Calls', data: Array(24).fill(0) }],
      chart: {
        type: 'bar', height: 200, background: 'transparent', fontFamily: 'inherit', toolbar: { show: false },
        events: {
          dataPointSelection: function(event, ctx) {
            var hours = ['12A','1A','2A','3A','4A','5A','6A','7A','8A','9A','10A','11A','12P','1P','2P','3P','4P','5P','6P','7P','8P','9P','10P','11P'];
            var selectedHour = hours[ctx.dataPointIndex] || '';
            if (!selectedHour) return;
            if (chartFilterState.type === 'hourly' && chartFilterState.value === ctx.dataPointIndex) {
              clearChartFilter();
            } else {
              setChartFilter('hourly', ctx.dataPointIndex, 'Hour: ' + selectedHour);
            }
          }
        }
      },
      colors: ['#0891B2'],
      plotOptions: { bar: { borderRadius: 2, columnWidth: '70%', distributed: false } },
      xaxis: {
        categories: ['12A','1A','2A','3A','4A','5A','6A','7A','8A','9A','10A','11A','12P','1P','2P','3P','4P','5P','6P','7P','8P','9P','10P','11P'],
        labels: { style: { colors: Array(24).fill(c.text), fontSize: '9px' }, rotate: -45, trim: false, hideOverlappingLabels: false },
      },
      yaxis: { min: 0, labels: { style: { colors: [c.text], fontSize: '9px' }, formatter: val => Math.floor(val) } },
      grid: { borderColor: c.border, strokeDashArray: 4 },
      legend: { show: false },
      tooltip: { theme: 'light' },
      dataLabels: { enabled: false },
    });
    chartHourly.render();
  }
}

var _lastChartFingerprints = {};

function _chartFingerprint(name, data) {
  var fp = JSON.stringify(data || []);
  if (_lastChartFingerprints[name] === fp) return false;
  _lastChartFingerprints[name] = fp;
  return true;
}

function updateCharts(leads, dispositionCounts, callbackCountsByDate, serverTimeline, chartExtras) {
  _storeVizData(leads, dispositionCounts, callbackCountsByDate, serverTimeline, chartExtras);
  var st = serverTimeline && typeof serverTimeline === 'object' ? serverTimeline : {};
  var extras = chartExtras && typeof chartExtras === 'object' ? chartExtras : {};
  var dc = dispositionCounts || {};
  var list = Array.isArray(leads) ? leads : [];

  var interested = Number(dc['Interested'] || 0);
  var notInterested = Number(dc['Not Interested'] || 0);
  var callbackPie = Number(dc['Call Later'] || 0) + Number(dc['Busy'] || 0) + Number(dc['Callback'] || 0);
  var cb = extras.calledBreakdown || {};
  var answered = Number(cb.plain_answered) || Number(dc['Answered'] || 0);
  var failed = Number(cb.failed) || Number(dc['Failed'] || 0);
  var noResponse = Number(cb.no_response) || Number(dc['No Response'] || 0);
  var voicemail = Number(cb.voicemail) || Number(dc['Voice Mail'] || dc['Voicemail'] || 0);
  if (!Number(cb.interested) && interested === 0 && list.length) {
    interested = list.filter(function (l) { return effectiveDispo(l) === 'Interested'; }).length;
  } else if (Number(cb.interested)) {
    interested = Number(cb.interested);
  }

  var calledCount = Number(extras.calledCount);
  if (!Number.isFinite(calledCount) || calledCount < 0) {
    calledCount = list.filter(isCalled).length;
  }

  // Donut — full outbound cohort from API (not the chart row sample)
  var siteVisitPie = Number(cb.site_visit) || Number(dc['Site Visit'] || 0);
  if (!siteVisitPie && typeof countSiteVisitFromLeads === 'function') {
    siteVisitPie = countSiteVisitFromLeads(list);
  }
  if (Number(cb.callbacks)) {
    callbackPie = Number(cb.callbacks);
  }
  var pieData = [interested, siteVisitPie, callbackPie, failed, answered, noResponse, voicemail];

  var pieSum = pieData.reduce(function (a, b) { return a + b; }, 0);
  if (chartPie && _chartFingerprint('pie', pieData)) {
    chartPie.updateOptions({
      series: pieSum === 0 ? [0, 0, 0, 0, 0, 0, 0] : pieData,
      plotOptions: {
        pie: {
          donut: {
            labels: {
              show: true,
              total: {
                show: true,
                showAlways: true,
                formatter: function () { return String(pieSum); },
              },
            },
          },
        },
      },
    }, false, true);
  }

  // Timeline
  if (chartTimeline) {
    var datesIsoRaw = Array.isArray(st.timeline_dates_iso) ? st.timeline_dates_iso : [];
    var serTotals = coerceNumArray7(st.timeline_total_calls || []);
    var serInterested = coerceNumArray7(st.timeline_interested || []);

    var datesIso = datesIsoRaw.length === 7 ? datesIsoRaw.slice() : new Array(7).fill('');
    window.__timelineDatesIso = datesIso;
    var categories = datesIso.map(function (iso) { return formatTimelineAxisLabel(iso || ''); });
    var totalCallsData, interestedData, callbackData;

    var useServerTimeline = datesIsoRaw.length === 7 && serTotals.length === 7 && serInterested.length === 7;
    if (useServerTimeline && !window.__vizDateFilterActive) {
      totalCallsData = serTotals.slice();
      interestedData = serInterested.slice();
      var cbFw = callbackCountsByDate || {};
      var wl3 = Array.isArray(st.timeline_week_labels) ? st.timeline_week_labels : [];
      callbackData = wl3.length === 7 ? wl3.map(function (d) { return cbFw[d] || 0; }) : getLast7DaysUtc().map(function (d) { return cbFw[d] || 0; });
    } else {
      var wl2 = Array.isArray(st.timeline_week_labels) ? st.timeline_week_labels : null;
      var last7Utc = getLast7DaysUtc();
      var cat2 = wl2 && wl2.length >= 2 ? wl2.slice() : last7Utc;
      var useIsoDates = cat2.length > 0 && cat2[0].indexOf('-') >= 0;
      categories = cat2.slice();
      var idx = {};
      cat2.forEach(function (d) { idx[d] = true; });
      var totalCallsByDay = {}, interestedByDay = {};
      cat2.forEach(function (d) { totalCallsByDay[d] = 0; interestedByDay[d] = 0; });
      list.forEach(function (lead) {
        var ms = leadTimelineMs(lead);
        if (isNaN(ms)) return;
        var key;
        if (useIsoDates) {
          key = _istDateStr(ms);
        } else {
          key = weekdayShortUtc(ms);
        }
        if (!idx[key]) return;
        totalCallsByDay[key]++;
        if (effectiveDispo(lead) === 'Interested') interestedByDay[key]++;
      });
      totalCallsData = cat2.map(function (d) { return totalCallsByDay[d] || 0; });
      interestedData = cat2.map(function (d) { return interestedByDay[d] || 0; });
      var cb = callbackCountsByDate || {};
      callbackData = cat2.map(function (d) { return cb[d] || 0; });
    }

    var isDark = document.documentElement.getAttribute('data-theme') === 'dark';
    var cTz = isDark ? { text: '#9C9CA0', border: '#1A1A1A' } : { text: '#6C6C70', border: '#E5E5E7' };
    if (_chartFingerprint('timeline', [totalCallsData, interestedData, categories])) {
      chartTimeline.updateOptions({
        series: [
          { name: 'Total Calls', data: totalCallsData },
          { name: 'Interested', data: interestedData },
        ],
        xaxis: { categories: categories, labels: { rotate: categories.length <= 14 ? -35 : -45, style: { colors: categories.map(function () { return cTz.text; }), fontSize: '10px' } } },
        yaxis: { min: 0, labels: { style: { colors: [cTz.text], fontSize: '9px' } } },
        grid: { borderColor: cTz.border, strokeDashArray: 4 },
      }, false, true);
    }
  }

  // Conversion Gauge — interested / all called (server totals)
  var convRate = calledCount > 0 ? (interested / calledCount) * 100 : 0;
  if (chartConversion && _chartFingerprint('conversion', [convRate])) {
    chartConversion.updateSeries([parseFloat(convRate.toFixed(1))], true);
  }

  // Campaign Progress (100% stacked bar)
  if (chartProgress) {
    var pc = extras.progressCounts || {};
    var connected = Number(pc.connected);
    var failedP = Number(pc.failed);
    var noAnswer = Number(pc.no_answer);
    var pending = Number(pc.pending);
    if (!Number.isFinite(connected)) {
      connected = list.filter(function (l) { return (l.status || '').toLowerCase() === 'completed'; }).length;
      failedP = list.filter(function (l) { var s = (l.status || '').toLowerCase(); return s === 'failed' || s === 'error'; }).length;
      noAnswer = list.filter(function (l) { var s = (l.status || '').toLowerCase(); return s === 'no answer' || s === 'busy'; }).length;
      pending = list.filter(function (l) { return (l.status || '').toLowerCase() === 'pending' || !l.status; }).length;
    }
    if (_chartFingerprint('progress', [connected, failedP, noAnswer, pending])) {
      chartProgress.updateSeries([
        { name: 'Connected', data: [connected] },
        { name: 'Failed', data: [failedP] },
        { name: 'No Answer', data: [noAnswer] },
        { name: 'Pending', data: [pending] },
      ], true);
    }
  }

  // Day of Week Distribution
  if (chartWeekday) {
    var weekday = Array.isArray(extras.weekdayCounts) && extras.weekdayCounts.length === 7
      ? extras.weekdayCounts.map(function (x) { return Number(x) || 0; })
      : null;
    if (!weekday) {
      weekday = [0, 0, 0, 0, 0, 0, 0];
      list.forEach(function (lead) {
        var ms = leadTimelineMs(lead);
        if (isNaN(ms)) return;
        var d = new Date(ms).getDay();
        var idx = d === 0 ? 6 : d - 1;
        if (idx >= 0 && idx <= 6) weekday[idx]++;
      });
    }
    if (_chartFingerprint('weekday', weekday)) {
      chartWeekday.updateSeries([{ name: 'Calls', data: weekday }], true);
    }
  }

  // Hourly Distribution
  if (typeof updateHourlyChartForLeads === 'function') {
      updateHourlyChartForLeads(list);
  }

  // Sparklines
  updateSparklines(list, st, extras);
}

/** Get IST date string (YYYY-MM-DD) from a timestamp in epoch ms. */
function _istDateStr(ms) {
    if (!Number.isFinite(ms)) return '';
    return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Kolkata', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(ms));
}

/** IST midnight start epoch ms for a YYYY-MM-DD string. */
function _istDayStartMs(dateStr) {
    if (!dateStr) return 0;
    var parts = dateStr.split('-');
    var y = parseInt(parts[0], 10);
    var m = parseInt(parts[1], 10) - 1;
    var d = parseInt(parts[2], 10);
    return Date.UTC(y, m, d) - 5.5 * 60 * 60 * 1000;
}

/** IST 23:59:59.999 epoch ms for a YYYY-MM-DD string. */
function _istDayEndMs(dateStr) {
    if (!dateStr) return Infinity;
    return _istDayStartMs(dateStr) + 24 * 60 * 60 * 1000 - 1;
}

// ─── Auto-apply date filter on every updateCharts call ───
(function() {
  var _origUpdateCharts = updateCharts;
  updateCharts = function(leads, dispositionCounts, callbackCountsByDate, serverTimeline, chartExtras) {
    if (!window.__vizDateFilterActive) {
      _origUpdateCharts(leads, dispositionCounts, callbackCountsByDate, serverTimeline, chartExtras);
      return;
    }
    var fromEl = document.getElementById('viz-date-from');
    var toEl = document.getElementById('viz-date-to');
    var fromVal = fromEl ? fromEl.value : '';
    var toVal = toEl ? toEl.value : '';
    if (!fromVal && !toVal) {
      _origUpdateCharts(leads, dispositionCounts, callbackCountsByDate, serverTimeline, chartExtras);
      return;
    }
    var fromMs = fromVal ? _istDayStartMs(fromVal) : 0;
    var toMs = toVal ? _istDayEndMs(toVal) : Infinity;
    var list = Array.isArray(leads) ? leads : [];
    var filtered = list.filter(function(l) {
      var ms = leadTimelineMs(l);
      if (isNaN(ms)) return false;
      return ms >= fromMs && ms <= toMs;
    });
    var fDc = {};
    filtered.forEach(function(l) {
      var d = typeof effectiveDispo === 'function' ? effectiveDispo(l) : (l.disposition || '');
      fDc[d] = (fDc[d] || 0) + 1;
    });
    var fCb = {};
    filtered.forEach(function(l) {
      var ms = leadTimelineMs(l);
      if (isNaN(ms)) return;
      var key = _istDateStr(ms);
      var d = typeof effectiveDispo === 'function' ? effectiveDispo(l) : (l.disposition || '');
      if (d === 'Call Later' || d === 'Callback' || d === 'Busy' || d === 'Callback scheduled') {
        fCb[key] = (fCb[key] || 0) + 1;
      }
    });
    var fTimeline = {};
    var dates = [];
    var s = fromVal ? _istDayStartMs(fromVal) : (_istDayEndMs(toVal) - 6 * 86400000);
    var e = toVal ? _istDayStartMs(toVal) : Date.now();
    var cur = new Date(s);
    while (cur <= new Date(e)) {
      dates.push(_istDateStr(cur.getTime()));
      cur.setDate(cur.getDate() + 1);
      if (dates.length > 31) break;
    }
    if (dates.length > 14) {
      var step = Math.ceil(dates.length / 14);
      var sampled = [];
      for (var i = 0; i < dates.length; i += step) sampled.push(dates[i]);
      dates = sampled;
    }
    fTimeline.timeline_dates_iso = dates;
    fTimeline.timeline_total_calls = dates.map(function(d) {
      var count = 0;
      filtered.forEach(function(l) {
        var ms = leadTimelineMs(l);
        if (isNaN(ms)) return;
        var ld = _istDateStr(ms);
        if (ld === d) count++;
      });
      return count;
    });
    fTimeline.timeline_interested = dates.map(function(d) {
      var count = 0;
      filtered.forEach(function(l) {
        var ms = leadTimelineMs(l);
        if (isNaN(ms)) return;
        if ((typeof effectiveDispo === 'function' ? effectiveDispo(l) : l.disposition) === 'Interested') {
          var ld = _istDateStr(ms);
          if (ld === d) count++;
        }
      });
      return count;
    });
    fTimeline.timeline_week_labels = dates;
    var fExtras = Object.assign({}, chartExtras || {});
    fExtras.progressCounts = undefined;
    fExtras.weekdayCounts = undefined;
    fExtras.calledCount = filtered.filter(typeof isCalled === 'function' ? isCalled : function(l) { return !!l.start_time; }).length;
    _storeVizData(leads, dispositionCounts, callbackCountsByDate, serverTimeline, chartExtras);
    _origUpdateCharts(filtered, fDc, fCb, fTimeline, fExtras);
    if (typeof updateVizStatCards === 'function') updateVizStatCards(filtered);
  };
})();

function updateHourlyChartForLeads(filteredLeads) {
  if (!chartHourly) return;
  var hourly = Array(24).fill(0);

  if (filteredLeads && filteredLeads.length > 0) {
    var maxMs = -1;
    filteredLeads.forEach(function (lead) {
      var ms = leadTimelineMs(lead);
      if (!isNaN(ms) && ms > maxMs) maxMs = ms;
    });

    if (maxMs > 0) {
      var latestDayString = _istDateStr(maxMs);

      filteredLeads.forEach(function (lead) {
        var ms = leadTimelineMs(lead);
        if (isNaN(ms)) return;
        var dayString = _istDateStr(ms);
        if (dayString === latestDayString) {
          var hStr = new Intl.DateTimeFormat('en-US', {
            timeZone: 'Asia/Kolkata',
            hour: 'numeric',
            hour12: false
          }).format(new Date(ms));
          var h = parseInt(hStr, 10);
          if (h === 24) h = 0;
          if (h >= 0 && h < 24) hourly[h]++;
        }
      });
    }
  }

  if (_chartFingerprint('hourly', hourly)) {
    chartHourly.updateSeries([{ name: 'Calls', data: hourly }], true);
  }
}

function updateSparklines(leads, st, extras) {
  extras = extras || {};
  var last7 = getLastSevenPlaceholderCategories();
  var days = st && Array.isArray(st.timeline_dates_iso) && st.timeline_dates_iso.length === 7
    ? st.timeline_dates_iso.slice()
    : last7;

  var cats = days.map(function (d) {
    if (d && /^\d{4}-\d{2}-\d{2}$/.test(d)) {
      return formatTimelineAxisLabel(d);
    }
    return d;
  });

  var useServer = st && Array.isArray(st.timeline_total_calls) && st.timeline_total_calls.length === 7;
  var totalByDay = useServer ? coerceNumArray7(st.timeline_total_calls) : Array(7).fill(0);
  var calledByDay = useServer ? totalByDay.slice() : Array(7).fill(0);
  var interestedByDay = useServer && Array.isArray(st.timeline_interested)
    ? coerceNumArray7(st.timeline_interested)
    : Array(7).fill(0);
  var notInterestedByDay = Array(7).fill(0);
  var failedByDay = Array(7).fill(0);

  if (!useServer) {
    var dayIndex = {};
    days.forEach(function (iso, i) {
      if (iso && /^\d{4}-\d{2}-\d{2}$/.test(iso)) dayIndex[iso] = i;
    });
    var list = Array.isArray(leads) ? leads : [];
    list.forEach(function (lead) {
      var ms = leadTimelineMs(lead);
      if (isNaN(ms)) return;
      var iso = _istDateStr(ms);
      var idx = dayIndex[iso];
      if (idx === undefined) return;
      totalByDay[idx]++;
      if (isCalled(lead)) {
        calledByDay[idx]++;
        var dispo = effectiveDispo(lead);
        if (dispo === 'Interested') interestedByDay[idx]++;
        else if (dispo === 'Site Visited' || dispo === 'Not Interested' || dispo === 'not_interested') notInterestedByDay[idx]++;
        if (isFailed(lead)) failedByDay[idx]++;
      }
    });
  }

  var RED = '#DC2626', GREEN = '#16A34A', GRAY = '#9CA3AF', PURPLE = '#9333EA', AMBER = '#D97706';

  var sparkConfigs = [
    { id: 'spark-total', data: totalByDay, color: GRAY },
    { id: 'spark-called', data: calledByDay, color: RED },
    { id: 'spark-interested', data: interestedByDay, color: GREEN },
    { id: 'spark-not-interested', data: notInterestedByDay, color: RED },
    { id: 'spark-conversion', data: calledByDay.map(function (c, i) { return c > 0 ? Math.round((interestedByDay[i] / c) * 100) : 0; }), color: GREEN },
    { id: 'spark-attempts', data: totalByDay, color: RED },
    { id: 'spark-failed', data: failedByDay, color: RED },
  ];

  sparkConfigs.forEach(function (cfg) {
    var el = document.getElementById(cfg.id);
    if (!el) return;
    if (!sparkCharts[cfg.id]) {
      sparkCharts[cfg.id] = new ApexCharts(el, {
        series: [{ data: cfg.data }],
        chart: { type: 'area', height: 36, width: '100%', sparkline: { enabled: true }, background: 'transparent' },
        fill: { type: 'solid', opacity: 0.15 },
        stroke: { curve: 'smooth', width: 1.5 },
        colors: [cfg.color],
        tooltip: { enabled: false },
      });
      sparkCharts[cfg.id].render();
    } else {
      sparkCharts[cfg.id].updateSeries([{ data: cfg.data }], true);
    }
  });
}
