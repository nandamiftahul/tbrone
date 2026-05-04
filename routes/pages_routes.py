from flask import Blueprint, render_template, redirect, url_for
from flask_login import login_required
from routes.auth_utils import role_required

pages_bp = Blueprint(
    "pages",
    __name__,
    template_folder="templates",
)

@pages_bp.route("/")
@login_required
def index():
    return render_template("index.html")

@pages_bp.route("/underdev")
def underdev_page():
    return render_template("underdev.html")

@pages_bp.route("/attendance")
@role_required("admin", "hrd")
def attendance_page():
    return render_template("main/attendance.html")

@pages_bp.route("/inventory")
@role_required("admin", "hrd")
def inventory_page():
    return render_template("main/inventory.html")

@pages_bp.route("/finance")
@login_required
def finance_page():
    return render_template("main/finance.html")

@pages_bp.route("/job")
@login_required
def job_page():
    return render_template("main/job.html")

@pages_bp.route("/project")
@login_required
def project_page():
    return render_template("main/project.html")

@pages_bp.route("/wiki")
@login_required
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
