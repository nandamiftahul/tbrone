# xweather_manual_routes.py
# Drop-in routes for your Flask app (app.py).
# Requirements (recommended): pip install weasyprint

from flask import Blueprint, render_template, make_response, request
from weasyprint import HTML

xweather_manual_bp = Blueprint("xweather_manual", __name__)

@xweather_manual_bp.route("/xweather/manual")
def xweather_manual_page():
    return render_template("xweather_manual.html")

@xweather_manual_bp.route("/xweather/manual.pdf")
def xweather_manual_pdf():
    # Render the same HTML template to PDF
    html = render_template("xweather_manual.html")
    base_url = request.url_root  # allows relative assets via url_for + static
    pdf_bytes = HTML(string=html, base_url=base_url).write_pdf()

    resp = make_response(pdf_bytes)
    resp.headers["Content-Type"] = "application/pdf"
    resp.headers["Content-Disposition"] = "attachment; filename=Xweather_Protect_User_Manual.pdf"
    return resp

