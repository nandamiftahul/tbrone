from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from datetime import datetime, date
from routes.models import db, Employee, Attendance, LeaveRequest, Office, Announcement, Shift
from flask_login import current_user

attendance_bp = Blueprint("attendance", __name__, url_prefix="/attendance")


# =========================================================
# DASHBOARD
# =========================================================
@attendance_bp.route("/")
def dashboard():
    today = date.today()

    records = Attendance.query.filter_by(work_date=today).all()

    stats = {
        "total": len(records),
        "present": len([r for r in records if r.status in ["present", "late"]]),
        "leave": len([r for r in records if r.status == "leave"]),
        "sick": len([r for r in records if r.status == "sick"]),
        "wfh": len([r for r in records if r.status == "wfh"]),
        "absent": len([r for r in records if r.status == "absent"]),
    }

    return render_template(
        "attendance/dashboard.html",
        today=today,
        stats=stats
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
def attendance_list():
    start = request.args.get("start")
    end = request.args.get("end")
    emp = request.args.get("employee")

    query = Attendance.query

    if start:
        query = query.filter(Attendance.work_date >= start)
    if end:
        query = query.filter(Attendance.work_date <= end)
    if emp:
        query = query.join(Employee).filter(Employee.name.ilike(f"%{emp}%"))

    rows = query.order_by(Attendance.work_date.desc()).all()

    return render_template(
        "attendance/list.html",
        rows=rows,
        start=start,
        end=end,
        emp=emp
    )


# =========================================================
# LEAVE REQUEST
# =========================================================
@attendance_bp.route("/leave", methods=["GET", "POST"])
def leave_request():
    if request.method == "POST":
        r = LeaveRequest(
            employee_id=request.form.get("employee_id"),
            type=request.form.get("type"),
            start_date=request.form.get("start_date"),
            end_date=request.form.get("end_date"),
            reason=request.form.get("reason"),
            status="pending"
        )
        db.session.add(r)
        db.session.commit()

        flash("Request submitted")
        return redirect(url_for("attendance.leave_request"))

    employees = Employee.query.all()
    return render_template("attendance/leave.html", employees=employees)


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
def announcements():
    if request.method == "POST":
        a = Announcement(
            title=request.form.get("title"),
            body=request.form.get("body"),
            level=request.form.get("level"),
            is_active=True
        )
        db.session.add(a)
        db.session.commit()
        return redirect(url_for("attendance.announcements"))

    rows = Announcement.query.order_by(Announcement.id.desc()).all()
    return render_template("attendance/announcements.html", rows=rows)


@attendance_bp.route("/announcements/<int:aid>/delete", methods=["POST"])
def announcement_delete(aid):
    a = Announcement.query.get_or_404(aid)
    db.session.delete(a)
    db.session.commit()
    return redirect(url_for("attendance.announcements"))


# =========================================================
# MOBILE WEB (WRAPPER)
# =========================================================
@attendance_bp.route("/mobile")
def mobile():
    return render_template("attendance/mobile_app.html")