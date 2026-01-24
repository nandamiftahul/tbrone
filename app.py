from flask import Flask, redirect, url_for
from xweather_manual_routes import xweather_manual_bp

def create_app():
    app = Flask(__name__)

    app.config["SECRET_KEY"] = "terrindo-xweather-manual"
    app.config["TEMPLATES_AUTO_RELOAD"] = True

    # Register blueprint
    app.register_blueprint(xweather_manual_bp)

    # Root redirect
    @app.route("/")
    def index():
        return redirect(url_for("xweather_manual.xweather_manual_page"))

    # 🔥 ALIAS ENDPOINT (FIX BuildError)
    @app.route("/xweather/manual.pdf", endpoint="xweather_manual_pdf")
    def xweather_manual_pdf_alias():
        return redirect(url_for("xweather_manual.xweather_manual_pdf"))

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True
    )
