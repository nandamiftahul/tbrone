import os
from pathlib import Path
from flask import Flask, redirect, url_for, render_template
from xweather_manual_routes import xweather_manual_bp
from twincityjakarta_manual_routes import twincityjakarta_manual_bp
from xweather_monthly_report_sqlite_routes import xweather_report_bp
from xweather_mapgolf_routes import xweather_mapgolf_bp
from kotatua_routes import kotatua_bp
from radar_routes import radar_bp


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

    app.config['SECRET_KEY'] = 'terrindo-xweather-manual'
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    app.config['XWEATHER_DB_PATH'] = 'xweather_reports.db'
    app.config['DATABASE_URL'] = os.getenv('DATABASE_URL', '').strip()
    app.config['RADAR_UPLOAD_FOLDER'] = os.path.join(app.root_path, 'uploads', 'radarviewer')
    app.config['RADAR_RENDER_FOLDER'] = os.path.join(app.root_path, 'static', 'radar')

    os.makedirs(app.config['RADAR_UPLOAD_FOLDER'], exist_ok=True)
    os.makedirs(app.config['RADAR_RENDER_FOLDER'], exist_ok=True)

    app.register_blueprint(xweather_manual_bp)
    app.register_blueprint(twincityjakarta_manual_bp)
    app.register_blueprint(xweather_mapgolf_bp)
    app.register_blueprint(xweather_report_bp)
    app.register_blueprint(kotatua_bp)
    app.register_blueprint(radar_bp)

    @app.route('/')
    def index():
        return render_template('index.html')

    @app.route('/xweather/manual.pdf', endpoint='xweather_manual_pdf')
    def xweather_manual_pdf_alias():
        return redirect(url_for('xweather_manual.xweather_manual_pdf'))

    @app.route('/twincityjakarta/manual.pdf', endpoint='twincityjakarta_manual_pdf')
    def twincityjakarta_manual_pdf_alias():
        return redirect(url_for('twincityjakarta_manual.twincityjakarta_manual_pdf'))

    return app
