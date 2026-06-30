from functools import wraps
from flask import abort
from flask_login import current_user, login_required

from routes.attendance_models import EMPLOYEE_ROLES, VALID_ROLES, UserPageAccess


PAGE_ACCESS = {
    "attendance": {
        "label": "Attendance",
        "endpoint": "pages.attendance_page",
        "roles": ("admin", *EMPLOYEE_ROLES),
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


def access_key(scope: str, key: str) -> str:
    return f"{scope}:{key}"


def role_can_access_project(role: str, project_key: str) -> bool:
    item = PROJECT_PAGE_ACCESS.get(project_key) or {}
    return role in item.get("roles", ())


def role_default_access_keys(role: str) -> set[str]:
    keys = {
        access_key("page", key)
        for key in PAGE_ACCESS
        if role_can_access_page(role, key)
    }
    keys.update(
        access_key("project", key)
        for key in PROJECT_PAGE_ACCESS
        if role_can_access_project(role, key)
    )
    return keys


def user_access_override_map(user) -> dict[str, bool]:
    if not user or not getattr(user, "id", None):
        return {}
    overrides = getattr(user, "page_access_overrides", None)
    if overrides is None:
        overrides = UserPageAccess.query.filter_by(user_id=user.id).all()
    return {item.access_key: bool(item.is_allowed) for item in overrides}


def effective_access_keys(user) -> set[str]:
    role = getattr(user, "role", "") or ""
    keys = set(role_default_access_keys(role))
    for key, allowed in user_access_override_map(user).items():
        if allowed:
            keys.add(key)
        else:
            keys.discard(key)
    return keys


def can_access(user, scope: str, key: str) -> bool:
    return access_key(scope, key) in effective_access_keys(user)


def accessible_page_keys(user_or_role) -> list[str]:
    if isinstance(user_or_role, str):
        keys = role_default_access_keys(user_or_role)
    else:
        keys = effective_access_keys(user_or_role)
    return [key for key in PAGE_ACCESS if access_key("page", key) in keys]


def accessible_page_labels(user_or_role) -> list[str]:
    return [PAGE_ACCESS[key]["label"] for key in accessible_page_keys(user_or_role)]


def accessible_project_keys(user_or_role) -> list[str]:
    if isinstance(user_or_role, str):
        keys = role_default_access_keys(user_or_role)
    else:
        keys = effective_access_keys(user_or_role)
    return [
        key
        for key in PROJECT_PAGE_ACCESS
        if access_key("project", key) in keys
    ]


def access_catalog() -> list[dict]:
    items = []
    for key, page in PAGE_ACCESS.items():
        items.append({
            "access_key": access_key("page", key),
            "scope": "page",
            "key": key,
            "label": page["label"],
            "default_roles": page["roles"],
        })
    for key, item in PROJECT_PAGE_ACCESS.items():
        items.append({
            "access_key": access_key("project", key),
            "scope": "project",
            "key": key,
            "label": item["label"],
            "default_roles": item["roles"],
        })
    return items


def page_required(page_key: str):
    def wrapper(fn):
        @wraps(fn)
        @login_required
        def decorated_view(*args, **kwargs):
            if not can_access(current_user, "page", page_key):
                abort(403)
            return fn(*args, **kwargs)
        return decorated_view
    return wrapper


def project_required(project_key: str):
    def wrapper(fn):
        @wraps(fn)
        @login_required
        def decorated_view(*args, **kwargs):
            if not can_access(current_user, "project", project_key):
                abort(403)
            return fn(*args, **kwargs)
        return decorated_view
    return wrapper


def save_user_access_overrides(db, user, selected_keys: set[str]):
    default_keys = role_default_access_keys(user.role)
    valid_keys = {item["access_key"] for item in access_catalog()}
    selected_keys = set(selected_keys) & valid_keys

    existing = {
        item.access_key: item
        for item in UserPageAccess.query.filter_by(user_id=user.id).all()
    }

    for key in valid_keys:
        default_allowed = key in default_keys
        selected_allowed = key in selected_keys
        item = existing.get(key)

        if selected_allowed == default_allowed:
            if item:
                db.session.delete(item)
            continue

        if item:
            item.is_allowed = selected_allowed
        else:
            db.session.add(UserPageAccess(
                user_id=user.id,
                access_key=key,
                is_allowed=selected_allowed,
            ))


def access_state_for_user(user) -> dict[str, bool]:
    keys = effective_access_keys(user)
    return {item["access_key"]: item["access_key"] in keys for item in access_catalog()}


def role_access_keys(role: str) -> list[str]:
    keys = role_default_access_keys(role)
    return [
        item["access_key"]
        for item in access_catalog()
        if item["access_key"] in keys
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
