# Scripts

Operational scripts for Technopolis/PitchXai. Run from the project root.

| Script | Description |
|--------|-------------|
| `audit_fix_today.py` | Audit + fix today's Interested/Site Visit leads on VPS |
| `audit_site_visits_all.py` | Audit ALL site_visit leads (both roles); keep real, fix fake |
| `backfill_log_id_phone_duplicates.py` | Copy `_log_id` / `start_time` from duplicate leads onto rows missing them |
| `backfill_log_ids_from_transcripts.py` | Attach `_log_id` to leads by matching `start_time` to transcript JSONL session timestamps |
| `backfill_recording_archive.py` | Copy session recordings into `Technopolis_Call_Recordings/{date}/{name_phone}/` for outcome leads |
| `build_kb_chunks.py` | Build `data/{role}/kb_chunks.json` from `rag_source.txt` section headers |
| `build_rag_db.py` | Build local RAG SQLite DB from project knowledge/docs files |
| `capture_live_greeting.py` | Capture Gemini Live native opening audio to `greeting_{role}.pcm` |
| `cleanup_fake_outcomes.py` | Full cleanup: site_visit + interested fake transcripts (run with `--apply`) |
| `export_role_leads.py` | Export leads for a role to CSV (optional disposition filter) |
| `migrate_sqlite_to_postgres.py` | One-time data migration from SQLite to PostgreSQL |
| `pre_campaign_vps_check.py` | Morning preflight: DB migrations, health, proof/lifecycle sanity |
| `purge_leads_before_date.py` | Delete campaign leads with activity before a calendar cutoff |
| `reanalyze_role_with_transcripts.py` | Re-run Gemini QA + soft-interest rules for one role |
| `reclassify_interest_from_transcripts.py` | Re-apply soft-interest rules to existing leads from transcripts + summaries |
| `restore_leads_from_db.py` | Copy all leads for a role from a source SQLite DB into the live DB |
| `setup_vobiz_inbound.py` | Provision Vobiz Applications + attach DIDs for inbound call routing |
| `sync_lead_analysis_from_db.py` | Merge analysis/status from a source DB into the live DB by phone+role |
| `sync_site_visit_cleanup.py` | Sync call_attempts + clear stale site_visit flags |
| `vobiz_callback_gateway.py` | Minimal public gateway for Vobiz callbacks during local testing |
| `weekly_zip_archive.py` | Weekly audio zip archive worker (packages previous week's recordings) |
| `wipe_local_full.py` | Wipe all local leads and campaign history for a fresh start |
