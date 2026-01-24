from flask import Flask, redirect, url_for
from xweather_manual_routes import xweather_manual_bp


def create_app():
    app = Flask(__name__)

    app.config["SECRET_KEY"] = "terrindo-xweather-manual"
    app.config["TEMPLATES_AUTO_RELOAD"] = True

    app.register_blueprint(xweather_manual_bp)

    @app.route("/")
    def index():
        return redirect(url_for("xweather_manual.xweather_manual_page"))

    @app.route("/xweather/manual.pdf", endpoint="xweather_manual_pdf")
    def xweather_manual_pdf_alias():
        return redirect(url_for("xweather_manual.xweather_manual_pdf"))

    return app
