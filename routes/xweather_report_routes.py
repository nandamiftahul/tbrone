from flask import Blueprint, render_template, make_response
import csv
import io

xweather_report_bp = Blueprint("xweather_report", __name__)

# ===============================
# PAGE HTML
# ===============================
@xweather_report_bp.route("/xweather/monthly-report")
def xweather_monthly_report():
    return render_template("project/xweather_monthly_report.html")


# ===============================
# CSV DOWNLOAD
# ===============================
@xweather_report_bp.route("/xweather/monthly-report.csv")
def xweather_monthly_report_csv():
    # ⚠️ nanti bisa diganti dari DB / API / XLS parsing
    headers = [
        "Severity", "Asset", "Extent (km)",
        "First Event Time", "Active Time",
        "Last Event Time", "Clear Time",
        "Duration (min)", "Strength (kA)", "Type"
    ]

    dummy_rows = [
        ["Warning", "ZONE01", 10, "01-01-2026 05:01", "01-01-2026 05:12",
         "01-01-2026 05:30", "01-01-2026 05:45", 15, 16.4, "CG-"],
        ["Alarm", "ZONE03", 4, "01-01-2026 06:10", "01-01-2026 06:12",
         "01-01-2026 06:40", "01-01-2026 06:55", 15, 43.8, "CG+"],
    ]

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(headers)
    writer.writerows(dummy_rows)

    resp = make_response(output.getvalue())
    resp.headers["Content-Type"] = "text/csv"
    resp.headers["Content-Disposition"] = (
        "attachment; filename=Xweather_Monthly_Report.csv"
    )
    return resp
