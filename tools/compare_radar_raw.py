#!/usr/bin/env python3
"""
compare_radar_raw.py

Utility to compare two Vaisala/IRIS RAW radar files and explain why one may
load/plot in a viewer while the other does not.

Usage:
    python compare_radar_raw.py /path/to/HLM260402125506.RAW1AZ0 /path/to/NGW260402131004.RAW0BXC
"""

from __future__ import annotations

import sys
import json
from pathlib import Path
from typing import Any

import numpy as np

try:
    import pyart
except Exception:
    print("ERROR: pyart is required. Install with: pip install arm_pyart")
    raise


PREFERRED_FIELDS = [
    "DBZ2", "DBZH", "DBZ", "TH", "DZ",
    "VEL2", "VRAD", "VEL", "WIDTH2", "WIDTH"
]


def summarize_field(arr: Any) -> dict[str, Any]:
    marr = np.ma.array(arr)
    filled = np.ma.filled(marr, np.nan).astype(float)
    finite = np.isfinite(filled)
    count = int(finite.sum())
    total = int(filled.size)
    if count == 0:
        return {
            "shape": list(filled.shape),
            "valid_count": 0,
            "total_count": total,
            "valid_ratio": 0.0,
            "min": None,
            "max": None,
            "mean": None,
        }
    return {
        "shape": list(filled.shape),
        "valid_count": count,
        "total_count": total,
        "valid_ratio": round(count / max(total, 1), 4),
        "min": float(np.nanmin(filled)),
        "max": float(np.nanmax(filled)),
        "mean": float(np.nanmean(filled)),
    }


def detect_best_field(radar) -> str | None:
    fields = list(radar.fields.keys())
    for f in PREFERRED_FIELDS:
        if f in fields:
            return f
    return fields[0] if fields else None


