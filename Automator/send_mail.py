import sys
import datetime
import win32com.client

TO     = "virat.arya@etgworld.com"
STATUS = sys.argv[1] if len(sys.argv) > 1 else "UNKNOWN"
MSG    = sys.argv[2] if len(sys.argv) > 2 else ""
NOW    = datetime.datetime.now().strftime("%d %b %Y  %H:%M")

if STATUS == "SUCCESS":
    subject = f"Index Positioning Update — {NOW}"
    body    = (
        f"Index (CFTC Index Traders) positioning sync completed successfully.\n\n"
        f"Time:    {NOW}\n"
        f"Status:  Parquet synced & pushed to GitHub\n"
        f"Detail:  {MSG}\n\n"
        f"Streamlit dashboard will auto-redeploy shortly."
    )
else:
    subject = f"Index Positioning FAILED — {NOW}"
    body    = (
        f"Index positioning sync encountered an error.\n\n"
        f"Time:    {NOW}\n"
        f"Status:  FAILED\n"
        f"Detail:  {MSG}\n\n"
        f"Check Automator\\run_log.txt for full output."
    )

outlook = win32com.client.Dispatch("Outlook.Application")
mail    = outlook.CreateItem(0)
mail.To      = TO
mail.Subject = subject
mail.Body    = body
mail.Send()
print(f"Mail sent: {subject}")
