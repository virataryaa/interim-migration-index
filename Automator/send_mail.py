import sys
import datetime
import win32com.client

TO     = "virat.arya@etgworld.com"
STATUS = sys.argv[1] if len(sys.argv) > 1 else "UNKNOWN"
MSG    = sys.argv[2] if len(sys.argv) > 2 else ""
NOW    = datetime.datetime.now().strftime("%d %b %Y  %H:%M")

SUBJECTS = {
    "SUCCESS_PUSHED":   f"Index Positioning — Ingest OK, Synced — {NOW}",
    "SUCCESS_NOCHANGE": f"Index Positioning — Ingest OK, No Changes — {NOW}",
    "INGEST_FAIL":      f"Index Positioning — Ingest FAILED — {NOW}",
    "PUSH_FAIL":        f"Index Positioning — Ingest OK, Push FAILED — {NOW}",
}
STATUS_LINES = {
    "SUCCESS_PUSHED":   "Ingest OK — parquet synced & pushed to GitHub",
    "SUCCESS_NOCHANGE": "Ingest OK — no new data this run",
    "INGEST_FAIL":      "Ingest FAILED",
    "PUSH_FAIL":        "Ingest OK, but git push FAILED",
}

subject = SUBJECTS.get(STATUS, f"Index Positioning — {STATUS} — {NOW}")
status_line = STATUS_LINES.get(STATUS, STATUS)
footer = ("Streamlit dashboard will auto-redeploy shortly." if STATUS == "SUCCESS_PUSHED"
          else "Check Automator\\run_log.txt for full output." if STATUS in ("INGEST_FAIL", "PUSH_FAIL")
          else "")
body = (
    f"Time:    {NOW}\n"
    f"Status:  {status_line}\n"
    f"Detail:  {MSG}\n\n"
    f"{footer}"
)

outlook = win32com.client.Dispatch("Outlook.Application")
mail    = outlook.CreateItem(0)
mail.To      = TO
mail.Subject = subject
mail.Body    = body
mail.Send()
print(f"Mail sent: {subject}")