def radar_summary(path: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": path,
        "filename": Path(path).name,
        "read_ok": False,
        "error": None,
    }

    try:
        radar = pyart.io.read_sigmet(path, file_field_names=True, full_xhdr=True, time_ordered="full")
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    result["read_ok"] = True
    result["metadata"] = {
        "instrument_name": radar.metadata.get("instrument_name"),
        "task_name": radar.metadata.get("task_name") or radar.metadata.get("sigmet_task_name"),
    }

    fields = list(radar.fields.keys())
    result["fields"] = fields
    result["field_count"] = len(fields)
    result["nsweeps"] = int(getattr(radar, "nsweeps", 0) or 0)

    try:
        result["nrays"] = int(getattr(radar, "nrays", 0) or len(radar.azimuth["data"]))
    except Exception:
        result["nrays"] = None

    try:
        result["ngates"] = int(getattr(radar, "ngates", 0) or len(radar.range["data"]))
    except Exception:
        result["ngates"] = None

    try:
        rng = np.asarray(radar.range["data"], dtype=float)
        result["range_first_m"] = float(rng[0]) if rng.size else None
        result["range_last_m"] = float(rng[-1]) if rng.size else None
        result["range_count"] = int(rng.size)
    except Exception:
        result["range_first_m"] = None
        result["range_last_m"] = None
        result["range_count"] = None

    try:
        az = np.asarray(radar.azimuth["data"], dtype=float)
        result["azimuth_count"] = int(az.size)
        result["azimuth_min"] = float(np.nanmin(az)) if az.size else None
        result["azimuth_max"] = float(np.nanmax(az)) if az.size else None
        result["azimuth_unique_rounded"] = int(len(np.unique(np.round(az, 1)))) if az.size else 0
    except Exception:
        result["azimuth_count"] = None
        result["azimuth_min"] = None
        result["azimuth_max"] = None
        result["azimuth_unique_rounded"] = None

    try:
        elev = np.asarray(radar.elevation["data"], dtype=float)
        result["elevation_count"] = int(elev.size)
        result["elevation_min"] = float(np.nanmin(elev)) if elev.size else None
        result["elevation_max"] = float(np.nanmax(elev)) if elev.size else None
    except Exception:
        result["elevation_count"] = None
        result["elevation_min"] = None
        result["elevation_max"] = None

    try:
        fixed = np.asarray(radar.fixed_angle["data"], dtype=float)
        result["fixed_angles_deg"] = [float(v) for v in fixed.tolist()]
    except Exception:
        result["fixed_angles_deg"] = []

    best_field = detect_best_field(radar)
    result["best_field_guess"] = best_field

    field_stats: dict[str, Any] = {}
    for name in fields[:20]:
        try:
            field_stats[name] = summarize_field(radar.fields[name]["data"])
        except Exception as exc:
            field_stats[name] = {"error": f"{type(exc).__name__}: {exc}"}
    result["field_stats"] = field_stats

    sweep_stats = []
    if best_field is not None and result["nsweeps"] > 0:
        for i in range(result["nsweeps"]):
            try:
                arr = radar.get_field(i, best_field)
                stat = summarize_field(arr)
                stat["sweep_index"] = i
                sweep_stats.append(stat)
            except Exception as exc:
                sweep_stats.append({"sweep_index": i, "error": f"{type(exc).__name__}: {exc}"})
    result["best_field_sweeps"] = sweep_stats

    issues = []
    if len(fields) == 0:
        issues.append("No radar fields found.")
    if (result.get("range_count") or 0) == 0:
        issues.append("Range array empty.")
    if (result.get("azimuth_count") or 0) == 0:
        issues.append("Azimuth array empty.")
    if result["nsweeps"] == 0:
        issues.append("No sweeps found.")
    if best_field is None:
        issues.append("No usable field for plotting.")
    else:
        bf = field_stats.get(best_field, {})
        if bf.get("valid_count", 0) == 0:
            issues.append(f"Best field {best_field} has zero valid data.")
        elif bf.get("valid_ratio", 0.0) < 0.01:
            issues.append(f"Best field {best_field} is extremely sparse (<1% valid).")
    if sweep_stats:
        valid_sweeps = sum(1 for s in sweep_stats if s.get("valid_count", 0) > 100)
        if valid_sweeps == 0:
            issues.append("No sweep has >100 valid samples on best field.")
        elif valid_sweeps < max(1, result["nsweeps"] // 3):
            issues.append("Only a few sweeps have enough valid samples.")
    result["issues"] = issues
    return result


def compare(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if not a.get("read_ok"):
        lines.append(f"{a['filename']} failed to read: {a.get('error')}")
    if not b.get("read_ok"):
        lines.append(f"{b['filename']} failed to read: {b.get('error')}")
    if lines:
        return lines

    def push(label: str, key: str):
        va = a.get(key)
        vb = b.get(key)
        if va != vb:
            lines.append(f"{label}: {a['filename']}={va} | {b['filename']}={vb}")

    for label, key in [
        ("Field count", "field_count"),
        ("Sweep count", "nsweeps"),
        ("Ray count", "nrays"),
        ("Gate count", "ngates"),
        ("Best field guess", "best_field_guess"),
        ("Range last (m)", "range_last_m"),
    ]:
        push(label, key)

    a_best = a.get("best_field_guess")
    b_best = b.get("best_field_guess")
    if a_best and b_best:
        a_stat = a.get("field_stats", {}).get(a_best, {})
        b_stat = b.get("field_stats", {}).get(b_best, {})
        lines.append(
            f"Best field valid ratio: {a['filename']} {a_best}={a_stat.get('valid_ratio')} | "
            f"{b['filename']} {b_best}={b_stat.get('valid_ratio')}"
        )
        lines.append(
            f"Best field valid count: {a['filename']} {a_best}={a_stat.get('valid_count')} | "
            f"{b['filename']} {b_best}={b_stat.get('valid_count')}"
        )

    if a.get("issues"):
        lines.append(f"Issues in {a['filename']}: " + "; ".join(a["issues"]))
    if b.get("issues"):
        lines.append(f"Issues in {b['filename']}: " + "; ".join(b["issues"]))
    return lines


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)

    file_a = sys.argv[1]
    file_b = sys.argv[2]
    summary_a = radar_summary(file_a)
    summary_b = radar_summary(file_b)
    out = {
        "summary_a": summary_a,
        "summary_b": summary_b,
        "comparison": compare(summary_a, summary_b),
    }
    def json_safe(obj):
        if isinstance(obj, bytes):
            return obj.decode('utf-8', errors='ignore')
        return str(obj)
    
    print(json.dumps(out, indent=2, default=json_safe))


if __name__ == "__main__":
    main()
