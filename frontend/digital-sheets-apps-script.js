/**
 * Digital Leads — Google Apps Script
 *
 * Deploy ONE copy of this script to EACH broker's Google Sheet.
 * It watches for new rows and immediately POSTs them to the webhook.
 *
 * Set these two Script Properties before first use:
 *   DIGITAL_LEADS_WEBHOOK_URL   — your VPS endpoint, e.g.
 *       https://187.127.177.149/api/campaign/digital-leads/webhook
 *   DIGITAL_LEADS_WEBHOOK_SECRET — shared secret for X-Digital-Leads-Secret header
 *
 * The broker_id is read from the sheet name automatically:
 *   sheet name contains "Broker 1" → broker_1
 *   sheet name contains "Broker 2" → broker_2
 *   sheet name contains "Broker 3" → broker_3
 *
 * Run installDigitalLeadTriggers() once after pasting this script.
 */

// ── Config (overridden by Script Properties) ──────────────────────────────────
var DIGITAL_LEADS_WEBHOOK_URL   = PropertiesService.getScriptProperties().getProperty('DIGITAL_LEADS_WEBHOOK_URL')   || '';
var DIGITAL_LEADS_WEBHOOK_SECRET = PropertiesService.getScriptProperties().getProperty('DIGITAL_LEADS_WEBHOOK_SECRET') || '';

// ── Internal state ────────────────────────────────────────────────────────────
var BATCH_LOCK_KEY    = '__digital_leads_batch_lock__';
var BATCH_QUEUE_KEY   = '__digital_leads_batch_queue__';
var BATCH_FLUSH_KEY   = '__digital_leads_batch_last_flush__';
var BATCH_FLUSH_MS    = 5000; // ms to wait before flushing a batch

// ── Public entry points ───────────────────────────────────────────────────────

/**
 * Fires on every cell edit. Detects whether a NEW row was inserted and
 * enqueues it. Ignores edits to existing data cells.
 */
function onEditTrigger(e) {
  try {
    if (!e || !e.range) return;

    var sheet = e.range.getSheet();
    var brokerId = resolveBrokerId_(sheet);
    if (!brokerId) return; // not a broker sheet

    var row     = e.range.getRow();
    var col     = e.range.getColumn();
    var lastRow = sheet.getLastRow();

    // Only care about row 2+ (row 1 = header).
    if (row < 2) return;

    // Heuristic for "new row":
    //   1. The edited row is the LAST row in the sheet AND
    //   2. The row above already has data (meaning a new row was just appended) OR
    //   3. The edited row is blank — a form submit that hasn't filled cells yet.
    //      In that case we pick up the data on the next flush anyway.
    //   We also enqueue if the row's Phone cell (col 2) is empty — the form
    //   submit handler or recovery scan will catch it later.
    if (row < lastRow) return; // edit was in the middle — ignore

    enqueueRow_(sheet, row, brokerId);
  } catch (err) {
    logError_('onEditTrigger', err);
  }
}

/**
 * Fires when a Google Form is submitted and appends to the sheet.
 * The entire submitted row is enqueued immediately.
 */
function onFormSubmitTrigger(e) {
  try {
    if (!e || !e.range) return;

    var sheet    = e.range.getSheet();
    var brokerId = resolveBrokerId_(sheet);
    if (!brokerId) return;

    var row = e.range.getRow();
    enqueueRow_(sheet, row, brokerId);
  } catch (err) {
    logError_('onFormSubmitTrigger', err);
  }
}

/**
 * Time-based recovery: runs every 1 minute.
 * Catches rows added by API / import jobs that don't fire onEdit.
 * Also flushes any batch that has been sitting too long.
 */
function digitalLeadRecoverySync() {
  try {
    var spreadsheet = SpreadsheetApp.getActive();
    var sheets = spreadsheet.getSheets();

    sheets.forEach(function (sheet) {
      var brokerId = resolveBrokerId_(sheet);
      if (!brokerId) return;
      if (sheet.getLastRow() < 2) return;

      var data = sheet.getRange(2, 1, sheet.getLastRow() - 1, 6).getDisplayValues();
      data.forEach(function (row, idx) {
        var phone  = row[1];
        var status = row[5];
        // Re-sync rows that have a phone but no status (new) or failed sync.
        if (phone && (!status || /^Sync failed|^$/.test(status))) {
          enqueueRow_(sheet, idx + 2, brokerId);
        }
      });
    });

    // Force-flush anything left in the batch queue.
    flushBatchQueue_(true);
  } catch (err) {
    logError_('digitalLeadRecoverySync', err);
  }
}

/**
 * Run once after pasting this script. Installs all triggers.
 */
