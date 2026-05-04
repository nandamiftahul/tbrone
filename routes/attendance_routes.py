from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, Response, send_file
from datetime import datetime, date
from flask_login import current_user, login_required
from routes.attendance_models import db, Employee, Attendance, LeaveRequest, Office, Announcement, Shift, User
import csv
import io
import json
from openpyxl import Workbook
import base64
import numpy as np

attendance_bp = Blueprint("attendance", __name__, url_prefix="/attendance")


# =========================================================
# DASHBOARD
# =========================================================
@attendance_bp.route("/")
@login_required
def dashboard():
    today = date.today()

    records = Attendance.query.filter_by(work_date=today).all()

    total_employees = Employee.query.filter_by(is_active=True).count()

    stats = {
        "total": len(records),
        "employees": total_employees,
        "present": len([r for r in records if r.status in ["present", "late"]]),
        "late": len([r for r in records if r.status == "late"]),
        "leave": len([r for r in records if r.status == "leave"]),
        "sick": len([r for r in records if r.status == "sick"]),
        "wfh": len([r for r in records if r.status == "wfh"]),
        "absent": len([r for r in records if r.status == "absent"]),
        "not_recorded": max(total_employees - len(records), 0),
    }

    recent_records = (
        Attendance.query
        .filter_by(work_date=today)
        .order_by(Attendance.check_in.desc().nullslast())
        .limit(8)
        .all()
    )

    pending_leave = LeaveRequest.query.filter(
        LeaveRequest.status.in_(["pending", "pending_manager", "pending_hrd"])
    ).count()

    return render_template(
        "attendance/dashboard.html",
        today=today,
        stats=stats,
        recent_records=recent_records,
        pending_leave=pending_leave
    )

# =========================================================
# CHECK IN / OUT
# =========================================================
@attendance_bp.route("/check", methods=["GET", "POST"])
def attendance_check():
    if request.method == "POST":
        emp_id = request.form.get("employee_id")
        action = request.form.get("action")
        lat = request.form.get("lat")
        lon = request.form.get("lon")

        today = date.today()

        rec = Attendance.query.filter_by(
            employee_id=emp_id,
            work_date=today
        ).first()

        if not rec:
            rec = Attendance(
                employee_id=emp_id,
                work_date=today
            )
            db.session.add(rec)

        now = datetime.utcnow()

        if action == "check_in":
            rec.check_in = now
        elif action == "check_out":
            rec.check_out = now

        db.session.commit()
        flash("Attendance updated")
        return redirect(url_for("attendance.attendance_check"))

    employees = Employee.query.filter_by(is_active=True).all()
    return render_template("attendance/check.html", employees=employees)


# =========================================================
# RECORD LIST
# =========================================================
@attendance_bp.route("/list")
@login_required
def attendance_list():
    start = request.args.get("start")
    end = request.args.get("end")
    emp = request.args.get("employee")
    status = request.args.get("status")

    query = Attendance.query.join(Employee)

    if start:
        query = query.filter(Attendance.work_date >= start)
    if end:
        query = query.filter(Attendance.work_date <= end)
    if emp:
        query = query.filter(
            db.or_(
                Employee.name.ilike(f"%{emp}%"),
                Employee.code.ilike(f"%{emp}%"),
                Employee.dept.ilike(f"%{emp}%")
            )
        )
    if status:
        query = query.filter(Attendance.status == status)

    rows = query.order_by(Attendance.work_date.desc(), Employee.name.asc()).all()

    stats = {
        "total": len(rows),
        "present": len([r for r in rows if r.status in ["present", "late"]]),
        "late": len([r for r in rows if r.status == "late"]),
        "leave": len([r for r in rows if r.status == "leave"]),
        "sick": len([r for r in rows if r.status == "sick"]),
        "wfh": len([r for r in rows if r.status == "wfh"]),
        "absent": len([r for r in rows if r.status == "absent"]),
    }

    return render_template(
        "attendance/list.html",
        rows=rows,
        start=start,
        end=end,
        emp=emp,
        status=status,
        stats=stats
    )

