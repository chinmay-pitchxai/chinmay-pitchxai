"""Sync call_attempts + clear stale site_visit flags. Run on VPS."""
import json
import sqlite3
from pathlib import Path

DB = Path("/opt/technopolis/backend/data/vernika.db")
TEST = "7204955388"

conn = sqlite3.connect(str(DB))
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# 1. Test numbers -> DNC
cur.execute("SELECT id, name, phone, status FROM leads WHERE phone LIKE ?", (f"%{TEST}%",))
for r in cur.fetchall():
    if r["status"] != "dnc":
        cur.execute(
            "UPDATE leads SET status='dnc', analysis=?, updated_at=datetime('now') WHERE id=?",
            (json.dumps({"summary": "Test number — excluded.", "disposition": "DNC", "site_visit_agreed": False}), r["id"]),
        )
        print("DNC", r["id"], r["name"])

# 2. Sync call_attempts when lead no longer site_visit
cur.execute(
    """
    SELECT ca.id, ca.lead_id, ca.status, ca.disposition, l.status AS lead_status, l.name
    FROM call_attempts ca
    JOIN leads l ON l.id = ca.lead_id
    WHERE ca.status = 'site_visit' AND l.status != 'site_visit'
    """
)
stale = cur.fetchall()
for r in stale:
    cur.execute(
        """
        UPDATE call_attempts
        SET status=?, disposition=?, summary=substr(summary,1,200)
        WHERE id=?
        """,
        (r["lead_status"], "No Response" if r["lead_status"] == "no response" else r["lead_status"], r["id"]),
    )
    print("SYNC attempt", r["id"], "lead", r["lead_id"], r["name"], "->", r["lead_status"])

# 3. Clear site_visit_agreed on non-site_visit leads
cur.execute(
    """
    SELECT id, name, status, analysis FROM leads
    WHERE status NOT IN ('site_visit', 'site visit', 'site_visited')
      AND analysis LIKE '%site_visit_agreed%true%'
    """
)
for r in cur.fetchall():
    try:
        aj = json.loads(r["analysis"] or "{}")
    except Exception:
        aj = {}
    if aj.get("site_visit_agreed"):
        aj["site_visit_agreed"] = False
        cur.execute("UPDATE leads SET analysis=? WHERE id=?", (json.dumps(aj), r["id"]))
        print("Cleared site_visit_agreed", r["id"], r["name"])

conn.commit()

# Report final Site Visit Scheduled (both roles)
cur.execute(
    """
    SELECT id, role, name, phone, status,
           json_extract(analysis,'$.disposition') disp,
           json_extract(analysis,'$.site_visit_agreed') sv
    FROM leads
    WHERE status = 'site_visit'
    ORDER BY role, id
    """
)
print("\n=== REAL SITE VISITS (both roles) ===")
for r in cur.fetchall():
    print(dict(r))

cur.execute(
    """
    SELECT COUNT(*) FROM leads
    WHERE status = 'site_visit' AND role = 'sales_1'
    """
)
print("Vernika (sales_1) count:", cur.fetchone()[0])

conn.close()