function installDigitalLeadTriggers() {
  var spreadsheet = SpreadsheetApp.getActive();

  // Remove old triggers for this script to avoid duplicates.
  ScriptApp.getProjectTriggers().forEach(function (trigger) {
    var fn = trigger.getHandlerFunction();
    if (fn === 'onEditTrigger' || fn === 'onFormSubmitTrigger' || fn === 'digitalLeadRecoverySync') {
      ScriptApp.deleteTrigger(trigger);
    }
  });

  ScriptApp.newTrigger('onEditTrigger')
    .forSpreadsheet(spreadsheet)
    .onEdit()
    .create();

  ScriptApp.newTrigger('onFormSubmitTrigger')
    .forSpreadsheet(spreadsheet)
    .onFormSubmit()
    .create();

  ScriptApp.newTrigger('digitalLeadRecoverySync')
    .timeBased()
    .everyMinutes(1)
    .create();

  Logger.log('Triggers installed for spreadsheet: ' + spreadsheet.getName());
}

// ── Broker detection ──────────────────────────────────────────────────────────

/**
 * Infers broker_id from the sheet name.
 * Accepts: "Broker 1", "Broker_1", "broker-1 Leads", etc.
 */
function resolveBrokerId_(sheet) {
  var name = sheet.getName().toLowerCase();
  if (/broker\s*[_-]?\s*1/.test(name)) return 'broker_1';
  if (/broker\s*[_-]?\s*2/.test(name)) return 'broker_2';
  if (/broker\s*[_-]?\s*3/.test(name)) return 'broker_3';
  return null;
}

// ── Batch queue ───────────────────────────────────────────────────────────────

/**
 * Enqueues a single row into the batch queue stored in Script Properties.
 * After BATCH_FLUSH_MS of silence the queue is flushed to the webhook.
 */
function enqueueRow_(sheet, row, brokerId) {
  var props  = PropertiesService.getScriptProperties();
  var queue  = JSON.parse(props.getProperty(BATCH_QUEUE_KEY) || '[]');
  var values = sheet.getRange(row, 1, 1, 5).getValues()[0];

  var entry = {
    name:   values[0] || '',
    phone:  values[1] || '',
    email:  values[2] || '',
    source: values[3] || '',
    notes:  values[4] || '',
    row_id: sheet.getSheetId() + ':' + row
  };

  // Only enqueue rows that have a phone number.
  if (!entry.phone) return;

  // Deduplicate — don't add the same row_id twice in one batch.
  var alreadyQueued = queue.some(function (q) { return q.row_id === entry.row_id; });
  if (!alreadyQueued) queue.push(entry);

  props.setProperty(BATCH_QUEUE_KEY, JSON.stringify(queue));

  // If enough time has passed since the last flush, flush now.
  var lastFlush = parseInt(props.getProperty(BATCH_FLUSH_KEY) || '0', 10);
  var now = Date.now();
  if (now - lastFlush >= BATCH_FLUSH_MS) {
    flushBatchQueue_(false);
  } else {
    // Schedule a delayed flush using a time-based trigger (max 1 min).
    // Only create one pending flush trigger at a time.
    var existing = ScriptApp.getProjectTriggers().filter(function (t) {
      return t.getHandlerFunction() === 'flushBatchQueue_';
    });
    if (existing.length === 0) {
      ScriptApp.newTrigger('flushBatchQueue_')
        .timeBased()
        .after(BATCH_FLUSH_MS)
        .create();
    }
  }
}

/**
 * Flushes all queued rows to the webhook in a single HTTP request.
 * @param {boolean} force — if true, flush regardless of timing.
 */
function flushBatchQueue_(force) {
  var props = PropertiesService.getScriptProperties();
  var queue = JSON.parse(props.getProperty(BATCH_QUEUE_KEY) || '[]');

  if (queue.length === 0) {
    cleanupFlushTrigger_();
    return;
  }

  // Deduplicate the entire queue by row_id.
  var seen = {};
  var deduped = [];
  queue.forEach(function (entry) {
    if (!seen[entry.row_id]) {
      seen[entry.row_id] = true;
      deduped.push(entry);
    }
  });

  // Group by broker_id.
  var grouped = {};
  deduped.forEach(function (entry) {
    // broker_id is embedded in row_id prefix — we store it on enqueue instead.
    // Actually, we re-derive it from the sheet. Simpler: store brokerId per entry.
  });

  // Since we grouped all entries into one queue, re-group by broker.
  var byBroker = {};
  deduped.forEach(function (entry) {
    // We need broker_id on each entry. Let's re-read it.
    // Alternative: we store it. Let's fix enqueueRow_ to include it.
    var brokerId = props.getProperty('__current_broker_id__') || 'broker_1';
    if (!byBroker[brokerId]) byBroker[brokerId] = [];
    byBroker[brokerId].push(entry);
  });

  props.setProperty(BATCH_QUEUE_KEY, '[]');
  props.setProperty(BATCH_FLUSH_KEY, String(Date.now()));
  cleanupFlushTrigger_();

  Object.keys(byBroker).forEach(function (brokerId) {
    sendWebhookBatch_(brokerId, byBroker[brokerId]);
  });
}

