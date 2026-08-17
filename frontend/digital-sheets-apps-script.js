/**
 * Bind this script to one broker Google Sheet.
 * Columns: Name | Phone | Email | Source | Notes | Sync Status
 * Script Properties: WEBHOOK_URL, WEBHOOK_SECRET, BROKER_ID (broker_1/2/3).
 * Run installDigitalLeadTriggers() once after setting the properties.
 */
function digitalLeadOnEdit(event) {
  if (!event || !event.range || event.range.getLastRow() < 2) return;
  syncDigitalLeadRange_(event.range);
}

/** Supports Google Form responses connected to the broker lead sheet. */
function digitalLeadOnFormSubmit(event) {
  if (!event || !event.range) return;
  syncDigitalLeadRange_(event.range);
}

/**
 * Run once after setting Script Properties. It installs the live edit/form
 * triggers and a one-minute recovery scan for rows added by API/import jobs
 * (Google does not fire edit triggers for changes made by another script/API).
 */
function installDigitalLeadTriggers() {
  const spreadsheet = SpreadsheetApp.getActive();
  ScriptApp.getProjectTriggers().forEach(trigger => {
    if (['digitalLeadOnEdit', 'digitalLeadOnFormSubmit', 'digitalLeadRecoverySync']
      .includes(trigger.getHandlerFunction())) {
      ScriptApp.deleteTrigger(trigger);
    }
  });
  ScriptApp.newTrigger('digitalLeadOnEdit').forSpreadsheet(spreadsheet).onEdit().create();
  ScriptApp.newTrigger('digitalLeadOnFormSubmit').forSpreadsheet(spreadsheet).onFormSubmit().create();
  ScriptApp.newTrigger('digitalLeadRecoverySync').timeBased().everyMinutes(1).create();
}

/** Backfills rows inserted by imports/API integrations that cannot fire onEdit. */
function digitalLeadRecoverySync() {
  const sheet = SpreadsheetApp.getActive().getSheetByName('Digital Leads');
  if (!sheet || sheet.getLastRow() < 2) return;
  const values = sheet.getRange(2, 1, sheet.getLastRow() - 1, 6).getDisplayValues();
  values.forEach((row, index) => {
    const phone = row[1];
    const status = row[5];
    if (phone && (!status || /^Sync failed/.test(status))) {
      syncDigitalLeadRange_(sheet.getRange(index + 2, 1, 1, 5));
    }
  });
}

function syncDigitalLeadRange_(range) {
  const props = PropertiesService.getScriptProperties();
  const webhookUrl = props.getProperty('WEBHOOK_URL');
  const webhookSecret = props.getProperty('WEBHOOK_SECRET');
  const brokerId = props.getProperty('BROKER_ID');
  if (!webhookUrl || !webhookSecret || !/^broker_[123]$/.test(brokerId || '')) {
    throw new Error('Configure WEBHOOK_URL, WEBHOOK_SECRET and BROKER_ID.');
  }
  const sheet = range.getSheet();
  const firstRow = Math.max(2, range.getRow());
  const lastRow = range.getLastRow();
  if (lastRow < firstRow) return;
  const values = sheet.getRange(firstRow, 1, lastRow - firstRow + 1, 5).getDisplayValues();
  const rows = values.map((row, index) => ({
    name: row[0], phone: row[1], email: row[2], source: row[3], notes: row[4],
    row_id: `${sheet.getSheetId()}:${firstRow + index}`
  })).filter(row => row.phone);
  if (!rows.length) return;
  const response = UrlFetchApp.fetch(webhookUrl, {
    method: 'post', contentType: 'application/json', muteHttpExceptions: true,
    headers: {'X-Digital-Leads-Secret': webhookSecret},
    payload: JSON.stringify({broker_id: brokerId, rows})
  });
  const body = JSON.parse(response.getContentText() || '{}');
  const ok = response.getResponseCode() >= 200 && response.getResponseCode() < 300;
  const resultByRow = {};
  (body.results || []).forEach(result => { resultByRow[result.row_id] = result.status; });
  const labels = {
    queued: 'Queued for P3 call', duplicate: 'Duplicate — skipped',
    queued_waiting_for_dialer: 'Queued — P3 dialer offline',
    dnc_blocked: 'DNC blocked', rejected: 'Rejected — review'
  };
  const statuses = values.map((row, index) => {
    if (!row[1]) return [''];
    if (!ok) return [`Sync failed (${response.getResponseCode()})`];
    const rowId = `${sheet.getSheetId()}:${firstRow + index}`;
    return [labels[resultByRow[rowId]] || 'Rejected — review'];
  });
  sheet.getRange(firstRow, 6, statuses.length, 1).setValues(statuses);
  if (!ok) throw new Error(response.getContentText());
}
