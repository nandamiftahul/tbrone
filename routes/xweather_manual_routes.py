from flask import Blueprint, render_template, make_response, request

xweather_manual_bp = Blueprint("xweather_manual", __name__)

@xweather_manual_bp.route("/xweather/manual")
def xweather_manual_page():
    return render_template("wiki/xweather_manual.html")

@xweather_manual_bp.route("/xweather/manual.pdf")
def xweather_manual_pdf():
    try:
        from weasyprint import HTML  # 🔥 lazy import
    except Exception as e:
        return (
            "PDF generation is not available on this server.<br>"
            "Please use the 'Print / Save as PDF' button in the browser.",
            503,
        )

    html = render_template("wiki/xweather_manual.html")
    base_url = request.url_root
    pdf_bytes = HTML(string=html, base_url=base_url).write_pdf()

    resp = make_response(pdf_bytes)
    resp.headers["Content-Type"] = "application/pdf"
    resp.headers["Content-Disposition"] = (
        "attachment; filename=Xweather_Protect_User_Manual.pdf"
    )
    return resp
