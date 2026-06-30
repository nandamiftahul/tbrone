from functools import wraps
from flask import abort
from flask_login import current_user, login_required

from routes.attendance_models import VALID_ROLES


PAGE_ACCESS = {
    "attendance": {
        "label": "Attendance",
        "endpoint": "pages.attendance_page",
        "roles": ("admin", "hrd", "staff", "manager", "general_manager"),
    },
    "inventory": {
        "label": "Inventory",
        "endpoint": "pages.inventory_page",
        "roles": ("admin", "hrd"),
    },
    "project": {
        "label": "Project",
        "endpoint": "pages.project_page",
        "roles": ("admin", "client"),
    },
    "finance": {
        "label": "Finance",
        "endpoint": "pages.finance_page",
        "roles": ("admin",),
    },
    "job": {
        "label": "Job",
        "endpoint": "pages.job_page",
        "roles": ("admin",),
    },
    "wiki": {
        "label": "Wiki",
        "endpoint": "pages.wiki_page",
        "roles": VALID_ROLES,
    },
}


PROJECT_PAGE_ACCESS = {
    "golf_demo": {
        "label": "Golf Area Demo",
        "roles": ("admin", "client"),
    },
    "wbn_report": {
        "label": "WBN Monthly Report",
        "roles": ("admin", "client"),
    },
    "radar": {
        "label": "Radar Viewer",
        "roles": ("admin",),
    },
    "hfradar": {
        "label": "HF Radar Viewer",
        "roles": ("admin",),
    },
    "kotatua": {
        "label": "Trial Map Zone",
        "roles": ("admin",),
    },
}


def role_can_access_page(role: str, page_key: str) -> bool:
    page = PAGE_ACCESS.get(page_key) or {}
    return role in page.get("roles", ())


def accessible_page_keys(role: str) -> list[str]:
    return [key for key in PAGE_ACCESS if role_can_access_page(role, key)]


def accessible_page_labels(role: str) -> list[str]:
    return [PAGE_ACCESS[key]["label"] for key in accessible_page_keys(role)]


def accessible_project_keys(role: str) -> list[str]:
    return [
        key
        for key, item in PROJECT_PAGE_ACCESS.items()
        if role in item.get("roles", ())
    ]


def role_required(*roles):
    def wrapper(fn):
        @wraps(fn)
        @login_required
        def decorated_view(*args, **kwargs):
            if current_user.role not in roles:
                abort(403)
            return fn(*args, **kwargs)
        return decorated_view
    return wrapper
