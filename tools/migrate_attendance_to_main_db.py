#!/usr/bin/env python3
"""
Migrate Terrindo Attendance data from OLD Railway PostgreSQL into MAIN Railway PostgreSQL.

Default behavior:
- Creates Attendance tables in the destination database if they do not exist.
- Copies data table-by-table from source to destination.
- Uses UPSERT by primary key so rerunning the script is safe.
- Resets PostgreSQL sequences after migration.

Install requirements:
    pip install SQLAlchemy psycopg2-binary python-dotenv

Run:
    python migrate_attendance_to_main_db.py

Recommended Railway usage:
    set SOURCE_DATABASE_URL and DEST_DATABASE_URL in local .env or shell.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, time
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    Time,
    create_engine,
    inspect,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert

load_dotenv()

# OLD attendance database
SOURCE_DATABASE_URL = os.getenv(
    "SOURCE_DATABASE_URL",
    "postgresql://postgres:iXswKyPEMCxUatmIbMbHvLXyGdRqOTwE@gondola.proxy.rlwy.net:13174/railway",
)

# MAIN database
DEST_DATABASE_URL = os.getenv(
    "DEST_DATABASE_URL",
    os.getenv(
        "DATABASE_URL",
        "postgresql://postgres:pXqVlRKSGpxpNuMmXuGlJextJZFldqpg@caboose.proxy.rlwy.net:25146/railway",
    ),
)

# Change this to True if you want destination rows with the same PK to be overwritten.
# False = skip row if id already exists.
UPDATE_EXISTING_ROWS = os.getenv("UPDATE_EXISTING_ROWS", "false").lower() in {"1", "true", "yes", "y"}

metadata = MetaData()

users = Table(
    "users",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(120), nullable=False),
    Column("email", String(120), unique=True, nullable=False),
    Column("password_hash", String(255), nullable=False),
    Column("role", String(20), default="staff"),
    Column("is_active", Boolean, default=True),
)

shifts = Table(
    "shifts",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(80), nullable=False, unique=True),
    Column("start_time", Time, nullable=False),
    Column("end_time", Time, nullable=False),
    Column("grace_in_min", Integer, default=15),
    Column("grace_out_min", Integer, default=0),
)

employees = Table(
    "employees",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("code", String(20), unique=True, nullable=False),
    Column("name", String(120), nullable=False),
    Column("email", String(120), unique=True),
    Column("dept", String(80)),
    Column("role", String(20), default="staff"),
    Column("is_active", Boolean, default=True),
    Column("phone", String(30)),
    Column("address", String(255)),
    Column("birth_date", Date),
    Column("ktp_number", String(32)),
    Column("user_id", Integer, ForeignKey("users.id")),
    Column("shift_id", Integer, ForeignKey("shifts.id")),
    Column("face_embedding", LargeBinary),
    Column("face_updated_at", DateTime),
)

holidays = Table(
    "holidays",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("date", Date, unique=True, nullable=False),
    Column("name", String(120), nullable=False),
)

approval_routes = Table(
    "approval_routes",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("dept", String(80), nullable=True),
    Column("stage", String(20), nullable=False),
    Column("approver_user_id", Integer, ForeignKey("users.id"), nullable=False),
    Column("is_active", Boolean, default=True),
    Column("priority", Integer, default=100),
)

leave_requests = Table(
    "leave_requests",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("employee_id", Integer, ForeignKey("employees.id"), nullable=False),
    Column("type", String(20), nullable=False),
    Column("start_date", Date, nullable=False),
    Column("end_date", Date, nullable=False),
    Column("reason", String(255)),
    Column("status", String(30), default="pending_manager"),
    Column("manager_approved_by", Integer, ForeignKey("users.id")),
    Column("manager_approved_at", DateTime),
    Column("manager_assigned_to", Integer, ForeignKey("users.id")),
    Column("hrd_approved_by", Integer, ForeignKey("users.id")),
    Column("hrd_approved_at", DateTime),
    Column("hrd_assigned_to", Integer, ForeignKey("users.id")),
    Column("rejected_by", Integer, ForeignKey("users.id")),
    Column("rejected_at", DateTime),
    Column("created_at", DateTime),
    Column("updated_at", DateTime),
)

attendance = Table(
    "attendance",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("employee_id", Integer, ForeignKey("employees.id"), nullable=False),
    Column("work_date", Date, index=True),
    Column("check_in", DateTime),
    Column("check_out", DateTime),
    Column("status", String(20), default="present"),
    Column("note", String(255)),
    Column("check_in_ip", String(64)),
    Column("check_in_ua", String(255)),
    Column("check_in_lat", Float),
    Column("check_in_lon", Float),
    Column("check_out_ip", String(64)),
    Column("check_out_ua", String(255)),
    Column("check_out_lat", Float),
    Column("check_out_lon", Float),
)

offices = Table(
    "offices",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(100), nullable=False),
    Column("lat", Float, nullable=False),
    Column("lon", Float, nullable=False),
    Column("radius_m", Integer, default=150),
    Column("is_active", Boolean, default=True),
)

announcements = Table(
    "announcements",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("title", String(160), nullable=False),
    Column("body", Text, nullable=False),
    Column("level", String(20), default="info"),
    Column("is_active", Boolean, default=True),
    Column("start_at", DateTime),
    Column("end_at", DateTime),
    Column("created_at", DateTime),
    Column("updated_at", DateTime),
)

# Important order because of foreign keys.
TABLES_IN_ORDER = [
    users,
    shifts,
    employees,
    holidays,
    approval_routes,
    leave_requests,
    attendance,
    offices,
    announcements,
]


def normalize_url(url: str) -> str:
    """SQLAlchemy 2.x works better with postgresql+psycopg2 URLs."""
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


def row_to_dict(row: Any) -> dict[str, Any]:
    return dict(row._mapping)


def create_missing_tables(dest_engine) -> None:
    print("Creating destination tables if missing...")
    metadata.create_all(dest_engine, checkfirst=True)
    ensure_destination_columns(dest_engine)


def column_ddl(col: Column) -> str:
    # Compile SQLAlchemy column type to PostgreSQL DDL.
    compiled_type = col.type.compile(dialect=dest_engine_global.dialect)
    return f'{col.name} {compiled_type}'


def ensure_destination_columns(dest_engine) -> None:
    """
    create_all() only creates missing tables, it does not ALTER existing tables.
    This keeps the MAIN database compatible when a table already exists with an older schema.
    """
    global dest_engine_global
    dest_engine_global = dest_engine
    insp = inspect(dest_engine)
    with dest_engine.begin() as conn:
        for table in TABLES_IN_ORDER:
            if not insp.has_table(table.name):
                continue
            existing_cols = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing_cols:
                    continue
                ddl = column_ddl(col)
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN IF NOT EXISTS {ddl}'))
                print(f"ALTER {table.name}: added missing column {col.name}")


def source_has_table(src_engine, table_name: str) -> bool:
    return inspect(src_engine).has_table(table_name)


def get_source_columns(src_engine, table_name: str) -> set[str]:
    insp = inspect(src_engine)
    if not insp.has_table(table_name):
        return set()
    return {c["name"] for c in insp.get_columns(table_name)}


def default_for_missing_column(table_name: str, col_name: str) -> Any:
    """
    Defaults used when the OLD attendance database has an older schema.
    Example: old approval_routes may not have dept.
    """
    defaults = {
        ("approval_routes", "dept"): None,
        ("approval_routes", "is_active"): True,
        ("approval_routes", "priority"): 100,

        ("leave_requests", "status"): "pending_manager",
        ("leave_requests", "manager_approved_by"): None,
        ("leave_requests", "manager_approved_at"): None,
        ("leave_requests", "manager_assigned_to"): None,
        ("leave_requests", "hrd_approved_by"): None,
        ("leave_requests", "hrd_approved_at"): None,
        ("leave_requests", "hrd_assigned_to"): None,
        ("leave_requests", "rejected_by"): None,
        ("leave_requests", "rejected_at"): None,
        ("leave_requests", "created_at"): None,
        ("leave_requests", "updated_at"): None,

        ("employees", "phone"): None,
        ("employees", "address"): None,
        ("employees", "birth_date"): None,
        ("employees", "ktp_number"): None,
        ("employees", "user_id"): None,
        ("employees", "shift_id"): None,
        ("employees", "face_embedding"): None,
        ("employees", "face_updated_at"): None,

        ("attendance", "note"): None,
        ("attendance", "check_in_ip"): None,
        ("attendance", "check_in_ua"): None,
        ("attendance", "check_in_lat"): None,
        ("attendance", "check_in_lon"): None,
        ("attendance", "check_out_ip"): None,
        ("attendance", "check_out_ua"): None,
        ("attendance", "check_out_lat"): None,
        ("attendance", "check_out_lon"): None,

        ("announcements", "start_at"): None,
        ("announcements", "end_at"): None,
        ("announcements", "created_at"): None,
        ("announcements", "updated_at"): None,
    }
    return defaults.get((table_name, col_name), None)


def copy_table(src_engine, dest_engine, table: Table) -> int:
    if not source_has_table(src_engine, table.name):
        print(f"SKIP {table.name}: table not found in source")
        return 0

    pk_cols = [c.name for c in table.primary_key.columns]
    if not pk_cols:
        raise RuntimeError(f"Table {table.name} has no primary key")

    src_cols = get_source_columns(src_engine, table.name)
    dest_col_names = [c.name for c in table.columns]
    selectable_cols = [table.c[name] for name in dest_col_names if name in src_cols]

    if not selectable_cols:
        print(f"SKIP {table.name}: no matching columns in source")
        return 0

    missing_cols = [name for name in dest_col_names if name not in src_cols]
    if missing_cols:
        print(f"INFO {table.name}: source missing columns -> {', '.join(missing_cols)}; using defaults/NULL")

    with src_engine.connect() as src_conn:
        raw_rows = [row_to_dict(r) for r in src_conn.execute(select(*selectable_cols)).fetchall()]

    rows = []
    for raw in raw_rows:
        normalized = {}
        for name in dest_col_names:
            if name in raw:
                normalized[name] = raw[name]
            else:
                normalized[name] = default_for_missing_column(table.name, name)
        rows.append(normalized)

    if not rows:
        print(f"OK   {table.name}: no rows")
        return 0

    with dest_engine.begin() as dest_conn:
        stmt = pg_insert(table).values(rows)
        if UPDATE_EXISTING_ROWS:
            update_cols = {
                c.name: getattr(stmt.excluded, c.name)
                for c in table.columns
                if c.name not in pk_cols
            }
            stmt = stmt.on_conflict_do_update(index_elements=pk_cols, set_=update_cols)
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=pk_cols)
        result = dest_conn.execute(stmt)

    copied = result.rowcount or 0
    print(f"OK   {table.name}: source={len(rows)} inserted/updated={copied}")
    return copied


def reset_sequence(dest_engine, table: Table) -> None:
    pk_cols = list(table.primary_key.columns)
    if len(pk_cols) != 1:
        return

    pk = pk_cols[0].name
    sql = text(
        """
        SELECT setval(
            pg_get_serial_sequence(:table_name, :pk_name),
            COALESCE((SELECT MAX({pk}) FROM {table_name}), 1),
            true
        )
        """.format(pk=pk, table_name=table.name)
    )
    try:
        with dest_engine.begin() as conn:
            conn.execute(sql, {"table_name": table.name, "pk_name": pk})
        print(f"SEQ  {table.name}.{pk}: reset")
    except Exception as e:
        print(f"WARN {table.name}.{pk}: sequence reset skipped -> {e}")


def main() -> int:
    if not SOURCE_DATABASE_URL or not DEST_DATABASE_URL:
        print("SOURCE_DATABASE_URL and DEST_DATABASE_URL are required.", file=sys.stderr)
        return 1

    src_engine = create_engine(normalize_url(SOURCE_DATABASE_URL), pool_pre_ping=True)
    dest_engine = create_engine(normalize_url(DEST_DATABASE_URL), pool_pre_ping=True)

    print("=== Terrindo Attendance Migration ===")
    print("Source: OLD attendance database")
    print("Dest  : MAIN database")
    print(f"Mode  : {'UPSERT update existing rows' if UPDATE_EXISTING_ROWS else 'insert only, keep existing rows'}")

    # Fast connection check.
    with src_engine.connect() as c:
        c.execute(text("SELECT 1"))
    with dest_engine.connect() as c:
        c.execute(text("SELECT 1"))

    create_missing_tables(dest_engine)

    total = 0
    for table in TABLES_IN_ORDER:
        total += copy_table(src_engine, dest_engine, table)

    print("Resetting destination sequences...")
    for table in TABLES_IN_ORDER:
        reset_sequence(dest_engine, table)

    print(f"DONE. Total inserted/updated rows: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
