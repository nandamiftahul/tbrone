from flask import Blueprint, render_template, make_response, request

twincityjakarta_manual_bp = Blueprint("twincityjakarta_manual", __name__)

@twincityjakarta_manual_bp.route("/twincityjakarta/manual")
def twincityjakarta_manual_page():
    return render_template("twincityjakarta_manual.html")

@twincityjakarta_manual_bp.route("/twincityjakarta/manual.pdf")
def twincityjakarta_manual_pdf():
    try:
        from weasyprint import HTML  # lazy import
    except Exception:
        return (
            "PDF generation is not available on this server.<br>"
            "Please use the 'Print / Save as PDF' button in the browser.",
            503,
        )

    html = render_template("twincityjakarta_manual.html")
    base_url = request.url_root
    pdf_bytes = HTML(string=html, base_url=base_url).write_pdf()

    resp = make_response(pdf_bytes)
    resp.headers["Content-Type"] = "application/pdf"
    resp.headers["Content-Disposition"] = (
        "attachment; filename=Jakarta_Air_Quality_Digital_Twin_Manual.pdf"
    )
    return resp
