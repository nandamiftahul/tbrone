from flask import Blueprint, render_template, redirect, url_for
from flask_login import current_user, login_required
from routes.auth_utils import PAGE_ACCESS, accessible_page_keys, accessible_project_keys, role_required

pages_bp = Blueprint(
    "pages",
    __name__,
    template_folder="templates",
)

@pages_bp.route("/")
@login_required
def index():
    return render_template("index.html", accessible_pages=accessible_page_keys(current_user.role))

@pages_bp.route("/underdev")
def underdev_page():
    return render_template("underdev.html")

@pages_bp.route("/attendance")
@role_required(*PAGE_ACCESS["attendance"]["roles"])
def attendance_page():
    return render_template("main/attendance.html")

@pages_bp.route("/inventory")
@role_required(*PAGE_ACCESS["inventory"]["roles"])
def inventory_page():
    return render_template("main/inventory.html")

@pages_bp.route("/finance")
@role_required(*PAGE_ACCESS["finance"]["roles"])
def finance_page():
    return render_template("main/finance.html")

@pages_bp.route("/job")
@role_required(*PAGE_ACCESS["job"]["roles"])
def job_page():
    return render_template("main/job.html")

@pages_bp.route("/project")
@role_required(*PAGE_ACCESS["project"]["roles"])
def project_page():
    return render_template("main/project.html", accessible_project_pages=accessible_project_keys(current_user.role))

@pages_bp.route("/wiki")
@role_required(*PAGE_ACCESS["wiki"]["roles"])
def wiki_page():
    return render_template("main/wiki.html")

# Optional aliases, supaya link lama / cepat tidak putus
@pages_bp.route("/terrindo-one")
@login_required
def terrindo_one_alias():
    return redirect(url_for("pages.index"))

@pages_bp.route("/terrindo-one/project")
@login_required
def terrindo_one_project_alias():
    return redirect(url_for("pages.project_page"))

@pages_bp.route("/terrindo-one/wiki")
@login_required
def terrindo_one_wiki_alias():
    return redirect(url_for("pages.wiki_page"))
