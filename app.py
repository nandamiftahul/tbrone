from flask import Flask, redirect, url_for
from xweather_manual_routes import xweather_manual_bp
from twincityjakarta_manual_routes import twincityjakarta_manual_bp
#from xweather_report_routes import xweather_report_bp
from xweather_monthly_report_sqlite_routes import xweather_report_bp
from xweather_mapgolf_routes import xweather_mapgolf_bp


def create_app():
    app = Flask(__name__)

    app.config["SECRET_KEY"] = "terrindo-xweather-manual"
    app.config["TEMPLATES_AUTO_RELOAD"] = True

    app.register_blueprint(xweather_manual_bp)
    app.register_blueprint(twincityjakarta_manual_bp)
    app.register_blueprint(xweather_mapgolf_bp)
    #app.register_blueprint(xweather_report_bp)
    app.config["XWEATHER_DB_PATH"] = "xweather_reports.db"  # boleh diganti path lain
    app.register_blueprint(xweather_report_bp)


    @app.route("/")
    def index():
        return redirect(url_for("xweather_manual.xweather_manual_page"))

    @app.route("/xweather/manual.pdf", endpoint="xweather_manual_pdf")
    def xweather_manual_pdf_alias():
        return redirect(url_for("xweather_manual.xweather_manual_pdf"))

    @app.route("/twincityjakarta/manual.pdf", endpoint="twincityjakarta_manual_pdf")
    def twincityjakarta_manual_pdf_alias():
        return redirect(url_for("twincityjakarta_manual.twincityjakarta_manual_pdf"))


    return app
