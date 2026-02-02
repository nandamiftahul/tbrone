from flask import Blueprint, render_template, request, jsonify, current_app, g, make_response
import sqlite3
import csv
import io
from datetime import datetime
from openpyxl import load_workbook

xweather_report_bp = Blueprint("xweather_report", __name__)

# =========================
# SQLite helpers
# =========================
def _db_path():
    # kamu bisa set app.config["XWEATHER_DB_PATH"]
    return current_app.config.get("XWEATHER_DB_PATH", "xweather_reports.db")

def get_db():
    if "db" not in g:
        conn = sqlite3.connect(_db_path())
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db

@xweather_report_bp.teardown_app_request
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    db = get_db()
    db.execute("""
    CREATE TABLE IF NOT EXISTS monthly_lightning_alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        report_month TEXT NOT NULL,           -- "YYYY-MM"
        severity TEXT NOT NULL,               -- "info"|"warning"|"alarm"
        asset_name TEXT NOT NULL,
        extent_km REAL,
        first_event_time TEXT,
        active_time TEXT,
        last_event_time TEXT,
        clear_time TEXT,
        duration_min INTEGER,
        strength_ka REAL,
        event_type TEXT,
        created_at TEXT NOT NULL
    )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_month ON monthly_lightning_alerts(report_month)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_asset ON monthly_lightning_alerts(asset_name)")
    db.commit()

def _now_iso():
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def _norm_sev(s: str) -> str:
    s = (s or "").strip().lower()
    if s in ("alarm", "warning", "info"):
        return s
    # fallback: detect
    if "alarm" in s: return "alarm"
    if "warn" in s: return "warning"
    return "info"


# =========================
# Pages
# =========================
@xweather_report_bp.route("/xweather/monthly-report-editor")
def monthly_report_editor_page():
    # ensure DB exists
    init_db()
    return render_template("xweather_monthly_report_editor_sqlite.html")


# =========================
# API
# =========================
@xweather_report_bp.route("/api/xweather/monthly-report", methods=["GET"])
def api_list_monthly_report():
    init_db()
    month = (request.args.get("month") or "").strip()  # "YYYY-MM"
    if not month:
        return jsonify({"ok": False, "error": "missing month (YYYY-MM)"}), 400

    db = get_db()
    rows = db.execute("""
        SELECT * FROM monthly_lightning_alerts
        WHERE report_month = ?
        ORDER BY id DESC
    """, (month,)).fetchall()

    data = [dict(r) for r in rows]
    return jsonify({"ok": True, "month": month, "rows": data})


@xweather_report_bp.route("/api/xweather/monthly-report", methods=["POST"])
def api_create_monthly_report_row():
    init_db()
    payload = request.get_json(force=True, silent=True) or {}

    report_month = (payload.get("report_month") or "").strip()
    if not report_month:
        return jsonify({"ok": False, "error": "report_month required (YYYY-MM)"}), 400

    severity = _norm_sev(payload.get("severity"))
    asset_name = (payload.get("asset_name") or "").strip()
    if not asset_name:
        return jsonify({"ok": False, "error": "asset_name required"}), 400

    def _to_float(v):
        if v in ("", None): return None
        try: return float(v)
        except: return None

    def _to_int(v):
        if v in ("", None): return None
        try: return int(float(v))
        except: return None

    db = get_db()
    cur = db.execute("""
        INSERT INTO monthly_lightning_alerts(
            report_month, severity, asset_name, extent_km,
            first_event_time, active_time, last_event_time, clear_time,
            duration_min, strength_ka, event_type, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        report_month,
        severity,
        asset_name,
        _to_float(payload.get("extent_km")),
        (payload.get("first_event_time") or "").strip(),
        (payload.get("active_time") or "").strip(),
        (payload.get("last_event_time") or "").strip(),
        (payload.get("clear_time") or "").strip(),
        _to_int(payload.get("duration_min")),
        _to_float(payload.get("strength_ka")),
        (payload.get("event_type") or "").strip(),
        _now_iso()
    ))
    db.commit()

    new_id = cur.lastrowid
    row = db.execute("SELECT * FROM monthly_lightning_alerts WHERE id = ?", (new_id,)).fetchone()
    return jsonify({"ok": True, "row": dict(row)}), 201


@xweather_report_bp.route("/api/xweather/monthly-report/<int:row_id>", methods=["PUT"])
def api_update_monthly_report_row(row_id: int):
    init_db()
    payload = request.get_json(force=True, silent=True) or {}

    db = get_db()
    existing = db.execute("SELECT * FROM monthly_lightning_alerts WHERE id = ?", (row_id,)).fetchone()
    if not existing:
        return jsonify({"ok": False, "error": "row not found"}), 404

    def pick(key, default=None):
        return payload[key] if key in payload else default

    severity = _norm_sev(pick("severity", existing["severity"]))
    asset_name = (pick("asset_name", existing["asset_name"]) or "").strip()
    report_month = (pick("report_month", existing["report_month"]) or "").strip()

    if not asset_name:
        return jsonify({"ok": False, "error": "asset_name required"}), 400
    if not report_month:
        return jsonify({"ok": False, "error": "report_month required (YYYY-MM)"}), 400

    def _to_float(v, fallback):
        if v in ("", None): return None
        try: return float(v)
        except: return fallback

    def _to_int(v, fallback):
        if v in ("", None): return None
        try: return int(float(v))
        except: return fallback

    extent_km = _to_float(pick("extent_km", existing["extent_km"]), existing["extent_km"])
    duration_min = _to_int(pick("duration_min", existing["duration_min"]), existing["duration_min"])
    strength_ka = _to_float(pick("strength_ka", existing["strength_ka"]), existing["strength_ka"])

    db.execute("""
        UPDATE monthly_lightning_alerts SET
            report_month = ?,
            severity = ?,
            asset_name = ?,
            extent_km = ?,
            first_event_time = ?,
            active_time = ?,
            last_event_time = ?,
            clear_time = ?,
            duration_min = ?,
            strength_ka = ?,
            event_type = ?
        WHERE id = ?
    """, (
        report_month,
        severity,
        asset_name,
        extent_km,
        (pick("first_event_time", existing["first_event_time"]) or "").strip(),
        (pick("active_time", existing["active_time"]) or "").strip(),
        (pick("last_event_time", existing["last_event_time"]) or "").strip(),
        (pick("clear_time", existing["clear_time"]) or "").strip(),
        duration_min,
        strength_ka,
        (pick("event_type", existing["event_type"]) or "").strip(),
        row_id
    ))
    db.commit()

    row = db.execute("SELECT * FROM monthly_lightning_alerts WHERE id = ?", (row_id,)).fetchone()
    return jsonify({"ok": True, "row": dict(row)})