def _attendance_filtered_query():
    start = request.args.get("start")
    end = request.args.get("end")
    emp = request.args.get("employee")
    status = request.args.get("status")

    query = Attendance.query.join(Employee)

    if start:
        query = query.filter(Attendance.work_date >= start)
    if end:
        query = query.filter(Attendance.work_date <= end)
    if emp:
        query = query.filter(
            db.or_(
                Employee.name.ilike(f"%{emp}%"),
                Employee.code.ilike(f"%{emp}%"),
                Employee.dept.ilike(f"%{emp}%")
            )
        )
    if status:
        query = query.filter(Attendance.status == status)

    return query.order_by(Attendance.work_date.desc(), Employee.name.asc())

@attendance_bp.route("/list/export.csv")
@login_required
def attendance_export_csv():
    rows = _attendance_filtered_query().all()

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "Date", "Code", "Name", "Dept",
        "Check In", "Check Out", "Hours",
        "Status", "Note"
    ])

    for r in rows:
        writer.writerow([
            r.work_date,
            r.employee.code if r.employee else "",
            r.employee.name if r.employee else "",
            r.employee.dept if r.employee else "",
            r.check_in or "",
            r.check_out or "",
            r.duration_hours,
            r.status or "",
            r.note or "",
        ])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=attendance_records.csv"
        }
    )

