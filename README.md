# Xweather Protect User Manual (HTML + PDF)

## Files
- templates/xweather_manual.html
- static/manual/xweather/*.png
- xweather_manual_routes.py (Blueprint routes)

## Quick install
1) Copy `templates/` and `static/` folders into your Flask project.
2) Import and register the blueprint:

```python
from xweather_manual_routes import xweather_manual_bp
app.register_blueprint(xweather_manual_bp)
```

3) Open:
- http://localhost:5000/xweather/manual
- http://localhost:5000/xweather/manual.pdf

## Notes
- PDF route uses WeasyPrint (pip install weasyprint).
- If PDF fails in your environment, you can still use the `Print / Save as PDF` button in the page.
