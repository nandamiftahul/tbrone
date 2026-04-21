from flask import Blueprint, render_template, jsonify

xweather_mapgolf_bp = Blueprint("xweather_mapgolf", __name__, url_prefix="/xweather")

@xweather_mapgolf_bp.get("/mapgolfdemo")
def mapgolfdemo_page():
    return render_template("project/xweather_golfareamapdemo.html")

@xweather_mapgolf_bp.get("/golfareamapdemo")
def golfareamapdemo_api():
    # kalau kamu sudah punya API existing, pindahkan/return data dari model kamu
    payload = {"assets": [], "events": []}
    return jsonify(payload)