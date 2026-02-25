#!/usr/bin/env python3
"""
Inject / upsert Golf Locations into PostgreSQL (Railway) from JSON.

Target table: golf_locations
Columns (based on your Railway screenshot):
- id (serial / identity, PK)
- name (text)
- latitude (double precision)
- longitude (double precision)
- alarm_km (int)
- warning_km (int)
- info_km (int)
- alarm_color (text)
- warning_color (text)
- info_color (text)
- created_at (timestamptz)

This script will:
1) Read golf_locations.json (list of objects)
2) Ensure table exists
3) Ensure UNIQUE constraint on (name)
4) UPSERT using ON CONFLICT(name)

Usage:
  export DATABASE_URL="postgresql://user:pass@host:port/db"
  python inject_golf.py --json golf_locations.json
"""

import os
import json
import argparse
from datetime import datetime, timezone

import psycopg

from dotenv import load_dotenv
load_dotenv()
DEFAULTS = {
    "alarm_km": 4,
    "warning_km": 10,
    "info_km": 20,
    "alarm_color": "#EF4444",   # red
    "warning_color": "#F97316", # orange
    "info_color": "#FBBF24",    # amber/yellow
}


def now_utc():
    return datetime.now(timezone.utc)


def load_json(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict) and "items" in data and isinstance(data["items"], list):
        data = data["items"]

    if not isinstance(data, list):
        raise ValueError("JSON harus berupa LIST of objects. Contoh: [{...}, {...}]")

    # Normalize keys
    cleaned = []
    for i, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Item ke-{i} bukan object/dict")

        name = (item.get("name") or item.get("Nama") or item.get("golf_name") or "").strip()
        if not name:
            raise ValueError(f"Item ke-{i} tidak punya 'name'")

        lat = item.get("latitude", item.get("lat"))
        lon = item.get("longitude", item.get("lon"))

        if lat is None or lon is None:
            raise ValueError(f"Item '{name}' tidak punya latitude/longitude")

        # Force float
        lat = float(lat)
        lon = float(lon)

        cleaned.append({
            "name": name,
            "latitude": lat,
            "longitude": lon,
            "alarm_km": int(item.get("alarm_km", DEFAULTS["alarm_km"])),
            "warning_km": int(item.get("warning_km", DEFAULTS["warning_km"])),
            "info_km": int(item.get("info_km", DEFAULTS["info_km"])),
            "alarm_color": str(item.get("alarm_color", DEFAULTS["alarm_color"])),
            "warning_color": str(item.get("warning_color", DEFAULTS["warning_color"])),
            "info_color": str(item.get("info_color", DEFAULTS["info_color"])),
        })

    return cleaned


def ensure_table(conn: psycopg.Connection):
    """
    Create table if not exists (safe).
    If you already created it manually in Railway, this won't change it.
    """
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS golf_locations (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            latitude DOUBLE PRECISION NOT NULL,
            longitude DOUBLE PRECISION NOT NULL,
            alarm_km INTEGER NOT NULL DEFAULT 4,
            warning_km INTEGER NOT NULL DEFAULT 10,
            info_km INTEGER NOT NULL DEFAULT 20,
            alarm_color TEXT NOT NULL DEFAULT '#EF4444',
            warning_color TEXT NOT NULL DEFAULT '#F97316',
            info_color TEXT NOT NULL DEFAULT '#FBBF24',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)
    conn.commit()


def ensure_unique_constraint(conn: psycopg.Connection):
    """
    Add UNIQUE(name) if not exists.
    PostgreSQL doesn't support "IF NOT EXISTS" for constraints directly,
    so we check catalog first.
    """
    constraint_name = "golf_locations_name_unique"
    with conn.cursor() as cur:
        cur.execute("""
            SELECT 1
            FROM pg_constraint
            WHERE conname = %s
            LIMIT 1;
        """, (constraint_name,))
        exists = cur.fetchone()

        if not exists:
            # Before adding unique, ensure there are no duplicates (just in case)
            cur.execute("""
                SELECT name, COUNT(*) AS cnt
                FROM golf_locations
                GROUP BY name
                HAVING COUNT(*) > 1;
            """)
            dups = cur.fetchall()
            if dups:
                # If duplicates exist, we can't add unique safely.
                # We'll raise an error with clear info.
                msg = "Tidak bisa add UNIQUE(name) karena ada duplikasi name:\n"
                for (name, cnt) in dups:
                    msg += f"- {name} : {cnt} rows\n"
                raise RuntimeError(msg)

            cur.execute(f"""
                ALTER TABLE golf_locations
                ADD CONSTRAINT {constraint_name} UNIQUE (name);
            """)
    conn.commit()


def upsert_locations(conn: psycopg.Connection, items: list[dict]):
    """
    UPSERT based on UNIQUE(name).
    If same name exists -> update lat/lon + rings + colors.
    created_at will be preserved for existing rows.
    """
    sql = """
        INSERT INTO golf_locations (
            name, latitude, longitude,
            alarm_km, warning_km, info_km,
            alarm_color, warning_color, info_color,
            created_at
        )
        VALUES (
            %(name)s, %(latitude)s, %(longitude)s,
            %(alarm_km)s, %(warning_km)s, %(info_km)s,
            %(alarm_color)s, %(warning_color)s, %(info_color)s,
            %(created_at)s
        )
        ON CONFLICT (name) DO UPDATE SET
            latitude = EXCLUDED.latitude,
            longitude = EXCLUDED.longitude,
            alarm_km = EXCLUDED.alarm_km,
            warning_km = EXCLUDED.warning_km,
            info_km = EXCLUDED.info_km,
            alarm_color = EXCLUDED.alarm_color,
            warning_color = EXCLUDED.warning_color,
            info_color = EXCLUDED.info_color
        ;
    """

    created = now_utc()
    with conn.cursor() as cur:
        for item in items:
            payload = dict(item)
            payload["created_at"] = created
            cur.execute(sql, payload)

    conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="golf_locations.json", help="Path ke JSON lokasi golf")
    args = ap.parse_args()

    db_url = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL") or os.getenv("PGDATABASE")
    if not db_url:
        raise SystemExit(
            "ENV DATABASE_URL belum ada.\n"
            "Set dulu: export DATABASE_URL='postgresql://user:pass@host:port/db'"
        )

    items = load_json(args.json)

    # Connect
    conn = psycopg.connect(db_url)

    try:
        ensure_table(conn)
        ensure_unique_constraint(conn)
        upsert_locations(conn, items)

        print(f"✅ Sukses UPSERT {len(items)} lokasi ke table golf_locations.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()