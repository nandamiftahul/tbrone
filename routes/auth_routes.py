from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_user, logout_user, login_required, current_user

from sqlalchemy import func, or_

from routes.attendance_models import User

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