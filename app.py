import os
from pathlib import Path
from flask import Flask, redirect, url_for

from routes.pages_routes import pages_bp
from routes.xweather_manual_routes import xweather_manual_bp
from routes.twincityjakarta_manual_routes import twincityjakarta_manual_bp
from routes.xweather_monthly_report_sqlite_routes import xweather_report_bp
from routes.xweather_mapgolf_routes import xweather_mapgolf_bp
from routes.kotatua_routes import kotatua_bp
from routes.radar_routes import radar_bp
from routes.iris_product_flow_routes import iris_product_flow_bp
from routes.hfradar_routes import hfradar_bp
from routes.attendance_routes import attendance_bp
from routes.terrindo_solutions_routes import terrindo_solutions_bp
from flask_login import LoginManager
from routes.attendance_models import db, User
from routes.auth_routes import auth_bp


def load_env_file(env_path: str = '.env') -> None:
    path = Path(env_path)
    if not path.exists():
        return
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def create_app():
    load_env_file('.env')

    app = Flask(__name__)

    app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'terrindo-xweather-manual')
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    app.config['XWEATHER_DB_PATH'] = 'xweather_reports.db'
    app.config['DATABASE_URL'] = os.getenv('DATABASE_URL', '').strip()

    # === WAJIB untuk Attendance / models.py ===
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL")
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    app.config['RADAR_UPLOAD_FOLDER'] = os.path.join(app.root_path, 'uploads', 'radarviewer')
    app.config['RADAR_RENDER_FOLDER'] = os.path.join(app.root_path, 'static', 'radar')

    os.makedirs(app.config['RADAR_UPLOAD_FOLDER'], exist_ok=True)
    os.makedirs(app.config['RADAR_RENDER_FOLDER'], exist_ok=True)

    # === init db sebelum register blueprint attendance ===
    db.init_app(app)
    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Silakan login terlebih dahulu."
    
    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))
    
    app.register_blueprint(auth_bp)
    app.register_blueprint(pages_bp)
    app.register_blueprint(xweather_manual_bp)
    app.register_blueprint(twincityjakarta_manual_bp)
    app.register_blueprint(xweather_mapgolf_bp)
    app.register_blueprint(xweather_report_bp)
    app.register_blueprint(kotatua_bp)
    app.register_blueprint(radar_bp)
    app.register_blueprint(iris_product_flow_bp)
    app.register_blueprint(hfradar_bp)
    app.register_blueprint(attendance_bp)
    app.register_blueprint(terrindo_solutions_bp)
    
    @app.route('/xweather/manual.pdf', endpoint='xweather_manual_pdf')
    def xweather_manual_pdf_alias():
        return redirect(url_for('xweather_manual.xweather_manual_pdf'))

    @app.route('/twincityjakarta/manual.pdf', endpoint='twincityjakarta_manual_pdf')
    def twincityjakarta_manual_pdf_alias():
        return redirect(url_for('twincityjakarta_manual.twincityjakarta_manual_pdf'))

    with app.app_context():
        db.create_all()

    # ==============================
    # ERROR HANDLER 403
    # ==============================
    from flask import render_template
    
    @app.errorhandler(403)
    def forbidden(error):
        return render_template("errors/403.html"), 403

    return app