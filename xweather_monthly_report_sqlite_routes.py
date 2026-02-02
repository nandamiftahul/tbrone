from flask import Blueprint, render_template, request, jsonify, current_app, g, make_response
import os
import csv
import io
from datetime import datetime
from collections import Counter
import re

from openpyxl import load_workbook

import psycopg
from psycopg.rows import dict_row


xweather_report_bp = Blueprint("xweather_report", __name__)

# =========================================================
# PostgreSQL helpers (Railway)
# =========================================================
def _db_dsn():
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL not set (Railway PostgreSQL required)")
    return dsn


def get_db():
    if "db" not in g:
        g.db = psycopg.connect(
            _db_dsn(),
            row_factory=dict_row,
            autocommit=False
        )
    return g.db


@xweather_report_bp.teardown_app_request
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        try:
            if exception:
                db.rollback()
            else:
                db.commit()
        except Exception:
            pass
        db.close()


def init_db():
    db = get_db()
    with db.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS monthly_lightning_alerts (
            id BIGSERIAL PRIMARY KEY,
            report_month TEXT NOT NULL,
            severity TEXT NOT NULL,
            asset_name TEXT NOT NULL,
            extent_km DOUBLE PRECISION,
            first_event_time TEXT,
            active_time TEXT,
            last_event_time TEXT,
            clear_time TEXT,
            duration_min INTEGER,
            strength_ka DOUBLE PRECISION,
            event_type TEXT,
            created_at TEXT NOT NULL
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_month ON monthly_lightning_alerts(report_month);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_asset ON monthly_lightning_alerts(asset_name);")
    db.commit()


# =========================================================
# Utilities
# =========================================================
def _now_iso():
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm_sev(s):
    s = (s or "").strip().lower()
    if s in ("info", "warning", "alarm"):
        return s
    if "alarm" in s:
        return "alarm"
    if "warn" in s:
        return "warning"
    return "info"


def to_float(v):
    if v is None:
        return None
    s = str(v).strip()
    if s == "":
        return None
    m = re.search(r"[-+]?\d*\.?\d+", s.replace(",", "."))
    if not m:
        return None
    try:
        return float(m.group(0))
    except Exception:
        return None


def to_int(v):
    if v is None:
        return None
    s = str(v).strip()
    if s == "":
        return None
    try:
        return int(float(s))
    except Exception:
        return None


# =========================================================
# Pages
# =========================================================
@xweather_report_bp.route("/xweather/monthly-report")
def monthly_report_viewer():
    init_db()
    return render_template("xweather_monthly_report_sqlite.html")


@xweather_report_bp.route("/xweather/monthly-report-editor")
def monthly_report_editor():
    init_db()
    return render_template("xweather_monthly_report_editor_sqlite.html")


# =========================================================
# API: List rows
# =========================================================
@xweather_report_bp.route("/api/xweather/monthly-report", methods=["GET"])
def api_list_monthly_report():
    init_db()
    month = (request.args.get("month") or "").strip()
    if not month:
        return jsonify({"ok": False, "error": "missing month (YYYY-MM)"}), 400

    db = get_db()
    with db.cursor() as cur:
        cur.execute("""
            SELECT * FROM monthly_lightning_alerts
            WHERE report_month = %s
            ORDER BY id DESC
        """, (month,))
        rows = cur.fetchall()

    return jsonify({"ok": True, "month": month, "rows": rows})


# =========================================================
# API: Create row
# =========================================================
@xweather_report_bp.route("/api/xweather/monthly-report", methods=["POST"])
def api_create_row():
    init_db()
    p = request.get_json(force=True) or {}

    report_month = (p.get("report_month") or "").strip()
    asset_name = (p.get("asset_name") or "").strip()
    if not report_month or not asset_name:
        return jsonify({"ok": False, "error": "report_month & asset_name required"}), 400

    db = get_db()
    with db.cursor() as cur:
        cur.execute("""
            INSERT INTO monthly_lightning_alerts(
                report_month, severity, asset_name, extent_km,
                first_event_time, active_time, last_event_time, clear_time,
                duration_min, strength_ka, event_type, created_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING *
        """, (
            report_month,
            _norm_sev(p.get("severity")),
            asset_name,
            to_float(p.get("extent_km")),
            (p.get("first_event_time") or "").strip(),
            (p.get("active_time") or "").strip(),
            (p.get("last_event_time") or "").strip(),
            (p.get("clear_time") or "").strip(),
            to_int(p.get("duration_min")),
            to_float(p.get("strength_ka")),
            (p.get("event_type") or "").strip(),
            _now_iso()
        ))
        row = cur.fetchone()

    return jsonify({"ok": True, "row": row}), 201


# =========================================================
# API: Update row
# =========================================================
@xweather_report_bp.route("/api/xweather/monthly-report/<int:row_id>", methods=["PUT"])
def api_update_row(row_id):
    init_db()
    p = request.get_json(force=True) or {}

    db = get_db()
    with db.cursor() as cur:
        cur.execute("SELECT * FROM monthly_lightning_alerts WHERE id=%s", (row_id,))
        existing = cur.fetchone()
        if not existing:
            return jsonify({"ok": False, "error": "row not found"}), 404

        cur.execute("""
            UPDATE monthly_lightning_alerts SET
                report_month=%s, severity=%s, asset_name=%s, extent_km=%s,
                first_event_time=%s, active_time=%s, last_event_time=%s, clear_time=%s,
                duration_min=%s, strength_ka=%s, event_type=%s
            WHERE id=%s
            RETURNING *
        """, (
            (p.get("report_month") or existing["report_month"]).strip(),
            _norm_sev(p.get("severity", existing["severity"])),
            (p.get("asset_name") or existing["asset_name"]).strip(),
            to_float(p.get("extent_km", existing["extent_km"])),
            p.get("first_event_time", existing["first_event_time"]),
            p.get("active_time", existing["active_time"]),
            p.get("last_event_time", existing["last_event_time"]),
            p.get("clear_time", existing["clear_time"]),
            to_int(p.get("duration_min", existing["duration_min"])),
            to_float(p.get("strength_ka", existing["strength_ka"])),
            p.get("event_type", existing["event_type"]),
            row_id
        ))
        row = cur.fetchone()

    return jsonify({"ok": True, "row": row})


# =========================================================
# API: Delete row
# =========================================================
@xweather_report_bp.route("/api/xweather/monthly-report/<int:row_id>", methods=["DELETE"])
def api_delete_row(row_id):
    init_db()
    db = get_db()
    with db.cursor() as cur:
        cur.execute("DELETE FROM monthly_lightning_alerts WHERE id=%s RETURNING id", (row_id,))
        deleted = cur.fetchone()

    if not deleted:
        return jsonify({"ok": False, "error": "row not found"}), 404
    return jsonify({"ok": True, "deleted_id": row_id})


# =========================================================
# CSV export
# =========================================================
@xweather_report_bp.route("/xweather/monthly-report.csv")
def monthly_report_csv():
    init_db()
    month = (request.args.get("month") or "").strip()
    if not month:
        return "missing month", 400

    db = get_db()
    with db.cursor() as cur:
        cur.execute("""
            SELECT severity, asset_name, extent_km, first_event_time, active_time,
                   last_event_time, clear_time, duration_min, strength_ka, event_type
            FROM monthly_lightning_alerts
            WHERE report_month=%s
            ORDER BY id DESC
        """, (month,))
        rows = cur.fetchall()

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([
        "Severity","Asset name","Extent (km)","First event time","Active time",
        "Last event time","Clear time","Duration (min)","Strength (kA)","Type"
    ])
    for r in rows:
        w.writerow([
            r["severity"], r["asset_name"], r["extent_km"], r["first_event_time"],
            r["active_time"], r["last_event_time"], r["clear_time"],
            r["duration_min"], r["strength_ka"], r["event_type"]
        ])

    resp = make_response(out.getvalue())
    resp.headers["Content-Type"] = "text/csv"
    resp.headers["Content-Disposition"] = f"attachment; filename=Xweather_Monthly_Report_{month}.csv"
    return resp


# =========================================================
# Import CSV / XLSX
# =========================================================
@xweather_report_bp.route("/api/xweather/monthly-report/import-csv", methods=["POST"])
def api_import_csv():
    init_db()

    f = request.files.get("file")
    report_month = (request.form.get("report_month") or "").strip()
    mode = (request.form.get("mode") or "append").strip()

    if not f or not report_month:
        return jsonify({"ok": False, "error": "file & report_month required"}), 400

    filename = f.filename.lower()
    rows_data = []

    if filename.endswith(".csv"):
        reader = csv.DictReader(io.StringIO(f.read().decode("utf-8-sig")))
        rows_data = list(reader)

    elif filename.endswith(".xlsx"):
        wb = load_workbook(f, data_only=True)
        ws = wb.active
        headers = [c.value for c in ws[1]]
        for r in ws.iter_rows(min_row=2, values_only=True):
            rows_data.append(dict(zip(headers, r)))
    else:
        return jsonify({"ok": False, "error": "unsupported file type"}), 400

    if not rows_data:
        return jsonify({"ok": False, "error": "no data"}), 400

    header_map = {k.lower(): k for k in rows_data[0].keys()}
    H = lambda *n: next((header_map[x.lower()] for x in n if x.lower() in header_map), None)

    db = get_db()
    with db.cursor() as cur:
        if mode == "replace":
            cur.execute("DELETE FROM monthly_lightning_alerts WHERE report_month=%s", (report_month,))

        for r in rows_data:
            asset = (r.get(H("Asset name","Asset")) or "").strip()
            if not asset:
                continue

            cur.execute("""
                INSERT INTO monthly_lightning_alerts(
                    report_month,severity,asset_name,extent_km,
                    first_event_time,active_time,last_event_time,clear_time,
                    duration_min,strength_ka,event_type,created_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                report_month,
                _norm_sev(r.get(H("Severity"))),
                asset,
                to_float(r.get(H("Extent (km)","Extent"))),
                r.get(H("First event time")),
                r.get(H("Active time")),
                r.get(H("Last event time")),
                r.get(H("Clear time")),
                to_int(r.get(H("Duration (min)","Duration"))),
                to_float(r.get(H("Strength (kA)","Strength"))),
                r.get(H("Type","Event type")),
                _now_iso()
            ))

    return jsonify({"ok": True})


# =========================================================
# Expert Statistical Analysis
# =========================================================
@xweather_report_bp.route("/api/xweather/monthly-report/expert")
def api_expert():
    init_db()
    month = (request.args.get("month") or "").strip()
    if not month:
        return jsonify({"ok": False, "error": "missing month"}), 400

    db = get_db()
    with db.cursor() as cur:
        cur.execute("""
            SELECT severity, first_event_time, duration_min, strength_ka, event_type
            FROM monthly_lightning_alerts
            WHERE report_month=%s
        """, (month,))
        rows = cur.fetchall()

    total = len(rows)
    if total == 0:
        return jsonify({"ok": True, "month": month, "metrics": []})

    strengths = [abs(float(r["strength_ka"])) for r in rows if r["strength_ka"] is not None]

    metrics = [
        {"metric": "Total events", "value": total},
        {"metric": "Minimum |peak current| (kA)", "value": f"{min(strengths):.2f}" if strengths else ""},
        {"metric": "Average |peak current| (kA)", "value": f"{sum(strengths)/len(strengths):.2f}" if strengths else ""},
        {"metric": "Maximum |peak current| (kA)", "value": f"{max(strengths):.2f}" if strengths else ""},
    ]

    return jsonify({"ok": True, "month": month, "metrics": metrics})