@attendance_bp.route("/list/export.xlsx")
@login_required
def attendance_export_xlsx():
    rows = _attendance_filtered_query().all()

    wb = Workbook()
    ws = wb.active
    ws.title = "Attendance Records"

    headers = [
        "Date", "Code", "Name", "Dept",
        "Check In", "Check Out", "Hours",
        "Status", "Note"
    ]
    ws.append(headers)

    for r in rows:
        ws.append([
            str(r.work_date),
            r.employee.code if r.employee else "",
            r.employee.name if r.employee else "",
            r.employee.dept if r.employee else "",
            str(r.check_in or ""),
            str(r.check_out or ""),
            r.duration_hours,
            r.status or "",
            r.note or "",
        ])

    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)

    return send_file(
        bio,
        as_attachment=True,
        download_name="attendance_records.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

@attendance_bp.route("/api/import", methods=["POST"])
@login_required
def attendance_import_json():
    f = request.files.get("file")
    overwrite = request.args.get("overwrite") == "1"

    if not f:
        return jsonify({"ok": False, "error": "No file uploaded"}), 400

    try:
        data = json.load(f)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Invalid JSON: {e}"}), 400

    if isinstance(data, dict):
        records = data.get("records") or data.get("attendance") or []
    elif isinstance(data, list):
        records = data
    else:
        return jsonify({"ok": False, "error": "JSON must be list or object with records"}), 400

    created = 0
    updated = 0
    skipped = 0

    for item in records:
        code = str(item.get("code") or item.get("employee_code") or "").strip()
        work_date = item.get("work_date") or item.get("date")

        if not code or not work_date:
            skipped += 1
            continue

        emp = Employee.query.filter_by(code=code).first()
        if not emp:
            skipped += 1
            continue

        rec = Attendance.query.filter_by(
            employee_id=emp.id,
            work_date=work_date
        ).first()

        if not rec:
            rec = Attendance(employee_id=emp.id, work_date=work_date)
            db.session.add(rec)
            created += 1
        else:
            if not overwrite:
                skipped += 1
                continue
            updated += 1

        rec.status = item.get("status") or rec.status
        rec.note = item.get("note") or rec.note

        if item.get("check_in"):
            rec.check_in = datetime.fromisoformat(str(item.get("check_in")).replace("Z", "+00:00")).replace(tzinfo=None)
        if item.get("check_out"):
            rec.check_out = datetime.fromisoformat(str(item.get("check_out")).replace("Z", "+00:00")).replace(tzinfo=None)

    db.session.commit()

    return jsonify({
        "ok": True,
        "created": created,
        "updated": updated,
        "skipped": skipped
    })
# =========================================================
# LEAVE REQUEST
# =========================================================
@attendance_bp.route("/leave", methods=["GET", "POST"])
@login_required
def leave_request():
    if request.method == "POST":
        r = LeaveRequest(
            employee_id=request.form.get("employee_id"),
            type=request.form.get("type"),
            start_date=request.form.get("start_date"),
            end_date=request.form.get("end_date"),
            reason=request.form.get("reason"),
            status="pending_manager"
        )
        db.session.add(r)
        db.session.commit()

        flash("Leave request submitted")
        return redirect(url_for("attendance.leave_request"))

    employees = Employee.query.filter_by(is_active=True).order_by(Employee.name.asc()).all()
    rows = LeaveRequest.query.order_by(LeaveRequest.id.desc()).limit(50).all()

    stats = {
        "total": LeaveRequest.query.count(),
        "pending": LeaveRequest.query.filter(
            LeaveRequest.status.in_(["pending", "pending_manager", "pending_hrd"])
        ).count(),
        "approved": LeaveRequest.query.filter_by(status="approved").count(),
        "rejected": LeaveRequest.query.filter_by(status="rejected").count(),
    }

    return render_template(
        "attendance/leave.html",
        employees=employees,
        rows=rows,
        stats=stats
    )

# =========================================================
# APPROVALS
# =========================================================
@attendance_bp.route("/approvals")
def approvals():
    rows = LeaveRequest.query.order_by(LeaveRequest.id.desc()).all()
    return render_template("attendance/approvals.html", rows=rows)


@attendance_bp.route("/approve/<int:rid>", methods=["POST"])
def approve(rid):
    r = LeaveRequest.query.get_or_404(rid)
    r.status = "approved"
    r.approved_by = current_user.username
    db.session.commit()
    return redirect(url_for("attendance.approvals"))


@attendance_bp.route("/reject/<int:rid>", methods=["POST"])
def reject(rid):
    r = LeaveRequest.query.get_or_404(rid)
    r.status = "rejected"
    r.approved_by = current_user.username
    db.session.commit()
    return redirect(url_for("attendance.approvals"))


# =========================================================
# EMPLOYEES
# =========================================================
@attendance_bp.route("/employees")
def employees():
    rows = Employee.query.all()
    shifts = Shift.query.all()
    return render_template("attendance/employees.html", rows=rows, shifts=shifts)


@attendance_bp.route("/employees/create", methods=["POST"])
def employee_create():
    e = Employee(
        code=request.form.get("code"),
        name=request.form.get("name"),
        email=request.form.get("email"),
        dept=request.form.get("dept"),
        role=request.form.get("role"),
        shift_id=request.form.get("shift_id") or None
    )
    db.session.add(e)
    db.session.commit()
    return redirect(url_for("attendance.employees"))

@attendance_bp.route("/employees/<int:emp_id>/update", methods=["POST"])
def employee_update(emp_id):
    e = Employee.query.get_or_404(emp_id)

    e.code = request.form.get("code")
    e.name = request.form.get("name")
    e.email = request.form.get("email") or None
    e.dept = request.form.get("dept") or None
    e.role = request.form.get("role") or "staff"
    e.shift_id = request.form.get("shift_id") or None
    e.is_active = True if request.form.get("is_active") else False

    login_username = (request.form.get("login_email") or "").strip().lower()
    new_password = request.form.get("new_password") or ""

    if e.user:
        if login_username:
            e.user.email = login_username

        e.user.name = e.name
        e.user.role = e.role
        e.user.is_active = e.is_active

        if request.form.get("reset_password") == "1" and new_password:
            e.user.set_password(new_password)

    elif request.form.get("create_login") == "1":
        if login_username and new_password:
            u = User(
                name=e.name,
                email=login_username,
                role=e.role,
                is_active=e.is_active
            )
            u.set_password(new_password)
            db.session.add(u)
            db.session.flush()
            e.user_id = u.id

    db.session.commit()
    flash("Employee and login account updated")
    return redirect(url_for("attendance.employees"))

@attendance_bp.route("/employees/<int:emp_id>/delete", methods=["POST"])
def employee_delete(emp_id):
    e = Employee.query.get_or_404(emp_id)
    db.session.delete(e)
    db.session.commit()
    return redirect(url_for("attendance.employees"))


# =========================================================
# OFFICE (GEOFENCE)
# =========================================================
@attendance_bp.route("/offices", methods=["GET", "POST"])
def offices():
    if request.method == "POST":
        if "create" in request.form:
            o = Office(
                name=request.form.get("name"),
                lat=request.form.get("lat"),
                lon=request.form.get("lon"),
                radius_m=request.form.get("radius_m")
            )
            db.session.add(o)

        if "set_active" in request.form:
            oid = request.form.get("office_id")
            Office.query.update({"is_active": False})
            active = Office.query.get(oid)
            active.is_active = True

        db.session.commit()
        return redirect(url_for("attendance.offices"))

    offices = Office.query.all()
    active = Office.query.filter_by(is_active=True).first()

    return render_template(
        "attendance/offices.html",
        offices=offices,
        active=active
    )


# =========================================================
# ANNOUNCEMENTS
# =========================================================
@attendance_bp.route("/announcements", methods=["GET", "POST"])
@login_required
def announcements():
    if request.method == "POST":
        start_at = request.form.get("start_at") or None
        end_at = request.form.get("end_at") or None

        a = Announcement(
            title=request.form.get("title"),
            body=request.form.get("body"),
            level=request.form.get("level") or "info",
            is_active=True if request.form.get("is_active") else False,
            start_at=datetime.fromisoformat(start_at) if start_at else None,
            end_at=datetime.fromisoformat(end_at) if end_at else None,
        )
        db.session.add(a)
        db.session.commit()
        flash("Announcement created")
        return redirect(url_for("attendance.announcements"))

    rows = Announcement.query.order_by(Announcement.id.desc()).all()

    stats = {
        "total": Announcement.query.count(),
        "active": Announcement.query.filter_by(is_active=True).count(),
        "inactive": Announcement.query.filter_by(is_active=False).count(),
        "warning": Announcement.query.filter_by(level="warning").count(),
        "danger": Announcement.query.filter_by(level="danger").count(),
    }

    return render_template(
        "attendance/announcements.html",
        rows=rows,
        stats=stats
    )


@attendance_bp.route("/announcements/<int:aid>/toggle", methods=["POST"])
@login_required
def announcement_toggle(aid):
    a = Announcement.query.get_or_404(aid)
    a.is_active = not a.is_active
    db.session.commit()
    flash("Announcement status updated")
    return redirect(url_for("attendance.announcements"))


@attendance_bp.route("/announcements/<int:aid>/delete", methods=["POST"])
@login_required
def announcement_delete(aid):
    a = Announcement.query.get_or_404(aid)
    db.session.delete(a)
    db.session.commit()
    flash("Announcement deleted")
    return redirect(url_for("attendance.announcements"))

# =========================================================
# MOBILE WEB (WRAPPER)
# =========================================================
@attendance_bp.route("/mobile")
def mobile():
    return render_template("attendance/mobile_app.html")

from werkzeug.security import check_password_hash
from flask_login import login_user, logout_user
from sqlalchemy import func

# =========================================================
# MOBILE API COMPATIBILITY
# =========================================================
@attendance_bp.route("/api/login", methods=["POST"])
def mobile_api_login():
    data = request.get_json(force=True, silent=True) or {}
    username = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    user = User.query.filter(func.lower(User.email) == username).first()

    if not user:
        return jsonify({"ok": False, "error": "User tidak ditemukan"}), 404

    if not user.is_active:
        return jsonify({"ok": False, "error": "Akun tidak aktif"}), 403

    if not user.check_password(password):
        return jsonify({"ok": False, "error": "Password salah"}), 401

    login_user(user)

    return jsonify({
        "ok": True,
        "user": {
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "role": user.role,
        }
    })


@attendance_bp.route("/api/logout", methods=["POST"])
@login_required
def mobile_api_logout():
    logout_user()
    return jsonify({"ok": True})


@attendance_bp.route("/api/me", methods=["GET"])
@login_required
def mobile_api_me():
    emp = Employee.query.filter_by(user_id=current_user.id).first()

    return jsonify({
        "ok": True,
        "user": {
            "id": current_user.id,
            "name": current_user.name,
            "email": current_user.email,
            "role": current_user.role,
        },
        "employee": {
            "id": emp.id,
            "code": emp.code,
            "name": emp.name,
            "dept": emp.dept,
            "phone": emp.phone,
            "address": emp.address,
            "birth_date": emp.birth_date.isoformat() if emp.birth_date else "",
            "ktp_number": emp.ktp_number,
        } if emp else None
    })


@attendance_bp.route("/api/attendance/check", methods=["POST"])
@login_required
def mobile_api_attendance_check():
    data = request.get_json(force=True, silent=True) or {}
    action = data.get("action")
    lat = data.get("lat")
    lon = data.get("lon")

    emp = Employee.query.filter_by(user_id=current_user.id).first()
    if not emp:
        return jsonify({"ok": False, "error": "Employee profile tidak ditemukan"}), 404

    today = date.today()

    rec = Attendance.query.filter_by(
        employee_id=emp.id,
        work_date=today
    ).first()

    if not rec:
        rec = Attendance(
            employee_id=emp.id,
            work_date=today,
            status="present"
        )
        db.session.add(rec)

    now = datetime.utcnow()

    if action == "check_in":
        rec.check_in = now
        rec.check_in_lat = lat
        rec.check_in_lon = lon
        rec.check_in_ip = request.remote_addr
        rec.check_in_ua = request.headers.get("User-Agent", "")[:255]

    elif action == "check_out":
        rec.check_out = now
        rec.check_out_lat = lat
        rec.check_out_lon = lon
        rec.check_out_ip = request.remote_addr
        rec.check_out_ua = request.headers.get("User-Agent", "")[:255]

    else:
        return jsonify({"ok": False, "error": "Invalid action"}), 400

    db.session.commit()

    return jsonify({
        "ok": True,
        "message": "Attendance updated",
        "time": now.isoformat() + "Z",
        "status": rec.status
    })


@attendance_bp.route("/api/attendance/my", methods=["GET"])
@login_required
def mobile_api_my_attendance():
    start = request.args.get("start")
    end = request.args.get("end")

    emp = Employee.query.filter_by(user_id=current_user.id).first()
    if not emp:
        return jsonify({"ok": False, "error": "Employee profile tidak ditemukan"}), 404

    query = Attendance.query.filter_by(employee_id=emp.id)

    if start:
        query = query.filter(Attendance.work_date >= start)
    if end:
        query = query.filter(Attendance.work_date <= end)

    records = query.order_by(Attendance.work_date.asc()).all()

    rows = []
    for r in records:
        rows.append({
            "id": r.id,
            "date": r.work_date.isoformat() if r.work_date else "",
            "check_in": r.check_in.isoformat() + "Z" if r.check_in else "",
            "check_out": r.check_out.isoformat() + "Z" if r.check_out else "",
            "status": r.status,
            "note": r.note,
        })

    return jsonify({"ok": True, "rows": rows})


@attendance_bp.route("/api/holidays", methods=["GET"])
@login_required
def mobile_api_holidays():
    return jsonify({"ok": True, "rows": []})


@attendance_bp.route("/api/leave/request", methods=["POST"])
@login_required
def mobile_api_leave_request():
    data = request.get_json(force=True, silent=True) or {}

    emp = Employee.query.filter_by(user_id=current_user.id).first()
    if not emp:
        return jsonify({"ok": False, "error": "Employee profile tidak ditemukan"}), 404

    r = LeaveRequest(
        employee_id=emp.id,
        type=data.get("type") or "leave",
        start_date=data.get("start_date"),
        end_date=data.get("end_date"),
        reason=data.get("reason"),
        status="pending_manager"
    )

    db.session.add(r)
    db.session.commit()

    return jsonify({"ok": True, "id": r.id})


@attendance_bp.route("/api/leave/my", methods=["GET"])
@login_required
def mobile_api_leave_my():
    emp = Employee.query.filter_by(user_id=current_user.id).first()
    if not emp:
        return jsonify({"ok": False, "error": "Employee profile tidak ditemukan"}), 404

    rows = LeaveRequest.query.filter_by(employee_id=emp.id).order_by(LeaveRequest.id.desc()).all()

    return jsonify({
        "ok": True,
        "rows": [
            {
                "id": r.id,
                "type": r.type,
                "start_date": r.start_date.isoformat() if r.start_date else "",
                "end_date": r.end_date.isoformat() if r.end_date else "",
                "reason": r.reason,
                "status": r.status,
            }
            for r in rows
        ]
    })


@attendance_bp.route("/api/leave/approvals", methods=["GET"])
@login_required
def mobile_api_leave_approvals():
    if current_user.role not in ["manager", "general_manager", "hrd", "admin"]:
        return jsonify({"ok": False, "error": "Unauthorized"}), 403

    rows = LeaveRequest.query.filter(
        LeaveRequest.status.in_(["pending", "pending_manager", "pending_hrd"])
    ).order_by(LeaveRequest.id.desc()).all()

    return jsonify({
        "ok": True,
        "rows": [
            {
                "id": r.id,
                "employee_name": r.employee.name if r.employee else "",
                "dept": r.employee.dept if r.employee else "",
                "type": r.type,
                "start_date": r.start_date.isoformat() if r.start_date else "",
                "end_date": r.end_date.isoformat() if r.end_date else "",
                "reason": r.reason,
                "status": r.status,
            }
            for r in rows
        ]
    })


@attendance_bp.route("/api/leave/<int:rid>/approve", methods=["POST"])
@login_required
def mobile_api_leave_approve(rid):
    if current_user.role not in ["manager", "general_manager", "hrd", "admin"]:
        return jsonify({"ok": False, "error": "Unauthorized"}), 403

    r = LeaveRequest.query.get_or_404(rid)
    r.status = "approved"
    r.hrd_approved_by = current_user.id
    r.hrd_approved_at = datetime.utcnow()
    db.session.commit()

    return jsonify({"ok": True})


@attendance_bp.route("/api/leave/<int:rid>/reject", methods=["POST"])
@login_required
def mobile_api_leave_reject(rid):
    if current_user.role not in ["manager", "general_manager", "hrd", "admin"]:
        return jsonify({"ok": False, "error": "Unauthorized"}), 403

    r = LeaveRequest.query.get_or_404(rid)
    r.status = "rejected"
    r.rejected_by = current_user.id
    r.rejected_at = datetime.utcnow()
    db.session.commit()

    return jsonify({"ok": True})


@attendance_bp.route("/api/profile/change_password", methods=["POST"])
@login_required
def mobile_api_change_password():
    data = request.get_json(force=True, silent=True) or {}
    old_password = data.get("old_password") or ""
    new_password = data.get("new_password") or ""

    if not current_user.check_password(old_password):
        return jsonify({"ok": False, "error": "Old password salah"}), 401

    if len(new_password) < 6:
        return jsonify({"ok": False, "error": "Password baru minimal 6 karakter"}), 400

    current_user.set_password(new_password)
    db.session.commit()

    return jsonify({"ok": True})


@attendance_bp.route("/api/profile/update", methods=["POST"])
@login_required
def mobile_api_profile_update():
    data = request.get_json(force=True, silent=True) or {}

    emp = Employee.query.filter_by(user_id=current_user.id).first()
    if not emp:
        return jsonify({"ok": False, "error": "Employee profile tidak ditemukan"}), 404

    emp.phone = data.get("phone") or None
    emp.address = data.get("address") or None
    emp.ktp_number = data.get("ktp_number") or None

    birth_date = data.get("birth_date")
    emp.birth_date = date.fromisoformat(birth_date) if birth_date else None

    db.session.commit()

    return jsonify({"ok": True})


@attendance_bp.route("/api/announcements/active", methods=["GET"])
@login_required
def mobile_api_announcements_active():
    now = datetime.utcnow()

    rows = Announcement.query.filter_by(is_active=True).order_by(Announcement.id.desc()).all()

    data = []
    for a in rows:
        if a.start_at and a.start_at > now:
            continue
        if a.end_at and a.end_at < now:
            continue

        data.append({
            "id": a.id,
            "title": a.title,
            "body": a.body,
            "level": a.level,
            "created_at": a.created_at.isoformat() + "Z" if a.created_at else "",
        })

    return jsonify({"ok": True, "rows": data})

def _simple_face_embedding_from_data_url(data_url):
    """
    Temporary lightweight embedding.
    Ini belum face recognition OpenCV/LBPH beneran,
    tapi sudah menyimpan signature numerik ke Employee.face_embedding.
    """
    if not data_url or "," not in data_url:
        return None

    raw = base64.b64decode(data_url.split(",", 1)[1])

    arr = np.frombuffer(raw, dtype=np.uint8)

    if arr.size == 0:
        return None

    # Buat embedding fixed-size 128 float32 dari byte image.
    bins = np.array_split(arr, 128)
    emb = np.array([b.mean() if b.size else 0 for b in bins], dtype=np.float32)

    norm = np.linalg.norm(emb)
    if norm > 0:
        emb = emb / norm

    return emb

@attendance_bp.route("/api/face/enroll", methods=["POST"])
@login_required
def mobile_api_face_enroll():
    data = request.get_json(force=True, silent=True) or {}
    image = data.get("image")

    emp = Employee.query.filter_by(user_id=current_user.id).first()
    if not emp:
        return jsonify({"ok": False, "error": "Employee profile tidak ditemukan"}), 404

    emb = _simple_face_embedding_from_data_url(image)
    if emb is None:
        return jsonify({"ok": False, "error": "Face image invalid"}), 400

    emp.face_embedding = emb.tobytes()
    emp.face_updated_at = datetime.utcnow()
    db.session.commit()

    return jsonify({
        "ok": True,
        "message": "Face enrolled"
    })


@attendance_bp.route("/api/face/verify", methods=["POST"])
@login_required
def mobile_api_face_verify():
    data = request.get_json(force=True, silent=True) or {}
    image = data.get("image")
    action = data.get("action")

    if action not in ["check_in", "check_out"]:
        return jsonify({"ok": False, "error": "Invalid action"}), 400

    emp = Employee.query.filter_by(user_id=current_user.id).first()
    if not emp:
        return jsonify({"ok": False, "error": "Employee profile tidak ditemukan"}), 404

    if not emp.face_embedding:
        return jsonify({
            "ok": False,
            "error": "Face belum dienroll. Buka Profile > Enroll Face dulu."
        }), 400

    current_emb = _simple_face_embedding_from_data_url(image)
    if current_emb is None:
        return jsonify({"ok": False, "error": "Face image invalid"}), 400

    stored_emb = np.frombuffer(emp.face_embedding, dtype=np.float32)

    if stored_emb.size != current_emb.size:
        return jsonify({"ok": False, "error": "Face embedding mismatch. Silakan enroll ulang."}), 400

    distance = float(np.linalg.norm(stored_emb - current_emb))

    # Threshold ini untuk lightweight embedding, perlu tuning.
    threshold = 0.35

    if distance > threshold:
        return jsonify({
            "ok": False,
            "error": f"Face tidak cocok. Distance={distance:.3f}"
        }), 401

    return jsonify({
        "ok": True,
        "token": f"face-ok-{current_user.id}-{int(datetime.utcnow().timestamp())}",
        "distance": distance,
        "message": "Face verified"
    })