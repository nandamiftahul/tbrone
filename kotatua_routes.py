from flask import Blueprint, render_template

kotatua_bp = Blueprint(
    "kotatua",
    __name__,
    template_folder="templates"
)

@kotatua_bp.route("/kotatuamap")
def kotatua_map():
    return render_template("kota_tua_dki_highlight_totaldki_with_toggles.html")