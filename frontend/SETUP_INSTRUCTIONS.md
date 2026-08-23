# Digital Leads Apps Script — Setup Instructions

## Prerequisites

- A Google Account with access to each broker's Google Sheet
- Your VPS webhook URL (e.g. `https://187.127.177.149/api/campaign/digital-leads/webhook`)
- A shared webhook secret configured on the VPS

## Step 1 — Open the Broker Sheet

Open the Google Sheet for **Broker 1** (repeat for Broker 2 and Broker 3).

## Step 2 — Open the Script Editor

1. Click **Extensions → Apps Script** in the menu bar.
2. Delete any placeholder code in the editor.

## Step 3 — Paste the Script

1. Copy the entire contents of `digital-sheets-apps-script.js`.
2. Paste it into the Apps Script editor, replacing everything.
3. Click the **Save** button (floppy disk icon).

## Step 4 — Set Script Properties

1. In the Apps Script editor, click the **gear icon** (Project Settings) in the left sidebar.
2. Scroll down to **Script Properties** and click **Edit script properties**.
3. Add the following properties:

| Property Name | Value |
|---|---|
| `DIGITAL_LEADS_WEBHOOK_URL` | `https://187.127.177.149/api/campaign/digital-leads/webhook` |
| `DIGITAL_LEADS_WEBHOOK_SECRET` | *(your shared secret from the VPS)* |

4. Click **Save script properties**.

> **Note:** The `broker_id` is detected automatically from the sheet name.
> Sheet names must contain "Broker 1", "Broker 2", or "Broker 3" (case-insensitive).
> Examples: `Broker 1 Leads`, `broker_2_sheet`, `Broker-3`.

## Step 5 — Install Triggers

1. Switch back to the **Code** tab in the editor.
2. Select `installDigitalLeadTriggers` from the function dropdown at the top.
3. Click **Run**.
4. The first time you run it, Google will ask for authorization:
   - Click **Review permissions**
   - Choose your Google Account
   - Click **Advanced → Go to *(project name)* (unsafe)**
   - Click **Allow**
5. You should see `Triggers installed for spreadsheet: ...` in the Execution log.

## Step 6 — Verify

1. In the broker sheet, add a test row in row 2 (or the next empty row):

   | Name | Phone | Email | Source | Notes | Sync Status |
   |------|-------|-------|--------|-------|-------------|
   | Test Lead | 5551234567 | test@example.com | Website | Test lead | |

2. Within a few seconds the **Sync Status** column should update (e.g. `Queued for P3 call`).
3. Check the VPS logs to confirm the webhook was received.

## Repeat for Each Broker

Repeat Steps 1–6 for Broker 2 and Broker 3 sheets.

## How It Works

| Trigger | What it does |
|---|---|
| `onEdit` | Detects new rows added at the bottom of the sheet and queues them for webhook POST |
| `onFormSubmit` | Catches Google Form submissions that append to the sheet |
| `Recovery scan` (every 1 min) | Catches rows added by API/import jobs that don't fire onEdit; also flushes any batched rows |

### Batch Mode

Multiple rows added in quick succession (within 5 seconds) are batched into a single webhook call. This avoids hammering the VPS when a user pastes several rows or a form submits rapidly.

### Error Handling

- If the webhook call fails, the row's Sync Status is set to `Sync failed — will retry`.
- The recovery scan retries failed rows on its next run.
- All errors are logged in the Apps Script **Executions** dashboard (Extensions → Apps Script → Executions).

## Troubleshooting

| Problem | Fix |
|---|---|
| "Configure DIGITAL_LEADS_WEBHOOK_URL..." error | Make sure both Script Properties are set and saved in Project Settings |
| Status stays blank | Check that row 1 has headers: `Name`, `Phone`, `Email`, `Source`, `Notes`, `Sync Status` |
| Status shows `Sync failed` | Check the webhook URL is reachable. Verify the secret matches. Check Executions log for details. |
| Triggers not firing | Re-run `installDigitalLeadTriggers`. Check Extensions → Apps Script → Triggers to see active triggers. |
| Sheet name not detected | Rename the sheet to include "Broker 1", "Broker 2", or "Broker 3". |

## Updating the Script

If you make changes to the Apps Script:
1. Paste the new code in the editor.
2. Save.
3. Run `installDigitalLeadTriggers` again to refresh triggers (old triggers are auto-removed).
