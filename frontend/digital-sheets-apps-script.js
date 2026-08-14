/**
 * Bind this script to one broker Google Sheet.
 * Columns: Name | Phone | Email | Source | Notes | Sync Status
 * Script Properties: WEBHOOK_URL, WEBHOOK_SECRET, BROKER_ID (broker_1/2/3).
 * Add an installable "On edit" trigger for digitalLeadOnEdit.
 */
function digitalLeadOnEdit(event) {
  if (!event || !event.range || event.range.getRow() < 2) return;
  const props = PropertiesService.getScriptProperties();
  const webhookUrl = props.getProperty('WEBHOOK_URL');
  const webhookSecret = props.getProperty('WEBHOOK_SECRET');
  const brokerId = props.getProperty('BROKER_ID');
  if (!webhookUrl || !webhookSecret || !/^broker_[123]$/.test(brokerId || '')) {
    throw new Error('Configure WEBHOOK_URL, WEBHOOK_SECRET and BROKER_ID.');
  }
  const sheet = event.range.getSheet();
  const rowNumber = event.range.getRow();
  const [name, phone, email, source, notes] = sheet.getRange(rowNumber, 1, 1, 5).getDisplayValues()[0];
  if (!phone) return;
  const response = UrlFetchApp.fetch(webhookUrl, {
    method: 'post', contentType: 'application/json', muteHttpExceptions: true,
    headers: {'X-Digital-Leads-Secret': webhookSecret},
    payload: JSON.stringify({broker_id: brokerId, rows: [{
      name, phone, email, source, notes,
      row_id: `${sheet.getSheetId()}:${rowNumber}`
    }]})
  });
  const body = JSON.parse(response.getContentText() || '{}');
  const ok = response.getResponseCode() >= 200 && response.getResponseCode() < 300;
  const status = ok
    ? (body.queued ? 'Queued for call' : body.duplicates ? 'Duplicate — skipped' : 'Rejected — review')
    : `Sync failed (${response.getResponseCode()})`;
  sheet.getRange(rowNumber, 6).setValue(status);
  if (!ok) throw new Error(response.getContentText());
}