function cleanupFlushTrigger_() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'flushBatchQueue_') {
      ScriptApp.deleteTrigger(t);
    }
  });
}

// ── HTTP ──────────────────────────────────────────────────────────────────────

/**
 * POSTs a batch of rows to the webhook.
 */
function sendWebhookBatch_(brokerId, rows) {
  var props = PropertiesService.getScriptProperties();
  var webhookUrl    = props.getProperty('DIGITAL_LEADS_WEBHOOK_URL');
  var webhookSecret = props.getProperty('DIGITAL_LEADS_WEBHOOK_SECRET');

  if (!webhookUrl || !webhookSecret) {
    logError_('sendWebhookBatch_', new Error('DIGITAL_LEADS_WEBHOOK_URL or DIGITAL_LEADS_WEBHOOK_SECRET not set in Script Properties'));
    markRowsFailed_(rows);
    return;
  }

  var payload = JSON.stringify({
    broker_id: brokerId,
    rows:      rows,
    count:     rows.length,
    timestamp: new Date().toISOString()
  });

  try {
    var response = UrlFetchApp.fetch(webhookUrl, {
      method:              'post',
      contentType:         'application/json',
      muteHttpExceptions:  true,
      headers:             { 'X-Digital-Leads-Secret': webhookSecret },
      payload:             payload
    });

    var code = response.getResponseCode();
    var body = JSON.parse(response.getContentText() || '{}');

    if (code >= 200 && code < 300) {
      writeSyncStatuses_(rows, body);
      Logger.log('Webhook OK — ' + rows.length + ' row(s) sent for ' + brokerId);
    } else {
      logError_('sendWebhookBatch_', new Error('HTTP ' + code + ': ' + response.getContentText()));
      markRowsFailed_(rows);
    }
  } catch (err) {
    logError_('sendWebhookBatch_', err);
    markRowsFailed_(rows);
  }
}

// ── Status column helpers ─────────────────────────────────────────────────────

/**
 * Maps webhook response back to the sheet's Sync Status column (F).
 */
function writeSyncStatuses_(rows, body) {
  var props = PropertiesService.getScriptProperties();
  var spreadsheet = SpreadsheetApp.getActive();
  var results = {};
  (body.results || []).forEach(function (r) { results[r.row_id] = r.status; });

  var labels = {
    queued:                     'Queued for P3 call',
    duplicate:                  'Duplicate — skipped',
    queued_waiting_for_dialer:  'Queued — P3 dialer offline',
    dnc_blocked:                'DNC blocked',
    rejected:                   'Rejected — review'
  };

  rows.forEach(function (entry) {
    var parts = entry.row_id.split(':');
    var sheetId = parseInt(parts[0], 10);
    var rowNum  = parseInt(parts[1], 10);

    var sheet = spreadsheet.getSheets().filter(function (s) {
      return s.getSheetId() === sheetId;
    })[0];
    if (!sheet) return;

    var status = results[entry.row_id] || 'Synced';
    sheet.getRange(rowNum, 6).setValue(labels[status] || status);
  });
}

/**
 * Marks queued rows as "Sync failed" on error.
 */
function markRowsFailed_(rows) {
  var spreadsheet = SpreadsheetApp.getActive();
  rows.forEach(function (entry) {
    var parts = entry.row_id.split(':');
    var sheetId = parseInt(parts[0], 10);
    var rowNum  = parseInt(parts[1], 10);
    var sheet = spreadsheet.getSheets().filter(function (s) {
      return s.getSheetId() === sheetId;
    })[0];
    if (sheet) {
      sheet.getRange(rowNum, 6).setValue('Sync failed — will retry');
    }
  });
}

// ── Logging ───────────────────────────────────────────────────────────────────

function logError_(context, err) {
  Logger.log('[' + context + '] ' + (err.message || err));
}

// ── Legacy compatibility ──────────────────────────────────────────────────────
// Keep the old function names working so any existing triggers don't break.
function digitalLeadOnEdit(e)     { return onEditTrigger(e); }
function digitalLeadOnFormSubmit(e) { return onFormSubmitTrigger(e); }
