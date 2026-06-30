from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_user, logout_user, login_required, current_user

from sqlalchemy.exc import IntegrityError
from sqlalchemy import func, or_

from routes.attendance_models import Employee, User, VALID_ROLES, db
from routes.auth_utils import PAGE_ACCESS, accessible_page_labels, role_required

auth_bp = Blueprint("auth", __name__)


# =========================================================
# LOGIN
# =========================================================
@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("pages.index"))

    if request.method == "POST":
        username = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""

        # DEBUG (optional, bisa dihapus nanti)
        print("LOGIN TRY:", username)

        # cari berdasarkan:
        # - email (yang kamu pakai sebagai username)
        # - name (opsional fallback)
        user = User.query.filter(
            or_(
                func.lower(User.email) == username,
                func.lower(User.name) == username
            )
        ).first()

        if not user:
            flash("User tidak ditemukan.", "error")
            return redirect(url_for("auth.login"))

        if not user.is_active:
            flash("Akun tidak aktif.", "error")
            return redirect(url_for("auth.login"))

        if not user.check_password(password):
            flash("Password salah.", "error")
            return redirect(url_for("auth.login"))

        login_user(user)

        print("LOGIN SUCCESS:", user.email)

        next_url = request.args.get("next")
        return redirect(next_url or url_for("pages.index"))

    return render_template("auth/login.html")


# =========================================================
# LOGOUT
# =========================================================
@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))


@auth_bp.route("/settings/users")
@login_required
@role_required("admin")
def user_settings():
    q = (request.args.get("q") or "").strip()
    query = User.query
    if q:
        like = f"%{q.lower()}%"
        query = query.filter(
            or_(
                func.lower(User.name).like(like),
                func.lower(User.email).like(like),
                func.lower(User.role).like(like),
            )
        )

    users = query.order_by(User.is_active.desc(), User.name.asc()).all()
    employee_by_user = {
        emp.user_id: emp
        for emp in Employee.query.filter(Employee.user_id.isnot(None)).all()
    }
    return render_template(
        "settings/users.html",
        users=users,
        employee_by_user=employee_by_user,
        roles=VALID_ROLES,
        page_access=PAGE_ACCESS,
        role_access_map={role: accessible_page_labels(role) for role in VALID_ROLES},
        q=q,
    )


@auth_bp.route("/settings/users/create", methods=["POST"])
@login_required
@role_required("admin")
def user_settings_create():
    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    role = (request.form.get("role") or "staff").strip()
    password = request.form.get("password") or ""
    is_active = bool(request.form.get("is_active"))

    if role not in VALID_ROLES:
        flash("Role tidak valid.", "error")
        return redirect(url_for("auth.user_settings"))
    if not name or not email or not password:
        flash("Name, username/email, dan password wajib diisi.", "error")
        return redirect(url_for("auth.user_settings"))

    user = User(name=name, email=email, role=role, is_active=is_active)
    user.set_password(password)
    db.session.add(user)
    try:
        db.session.commit()
        flash("User baru berhasil dibuat.", "success")
    except IntegrityError:
        db.session.rollback()
        flash("Username/email sudah dipakai user lain.", "error")
    return redirect(url_for("auth.user_settings"))


@auth_bp.route("/settings/users/<int:user_id>/update", methods=["POST"])
@login_required
@role_required("admin")
def user_settings_update(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash("User tidak ditemukan.", "error")
        return redirect(url_for("auth.user_settings"))

    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    role = (request.form.get("role") or "staff").strip()
    new_password = request.form.get("new_password") or ""

    if role not in VALID_ROLES:
        flash("Role tidak valid.", "error")
        return redirect(url_for("auth.user_settings"))
    if not name or not email:
        flash("Name dan username/email wajib diisi.", "error")
        return redirect(url_for("auth.user_settings"))

    if user.id == current_user.id:
        is_active = True
        role = "admin"
    else:
        is_active = bool(request.form.get("is_active"))

    user.name = name
    user.email = email
    user.role = role
    user.is_active = is_active

    if new_password:
        user.set_password(new_password)

    emp = Employee.query.filter_by(user_id=user.id).first()
    if emp:
        emp.name = name
        emp.email = email
        emp.role = role
        emp.is_active = is_active

    try:
        db.session.commit()
        flash("User berhasil diperbarui.", "success")
    except IntegrityError:
        db.session.rollback()
        flash("Username/email sudah dipakai user lain.", "error")
    return redirect(url_for("auth.user_settings"))
