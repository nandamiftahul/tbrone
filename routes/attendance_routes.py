from flask import Blueprint, abort, render_template, request, redirect, url_for, flash, jsonify, Response, send_file, session
from datetime import datetime, date, timedelta
from flask_login import current_user, login_required
from routes.attendance_models import db, Employee, Attendance, LeaveRequest, Office, Announcement, Shift, User, Holiday, EMPLOYEE_ROLE_CHOICES, EMPLOYEE_ROLE_LABELS
from routes.auth_utils import can_access
import csv
import io
import json
import secrets
from openpyxl import Workbook
import base64
import numpy as np
from sqlalchemy.exc import IntegrityError

attendance_bp = Blueprint("attendance", __name__, url_prefix="/attendance")

ATTENDANCE_ADMIN_ROLES = {"admin", "hrd"}
ATTENDANCE_APPROVER_ROLES = {
    "admin",
    "hrd",
    "supervisor",
    "assistant_manager",
    "manager",
    "general_manager",
    "director",
    "ceo",
}
MOBILE_FACE_TOKEN_MAX_AGE_SECONDS = 120
LEAVE_TYPES = {"leave", "sick", "wfh", "on_site", "late_work", "early_finish"}


@attendance_bp.before_request
def require_attendance_page_access():
    if request.endpoint in {"attendance.mobile", "attendance.mobile_api_login"}:
        return None
    if (request.endpoint or "").startswith("attendance.mobile_api_") and not current_user.is_authenticated:
        return jsonify({"ok": False, "error": "Session expired. Silakan login ulang."}), 401
    if request.endpoint == "attendance.mobile_api_logout":
        return None
    if current_user.is_authenticated and not can_access(current_user, "page", "attendance"):
        if (request.endpoint or "").startswith("attendance.mobile_api_"):
            return jsonify({"ok": False, "error": "User tidak punya akses Attendance"}), 403
        abort(403)
    return None


def _role_allowed(roles):
    return current_user.is_authenticated and current_user.role in roles


def _require_roles(roles):
    if not _role_allowed(roles):
        abort(403)


def _current_employee():
    if not current_user.is_authenticated:
        return None
    return Employee.query.filter_by(user_id=current_user.id).first()


def _parse_iso_date(value, field_name):
    try:
        return date.fromisoformat(str(value or ""))
    except ValueError:
        raise ValueError(f"{field_name} tidak valid.")


def _issue_face_token(action):
    token = secrets.token_urlsafe(24)
    session["attendance_face_token"] = {
        "token": token,
        "action": action,
        "expires_at": (datetime.utcnow() + timedelta(seconds=MOBILE_FACE_TOKEN_MAX_AGE_SECONDS)).isoformat(),
    }
    return token


def _consume_face_token(token, action):
    payload = session.pop("attendance_face_token", None)
    if not payload:
        return False
    try:
        expires_at = datetime.fromisoformat(payload.get("expires_at", ""))
    except ValueError:
        return False
    return (
        secrets.compare_digest(str(payload.get("token", "")), str(token or ""))
        and payload.get("action") == action
        and expires_at >= datetime.utcnow()
    )


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
        pending_leave=pending_leave,
        approver_roles=ATTENDANCE_APPROVER_ROLES,
        admin_roles=ATTENDANCE_ADMIN_ROLES,
    )

