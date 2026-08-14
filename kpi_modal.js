// ─── Reusable KPI Details Modal ───
// Config-driven modal for showing details of each KPI card.

(function () {
  'use strict';

  // ─── Internal State ───
  var _k = {
    current: null, 
    page: 1, 
    pageSize: 10,
    sortKey: null, 
    sortDir: 'desc',
    search: '', 
    dateFilter: 'all',
    timer: null, 
    raw: [],
  };

  // ─── Helpers ───
  function _fmtDate(ts) {
    if (!ts) return '—';
    var d = typeof ts === 'number' ? new Date(ts * 1000) : new Date(ts);
    if (isNaN(d.getTime())) return String(ts).slice(0, 10) || '—';
    return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  function _fmtDuration(sec) {
    if (!sec || sec <= 0) return '—';
    if (sec < 60) return Math.round(sec) + 's';
    return (sec / 60).toFixed(1) + 'm';
  }

  function _esc(s) {
    if (typeof s !== 'string') s = String(s || '');
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  function _statusBadge(status) {
    var s = (status || '').toLowerCase();
    var failed = s === 'failed' || s === 'no answer' || s === 'no_answer' || s === 'no response' || s === 'no_response';
    var cls = s === 'completed' || s === 'answered' ? 'tag-int' : failed ? 'tag-fail' : s === 'not_interested' ? 'tag-noint' : 'tag-cbk';
    var text = s === 'completed' || s === 'answered' ? 'Answered' : failed ? 'No Answer' : s;
    return '<span class="badge-tag ' + cls + '">' + _esc(text) + '</span>';
  }

  function _dispoBadge(dispo) {
    var d = (dispo || '').toLowerCase();
    var cls = d === 'interested' ? 'tag-int' : d === 'not interested' ? 'tag-noint' : d === 'failed' ? 'tag-fail' : 'tag-cbk';
    return '<span class="badge-tag ' + cls + '">' + _esc(dispo) + '</span>';
  }

  // ─── KPI Configurations ───
  var CONFIGS = {
    total: {
      title: 'Total Leads Details',
      noData: 'No leads found in this sandbox.',
      searchFields: ['name','phone','company'],
      filter: function (leads) { return leads || []; },
      summary: function (leads) {
        var called = (leads || []).filter(isCalled).length;
        var pending = (leads || []).filter(l => l.status === 'pending').length;
        var failed = (leads || []).filter(isFailed).length;
        return [
          { label: 'Total Leads', value: leads.length },
          { label: 'Called', value: called },
          { label: 'Pending', value: pending },
          { label: 'Failed', value: failed },
        ];
      },
      columns: [
        { key: 'name',      label: 'Lead Name',  sortable: true, render: 'name' },
        { key: 'phone',     label: 'Phone',      sortable: true, render: 'phone' },
        { key: 'company',   label: 'Source',     sortable: true },
        { key: 'segment',   label: 'Campaign',   sortable: true },
        { key: 'status',    label: 'Status',     sortable: true, render: 'status' },
        { key: 'created_at',label: 'Created',    sortable: true, render: 'date' },
        { key: '_actions',  label: 'Actions',    render: 'actions' },
      ],
    },

    called: {
      title: 'Calls Made Details',
      noData: 'No calls made yet in this sandbox.',
      searchFields: ['name','phone','company'],
      filter: function (leads) {
        return (leads || []).filter(isCalled);
      },
      summary: function (leads) {
        var answered = leads.filter(l => l.status === 'completed').length;
        var failed = leads.filter(isFailed).length;
        return [
          { label: 'Total Calls', value: leads.length },
          { label: 'Answered', value: answered },
          { label: 'No Answer', value: failed },
        ];
      },
      columns: [
        { key: 'name',        label: 'Lead',        sortable: true, render: 'name' },
        { key: 'phone',       label: 'Phone',       sortable: true, render: 'phone' },
        { key: 'called_at_iso', label: 'Call Time', sortable: true, render: 'date' },
        { key: 'duration_sec',  label: 'Duration',  sortable: true, render: 'duration' },
        { key: 'segment',     label: 'Campaign',    sortable: true },
        { key: 'status',      label: 'Call Status', sortable: true, render: 'status' },
        { key: '_actions',    label: 'Actions',     render: 'actions' },
      ],
    },

    interested: {
      title: 'Interested Leads Details',
      noData: 'No interested leads in this sandbox.',
      searchFields: ['name','phone','company','summary'],
      filter: function (leads) {
        return (leads || []).filter(l => l.disposition === 'Interested');
      },
      summary: function (leads) {
        var sched = leads.filter(l => l.site_visit_scheduled).length;
        return [
          { label: 'Interested', value: leads.length },
          { label: 'Site Visits Scheduled', value: sched },
        ];
      },
      columns: [
        { key: 'name',        label: 'Lead',           sortable: true, render: 'name' },
        { key: 'phone',       label: 'Phone',          sortable: true, render: 'phone' },
        { key: 'company',     label: 'Project',        sortable: true },
        { key: 'called_at_iso', label: 'Call Time',    sortable: true, render: 'date' },
        { key: 'summary',     label: 'Remarks',        sortable: false, render: 'truncate' },
        { key: 'disposition', label: 'Disposition',    sortable: true, render: 'dispo' },
        { key: '_actions',    label: 'Actions',        render: 'actions' },
      ],
    },

    conversion: {
      title: 'Conversion Rate Analysis',
      noData: 'No converted leads in this sandbox.',
      searchFields: ['name','phone','company'],
      filter: function (leads) {
        return (leads || []).filter(l => l.disposition === 'Interested');
      },
      summary: function (leads) {
        var totalCalled = window.appState.allLeads.filter(l => l.sandbox === window.appState.currentSandbox && isCalled(l)).length;
        var pct = totalCalled > 0 ? ((leads.length / totalCalled) * 100).toFixed(1) : '0';
        return [
          { label: 'Total Called', value: totalCalled },
          { label: 'Interested (Converted)', value: leads.length },
          { label: 'Conversion %', value: pct + '%' },
        ];
      },
      columns: [
        { key: 'name',         label: 'Lead',      sortable: true, render: 'name' },
        { key: 'phone',        label: 'Phone',     sortable: true, render: 'phone' },
        { key: 'company',      label: 'Source',    sortable: true },
        { key: 'segment',      label: 'Campaign',  sortable: true },
        { key: 'called_at_iso',label: 'Converted At', sortable: true, render: 'date' },
        { key: 'disposition',  label: 'Outcome',   sortable: true, render: 'dispo' },
        { key: '_actions',     label: 'Actions',   render: 'actions' },
      ],
    },

    whatsapp_sent: {
      title: 'WhatsApp Communications Sent',
      noData: 'No WhatsApp messages sent in this sandbox.',
      searchFields: ['name','phone'],
      filter: function (leads) {
        return (leads || []).filter(l => l.whatsapp_sent);
      },
      summary: function (leads) {
        return [
          { label: 'WhatsApp Sent', value: leads.length },
          { label: 'Delivered', value: leads.length }, // 100% simulated delivery
        ];
      },
      columns: [
        { key: 'name',         label: 'Lead',         sortable: true, render: 'name' },
        { key: 'phone',        label: 'Phone',        sortable: true, render: 'phone' },
        { key: 'company',      label: 'Project',      sortable: true },
        { key: 'called_at_iso',label: 'Dispatch Time', sortable: true, render: 'date' },
        { key: '_actions',     label: 'Actions',      render: 'actions' },
      ],
    },

    site_visit_scheduled: {
      title: 'Site Visit Schedules',
      noData: 'No site visits scheduled in this sandbox.',
      searchFields: ['name','phone','company'],
      filter: function (leads) {
        return (leads || []).filter(l => l.site_visit_scheduled);
      },
      summary: function (leads) {
        return [
          { label: 'Total Schedules', value: leads.length },
        ];
      },
      columns: [
        { key: 'name',         label: 'Lead',          sortable: true, render: 'name' },
        { key: 'phone',        label: 'Phone',         sortable: true, render: 'phone' },
        { key: 'company',      label: 'Project Source', sortable: true },
        { key: 'called_at_iso',label: 'Scheduled For',  sortable: true, render: 'date' },
        { key: 'summary',      label: 'Visit Details',  sortable: false, render: 'truncate' },
        { key: '_actions',     label: 'Actions',       render: 'actions' },
      ],
    },

    not_interested: {
      title: 'Not Interested Details',
      noData: 'No leads marked not-interested in this sandbox.',
      searchFields: ['name','phone','summary'],
      filter: function (leads) {
        return (leads || []).filter(l => l.disposition === 'Not Interested');
      },
      summary: function (leads) {
        return [
          { label: 'Not Interested Count', value: leads.length },
        ];
      },
      columns: [
        { key: 'name',        label: 'Lead',        sortable: true, render: 'name' },
        { key: 'phone',       label: 'Phone',       sortable: true, render: 'phone' },
        { key: 'summary',     label: 'Reason Given', sortable: false, render: 'truncate' },
        { key: 'called_at_iso', label: 'Call Time', sortable: true, render: 'date' },
        { key: 'disposition', label: 'Outcome',     sortable: true, render: 'dispo' },
        { key: '_actions',    label: 'Actions',     render: 'actions' },
      ],
    },

    callbacks: {
      title: 'Requested Callbacks Details',
      noData: 'No callback requests in this sandbox.',
      searchFields: ['name','phone'],
      filter: function (leads) {
        return (leads || []).filter(l => l.disposition === 'Call Later' || l.disposition === 'Callback');
      },
      summary: function (leads) {
        return [
          { label: 'Callback Total', value: leads.length },
        ];
      },
      columns: [
        { key: 'name',         label: 'Lead',           sortable: true, render: 'name' },
        { key: 'phone',        label: 'Phone',          sortable: true, render: 'phone' },
        { key: 'called_at_iso', label: 'Call Time',     sortable: true, render: 'date' },
        { key: 'summary',       label: 'Callback Note',  sortable: false, render: 'truncate' },
        { key: 'disposition',  label: 'Disposition',    sortable: true, render: 'dispo' },
        { key: '_actions',     label: 'Actions',        render: 'actions' },
      ],
    },
  };

  // ─── Modal Controller ───
  function getConfig() { return CONFIGS[_k.current]; }

  function getLeads() {
    return Array.isArray(window.allLeads) ? window.allLeads : [];
  }

  function applyKpiFilter(config) {
    return config.filter(getLeads());
  }

  function applySearch(rows, config) {
    if (!_k.search) return rows;
    var q = _k.search.toLowerCase().trim();
    var fields = config.searchFields || ['name','phone'];
    return rows.filter(function (r) {
      return fields.some(function (f) {
        return (String(r[f] || '')).toLowerCase().indexOf(q) !== -1;
      });
    });
  }

  function applyDateFilter(rows) {
    if (_k.dateFilter === 'all') return rows;
    var now = Date.now(), cutoff;
    if (_k.dateFilter === 'today') {
      var d = new Date();
      cutoff = new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime() / 1000;
    } else if (_k.dateFilter === '7d') {
      cutoff = (now - 7 * 86400000) / 1000;
    } else if (_k.dateFilter === '30d') {
      cutoff = (now - 30 * 86400000) / 1000;
    }
    if (!cutoff) return rows;
    return rows.filter(function (r) { return r.start_time && r.start_time >= cutoff; });
  }

  function applySort(rows) {
    if (!_k.sortKey) return rows;
    var sk = _k.sortKey, dir = _k.sortDir === 'asc' ? 1 : -1;
    return rows.slice().sort(function (a, b) {
      var av = a[sk], bv = b[sk];
      if (av == null) av = '';
      if (bv == null) bv = '';
      if (typeof av === 'string') av = av.toLowerCase();
      if (typeof bv === 'string') bv = bv.toLowerCase();
      if (av < bv) return -1 * dir;
      if (av > bv) return 1 * dir;
      return 0;
    });
  }

  function dateRangeLabel() {
    var labels = { all: 'All Time', today: 'Today', '7d': 'Last 7 Days', '30d': 'Last 30 Days' };
    return labels[_k.dateFilter] || 'All Time';
  }

  // ─── Rendering ───
  function renderHeader(config, count) {
    var titleEl = document.getElementById('kdi-title');
    var metaEl = document.getElementById('kdi-meta');
    if (titleEl) titleEl.textContent = config.title;
    if (metaEl) metaEl.textContent = 'Total Records: ' + count + ' \u00b7 ' + dateRangeLabel();
  }

  function renderSummary(config, filteredLeads) {
    var el = document.getElementById('kdi-summary');
    if (!el) return;
    var items = config.summary(filteredLeads);
    if (!items || !items.length) { el.innerHTML = ''; return; }
    el.innerHTML = items.map(function (item) {
      return '<div class="flex flex-col p-3 border border-outline-variant bg-surface-container rounded-lg"><span class="text-[10px] font-bold text-on-surface-variant uppercase tracking-wider">' + _esc(item.label) + '</span><span class="text-xl font-bold text-on-surface">' + _esc(String(item.value)) + '</span></div>';
    }).join('');
  }

  function renderCell(col, lead) {
    if (col.render === 'name') {
      return _esc(lead.name || 'Unknown');
    }
    if (col.render === 'phone') {
      var phone = lead.phone || '';
      return '<span class="text-secondary cursor-pointer hover:underline" onclick="event.stopPropagation();window.copyPhoneToClipboard(\'' + _esc(phone) + '\')">' + _esc(phone || '—') + '</span>';
    }
    if (col.render === 'status') {
      return _statusBadge(lead.status || '');
    }
    if (col.render === 'dispo') {
      return _dispoBadge(lead.disposition || '');
    }
    if (col.render === 'date') {
      return _esc(_fmtDate(lead[col.key] || lead.called_at_iso || (lead.start_time ? lead.start_time : null)));
    }
    if (col.render === 'duration') {
      return _esc(_fmtDuration(lead.duration_sec));
    }
    if (col.render === 'truncate') {
      var t = (lead[col.key] || '').trim();
      if (!t) return '—';
      return t.length > 50 ? _esc(t.slice(0, 50)) + '…' : _esc(t);
    }
    if (col.key === '_actions') {
      var html = '<div class="flex items-center gap-2">';
      html += '<button class="px-2 py-1 text-xs border border-outline-variant rounded hover:bg-surface-container transition-colors font-medium" onclick="event.stopPropagation();window.closeKpiModal();window.openTranscriptModal(' + lead.id + ')">View</button>';
      if (lead.duration_sec > 0) {
        html += '<button class="px-2 py-1 text-xs bg-primary text-on-primary rounded hover:opacity-90 transition-colors font-medium" onclick="event.stopPropagation();window.playAudioRecording(\'' + _esc(lead.name) + '\', ' + lead.id + ')">Listen</button>';
      }
      html += '</div>';
      return html;
    }
    var val = lead[col.key];
    return _esc(String(val != null ? val : '—'));
  }

  function renderTable(config, rows) {
    var thead = document.getElementById('kdi-thead');
    var tbody = document.getElementById('kdi-tbody');
    if (!thead || !tbody) return;

    var cols = config.columns;
    thead.innerHTML = '<tr class="bg-surface-container-low border-b border-surface-container">' + cols.map(function (col) {
      var classes = 'p-3 text-left text-label-sm font-label-sm text-on-surface-variant uppercase tracking-wider';
      var style = '';
      if (col.sortable) {
        classes += ' cursor-pointer hover:bg-surface-container-high transition-colors';
        style = ' style="user-select:none;"';
      }
      var sortIndicator = '';
      if (col.key === _k.sortKey) {
        sortIndicator = _k.sortDir === 'asc' ? ' ▲' : ' ▼';
      }
      return '<th class="' + classes + '"' + style + ' data-sortable="' + (col.sortable ? '1' : '0') + '" data-key="' + col.key + '">' + _esc(col.label) + sortIndicator + '</th>';
    }).join('') + '</tr>';

    var ths = thead.querySelectorAll('th');
    for (var i = 0; i < ths.length; i++) {
      if (ths[i].getAttribute('data-sortable') === '1') {
        ths[i].onclick = function () {
          var key = this.getAttribute('data-key');
          if (_k.sortKey === key) {
            _k.sortDir = _k.sortDir === 'asc' ? 'desc' : 'asc';
          } else {
            _k.sortKey = key;
            _k.sortDir = 'desc';
          }
          _k.page = 1;
          populateKpiModal();
        };
      }
    }

    if (rows.length === 0) {
      tbody.innerHTML = '<tr><td colspan="' + cols.length + '" class="p-8 text-center text-on-surface-variant font-medium">' + _esc(config.noData) + '</td></tr>';
      return;
    }

    tbody.innerHTML = rows.map(function (row) {
      return '<tr class="border-b border-surface-container hover:bg-surface-container-low transition-colors">' +
        cols.map(function (col) {
          return '<td class="p-3 text-body-sm text-on-surface">' + renderCell(col, row) + '</td>';
        }).join('') + '</tr>';
    }).join('');
  }

  function renderPagination(total) {
    var el = document.getElementById('kdi-pagination');
    if (!el) return;
    var totalPages = Math.max(1, Math.ceil(total / _k.pageSize));
    if (_k.page > totalPages) _k.page = totalPages;
    var from = total === 0 ? 0 : (_k.page - 1) * _k.pageSize + 1;
    var to = Math.min(_k.page * _k.pageSize, total);

    var html = '<div class="flex items-center justify-between w-full mt-4 flex-wrap gap-4">';
    html += '<div class="text-body-sm text-on-surface-variant">Showing ' + from + '–' + to + ' of ' + total + '</div>';
    
    html += '<div class="flex items-center gap-2">';
    html += '<button class="px-3 py-1.5 border border-outline-variant rounded hover:bg-surface-container transition-colors text-xs font-semibold" onclick="window._kdiPage(' + (_k.page - 1) + ')"' + (_k.page <= 1 ? ' disabled style="opacity:0.4;cursor:default;"' : '') + '>Prev</button>';
    html += '<span class="text-xs font-semibold text-on-surface">Page ' + _k.page + ' of ' + totalPages + '</span>';
    html += '<button class="px-3 py-1.5 border border-outline-variant rounded hover:bg-surface-container transition-colors text-xs font-semibold" onclick="window._kdiPage(' + (_k.page + 1) + ')"' + (_k.page >= totalPages ? ' disabled style="opacity:0.4;cursor:default;"' : '') + '>Next</button>';
    html += '</div>';

    html += '<div class="flex items-center gap-4">';
    html += '<select class="bg-surface border border-outline-variant rounded px-2 py-1 text-xs" onchange="window._kdiPageSize(this.value)">';
    [5, 10, 20, 50].forEach(function (s) {
      var sel = s === _k.pageSize ? ' selected' : '';
      html += '<option value="' + s + '"' + sel + '>' + s + ' rows</option>';
    });
    html += '</select>';
    html += '<button class="px-3 py-1.5 bg-secondary text-on-secondary rounded hover:opacity-90 transition-colors text-xs font-semibold shadow-sm" onclick="window._kdiExportCSV()">Export CSV</button>';
    html += '</div>';

    html += '</div>';
    el.innerHTML = html;
  }

  // ─── Exported Actions ───
  window._kdiPage = function (p) {
    _k.page = p;
    populateKpiModal();
  };

  window._kdiPageSize = function (s) {
    _k.pageSize = parseInt(s, 10) || 10;
    _k.page = 1;
    populateKpiModal();
  };

  window.copyPhoneToClipboard = function (phone) {
    if (!phone) return;
    navigator.clipboard.writeText(phone).then(function () {
      window.showToast("Copied phone number: " + phone);
    });
  };

  window._kdiExportCSV = function () {
    var config = getConfig();
    if (!config || !_k.raw || _k.raw.length === 0) return;
    var rows = _k.raw;
    var cols = config.columns.filter(function (c) { return c.key !== '_actions'; });
    var header = cols.map(function (c) { return '"' + (c.label || '') + '"'; }).join(',');
    var body = rows.map(function (r) {
      return cols.map(function (c) {
        var v = r[c.key];
        if (c.render === 'name') v = r.name || '';
        if (c.render === 'phone') v = r.phone || '';
        if (c.render === 'date') v = _fmtDate(r[c.key] || r.called_at_iso || r.start_time);
        if (c.render === 'duration') v = _fmtDuration(r.duration_sec);
        if (c.render === 'status') v = r.status || '';
        if (c.render === 'dispo') v = r.disposition || '';
        return '"' + String(v != null ? v : '').replace(/"/g, '""') + '"';
      }).join(',');
    }).join('\n');
    var csv = header + '\n' + body;
    var blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = (config.title || 'kpi').replace(/\s+/g, '_') + '.csv';
    a.click();
    URL.revokeObjectURL(url);
  };

  // ─── Main API ───
  window.openKpiModal = function (kpiKey) {
    var config = CONFIGS[kpiKey];
    if (!config) { console.warn('Unknown KPI:', kpiKey); return; }

    _k.current = kpiKey;
    _k.page = 1;
    _k.sortKey = null;
    _k.sortDir = 'desc';
    _k.search = '';
    _k.dateFilter = 'all';

    var searchEl = document.getElementById('kdi-search');
    if (searchEl) searchEl.value = '';
    var dateEl = document.getElementById('kdi-filter-date');
    if (dateEl) dateEl.value = 'all';

    window.openModal('modal-kpi-detail');
    populateKpiModal();
  };

  window.closeKpiModal = function () {
    window.closeModal('modal-kpi-detail');
  };

  function populateKpiModal() {
    var config = getConfig();
    if (!config) return;

    var filtered = applyKpiFilter(config);
    filtered = applySearch(filtered, config);
    filtered = applyDateFilter(filtered);
    filtered = applySort(filtered);
    _k.raw = filtered;

    var total = filtered.length;
    var totalPages = Math.max(1, Math.ceil(total / _k.pageSize));
    if (_k.page > totalPages) _k.page = totalPages;
    var start = (_k.page - 1) * _k.pageSize;
    var pageRows = filtered.slice(start, start + _k.pageSize);

    renderHeader(config, total);
    renderSummary(config, filtered);
    renderTable(config, pageRows);
    renderPagination(total);
  }

  window.populateKpiModal = populateKpiModal;

  // ─── Wire Input listeners on ready ───
  function wireSearch() {
    var el = document.getElementById('kdi-search');
    if (!el || el._wired) return;
    el._wired = true;
    el.addEventListener('input', function () {
      clearTimeout(el._debounce);
      el._debounce = setTimeout(function () {
        _k.search = el.value;
        _k.page = 1;
        populateKpiModal();
      }, 300);
    });
  }

  function wireDateFilter() {
    var el = document.getElementById('kdi-filter-date');
    if (!el || el._wired) return;
    el._wired = true;
    el.addEventListener('change', function () {
      _k.dateFilter = el.value;
      _k.page = 1;
      populateKpiModal();
    });
  }

  document.addEventListener('DOMContentLoaded', function () { 
    wireSearch(); 
    wireDateFilter(); 
  });

  // Re-wire in open hook
  var _origOpen = window.openKpiModal;
  window.openKpiModal = function (kpiKey) {
    wireSearch(); wireDateFilter();
    _origOpen(kpiKey);
  };

})();