@xweather_report_bp.route("/api/xweather/monthly-report/<int:row_id>", methods=["DELETE"])
def api_delete_monthly_report_row(row_id: int):
    init_db()
    db = get_db()
    cur = db.execute("DELETE FROM monthly_lightning_alerts WHERE id = ?", (row_id,))
    db.commit()
    if cur.rowcount == 0:
        return jsonify({"ok": False, "error": "row not found"}), 404
    return jsonify({"ok": True, "deleted_id": row_id})


# =========================
# CSV export (by month)
# =========================
@xweather_report_bp.route("/xweather/monthly-report.csv", methods=["GET"])
def monthly_report_csv():
    init_db()
    month = (request.args.get("month") or "").strip()
    if not month:
        return "missing month (YYYY-MM)", 400

    db = get_db()
    rows = db.execute("""
        SELECT severity, asset_name, extent_km, first_event_time, active_time,
               last_event_time, clear_time, duration_min, strength_ka, event_type
        FROM monthly_lightning_alerts
        WHERE report_month = ?
        ORDER BY id DESC
    """, (month,)).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Severity","Asset name","Extent (km)","First event time","Active time",
        "Last event time","Clear time","Duration (min)","Strength (kA)","Type"
    ])
    for r in rows:
        writer.writerow([
            r["severity"], r["asset_name"], r["extent_km"], r["first_event_time"], r["active_time"],
            r["last_event_time"], r["clear_time"], r["duration_min"], r["strength_ka"], r["event_type"]
        ])

    resp = make_response(output.getvalue())
    resp.headers["Content-Type"] = "text/csv"
    resp.headers["Content-Disposition"] = f"attachment; filename=Xweather_Monthly_Report_{month}.csv"
    return resp

@xweather_report_bp.route("/api/xweather/monthly-report/assets", methods=["GET"])
def api_monthly_report_assets():
    init_db()
    month = (request.args.get("month") or "").strip()
    if not month:
        return jsonify({"ok": False, "error": "missing month (YYYY-MM)"}), 400

    db = get_db()
    rows = db.execute("""
        SELECT DISTINCT asset_name
        FROM monthly_lightning_alerts
        WHERE report_month = ?
        ORDER BY asset_name ASC
    """, (month,)).fetchall()

    assets = [r["asset_name"] for r in rows]
    return jsonify({"ok": True, "month": month, "assets": assets})

@xweather_report_bp.route("/xweather/monthly-report")
def xweather_monthly_report_viewer():
    init_db()
    return render_template("xweather_monthly_report_sqlite.html")


@xweather_report_bp.route("/api/xweather/monthly-report/import-csv", methods=["POST"])
def api_import_monthly_report_csv():
    init_db()

    f = request.files.get("file")
    report_month = (request.form.get("report_month") or "").strip()
    mode = (request.form.get("mode") or "append").strip().lower()

    if not f:
        return jsonify({"ok": False, "error": "missing file"}), 400
    if not report_month:
        return jsonify({"ok": False, "error": "report_month required (YYYY-MM)"}), 400
    if mode not in ("append", "replace"):
        return jsonify({"ok": False, "error": "mode must be append or replace"}), 400

    filename = f.filename.lower()

    # =========================
    # READ DATA (CSV or XLSX)
    # =========================
    rows_data = []

    if filename.endswith(".csv"):
        raw = f.read()
        try:
            text = raw.decode("utf-8-sig")
        except Exception:
            text = raw.decode("latin-1")

        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            return jsonify({"ok": False, "error": "CSV has no header"}), 400

        rows_data = list(reader)

    elif filename.endswith(".xlsx") or filename.endswith(".xls"):
        wb = load_workbook(f, data_only=True)
        ws = wb.active

        headers = []
        for c in ws[1]:
            headers.append(str(c.value).strip() if c.value else "")

        if not headers or not headers[0]:
            return jsonify({"ok": False, "error": "Excel header row missing"}), 400

        for r in ws.iter_rows(min_row=2, values_only=True):
            row = {}
            for i, h in enumerate(headers):
                row[h] = r[i] if i < len(r) else None
            rows_data.append(row)

    else:
        return jsonify({
            "ok": False,
            "error": "Unsupported file type. Use CSV, XLS, or XLSX"
        }), 400