# =========================================================
# CHECK IN / OUT
# =========================================================
@attendance_bp.route("/check", methods=["GET", "POST"])
@login_required
def attendance_check():
    if request.method == "POST":
        if _role_allowed(ATTENDANCE_ADMIN_ROLES):
            emp_id = request.form.get("employee_id")
        else:
            emp = _current_employee()
            if not emp:
                flash("Employee profile tidak ditemukan.", "error")
                return redirect(url_for("attendance.attendance_check"))
            emp_id = emp.id
        action = request.form.get("action")
        lat = request.form.get("lat")
        lon = request.form.get("lon")
        if action not in {"check_in", "check_out"}:
            flash("Action attendance tidak valid.", "error")
            return redirect(url_for("attendance.attendance_check"))
        if not Employee.query.filter_by(id=emp_id, is_active=True).first():
            flash("Employee tidak valid atau tidak aktif.", "error")
            return redirect(url_for("attendance.attendance_check"))

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

    if _role_allowed(ATTENDANCE_ADMIN_ROLES):
        employees = Employee.query.filter_by(is_active=True).all()
    else:
        emp = _current_employee()
        employees = [emp] if emp and emp.is_active else []
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
    if not _role_allowed(ATTENDANCE_ADMIN_ROLES | ATTENDANCE_APPROVER_ROLES):
        current_emp = _current_employee()
        if current_emp:
            query = query.filter(Attendance.employee_id == current_emp.id)
        else:
            query = query.filter(False)

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
    if not _role_allowed(ATTENDANCE_ADMIN_ROLES | ATTENDANCE_APPROVER_ROLES):
        current_emp = _current_employee()
        if current_emp:
            query = query.filter(Attendance.employee_id == current_emp.id)
        else:
            query = query.filter(False)

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
    _require_roles(ATTENDANCE_ADMIN_ROLES)
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
        emp = _current_employee()
        if _role_allowed(ATTENDANCE_ADMIN_ROLES):
            employee_id = request.form.get("employee_id")
        elif emp:
            employee_id = emp.id
        else:
            flash("Employee profile tidak ditemukan.", "error")
            return redirect(url_for("attendance.leave_request"))

        r = LeaveRequest(
            employee_id=employee_id,
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

    if _role_allowed(ATTENDANCE_ADMIN_ROLES):
        employees = Employee.query.filter_by(is_active=True).order_by(Employee.name.asc()).all()
        leave_query = LeaveRequest.query
    else:
        emp = _current_employee()
        employees = [emp] if emp and emp.is_active else []
        leave_query = LeaveRequest.query.filter_by(employee_id=emp.id) if emp else LeaveRequest.query.filter(False)
    rows = leave_query.order_by(LeaveRequest.id.desc()).limit(50).all()

    stats = {
        "total": leave_query.count(),
        "pending": leave_query.filter(
            LeaveRequest.status.in_(["pending", "pending_manager", "pending_hrd"])
        ).count(),
        "approved": leave_query.filter_by(status="approved").count(),
        "rejected": leave_query.filter_by(status="rejected").count(),
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
@login_required
def approvals():
    _require_roles(ATTENDANCE_APPROVER_ROLES)
    rows = LeaveRequest.query.order_by(LeaveRequest.id.desc()).all()
    return render_template("attendance/approvals.html", rows=rows)


@attendance_bp.route("/approve/<int:rid>", methods=["POST"])
@login_required
def approve(rid):
    _require_roles(ATTENDANCE_APPROVER_ROLES)
    r = LeaveRequest.query.get_or_404(rid)
    r.status = "approved"
    r.hrd_approved_by = current_user.id
    r.hrd_approved_at = datetime.utcnow()
    db.session.commit()
    return redirect(url_for("attendance.approvals"))


@attendance_bp.route("/reject/<int:rid>", methods=["POST"])
@login_required
def reject(rid):
    _require_roles(ATTENDANCE_APPROVER_ROLES)
    r = LeaveRequest.query.get_or_404(rid)
    r.status = "rejected"
    r.rejected_by = current_user.id
    r.rejected_at = datetime.utcnow()
    db.session.commit()
    return redirect(url_for("attendance.approvals"))


# =========================================================
# EMPLOYEES
# =========================================================
@attendance_bp.route("/employees")
@login_required
def employees():
    _require_roles(ATTENDANCE_ADMIN_ROLES)
    rows = Employee.query.all()
    shifts = Shift.query.all()
    return render_template(
        "attendance/employees.html",
        rows=rows,
        shifts=shifts,
        employee_role_choices=EMPLOYEE_ROLE_CHOICES,
        employee_role_labels=EMPLOYEE_ROLE_LABELS,
    )


@attendance_bp.route("/employees/create", methods=["POST"])
@login_required
def employee_create():
    _require_roles(ATTENDANCE_ADMIN_ROLES)
    role = request.form.get("role") or "staff"
    if role not in EMPLOYEE_ROLE_LABELS:
        flash("Role tidak valid.", "error")
        return redirect(url_for("attendance.employees"))

    e = Employee(
        code=request.form.get("code"),
        name=request.form.get("name"),
        email=request.form.get("email"),
        dept=request.form.get("dept"),
        role=role,
        shift_id=request.form.get("shift_id") or None
    )
    db.session.add(e)
    if request.form.get("create_login") == "1":
        login_username = (request.form.get("login_email") or "").strip().lower()
        login_password = request.form.get("login_password") or ""
        if not login_username or not login_password:
            db.session.rollback()
            flash("Login email dan initial password wajib diisi saat create login.", "error")
            return redirect(url_for("attendance.employees"))
        u = User(
            name=e.name,
            email=login_username,
            role=e.role,
            is_active=True,
        )
        u.set_password(login_password)
        db.session.add(u)
        db.session.flush()
        e.user_id = u.id

    try:
        db.session.commit()
        flash("Employee created")
    except IntegrityError:
        db.session.rollback()
        flash("Code, employee email, atau login email sudah dipakai.", "error")
    return redirect(url_for("attendance.employees"))

@attendance_bp.route("/employees/<int:emp_id>/update", methods=["POST"])
@login_required
def employee_update(emp_id):
    _require_roles(ATTENDANCE_ADMIN_ROLES)
    e = Employee.query.get_or_404(emp_id)
    role = request.form.get("role") or "staff"
    if role not in EMPLOYEE_ROLE_LABELS:
        flash("Role tidak valid.", "error")
        return redirect(url_for("attendance.employees"))

    e.code = request.form.get("code")
    e.name = request.form.get("name")
    e.email = request.form.get("email") or None
    e.dept = request.form.get("dept") or None
    e.role = role
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

    try:
        db.session.commit()
        flash("Employee and login account updated")
    except IntegrityError:
        db.session.rollback()
        flash("Code, employee email, atau login email sudah dipakai.", "error")
    return redirect(url_for("attendance.employees"))

@attendance_bp.route("/employees/<int:emp_id>/delete", methods=["POST"])
@login_required
def employee_delete(emp_id):
    _require_roles(ATTENDANCE_ADMIN_ROLES)
    e = Employee.query.get_or_404(emp_id)
    db.session.delete(e)
    db.session.commit()
    return redirect(url_for("attendance.employees"))


# =========================================================
# OFFICE (GEOFENCE)
# =========================================================
@attendance_bp.route("/offices", methods=["GET", "POST"])
@login_required
def offices():
    _require_roles(ATTENDANCE_ADMIN_ROLES)
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
            if active:
                active.is_active = True
            else:
                flash("Office tidak ditemukan.", "error")
                return redirect(url_for("attendance.offices"))

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
    _require_roles(ATTENDANCE_ADMIN_ROLES)
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
    _require_roles(ATTENDANCE_ADMIN_ROLES)
    a = Announcement.query.get_or_404(aid)
    a.is_active = not a.is_active
    db.session.commit()
    flash("Announcement status updated")
    return redirect(url_for("attendance.announcements"))


@attendance_bp.route("/announcements/<int:aid>/delete", methods=["POST"])
@login_required
def announcement_delete(aid):
    _require_roles(ATTENDANCE_ADMIN_ROLES)
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
    if not can_access(user, "page", "attendance"):
        logout_user()
        return jsonify({"ok": False, "error": "User tidak punya akses Attendance"}), 403

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
    session.pop("attendance_face_token", None)
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
    face_token = data.get("face_token")

    if action not in ["check_in", "check_out"]:
        return jsonify({"ok": False, "error": "Invalid action"}), 400

    if not _consume_face_token(face_token, action):
        return jsonify({"ok": False, "error": "Face verification expired atau tidak valid. Silakan ulangi verifikasi wajah."}), 403

    emp = Employee.query.filter_by(user_id=current_user.id).first()
    if not emp:
        return jsonify({"ok": False, "error": "Employee profile tidak ditemukan"}), 404

    today = date.today()

    rec = Attendance.query.filter_by(
        employee_id=emp.id,
        work_date=today
    ).first()

    if not rec and action == "check_out":
        return jsonify({"ok": False, "error": "Check In harus dilakukan sebelum Check Out."}), 400

    if not rec:
        rec = Attendance(
            employee_id=emp.id,
            work_date=today,
            status="present"
        )
        db.session.add(rec)

    now = datetime.utcnow()

    if action == "check_in":
        if rec.check_in:
            return jsonify({"ok": False, "error": "Check In hari ini sudah tercatat."}), 409
        rec.check_in = now
        rec.check_in_lat = lat
        rec.check_in_lon = lon
        rec.check_in_ip = request.remote_addr
        rec.check_in_ua = request.headers.get("User-Agent", "")[:255]

    elif action == "check_out":
        if not rec.check_in:
            return jsonify({"ok": False, "error": "Check In harus dilakukan sebelum Check Out."}), 400
        if rec.check_out:
            return jsonify({"ok": False, "error": "Check Out hari ini sudah tercatat."}), 409
        rec.check_out = now
        rec.check_out_lat = lat
        rec.check_out_lon = lon
        rec.check_out_ip = request.remote_addr
        rec.check_out_ua = request.headers.get("User-Agent", "")[:255]

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
    start = request.args.get("start")
    end = request.args.get("end")
    query = Holiday.query
    if start:
        try:
            query = query.filter(Holiday.date >= _parse_iso_date(start, "Start date"))
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
    if end:
        try:
            query = query.filter(Holiday.date <= _parse_iso_date(end, "End date"))
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    rows = query.order_by(Holiday.date.asc()).all()
    return jsonify({
        "ok": True,
        "rows": [
            {
                "id": h.id,
                "date": h.date.isoformat() if h.date else "",
                "name": h.name,
            }
            for h in rows
        ],
    })


@attendance_bp.route("/api/leave/request", methods=["POST"])
@login_required
def mobile_api_leave_request():
    data = request.get_json(force=True, silent=True) or {}
    leave_type = (data.get("type") or "leave").strip()
    if leave_type not in LEAVE_TYPES:
        return jsonify({"ok": False, "error": "Tipe leave tidak valid."}), 400

    try:
        start_date = _parse_iso_date(data.get("start_date"), "Start date")
        end_date = _parse_iso_date(data.get("end_date"), "End date")
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    if end_date < start_date:
        return jsonify({"ok": False, "error": "End date harus >= start date."}), 400

    emp = Employee.query.filter_by(user_id=current_user.id).first()
    if not emp:
        return jsonify({"ok": False, "error": "Employee profile tidak ditemukan"}), 404

    r = LeaveRequest(
        employee_id=emp.id,
        type=leave_type,
        start_date=start_date,
        end_date=end_date,
        reason=(data.get("reason") or "").strip() or None,
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
    if current_user.role not in ATTENDANCE_APPROVER_ROLES:
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
    if current_user.role not in ATTENDANCE_APPROVER_ROLES:
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
    if current_user.role not in ATTENDANCE_APPROVER_ROLES:
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
    try:
        emp.birth_date = _parse_iso_date(birth_date, "Tanggal lahir") if birth_date else None
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

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

    try:
        stored_bytes = bytes(emp.face_embedding)
    
        if len(stored_bytes) % np.dtype(np.float32).itemsize != 0:
            emp.face_embedding = None
            emp.face_updated_at = None
            db.session.commit()
            return jsonify({
                "ok": False,
                "error": "Data face lama tidak valid. Silakan enroll ulang di Profile > Enroll Face."
            }), 400
    
        stored_emb = np.frombuffer(stored_bytes, dtype=np.float32)
    
    except Exception:
        return jsonify({
            "ok": False,
            "error": "Gagal membaca face embedding. Silakan enroll ulang."
        }), 400
    
    if stored_emb.size != current_emb.size:
        emp.face_embedding = None
        emp.face_updated_at = None
        db.session.commit()
        return jsonify({
            "ok": False,
            "error": "Format face embedding lama tidak cocok. Silakan enroll ulang."
        }), 400

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
        "token": _issue_face_token(action),
        "expires_in": MOBILE_FACE_TOKEN_MAX_AGE_SECONDS,
        "distance": distance,
        "message": "Face verified"
    })
