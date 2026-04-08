
from __future__ import annotations

import configparser
import json
import os
import re
import tempfile
from collections import OrderedDict
from hashlib import md5
from datetime import datetime, timedelta
from io import BytesIO
from typing import Any

import matplotlib
import h5py
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap, ListedColormap
import numpy as np
os.environ["PYART_QUIET"] = "1"
import patch_pyart  # noqa: F401
import pyart
from flask import Blueprint, current_app, jsonify, redirect, render_template, request, send_file, url_for

from scipy.ndimage import generic_filter, median_filter, distance_transform_edt, gaussian_filter
from scipy.ndimage.measurements import variance
from scipy.ndimage.filters import uniform_filter
from scipy.interpolate import griddata
from scipy.spatial import cKDTree
import cv2
from skimage import exposure

radar_bp = Blueprint(
    'radar_bp',
    __name__,
    template_folder='templates',
    static_folder='static',
    url_prefix='/radarviewer',
)

radar_groups: dict[str, list[dict[str, Any]]] = {}
uploaded_files_mem: list[dict[str, Any]] = []
available_fields: list[str] = []
radar_extent = [[-10, 100], [10, 120]]
time_bounds: dict[str, dict[str, float]] = {}
merge_window_minutes: int = 5

# in-memory caches to keep playback responsive
render_cache: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
merged_radar_cache: "OrderedDict[str, tuple[Any, list[str]]]" = OrderedDict()
CACHE_MAX_ITEMS = 96
MERGED_CACHE_MAX_ITEMS = 24

default_configs = {
    'DBZ2': dict(vmin=-20, vmax=70, cmap='turbo'),
    'VEL2': dict(vmin=-30, vmax=30, cmap='seismic'),
    'WIDTH2': dict(vmin=0, vmax=5, cmap='turbo'),
    'ZDR2': dict(vmin=-5, vmax=5, cmap='turbo'),
    'KDP2': dict(vmin=-1, vmax=5, cmap='viridis'),
    'RHOHV2': dict(vmin=0.8, vmax=1.05, cmap='seismic'),
    'VELC2': dict(vmin=-30, vmax=30, cmap='seismic'),
    'SQI2': dict(vmin=0, vmax=1, cmap='viridis'),
    'PHIDP2': dict(vmin=-180, vmax=180, cmap='twilight_shifted'),
    'HCLASS2': dict(vmin=0, vmax=10, cmap='turbo'),
    'SNR16': dict(vmin=-20, vmax=40, cmap='turbo'),
    'PMI16': dict(vmin=0, vmax=1, cmap='viridis'),
    'LOG16': dict(vmin=0, vmax=5, cmap='plasma'),
    'CSP16': dict(vmin=0, vmax=1, cmap='turbo'),
}

QUALITY_PRESETS = {
    'low': {
        'figsize': 5.0,
        'dpi': 110,
        'ppi_grid': 500,
        'max_cappi_grid': 450,
        'vertical_levels': 10,
    },
    'medium': {
        'figsize': 6.0,
        'dpi': 140,
        'ppi_grid': 700,
        'max_cappi_grid': 650,
        'vertical_levels': 12,
    },
    'high': {
        'figsize': 7.0,
        'dpi': 180,
        'ppi_grid': 900,
        'max_cappi_grid': 800,
        'vertical_levels': 14,
    },
    'ultra': {
        'figsize': 8.5,
        'dpi': 220,
        'ppi_grid': 1200,
        'max_cappi_grid': 1000,
        'vertical_levels': 16,
    },
}


def _normalize_render_quality(value: Any) -> str:
    quality = str(value or 'high').strip().lower()
    return quality if quality in QUALITY_PRESETS else 'high'


def _quality_settings(value: Any) -> dict[str, Any]:
    return QUALITY_PRESETS[_normalize_render_quality(value)]


def _unique_warning_messages(warnings: list[str] | tuple[str, ...] | None) -> list[str]:
    unique = []
    seen = set()
    for item in warnings or []:
        msg = str(item or '').strip()
        if not msg:
            continue
        key = msg.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(msg)
    return unique

def _safe_float(value: Any) -> float | None:
    try:
        if value in (None, '', 'null', 'None'):
            return None
        return float(value)
    except Exception:
        return None


def _build_custom_cmap(spec: Any, fallback_name: str = 'turbo'):
    if not spec:
        return plt.get_cmap(fallback_name).copy()
    try:
        if isinstance(spec, str):
            spec = json.loads(spec)
        mode = str((spec or {}).get('mode') or 'gradients').strip().lower()
        stops = (spec or {}).get('stops') or []
        parsed = []
        for stop in stops:
            if not isinstance(stop, dict):
                continue
            value = _safe_float(stop.get('value'))
            color = str(stop.get('color') or '').strip()
            if value is None or not color:
                continue
            if not color.startswith('#'):
                color = f'#{color}'
            parsed.append((float(value), color))
        if len(parsed) < 2:
            return plt.get_cmap(fallback_name).copy()
        parsed.sort(key=lambda item: item[0])
        values = [item[0] for item in parsed]
        colors = [item[1] for item in parsed]
        vmin = min(values)
        vmax = max(values)
        if vmax <= vmin:
            return plt.get_cmap(fallback_name).copy()
        if mode == 'ranges':
            cmap = ListedColormap(colors, name='custom_ranges')
        else:
            norm_positions = [0.0 if vmax == vmin else (v - vmin) / (vmax - vmin) for v in values]
            cmap = LinearSegmentedColormap.from_list('custom_gradients', list(zip(norm_positions, colors)))
        return cmap.copy() if hasattr(cmap, 'copy') else cmap
    except Exception:
        return plt.get_cmap(fallback_name).copy()


def _resolve_cmap(field: str, cmap_override: str = '', custom_cmap: Any = None):
    cfg = default_configs.get(field, {})
    fallback_name = cmap_override or cfg.get('cmap', 'turbo')
    cmap_obj = _build_custom_cmap(custom_cmap, fallback_name=fallback_name) if custom_cmap else plt.get_cmap(fallback_name).copy()
    try:
        cmap_obj.set_bad(alpha=0.0)
    except Exception:
        pass
    return cmap_obj


def _choose_reflectivity_field(radar, preferred: str | None = None) -> str | None:
    candidates = []
    if preferred:
        candidates.append(str(preferred))
    candidates.extend(['DBZ2', 'DBT2', 'DBZH', 'DBZ', 'TH', 'DZ'])
    seen = set()
    for name in candidates:
        if not name or name in seen:
            continue
        seen.add(name)
        if name in radar.fields:
            return name
    for name in radar.fields.keys():
        up = str(name).upper()
        if 'DBZ' in up or 'DBT' in up or up in ('TH', 'DZ'):
            return name
    return list(radar.fields.keys())[0] if radar.fields else None


def _dbz_to_rain_rate(dbz: Any, a: float = 200.0, b: float = 1.6):
    arr = np.asarray(dbz, dtype=float)
    z_lin = np.power(10.0, arr / 10.0)
    with np.errstate(invalid='ignore', divide='ignore', over='ignore'):
        rain = np.power(np.maximum(z_lin / max(a, 1e-6), 0.0), 1.0 / max(b, 1e-6))
    rain = np.where(np.isfinite(arr), rain, np.nan)
    rain = np.where(rain < 0, np.nan, rain)
    return rain


def _estimate_accumulation_from_rate(rain_rate_mmh: Any, accumulation_minutes: float):
    arr = np.asarray(rain_rate_mmh, dtype=float)
    hours = max(float(accumulation_minutes), 0.0) / 60.0
    acc = arr * hours
    acc = np.where(np.isfinite(arr), acc, np.nan)
    acc = np.where(acc < 0, np.nan, acc)
    return acc

def _build_cartesian_grid_for_product(radar, field: str, quality_cfg: dict[str, Any], rng_km: float, extra_z_levels: int = 0):
    grid_limit_xy = rng_km * 1000.0
    z_top = max(12000.0, float(getattr(radar, 'nsweeps', 1) or 1) * 1000.0)
    grid = pyart.map.grid_from_radars(
        (radar,),
        grid_shape=(quality_cfg['vertical_levels'] + int(extra_z_levels), quality_cfg['max_cappi_grid'], quality_cfg['max_cappi_grid']),
        grid_limits=((0.0, z_top), (-grid_limit_xy, grid_limit_xy), (-grid_limit_xy, grid_limit_xy)),
        fields=[field],
        weighting_function='Barnes2',
    )
    arr3 = np.ma.filled(grid.fields[field]['data'], np.nan).astype(float)
    try:
        z_levels = np.asarray(grid.z['data'], dtype=float)
    except Exception:
        z_levels = np.linspace(0.0, z_top, arr3.shape[0])
    return grid, arr3, z_levels


def _compute_vertical_derived_array(arr3: Any, z_levels: Any, product_used: str, threshold_dbz: float = 18.0):
    arr3 = np.asarray(arr3, dtype=float)
    z_levels = np.asarray(z_levels, dtype=float)
    product_used = str(product_used or '').upper()
    if arr3.ndim != 3:
        return np.asarray(arr3, dtype=float), '', None, None

    if z_levels.size != arr3.shape[0]:
        z_levels = np.linspace(0.0, max(1000.0, float(arr3.shape[0] - 1) * 1000.0), arr3.shape[0])

    mask = np.isfinite(arr3) & (arr3 >= threshold_dbz)
    z3 = z_levels[:, None, None]

    if product_used == 'ETOP':
        top = np.nanmax(np.where(mask, z3, np.nan), axis=0) / 1000.0
        return top, 'km', 0.0, max(12.0, float(np.nanmax(top)) if np.isfinite(np.nanmax(top)) else 12.0)

    if product_used == 'EBASE':
        base = np.nanmin(np.where(mask, z3, np.nan), axis=0) / 1000.0
        return base, 'km', 0.0, max(12.0, float(np.nanmax(base)) if np.isfinite(np.nanmax(base)) else 12.0)

    if product_used == 'ETHICK':
        top = np.nanmax(np.where(mask, z3, np.nan), axis=0)
        base = np.nanmin(np.where(mask, z3, np.nan), axis=0)
        thick = (top - base) / 1000.0
        thick = np.where(np.isfinite(top) & np.isfinite(base), thick, np.nan)
        return thick, 'km', 0.0, max(12.0, float(np.nanmax(thick)) if np.isfinite(np.nanmax(thick)) else 12.0)

    if product_used == 'LMEAN':
        mean_dbz = np.nanmean(np.where(mask, arr3, np.nan), axis=0)
        return mean_dbz, 'dBZ', 0.0, 70.0

    if product_used == 'VIL':
        z_lin = np.where(np.isfinite(arr3), np.power(10.0, arr3 / 10.0), np.nan)
        term = np.power(np.maximum(z_lin, 0.0), 4.0 / 7.0)
        if z_levels.size >= 2:
            dz = np.diff(z_levels)
            dz = np.r_[dz, dz[-1]]
        else:
            dz = np.array([1000.0])
        vil = 3.44e-6 * np.nansum(term * dz[:, None, None], axis=0)
        vil = np.where(np.any(mask, axis=0), vil, np.nan)
        vmax = float(np.nanpercentile(vil[np.isfinite(vil)], 99)) if np.any(np.isfinite(vil)) else 10.0
        return vil, 'kg/m²', 0.0, max(10.0, vmax)

    return np.nanmax(arr3, axis=0), '', None, None


def _sample_grid_value(grid, arr2: Any, radar, lat: float, lon: float):
    arr2 = np.asarray(arr2, dtype=float)
    x_km, y_km = _latlon_to_local_km(float(radar.latitude['data'][0]), float(radar.longitude['data'][0]), np.asarray([lat]), np.asarray([lon]))
    x_m = float(x_km[0] * 1000.0)
    y_m = float(y_km[0] * 1000.0)
    gx = np.asarray(grid.x['data'], dtype=float)
    gy = np.asarray(grid.y['data'], dtype=float)
    if np.hypot(x_m, y_m) > float(np.nanmax(radar.range['data'])):
        return {'ok': False, 'reason': 'outside_range'}
    ix = int(np.argmin(np.abs(gx - x_m)))
    iy = int(np.argmin(np.abs(gy - y_m)))
    value = float(arr2[iy, ix]) if np.isfinite(arr2[iy, ix]) else float('nan')
    if np.isfinite(value):
        return {'ok': True, 'value': value, 'x_km': x_m / 1000.0, 'y_km': y_m / 1000.0}
    return {'ok': False, 'reason': 'no_data'}


def _sample_nearest_polar_value(radar, data2d, sweep_idx: int, lat: float, lon: float):
    lat0 = float(radar.latitude['data'][0])
    lon0 = float(radar.longitude['data'][0])
    x_km, y_km = _latlon_to_local_km(lat0, lon0, np.asarray([lat]), np.asarray([lon]))
    x_km = float(x_km[0])
    y_km = float(y_km[0])
    range_km = float(np.hypot(x_km, y_km))
    azimuth = float((np.degrees(np.arctan2(x_km, y_km)) + 360.0) % 360.0)

    range_data_km = np.asarray(radar.range['data'], dtype=float) / 1000.0
    max_range_km = float(np.nanmax(range_data_km)) if range_data_km.size else 0.0
    if range_km > max_range_km:
        return {'ok': False, 'reason': 'outside_range', 'range_km': range_km, 'azimuth_deg': azimuth}

    sweep_starts = np.asarray(radar.sweep_start_ray_index['data'], dtype=int)
    sweep_ends = np.asarray(radar.sweep_end_ray_index['data'], dtype=int)
    fixed_angles = np.asarray(radar.fixed_angle['data'], dtype=float) if getattr(radar, 'fixed_angle', None) is not None else np.zeros(len(sweep_starts), dtype=float)
    sweep_idx = int(np.clip(sweep_idx, 0, max(len(sweep_starts) - 1, 0)))
    rs = int(sweep_starts[sweep_idx])
    re = int(sweep_ends[sweep_idx])
    az = np.asarray(radar.azimuth['data'][rs:re+1], dtype=float)
    if az.size == 0:
        return {'ok': False, 'reason': 'empty_sweep'}
    ray_local = int(np.argmin(_circular_abs_diff_deg(az, azimuth)))
    ray_idx = rs + ray_local
    gate_idx = int(np.argmin(np.abs(range_data_km - range_km)))
    try:
        value = float(np.asarray(data2d, dtype=float)[ray_idx, gate_idx])
    except Exception:
        value = float('nan')
    if not np.isfinite(value):
        return {'ok': False, 'reason': 'no_data', 'range_km': range_km, 'azimuth_deg': azimuth, 'ray_idx': ray_idx, 'gate_idx': gate_idx}
    elevation = float(fixed_angles[min(sweep_idx, len(fixed_angles) - 1)]) if fixed_angles.size else 0.0
    return {
        'ok': True,
        'value': value,
        'range_km': range_km,
        'azimuth_deg': azimuth,
        'elevation_deg': elevation,
        'ray_idx': ray_idx,
        'gate_idx': gate_idx,
        'x_km': x_km,
        'y_km': y_km,
    }


def _compute_product_field_data(
    radar,
    field,
    product_used: str,
    selected_sweep_idx: int,
    cappi_height_km: float,
    accumulation_minutes: float = 5.0,
):
    product_used = (product_used or 'PPI').upper()
    source_field = field
    source_data = np.ma.filled(radar.fields[field]['data'], np.nan).astype(float)

    if product_used in ('SRI', 'ACC'):
        source_field = _choose_reflectivity_field(radar, preferred=field)
        source_data = np.ma.filled(radar.fields[source_field]['data'], np.nan).astype(float)
        rain_rate = _dbz_to_rain_rate(source_data)
        if product_used == 'ACC':
            return source_field, source_data, _estimate_accumulation_from_rate(rain_rate, accumulation_minutes)
        return source_field, source_data, rain_rate

    return source_field, source_data, source_data




def _sample_accumulation_series_by_group(
    group: str,
    field: str,
    lat: float,
    lon: float,
    filters=None,
    requested_sweep=None,
    requested_elevation=None,
    cappi_height_km: float = 2.0,
):
    filters = filters or {}
    if group not in radar_groups:
        raise RuntimeError('Group not found')

    series = []
    frames = radar_groups[group]
    for idx, frame_data in enumerate(frames):
        try:
            payload = _sample_product_value_from_files(
                frame_data['files'],
                field,
                lat,
                lon,
                filters=filters,
                requested_sweep=requested_sweep,
                requested_elevation=requested_elevation,
                derived_product='ACC',
                cappi_height_km=cappi_height_km,
                accumulation_minutes=float(merge_window_minutes),
            )
            value = payload.get('value')
            value = float(value) if value is not None and np.isfinite(value) else None
        except Exception:
            value = None

        series.append({
            'frame': idx,
            'timestamp': frame_data.get('time'),
            'epoch': frame_data.get('epoch'),
            'value': value,
        })
    return series

def _sample_product_value_from_files(
    filepaths,
    field,
    lat,
    lon,
    filters=None,
    requested_sweep=None,
    requested_elevation=None,
    derived_product='PPI',
    cappi_height_km=2.0,
    accumulation_minutes: float = 5.0,
):
    filters = filters or {}
    radar, warnings = _merge_radars(filepaths)
    if field not in radar.fields:
        field = _choose_reflectivity_field(radar, preferred='DBZ2')

    selected_sweep_idx, sweep_options, selected_elevation = _resolve_sweep_index(radar, requested_sweep, requested_elevation)

    for fname in list(radar.fields.keys()):
        raw_data = radar.fields[fname]['data'].copy()
        filtered_data, _ = _apply_pyiris_filters(radar, fname, raw_data, filters, load_pyiris_defaults())
        radar.fields[fname]['data'] = np.ma.masked_invalid(filtered_data)

    product_used = (derived_product or 'PPI').upper()
    source_field, source_data, product_data = _compute_product_field_data(
        radar, field, product_used, selected_sweep_idx, cappi_height_km, accumulation_minutes=accumulation_minutes
    )

    unit = 'value'
    display_name = source_field
    detail = {}

    if product_used == 'PPI':
        sample = _sample_nearest_polar_value(radar, product_data, selected_sweep_idx, lat, lon)
        unit = 'dBZ' if 'DB' in str(source_field).upper() else 'value'
        display_name = source_field
    elif product_used == 'SRI':
        lowest_idx = 0
        sample = _sample_nearest_polar_value(radar, product_data, lowest_idx, lat, lon)
        unit = 'mm/h'
        display_name = 'SRI'
    elif product_used == 'ACC':
        lowest_idx = 0
        sample = _sample_nearest_polar_value(radar, product_data, lowest_idx, lat, lon)
        unit = 'mm'
        display_name = 'Estimated Rain Accumulation'
    elif product_used == 'MAX':
        samples = []
        nsweeps = int(getattr(radar, 'nsweeps', 1) or 1)
        for sidx in range(nsweeps):
            s = _sample_nearest_polar_value(radar, product_data, sidx, lat, lon)
            if s.get('ok'):
                samples.append(s)
        if samples:
            best = max(samples, key=lambda s: s['value'])
            sample = dict(best)
            sample['sweep_count_used'] = len(samples)
        else:
            sample = {'ok': False, 'reason': 'no_data'}
        unit = 'dBZ' if 'DB' in str(source_field).upper() else 'value'
        display_name = f'MAX {source_field}'
    elif product_used == 'CAPPI':
        grid_limit_xy = float(radar.range['data'][-1])
        z_top = max(12000.0, float(getattr(radar, 'nsweeps', 1) or 1) * 1000.0)
        grid = pyart.map.grid_from_radars(
            (radar,),
            grid_shape=(max(6, _quality_settings('medium')['vertical_levels']), 320, 320),
            grid_limits=((0.0, z_top), (-grid_limit_xy, grid_limit_xy), (-grid_limit_xy, grid_limit_xy)),
            fields=[source_field],
            weighting_function='Barnes2',
        )
        arr3 = np.ma.filled(grid.fields[source_field]['data'], np.nan)
        try:
            z_levels = np.asarray(grid.z['data'], dtype=float)
        except Exception:
            z_levels = np.linspace(0.0, z_top, arr3.shape[0])
        target = float(np.clip(float(cappi_height_km) * 1000.0, z_levels.min(), z_levels.max()))
        hi = int(np.searchsorted(z_levels, target, side='left'))
        hi = max(1, min(hi, len(z_levels) - 1))
        lo = hi - 1
        z0 = float(z_levels[lo])
        z1 = float(z_levels[hi])
        a0 = arr3[lo]
        a1 = arr3[hi]
        w = 0.0 if z1 <= z0 else (target - z0) / (z1 - z0)
        arr2 = (1.0 - w) * a0 + w * a1
        x_km, y_km = _latlon_to_local_km(float(radar.latitude['data'][0]), float(radar.longitude['data'][0]), np.asarray([lat]), np.asarray([lon]))
        x_m = float(x_km[0] * 1000.0)
        y_m = float(y_km[0] * 1000.0)
        gx = np.asarray(grid.x['data'], dtype=float)
        gy = np.asarray(grid.y['data'], dtype=float)
        if np.hypot(x_m, y_m) > float(np.nanmax(radar.range['data'])):
            sample = {'ok': False, 'reason': 'outside_range'}
        else:
            ix = int(np.argmin(np.abs(gx - x_m)))
            iy = int(np.argmin(np.abs(gy - y_m)))
            value = float(arr2[iy, ix]) if np.isfinite(arr2[iy, ix]) else float('nan')
            if np.isfinite(value):
                sample = {'ok': True, 'value': value, 'x_km': x_m / 1000.0, 'y_km': y_m / 1000.0, 'height_km': target / 1000.0}
            else:
                sample = {'ok': False, 'reason': 'no_data'}
        unit = 'dBZ' if 'DB' in str(source_field).upper() else 'value'
        display_name = f'CAPPI {source_field}'
    elif product_used in ('ETOP', 'EBASE', 'ETHICK', 'LMEAN', 'VIL'):
        grid, arr3, z_levels = _build_cartesian_grid_for_product(radar, source_field, _quality_settings('medium'), float(radar.range['data'][-1]) / 1000.0, extra_z_levels=2)
        arr2, derived_unit, _, _ = _compute_vertical_derived_array(arr3, z_levels, product_used)
        sample = _sample_grid_value(grid, arr2, radar, lat, lon)
        unit = derived_unit or ('dBZ' if 'DB' in str(source_field).upper() else 'value')
        display_names = {
            'ETOP': 'Echo Top',
            'EBASE': 'Echo Base',
            'ETHICK': 'Echo Thickness',
            'LMEAN': 'Layer Mean Reflectivity',
            'VIL': 'VIL',
        }
        display_name = display_names.get(product_used, product_used)
    else:
        sample = _sample_nearest_polar_value(radar, product_data, selected_sweep_idx, lat, lon)

    return {
        'ok': bool(sample.get('ok')),
        'product': product_used,
        'field': display_name,
        'source_field': source_field,
        'unit': unit,
        'lat': float(lat),
        'lon': float(lon),
        'value': sample.get('value'),
        'details': {k: v for k, v in sample.items() if k not in ('ok', 'value')},
        'warnings': warnings,
        'selected_sweep_idx': selected_sweep_idx,
        'selected_elevation': selected_elevation,
        'sweep_options': sweep_options,
    }

PYIRIS_DEFAULTS = {
    'enable_standard_filter': False,
    'active_LOG': True,
    'active_SQI': True,
    'active_PMI': True,
    'active_CSR': True,
    'active_SNR': True,
    'active_PHI': True,
    'active_SDZ': True,
    'active_MDZ': True,
    'enable_speckle_filter': False,
    'speckle_type': 'mdfill',
    'window_size': 3,
    'th_LOGZ': 2.00,
    'th_LOGE': 2.00,
    'th_LOGV': 1.44,
    'th_LOGW': 1.44,
    'th_LOGD': 1.44,
    'th_SQIZ': 0.2,
    'th_SQIE': 0.2,
    'th_SQIV': 0.4,
    'th_SQIW': 0.4,
    'th_SQID': 0.2,
    'th_PMIZ': 0.4,
    'th_PMIE': 0.4,
    'th_PMIV': 0.4,
    'th_PMIW': 0.4,
    'th_PMID': 0.4,
    'th_CSRZ': 40.0,
    'th_CSRE': 40.0,
    'th_CSRV': 40.0,
    'th_CSRW': 40.0,
    'th_CSRD': 40.0,
    'th_SNLGZ': 2.0,
    'th_SNLGE': 1.5,
    'th_SNLGV': 2.0,
    'th_SNLGW': 2.0,
    'th_SNLGD': 2.0,
    'th_SNRD': 0.0,
    'th_PHID1': 10.0,
    'th_PHID2': 20.0,
    'th_SDZ': 35.0,
    'th_MDZ': -35.0,
}


def _cache_store(cache_obj: OrderedDict, key: str, value: Any, max_items: int):
    cache_obj[key] = value
    cache_obj.move_to_end(key)
    while len(cache_obj) > max_items:
        cache_obj.popitem(last=False)

def _cache_get(cache_obj: OrderedDict, key: str):
    if key not in cache_obj:
        return None
    cache_obj.move_to_end(key)
    return cache_obj[key]

def _make_render_cache_key(
    group: str,
    idx: int,
    field: str,
    cmap_override: str,
    vmin_override: Any,
    vmax_override: Any,
    filters: dict[str, Any],
    requested_sweep: Any,
    requested_elevation: Any,
    derived_product: str,
    cappi_height_km: Any,
    render_quality: str = 'high',
    custom_cmap: Any = None,
):
    raw = json.dumps({
        'group': group,
        'frame': idx,
        'field': field,
        'cmap': cmap_override or '',
        'vmin': vmin_override,
        'vmax': vmax_override,
        'filters': filters or {},
        'sweep': requested_sweep,
        'elevation': requested_elevation,
        'derived_product': derived_product or 'PPI',
        'cappi_height_km': cappi_height_km,
        'render_quality': _normalize_render_quality(render_quality),
        'custom_cmap': custom_cmap or {},
    }, sort_keys=True, default=str)
    return md5(raw.encode('utf-8')).hexdigest()

def _make_merged_cache_key(filepaths):
    parts = []
    for entry in filepaths:
        if isinstance(entry, dict):
            parts.append(f"{entry.get('filename','radar.raw')}:{len(entry.get('content', b''))}")
        else:
            parts.append(str(entry))
    return md5("|".join(parts).encode('utf-8')).hexdigest()

def load_pyiris_defaults() -> dict[str, Any]:
    defaults = dict(PYIRIS_DEFAULTS)
    try:
        cfg_path = current_app.config.get('PYIRIS_CONFIG_PATH') or os.path.join(current_app.root_path, 'pyiris.conf')
        if not os.path.exists(cfg_path):
            return defaults
        cfg = configparser.ConfigParser()
        cfg.read(cfg_path)
        filt = cfg['FILTER'] if 'FILTER' in cfg else {}
        mode = cfg['FILTERMODE'] if 'FILTERMODE' in cfg else {}
        ftype = cfg['FILTERTYPE'] if 'FILTERTYPE' in cfg else {}
        defaults['enable_standard_filter'] = str(mode.get('standard_filter', 'disable')).lower() == 'enable'
        defaults['active_LOG'] = True
        defaults['active_SQI'] = True
        defaults['active_PMI'] = True
        defaults['active_CSR'] = True
        defaults['active_SNR'] = True
        defaults['active_PHI'] = True
        defaults['active_SDZ'] = True
        defaults['active_MDZ'] = True
        defaults['enable_speckle_filter'] = str(mode.get('speckle_filter', 'disable')).lower() == 'enable'
        defaults['speckle_type'] = str(ftype.get('speckle', defaults['speckle_type'])).split(',')[0].strip() or defaults['speckle_type']
        defaults['window_size'] = int(filt.get('me_windowSize', defaults['window_size']))
        for k in list(PYIRIS_DEFAULTS.keys()):
            if k.startswith('th_'):
                if k in filt:
                    defaults[k] = float(filt.get(k, defaults[k]))
    except Exception:
        pass
    return defaults

def _uploads_dir() -> str:
    root = current_app.config.get('RADAR_UPLOAD_FOLDER') or os.path.join(current_app.root_path, 'uploads', 'radarviewer')
    os.makedirs(root, exist_ok=True)
    return root

def _cache_dir() -> str:
    root = current_app.config.get('RADAR_RENDER_FOLDER') or os.path.join(current_app.root_path, 'static', 'radar')
    os.makedirs(root, exist_ok=True)
    return root

def _safe_text(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, bytes):
        for enc in ('utf-8', 'latin-1', 'ascii'):
            try:
                return value.decode(enc, errors='ignore').strip()
            except Exception:
                pass
        return str(value).strip()
    text = str(value).strip()
    m = re.fullmatch(r"[bB][\"\'](.*)[\"\']", text)
    if m:
        text = m.group(1).strip()
    return text.strip().strip('\x00').strip()

def _safe_string_attr(value: Any, default: str = '') -> bytes:
    text = _safe_text(value) or default
    return np.bytes_(text)

def _normalize_task_name(task_name: str) -> str:
    task = _safe_text(task_name).upper().replace('\x00', '').strip()
    task = re.sub(r'\s+', '', task)
    task = task.strip("._- ")
    task = re.sub(r'([_-])[A-Z]$', '', task)
    task = re.sub(r'(?<=\d)[A-Z]$', '', task)
    return task or 'UNKNOWN'

def _extract_file_datetime(filename: str) -> datetime | None:
    stem = os.path.basename(filename).split('.')[0].upper()
    m = re.search(r'([A-Z]{3})(\d{12})$', stem)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(2), '%y%m%d%H%M%S')
    except Exception:
        return None

def _extract_sweep_code(filename: str, task_name: str) -> str:
    task_text = _safe_text(task_name).upper()
    m = re.search(r'[_-]([A-Z0-9]+)$', task_text)
    if m:
        return m.group(1)
    m = re.search(r'RAW[A-Z]*([A-Z0-9]+)$', os.path.basename(filename).upper())
    if m:
        return m.group(1)
    return os.path.basename(filename)

def _extract_subscan_letter(task_name: str, sweep_code: str, filename: str) -> str:
    task_text = _safe_text(task_name).upper()
    for pat in (r'[_-]([A-Z])$', r'(?<=\d)([A-Z])$'):
        m = re.search(pat, task_text)
        if m:
            return m.group(1)
    sweep_text = _safe_text(sweep_code).upper()
    m = re.search(r'([A-Z])$', sweep_text)
    if m:
        return m.group(1)
    stem = os.path.basename(filename).split('.')[0].upper()
    m = re.search(r'([A-Z])$', stem)
    return m.group(1) if m else ''

def _subscan_rank(letter: str) -> int:
    if not letter:
        return 999
    c = str(letter).upper()[0]
    return ord(c) - ord('A') + 1 if 'A' <= c <= 'Z' else 999

def _finalize_frame_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    if not items:
        return {'files': [], 'file_count': 0, 'time': 'N/A', 'epoch': None, 'sweeps': [], 'elevations_text': ''}
    preferred = sorted(items, key=lambda x: (_subscan_rank(x.get('subscan_letter', '')), x.get('epoch') if x.get('epoch') is not None else float('inf'), x.get('filename', '')))
    anchor_item = preferred[0]
    sweeps, seen_sweeps, elevations, seen_elev, files = [], set(), [], set(), []
    for item in preferred:
        files.append(item['filepath'])
        code = str(item.get('sweep_code') or '').strip()
        if code and code not in seen_sweeps:
            seen_sweeps.add(code); sweeps.append(code)
        elev = str(item.get('elevations_text') or '').strip()
        if elev and elev not in seen_elev:
            seen_elev.add(elev); elevations.append(elev)
    return {'files': files, 'file_count': len(files), 'time': anchor_item.get('time', 'N/A'), 'epoch': anchor_item.get('epoch'), 'sweeps': sweeps, 'elevations_text': ' | '.join(elevations)}


def _temp_path_from_bytes(filename: str, content: bytes) -> str:
    suffix = os.path.splitext(filename)[1] or '.raw'
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(content)
    tmp.flush()
    tmp.close()
    return tmp.name

def _read_radar_fileentry(fileentry: dict[str, Any]):
    temp_path = _temp_path_from_bytes(fileentry.get('filename', 'radar.raw'), fileentry['content'])
    try:
        radar = _read_radar(temp_path)
    finally:
        try:
            os.remove(temp_path)
        except Exception:
            pass
    return radar

def _fill_nan_nearest_2d(arr):
    arr = np.asarray(arr, dtype=float)
    if arr.ndim != 2:
        return arr
    mask = ~np.isfinite(arr)
    if not mask.any():
        return arr
    if mask.all():
        return arr
    _, indices = distance_transform_edt(mask, return_indices=True)
    filled = arr[tuple(indices)]
    return filled

def _read_radar(filepath: str):
    return pyart.io.read_sigmet(filepath, file_field_names=True, full_xhdr=True, time_ordered='full')

def _get_radar_epoch_and_label(radar, filepath: str) -> tuple[str, float | None]:
    try:
        start = radar.time['units']
        base = start.split('since')[-1].strip()
        base_dt = datetime.fromisoformat(base.replace('Z', ''))
        sweep_time = base_dt + timedelta(seconds=float(radar.time['data'][0]))
        return sweep_time.strftime('%Y-%m-%d %H:%M:%S'), sweep_time.timestamp()
    except Exception:
        file_dt = _extract_file_datetime(os.path.basename(filepath))
        if file_dt:
            return file_dt.strftime('%Y-%m-%d %H:%M:%S'), file_dt.timestamp()
        return 'N/A', None

def _merge_radars(file_entries):
    cache_key = _make_merged_cache_key(file_entries)
    cached = _cache_get(merged_radar_cache, cache_key)
    if cached is not None:
        return cached

    warnings, radars = [], []
    for entry in file_entries:
        try:
            if isinstance(entry, dict) and 'content' in entry:
                radars.append(_read_radar_fileentry(entry))
            else:
                radars.append(_read_radar(entry))
        except Exception as exc:
            name = entry.get('filename', 'radar.raw') if isinstance(entry, dict) else os.path.basename(str(entry))
            warnings.append(f'{name} failed: {exc}')
    if not radars:
        raise RuntimeError('No valid radar files in merged frame')
    merged = radars[0]
    for i, nxt in enumerate(radars[1:], start=1):
        try:
            merged = pyart.util.join_radar(merged, nxt)
        except Exception as exc:
            warnings.append(f'join failed for radar #{i+1}: {exc}')
    _cache_store(merged_radar_cache, cache_key, (merged, warnings), MERGED_CACHE_MAX_ITEMS)
    return merged, warnings

# ---------- pyIRIS-like filters ----------
def std_deviation(arr):
    return np.std(arr)

def std_mean(arr):
    return np.mean(arr)

def sdev_filter(array_in, window_size):
    return generic_filter(array_in, std_deviation, size=window_size, mode='nearest')

def mean_filter(array_in, window_size):
    return generic_filter(array_in, std_mean, size=window_size, mode='nearest')

def box_kernel(size):
    return np.ones((size, size), np.float32) / (size ** 2)

def lee_filter(img, size):
    img_mean = uniform_filter(img, (size, size))
    img_sqr_mean = uniform_filter(img**2, (size, size))
    img_variance = img_sqr_mean - img_mean**2
    overall_variance = variance(img)
    img_weights = img_variance**2 / (img_variance**2 + overall_variance**2 + 1e-12)
    return img_mean + img_weights * (img - img_mean)

def ndwi(data, max_iter=100, threshold=0.1):
    data = np.asarray(data, dtype=float)
    data_min = np.nanmin(data)
    data_max = np.nanmax(data)
    if not np.isfinite(data_min) or not np.isfinite(data_max) or data_max <= data_min:
        return data
    arr = exposure.rescale_intensity(data, in_range=(data_min, data_max), out_range=(0,255)).astype(np.uint8)
    for _ in range(max_iter):
        filtered = cv2.fastNlMeansDenoising(arr)
        mad = np.abs(arr.astype(float) - filtered.astype(float)).mean()
        if mad < threshold or np.var(filtered) < 0.01:
            arr = filtered
            break
        arr = filtered
    return np.interp(arr, [0,255], [data_min, data_max])

def mdfill(data):
    data = np.array(data, dtype=float, copy=True)
    m, n = data.shape
    data[data >= 327] = np.nan
    data[data <= -327] = np.nan
    nanindex = np.where(np.isnan(data))
    for r, c in zip(nanindex[0], nanindex[1]):
        if r != 0 and c != 0 and r != m-1 and c != n-1:
            ne = np.array([
                data[r-1][c-1], data[r-1][c], data[r-1][c+1],
                data[r][c-1], data[r][c+1],
                data[r+1][c-1], data[r+1][c], data[r+1][c+1]
            ], dtype=float)
            valid_n = ne.size - np.count_nonzero(np.isnan(ne))
            if valid_n >= 6:
                data[r][c] = np.nanmean(ne)
    return data

def _moment_category(field_name: str) -> str:
    f = (field_name or '').upper()
    if 'DBZE' in f:
        return 'E'
    if 'DBZ' in f or 'DBT' in f:
        return 'Z'
    if 'VEL' in f:
        return 'V'
    if 'WIDTH' in f:
        return 'W'
    return 'D'

def _threshold_value(name_prefix: str, category: str, filters: dict[str, Any], defaults: dict[str, Any]) -> float:
    key = f'{name_prefix}{category}'
    if key in filters and filters[key] not in ('', None):
        return float(filters[key])
    return float(defaults.get(key, 0.0))

def _field_by_substring(radar, *tokens):
    for name in radar.fields.keys():
        up = name.upper()
        if any(token in up for token in tokens):
            return name
    return None


def _compute_filter_availability(radar, field_name: str) -> dict[str, Any]:
    category = _moment_category(field_name)
    log_field = _field_by_substring(radar, 'LOG')
    sqi_field = _field_by_substring(radar, 'SQI')
    pmi_field = _field_by_substring(radar, 'PMI')
    csr_field = _field_by_substring(radar, 'CSP', 'CSR')
    snr_field = _field_by_substring(radar, 'SNR')
    phi_field = _field_by_substring(radar, 'PHIDP', 'PHI')

    helper_fields = {
        'LOG': log_field,
        'SQI': sqi_field,
        'PMI': pmi_field,
        'CSR': csr_field,
        'SNR': snr_field,
        'PHI': phi_field,
    }

    availability = {
        'field_category': category,
        'field_name': field_name,
        'helper_fields': helper_fields,
        'filters': {
            'LOG': bool(log_field) and category in ('Z', 'E'),
            'SQI': bool(sqi_field) and category in ('V', 'W'),
            'PMI': bool(pmi_field) and category in ('V', 'W'),
            'CSR': bool(csr_field) and category in ('Z', 'E', 'V', 'W'),
            'SNR': bool(snr_field) and category in ('Z', 'E', 'V', 'W'),
            'PHI': bool(phi_field) and category in ('Z', 'E'),
            'SDZ': category in ('Z', 'E'),
            'MDZ': category in ('Z', 'E'),
            'SPECKLE': True,
        }
    }
    return availability

def _apply_pyiris_filters(radar, field_name: str, data_in, filters: dict[str, Any], defaults: dict[str, Any]):
    data = _safe_masked_to_float(np.ma.array(data_in).copy())
    warnings = []
    category = _moment_category(field_name)
    enable_standard = bool(filters.get('enable_standard_filter'))
    enable_speckle = bool(filters.get('enable_speckle_filter'))
    window_size = max(3, int(float(filters.get('window_size') or defaults['window_size'])))
    if window_size % 2 == 0:
        window_size += 1

    if enable_standard:
        active_log = bool(filters.get('active_LOG', defaults.get('active_LOG', True)))
        active_sqi = bool(filters.get('active_SQI', defaults.get('active_SQI', True)))
        active_pmi = bool(filters.get('active_PMI', defaults.get('active_PMI', True)))
        active_csr = bool(filters.get('active_CSR', defaults.get('active_CSR', True)))
        active_snr = bool(filters.get('active_SNR', defaults.get('active_SNR', True)))
        active_phi = bool(filters.get('active_PHI', defaults.get('active_PHI', True)))
        active_sdz = bool(filters.get('active_SDZ', defaults.get('active_SDZ', True)))
        active_mdz = bool(filters.get('active_MDZ', defaults.get('active_MDZ', True)))

        log_field = _field_by_substring(radar, 'LOG')
        sqi_field = _field_by_substring(radar, 'SQI')
        pmi_field = _field_by_substring(radar, 'PMI')
        csr_field = _field_by_substring(radar, 'CSP', 'CSR')
        snr_field = _field_by_substring(radar, 'SNR')
        phi_field = _field_by_substring(radar, 'PHIDP', 'PHI')

        if active_log:
            if category in ('Z', 'E') and log_field:
                thr = _threshold_value('th_LOG', category, filters, defaults)
                data[np.asarray(radar.fields[log_field]['data'], dtype=float) < thr] = np.nan
            elif category in ('Z', 'E') and filters.get('warn_missing_helpers', True) and not log_field:
                warnings.append('LOG field not available')

        if active_sqi:
            if category in ('V', 'W') and sqi_field:
                thr = _threshold_value('th_SQI', category, filters, defaults)
                data[np.asarray(radar.fields[sqi_field]['data'], dtype=float) < thr] = np.nan
            elif category in ('V', 'W') and filters.get('warn_missing_helpers', True) and not sqi_field:
                warnings.append('SQI field not available')

        if active_pmi:
            if category in ('V', 'W') and pmi_field:
                thr = _threshold_value('th_PMI', category, filters, defaults)
                data[np.asarray(radar.fields[pmi_field]['data'], dtype=float) < thr] = np.nan
            elif category in ('V', 'W') and filters.get('warn_missing_helpers', True) and not pmi_field:
                warnings.append('PMI field not available')

        if active_csr:
            if category in ('Z', 'E', 'V', 'W') and csr_field:
                thr = _threshold_value('th_CSR', category, filters, defaults)
                data[np.asarray(radar.fields[csr_field]['data'], dtype=float) > thr] = np.nan
            elif category in ('Z', 'E', 'V', 'W') and filters.get('warn_missing_helpers', True) and not csr_field:
                warnings.append('CSR/CSP field not available')

        if active_snr:
            if category in ('Z', 'E', 'V', 'W') and snr_field:
                base_thr = _threshold_value('th_SNLG', category, filters, defaults)
                snr_arr = np.asarray(radar.fields[snr_field]['data'], dtype=float)
                try:
                    conv_thr = 10 * np.log10(np.maximum(10**(base_thr/10) - 1, 1e-6))
                except Exception:
                    conv_thr = base_thr
                data[snr_arr < conv_thr] = np.nan
            elif category in ('Z', 'E', 'V', 'W') and filters.get('warn_missing_helpers', True) and not snr_field:
                warnings.append('SNR field not available')

        if active_phi:
            if category in ('Z', 'E') and phi_field:
                phi_arr = np.asarray(radar.fields[phi_field]['data'], dtype=float)
                phi_sd = sdev_filter(phi_arr, (3, 3))
                if snr_field:
                    snr_arr = np.asarray(radar.fields[snr_field]['data'], dtype=float)
                    th1 = float(filters.get('th_PHID1') or defaults['th_PHID1'])
                    th2 = float(filters.get('th_PHID2') or defaults['th_PHID2'])
                    snrd = float(filters.get('th_SNRD') or defaults['th_SNRD'])
                    mask = np.logical_or(np.logical_and(snr_arr > snrd, phi_sd > th1), np.logical_and(snr_arr <= snrd, phi_sd > th2))
                else:
                    th2 = float(filters.get('th_PHID2') or defaults['th_PHID2'])
                    mask = phi_sd > th2
                data[mask] = np.nan
            elif category in ('Z', 'E') and filters.get('warn_missing_helpers', True) and not phi_field:
                warnings.append('PHIDP field not available')

        if category in ('Z', 'E'):
            temp = data.copy()
            temp[np.isnan(temp)] = -327
            if active_sdz:
                th_sdz = float(filters.get('th_SDZ') or defaults['th_SDZ'])
                sd = sdev_filter(temp, (3, 3))
                data[np.logical_or(sd == 0, sd > th_sdz)] = np.nan
            if active_mdz:
                th_mdz = float(filters.get('th_MDZ') or defaults['th_MDZ'])
                md = median_filter(temp, size=3)
                data[md < th_mdz] = np.nan

    if enable_speckle:
        kind = str(filters.get('speckle_type') or defaults['speckle_type']).strip().lower()
        try:
            if kind == 'median':
                data = median_filter(data, size=window_size)
            elif kind == 'mean':
                data = cv2.filter2D(data.astype(np.float32), -1, box_kernel(window_size))
            elif kind == 'stdmean':
                data = mean_filter(data, window_size)
            elif kind == 'lee':
                data = lee_filter(data, window_size)
            elif kind == 'ndwi':
                data = ndwi(data)
            elif kind == 'mdfill':
                data = mdfill(data)
        except Exception as exc:
            warnings.append(f'Speckle filter failed: {exc}')

    return np.ma.masked_invalid(data), warnings

def _extract_active_sweep_options(radar) -> list[dict[str, Any]]:
    options = []
    try:
        fixed = np.asarray(radar.fixed_angle['data']).astype(float).tolist()
    except Exception:
        fixed = []
    if fixed:
        seen = set()
        for idx, elev in enumerate(fixed):
            elev_round = round(float(elev), 2)
            key = f'{elev_round:.2f}'
            if key in seen:
                continue
            seen.add(key)
            options.append({'index': idx, 'label': f'{elev_round:g}°', 'elevation': elev_round})
    else:
        count = int(getattr(radar, 'nsweeps', 1) or 1)
        for idx in range(max(1, count)):
            options.append({'index': idx, 'label': f'Sweep {idx + 1}', 'elevation': None})
    return options

def _resolve_sweep_index(radar, requested_sweep: str | None, requested_elevation: str | None):
    options = _extract_active_sweep_options(radar)
    if not options:
        return 0, [], None
    chosen_idx = 0
    chosen_elev = options[0].get('elevation')
    if requested_sweep not in (None, '', 'all'):
        try:
            req_idx = int(requested_sweep)
            if 0 <= req_idx < len(options):
                chosen_idx = req_idx
                return chosen_idx, options, options[chosen_idx].get('elevation')
        except Exception:
            pass
    if requested_elevation not in (None, '', 'all'):
        try:
            target = float(requested_elevation)
            best = min(options, key=lambda opt: abs((opt.get('elevation') if opt.get('elevation') is not None else target) - target))
            chosen_idx = int(best['index'])
            return chosen_idx, options, best.get('elevation')
        except Exception:
            pass
    return chosen_idx, options, chosen_elev

def _render_radar_png_from_files(
    filepaths,
    field,
    cmap_override='',
    vmin_override=None,
    vmax_override=None,
    filters=None,
    requested_sweep=None,
    requested_elevation=None,
    derived_product='PPI',
    cappi_height_km=2.0,
    render_quality='high',
    custom_cmap=None,
):
    filters = filters or {}
    render_quality = _normalize_render_quality(render_quality)
    quality_cfg = _quality_settings(render_quality)
    radar, warnings = _merge_radars(filepaths)
    if field not in radar.fields:
        field = 'DBZ2' if 'DBZ2' in radar.fields else list(radar.fields.keys())[0]

    selected_sweep_idx, sweep_options, selected_elevation = _resolve_sweep_index(radar, requested_sweep, requested_elevation)
    raw_data = radar.fields[field]['data'].copy()
    filtered_data, filter_warnings = _apply_pyiris_filters(radar, field, raw_data, filters, load_pyiris_defaults())
    warnings.extend(filter_warnings)
    if filters.get('clipRange') and vmin_override is not None and vmax_override is not None:
        filtered_data = np.ma.array(np.clip(filtered_data.filled(np.nan), vmin_override, vmax_override))
    radar.fields[field]['data'] = np.ma.masked_invalid(filtered_data)

    product_used = (derived_product or 'PPI').upper()
    cfg = default_configs.get(field, {})
    if vmin_override is None:
        vmin_override = cfg.get('vmin')
    if vmax_override is None:
        vmax_override = cfg.get('vmax')

    cmap_obj = _resolve_cmap(field, cmap_override, custom_cmap)

    try:
        lat0 = float(radar.latitude['data'][0])
        lon0 = float(radar.longitude['data'][0])
    except Exception:
        lat0, lon0 = 0.0, 0.0

    try:
        rng_km = float(radar.range['data'][-1]) / 1000.0
    except Exception:
        rng_km = 250.0

    fig, ax = plt.subplots(figsize=(quality_cfg['figsize'], quality_cfg['figsize']))
    extent = None

    if product_used in ('MAX', 'CAPPI', 'SRI', 'ACC', 'ETOP', 'EBASE', 'ETHICK', 'LMEAN', 'VIL'):
        try:
            grid_limit_xy = rng_km * 1000.0
            if product_used == 'MAX':
                z_top = max(12000.0, float(getattr(radar, 'nsweeps', 1) or 1) * 1000.0)
                grid = pyart.map.grid_from_radars(
                    (radar,),
                    grid_shape=(quality_cfg['vertical_levels'], quality_cfg['max_cappi_grid'], quality_cfg['max_cappi_grid']),
                    grid_limits=((0.0, z_top), (-grid_limit_xy, grid_limit_xy), (-grid_limit_xy, grid_limit_xy)),
                    fields=[field],
                    weighting_function='Barnes2',
                )
                arr3 = np.ma.filled(grid.fields[field]['data'], np.nan)
                arr2 = np.nanmax(arr3, axis=0)
                arr2 = np.ma.masked_invalid(arr2)
            elif product_used in ('SRI', 'ACC'):
                rain_field = _choose_reflectivity_field(radar, preferred=field)
                selected_sweep_idx = 0
                raw_rain = np.ma.filled(radar.fields[rain_field]['data'], np.nan).astype(float)
                filtered_rain, rain_warnings = _apply_pyiris_filters(radar, rain_field, raw_rain, filters, load_pyiris_defaults())
                warnings.extend(rain_warnings)
                radar.fields[rain_field]['data'] = np.ma.masked_invalid(filtered_rain)
                field = rain_field

                if product_used == 'SRI':
                    if vmin_override is None:
                        vmin_override = 0.0
                    if vmax_override is None:
                        vmax_override = 150.0
                    temp_field = '__SRI__'
                    rain_data = _dbz_to_rain_rate(np.ma.filled(radar.fields[rain_field]['data'], np.nan))
                    units = 'mm/h'
                    long_name = 'surface_rainfall_intensity'
                    standard_name = 'rainfall_rate'
                else:
                    if vmin_override is None:
                        vmin_override = 0.0
                    if vmax_override is None:
                        vmax_override = max(5.0, float(merge_window_minutes) * 2.5)
                    temp_field = '__ACC__'
                    rain_rate = _dbz_to_rain_rate(np.ma.filled(radar.fields[rain_field]['data'], np.nan))
                    rain_data = _estimate_accumulation_from_rate(rain_rate, float(merge_window_minutes))
                    units = 'mm'
                    long_name = 'estimated_rain_accumulation'
                    standard_name = 'lwe_thickness_of_precipitation_amount'

                if not cmap_override and not custom_cmap:
                    cmap_obj = plt.get_cmap('turbo').copy()
                    try:
                        cmap_obj.set_bad(alpha=0.0)
                    except Exception:
                        pass

                display = pyart.graph.RadarDisplay(radar)
                radar.fields[temp_field] = {
                    **radar.fields[rain_field],
                    'data': np.ma.masked_invalid(rain_data),
                    'units': units,
                    'long_name': long_name,
                    'standard_name': standard_name,
                }
                display.plot(
                    temp_field,
                    selected_sweep_idx,
                    ax=ax,
                    colorbar_flag=False,
                    vmin=vmin_override,
                    vmax=vmax_override,
                    cmap=cmap_obj,
                    title_flag=False
                )
                ax.axis('off')
                d = rng_km / 111.0
                extent = [lat0 - d, lon0 - d, lat0 + d, lon0 + d]
            else:
                grid, arr3, z_levels = _build_cartesian_grid_for_product(radar, field, quality_cfg, rng_km, extra_z_levels=2)

                if product_used == 'CAPPI':
                    cappi_h = float(cappi_height_km) * 1000.0
                    if arr3.ndim != 3 or arr3.shape[0] < 2:
                        arr2 = np.squeeze(arr3)
                    else:
                        target = float(np.clip(cappi_h, z_levels.min(), z_levels.max()))
                        hi = int(np.searchsorted(z_levels, target, side='left'))
                        hi = max(1, min(hi, len(z_levels) - 1))
                        lo = hi - 1

                        z0 = float(z_levels[lo])
                        z1 = float(z_levels[hi])
                        a0 = arr3[lo]
                        a1 = arr3[hi]

                        if z1 <= z0:
                            arr2 = a0
                        else:
                            w = (target - z0) / (z1 - z0)
                            arr2 = (1.0 - w) * a0 + w * a1

                        arr2 = _fill_nan_nearest_2d(arr2)
                        arr2 = np.asarray(arr2, dtype=float)
                else:
                    arr2, derived_unit, auto_vmin, auto_vmax = _compute_vertical_derived_array(arr3, z_levels, product_used)
                    if vmin_override is None and auto_vmin is not None:
                        vmin_override = auto_vmin
                    if vmax_override is None and auto_vmax is not None:
                        vmax_override = auto_vmax
                    if not cmap_override and not custom_cmap and product_used in ('ETOP', 'EBASE', 'ETHICK', 'VIL'):
                        cmap_obj = plt.get_cmap('turbo').copy()
                        try:
                            cmap_obj.set_bad(alpha=0.0)
                        except Exception:
                            pass

                ny, nx = arr2.shape
                x = np.linspace(-rng_km, rng_km, nx)
                y = np.linspace(-rng_km, rng_km, ny)
                xx, yy = np.meshgrid(x, y)
                rr = np.sqrt(xx**2 + yy**2)
                arr2 = np.where(rr <= rng_km, arr2, np.nan)
                arr2 = np.ma.masked_invalid(arr2)

            if product_used not in ('SRI', 'ACC'):
                ax.imshow(
                    arr2,
                    origin='lower',
                    extent=[-rng_km, rng_km, -rng_km, rng_km],
                    cmap=cmap_obj,
                    vmin=vmin_override,
                    vmax=vmax_override,
                    interpolation='nearest',
                    aspect='equal'
                )
                ax.axis('off')
                dlat = rng_km / 111.0
                dlon = rng_km / max(0.1, 111.0 * np.cos(np.deg2rad(lat0)))
                extent = [lat0 - dlat, lon0 - dlon, lat0 + dlat, lon0 + dlon]
        except Exception as exc:
            warnings.append(f'{product_used} generation failed, fallback to PPI: {exc}')
            product_used = 'PPI'

    if product_used == 'PPI':
        display = pyart.graph.RadarDisplay(radar)
        display.plot(
            field,
            selected_sweep_idx,
            ax=ax,
            colorbar_flag=False,
            vmin=vmin_override,
            vmax=vmax_override,
            cmap=cmap_obj,
            title_flag=False
        )
        ax.axis('off')
        d = rng_km / 111.0
        extent = [lat0 - d, lon0 - d, lat0 + d, lon0 + d]

    buf = BytesIO()
    plt.savefig(buf, format='png', dpi=quality_cfg['dpi'], bbox_inches='tight', pad_inches=0, transparent=True)
    plt.close(fig)
    buf.seek(0)
    return buf, extent, radar, warnings, selected_sweep_idx, sweep_options, selected_elevation, product_used


def _latlon_to_local_km(lat0: float, lon0: float, lat: np.ndarray, lon: np.ndarray):
    lat = np.asarray(lat, dtype=float)
    lon = np.asarray(lon, dtype=float)
    y = (lat - lat0) * 111.32
    x = (lon - lon0) * 111.32 * np.cos(np.deg2rad(lat0))
    return x, y


def _circular_abs_diff_deg(a, b):
    diff = np.abs(np.asarray(a, dtype=float) - np.asarray(b, dtype=float))
    return np.minimum(diff, 360.0 - diff)




def _weighted_smooth_nan_2d(arr: Any, sigma: float = 1.0):
    arr = np.asarray(arr, dtype=float)
    if arr.ndim != 2 or sigma <= 0.01:
        return arr
    valid = np.isfinite(arr)
    if not np.any(valid):
        return arr
    work = np.where(valid, arr, 0.0)
    weights = valid.astype(float)
    work_s = gaussian_filter(work, sigma=sigma)
    weights_s = gaussian_filter(weights, sigma=sigma)
    with np.errstate(invalid='ignore', divide='ignore'):
        out = work_s / weights_s
    return np.where(weights_s > 0.03, out, np.nan)


def _prepare_3d_volume_data(data: Any, interpolate: bool = False, gap_fill: bool = False):
    arr = np.asarray(data, dtype=float)
    if arr.ndim != 2 or arr.size == 0:
        return arr
    if not interpolate and not gap_fill:
        return arr

    base = arr.copy()
    if gap_fill:
        try:
            base = _fill_nan_nearest_2d(base)
        except Exception:
            pass

    if interpolate:
        sigma = 1.1 if gap_fill else 0.75
        smooth = _weighted_smooth_nan_2d(base, sigma=sigma)
        if gap_fill:
            base = np.where(np.isfinite(smooth), smooth, base)
        else:
            original_valid = np.isfinite(arr)
            expanded = _weighted_smooth_nan_2d(arr, sigma=0.55)
            base = np.where(original_valid, smooth, expanded)

    return base

def _render_cross_section_png_from_files(
    filepaths,
    field,
    start_lat,
    start_lon,
    end_lat,
    end_lon,
    cmap_override='',
    vmin_override=None,
    vmax_override=None,
    filters=None,
    render_quality='high',
    custom_cmap=None,
    x_min=None,
    x_max=None,
    y_min=None,
    y_max=None,
    interpolate=False,
    gap_fill=False,
    smooth_interp=1.0,
    smooth_gap=1.0,
):
    filters = filters or {}
    render_quality = _normalize_render_quality(render_quality)
    quality_cfg = _quality_settings(render_quality)
    try:
        smooth_interp = float(smooth_interp)
    except Exception:
        smooth_interp = 1.0
    try:
        smooth_gap = float(smooth_gap)
    except Exception:
        smooth_gap = 1.0
    smooth_interp = float(np.clip(smooth_interp, 0.0, 10.0))
    smooth_gap = float(np.clip(smooth_gap, 0.0, 50.0))
    radar, warnings = _merge_radars(filepaths)
    if field not in radar.fields:
        field = 'DBZ2' if 'DBZ2' in radar.fields else list(radar.fields.keys())[0]

    raw_data = radar.fields[field]['data'].copy()
    filtered_data, filter_warnings = _apply_pyiris_filters(radar, field, raw_data, filters, load_pyiris_defaults())
    warnings.extend(filter_warnings)
    radar.fields[field]['data'] = np.ma.masked_invalid(filtered_data)

    cfg = default_configs.get(field, {})
    if vmin_override is None:
        vmin_override = cfg.get('vmin')
    if vmax_override is None:
        vmax_override = cfg.get('vmax')

    cmap_obj = _resolve_cmap(field, cmap_override, custom_cmap)

    lat0 = float(radar.latitude['data'][0])
    lon0 = float(radar.longitude['data'][0])
    alt0_km = float(np.asarray(radar.altitude['data']).ravel()[0]) / 1000.0 if 'altitude' in radar.__dict__ or getattr(radar, 'altitude', None) is not None else 0.0

    n_samples = max(160, min(400, int(quality_cfg['ppi_grid'] * 0.25)))
    lats = np.linspace(float(start_lat), float(end_lat), n_samples)
    lons = np.linspace(float(start_lon), float(end_lon), n_samples)
    sx, sy = _latlon_to_local_km(lat0, lon0, lats, lons)
    dist_from_start = np.sqrt((sx - sx[0])**2 + (sy - sy[0])**2)
    ranges_km = np.sqrt(sx**2 + sy**2)
    azimuths = (np.degrees(np.arctan2(sx, sy)) + 360.0) % 360.0

    max_range_km = float(np.nanmax(np.asarray(radar.range['data'], dtype=float)) / 1000.0)
    valid_line = np.isfinite(ranges_km) & (ranges_km <= max_range_km)
    if not np.any(valid_line):
        raise RuntimeError('Cross section line is outside radar coverage')

    range_data_km = np.asarray(radar.range['data'], dtype=float) / 1000.0
    sweep_starts = np.asarray(radar.sweep_start_ray_index['data'], dtype=int)
    sweep_ends = np.asarray(radar.sweep_end_ray_index['data'], dtype=int)
    fixed_angles = np.asarray(radar.fixed_angle['data'], dtype=float) if getattr(radar, 'fixed_angle', None) is not None else np.zeros(len(sweep_starts), dtype=float)
    data = np.ma.filled(radar.fields[field]['data'], np.nan).astype(float)

    xs, zs, vals = [], [], []
    for sweep_idx, (rs, re) in enumerate(zip(sweep_starts, sweep_ends)):
        az = np.asarray(radar.azimuth['data'][rs:re+1], dtype=float)
        if az.size == 0:
            continue
        elev_deg = float(fixed_angles[min(sweep_idx, len(fixed_angles)-1)]) if fixed_angles.size else 0.0
        ray_sel_local = np.argmin(_circular_abs_diff_deg(az[:, None], azimuths[None, :]), axis=0)
        ray_sel = rs + ray_sel_local
        gate_sel = np.abs(range_data_km[:, None] - ranges_km[None, :]).argmin(axis=0)
        sample_vals = data[ray_sel, gate_sel]
        heights_km = alt0_km + ranges_km * np.sin(np.deg2rad(elev_deg))
        valid = valid_line & np.isfinite(sample_vals) & np.isfinite(heights_km)
        if np.any(valid):
            xs.append(dist_from_start[valid])
            zs.append(heights_km[valid])
            vals.append(sample_vals[valid])

    if not xs:
        raise RuntimeError('No valid samples found for cross section')

    x = np.concatenate(xs)
    z = np.concatenate(zs)
    v = np.concatenate(vals)
    line_length_km = float(dist_from_start[-1])

    fig_w = max(7.0, quality_cfg['figsize'])
    fig_h = max(4.2, quality_cfg['figsize'] * 0.58)
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    ax.set_xlabel('Distance along section (km)')
    ax.set_ylabel('Approx. height (km)')
    ax.set_title(f'Cross Section {field}')

    default_xmin = 0.0
    default_xmax = max(1.0, line_length_km)
    try:
        z_p98 = float(np.nanpercentile(z, 98))
    except Exception:
        z_p98 = float('nan')
    ymax = z_p98 if np.isfinite(z_p98) else max(5.0, float(np.nanmax(z)))
    default_ymin = 0.0
    default_ymax = max(2.0, ymax * 1.08)

    def _sanitize_axis_limits(lo, hi, data_lo, data_hi, min_span):
        lo = data_lo if lo in (None, '') else float(lo)
        hi = data_hi if hi in (None, '') else float(hi)
        if not np.isfinite(lo):
            lo = data_lo
        if not np.isfinite(hi):
            hi = data_hi
        lo = max(data_lo, min(lo, data_hi))
        hi = max(data_lo, min(hi, data_hi))
        if hi - lo < min_span:
            center = (lo + hi) / 2.0
            half = min_span / 2.0
            lo = max(data_lo, center - half)
            hi = min(data_hi, center + half)
            if hi - lo < min_span:
                lo = data_lo
                hi = data_hi
        return lo, hi

    x_lo, x_hi = _sanitize_axis_limits(x_min, x_max, default_xmin, default_xmax, 0.25)
    y_lo, y_hi = _sanitize_axis_limits(y_min, y_max, default_ymin, default_ymax, 0.10)

    ax.set_xlim(x_lo, x_hi)
    ax.set_ylim(bottom=y_lo, top=y_hi)

    if interpolate:
        valid_interp = np.isfinite(x) & np.isfinite(z) & np.isfinite(v)
        interp_points = np.column_stack((x[valid_interp], z[valid_interp]))
        interp_values = v[valid_interp]

        def _weighted_smooth(arr, sigma):
            valid_mask_local = np.isfinite(arr)
            if not np.any(valid_mask_local) or sigma <= 0.01:
                return arr
            tmp = np.where(valid_mask_local, arr, 0.0)
            w = valid_mask_local.astype(float)
            tmp_s = gaussian_filter(tmp, sigma=sigma)
            w_s = gaussian_filter(w, sigma=sigma)
            with np.errstate(invalid='ignore', divide='ignore'):
                arr_s = tmp_s / w_s
            return np.where(w_s > 0.03, arr_s, np.nan)

        if interp_points.shape[0] < 4:
            warnings.append('Cross-section interpolation has too few valid samples, showing raw scatter only')
            sc = ax.scatter(x, z, c=v, s=10, marker='s', cmap=cmap_obj, vmin=vmin_override, vmax=vmax_override, linewidths=0)
        else:
            sparse_mode = interp_points.shape[0] < 150
            grid_nx = max(260, min(900, int(quality_cfg['ppi_grid'] * 0.70)))
            grid_nz = max(180, min(520, int(grid_nx * 0.62)))
            if sparse_mode:
                grid_nx = max(220, int(grid_nx * 0.86))
                grid_nz = max(160, int(grid_nz * 0.86))
            grid_x = np.linspace(x_lo, x_hi, grid_nx)
            grid_z = np.linspace(y_lo, y_hi, grid_nz)
            gx, gz = np.meshgrid(grid_x, grid_z)

            try:
                linear_ok = interp_points.shape[0] >= 8
                if linear_ok:
                    grid_linear = griddata(interp_points, interp_values, (gx, gz), method='linear')
                else:
                    grid_linear = np.full_like(gx, np.nan, dtype=float)
            except Exception as exc:
                warnings.append(f'Cross-section linear interpolation failed: {exc}')
                grid_linear = np.full_like(gx, np.nan, dtype=float)

            try:
                grid_nearest = griddata(interp_points, interp_values, (gx, gz), method='nearest')
            except Exception as exc:
                warnings.append(f'Cross-section nearest interpolation failed: {exc}')
                grid_nearest = np.full_like(gx, np.nan, dtype=float)

            try:
                tree = cKDTree(interp_points)
                query_points = np.column_stack((gx.ravel(), gz.ravel()))
                nearest_dist, _ = tree.query(query_points, k=1)
                nearest_dist = nearest_dist.reshape(gx.shape)

                nn_spacing = 0.0
                if interp_points.shape[0] >= 3:
                    try:
                        k = min(3, interp_points.shape[0])
                        pt_dists, _ = tree.query(interp_points, k=k)
                        if np.ndim(pt_dists) == 2 and pt_dists.shape[1] >= 2:
                            local_spacing = pt_dists[:, 1]
                            local_spacing = local_spacing[np.isfinite(local_spacing) & (local_spacing > 0)]
                            if local_spacing.size:
                                nn_spacing = float(np.nanmedian(local_spacing))
                    except Exception:
                        nn_spacing = 0.0

                dx = (x_hi - x_lo) / max(grid_nx - 1, 1)
                dz = (y_hi - y_lo) / max(grid_nz - 1, 1)
                base_support = max(dx * 3.0, dz * 3.0, 0.12)
                if nn_spacing > 0:
                    base_support = max(base_support, nn_spacing * (1.10 if sparse_mode else 0.90))
                support_radius = base_support * (1.75 if sparse_mode else 1.0)
                gap_fill_radius = support_radius * (2.6 if sparse_mode else 1.65)
                support_mask = nearest_dist <= support_radius
                gap_fill_mask = nearest_dist <= gap_fill_radius
            except Exception as exc:
                warnings.append(f'Cross-section support mask skipped: {exc}')
                support_mask = np.isfinite(grid_linear) | np.isfinite(grid_nearest)
                gap_fill_mask = support_mask.copy()
                sparse_mode = True

            if sparse_mode:
                sigma_sparse = 0.32 + (smooth_interp * 0.90)
                grid_v = np.where(support_mask, grid_nearest, np.nan)
                grid_v = _weighted_smooth(grid_v, sigma_sparse)
                if np.any(np.isfinite(grid_linear)):
                    linear_soft = _weighted_smooth(np.where(support_mask, grid_linear, np.nan), max(0.20, sigma_sparse * 0.75))
                    grid_v = np.where(np.isfinite(linear_soft), linear_soft, grid_v)
                if gap_fill:
                    expanded = np.where(gap_fill_mask, grid_nearest, np.nan)
                    expanded = _weighted_smooth(expanded, 0.40 + (smooth_gap * 0.95))
                    grid_v = np.where(np.isfinite(grid_v), grid_v, expanded)
                warnings.append('Cross-section sparse interpolation mode applied')
            else:
                grid_v = np.where(support_mask, grid_linear, np.nan)
                try:
                    sigma_pre = 0.12 + (smooth_interp * (0.60 if not gap_fill else 0.72))
                    grid_v = _weighted_smooth(grid_v, sigma_pre)
                    grid_v = np.where(support_mask, grid_v, np.nan)
                except Exception as exc:
                    warnings.append(f'Cross-section smoothing skipped: {exc}')

                if gap_fill:
                    try:
                        nearest_v = np.where(gap_fill_mask, grid_nearest, np.nan)
                        grid_v = np.where(np.isfinite(grid_v), grid_v, nearest_v)
                        sigma_post = 0.18 + (smooth_gap * 0.82)
                        grid_v = _weighted_smooth(grid_v, sigma_post)
                    except Exception as exc:
                        warnings.append(f'Cross-section gap fill skipped: {exc}')

            masked_grid = np.ma.masked_invalid(grid_v)
            sc = ax.imshow(
                masked_grid,
                origin='lower',
                extent=[x_lo, x_hi, y_lo, y_hi],
                cmap=cmap_obj,
                vmin=vmin_override,
                vmax=vmax_override,
                interpolation='bilinear',
                aspect='auto'
            )
    else:
        sc = ax.scatter(x, z, c=v, s=10, marker='s', cmap=cmap_obj, vmin=vmin_override, vmax=vmax_override, linewidths=0)

    ax.grid(True, alpha=0.2)
    cbar = fig.colorbar(sc, ax=ax, pad=0.02)
    cbar.ax.tick_params(labelsize=8)
    buf = BytesIO()
    plt.tight_layout()
    plt.savefig(buf, format='png', dpi=quality_cfg['dpi'], bbox_inches='tight', facecolor='white')
    plt.close(fig)
    buf.seek(0)
    meta = {
        'line_length_km': float(line_length_km),
        'x_min': float(x_lo),
        'x_max': float(x_hi),
        'y_min': float(y_lo),
        'y_max': float(y_hi),
        'default_x_min': float(default_xmin),
        'default_x_max': float(default_xmax),
        'default_y_min': float(default_ymin),
        'default_y_max': float(default_ymax),
        'interpolate': bool(interpolate),
        'gap_fill': bool(gap_fill),
        'smooth_interp': float(smooth_interp),
        'smooth_gap': float(smooth_gap),
    }
    return buf, warnings, meta





def _pyiris_fill_missing_rays_by_raycount(radar, target_rays: int | None = None):
    fields = list(radar.fields.keys())
    filled_fields = {f: [] for f in fields}
    filled_azimuth = []
    filled_elevation = []
    filled_time = []
    sweep_start = []
    sweep_end = []
    ray_idx = 0

    for sweep in range(int(getattr(radar, 'nsweeps', 0) or 0)):
        start = int(radar.sweep_start_ray_index['data'][sweep])
        end = int(radar.sweep_end_ray_index['data'][sweep]) + 1
        az = np.asarray(radar.azimuth['data'][start:end], dtype=float)
        elev = np.asarray(radar.elevation['data'][start:end], dtype=float)
        t = np.asarray(radar.time['data'][start:end], dtype=float)
        if az.size == 0:
            continue

        target_rays_sweep = int(target_rays) if target_rays is not None else int(az.size)
        n_missing = max(0, target_rays_sweep - int(az.size))
        az_list = list(az)
        elev_list = list(elev)
        time_list = list(t)

        field_lists = {}
        for f in fields:
            field_slice = _safe_masked_to_float(radar.fields[f]['data'][start:end])
            if field_slice.ndim == 1:
                field_slice = field_slice[:, np.newaxis]
            field_lists[f] = [np.asarray(row, dtype=float) for row in field_slice]

        for _ in range(n_missing):
            current_az = np.asarray(az_list, dtype=float)
            if current_az.size == 0:
                break

            az_extended = np.append(current_az, current_az[0] + 360.0)
            gaps = np.diff(az_extended)
            gap_idx = int(np.argmax(gaps))
            next_idx = (gap_idx + 1) % len(current_az)

            az1 = current_az[gap_idx]
            az2 = current_az[next_idx]
            gap_diff = (az2 - az1) % 360.0
            new_az = (az1 + gap_diff / 2.0) % 360.0
            insert_idx = gap_idx + 1

            az_list.insert(insert_idx, float(new_az))
            elev_list.insert(insert_idx, float(np.nanmedian(elev_list)) if elev_list else 0.0)
            nearest_time = time_list[max(0, min(insert_idx - 1, len(time_list) - 1))] if time_list else 0.0
            time_list.insert(insert_idx, float(nearest_time))

            for f in fields:
                rows = field_lists[f]
                if not rows:
                    continue
                val1 = np.asarray(rows[gap_idx], dtype=float)
                val2 = np.asarray(rows[next_idx], dtype=float)
                interp_row = np.nanmean(np.vstack([val1, val2]), axis=0)
                rows.insert(insert_idx, np.asarray(interp_row, dtype=float))

        filled_azimuth.extend(az_list)
        filled_elevation.extend(elev_list)
        filled_time.extend(time_list)

        for f in fields:
            rows = field_lists[f]
            if not rows:
                continue
            arr = np.vstack([np.asarray(row, dtype=float) for row in rows]).astype(np.float32)
            filled_fields[f].append(np.ma.masked_invalid(arr))

        sweep_start.append(ray_idx)
        ray_idx += len(az_list)
        sweep_end.append(ray_idx - 1)

    new_radar = radar.deepcopy() if hasattr(radar, 'deepcopy') else __import__('copy').deepcopy(radar)
    new_radar.azimuth['data'] = np.asarray(filled_azimuth, dtype=np.float32)
    new_radar.elevation['data'] = np.asarray(filled_elevation, dtype=np.float32)
    new_radar.time['data'] = np.asarray(filled_time, dtype=np.float64)
    new_radar.sweep_start_ray_index['data'] = np.asarray(sweep_start, dtype=np.int32)
    new_radar.sweep_end_ray_index['data'] = np.asarray(sweep_end, dtype=np.int32)
    if hasattr(new_radar, 'rays_per_sweep') and isinstance(new_radar.rays_per_sweep, dict):
        new_radar.rays_per_sweep['data'] = np.asarray([(e - s + 1) for s, e in zip(sweep_start, sweep_end)], dtype=np.int32)
    for f in fields:
        if filled_fields[f]:
            new_radar.fields[f]['data'] = np.ma.vstack(filled_fields[f]).astype(np.float32)
    return new_radar


def _pyiris_rpm_check(radar):
    new_radar = radar.deepcopy() if hasattr(radar, 'deepcopy') else __import__('copy').deepcopy(radar)
    sweep_rpms = []
    for sweep in range(int(getattr(new_radar, 'nsweeps', 0) or 0)):
        start = int(new_radar.sweep_start_ray_index['data'][sweep])
        end = int(new_radar.sweep_end_ray_index['data'][sweep]) + 1
        time_sweep = np.asarray(new_radar.time['data'][start:end], dtype=float)
        if time_sweep.size < 2:
            continue
        duration = float(time_sweep[-1] - time_sweep[0])
        if duration > 0:
            sweep_rpms.append(60.0 / duration)
    if not sweep_rpms:
        return new_radar
    target_rpm = float(np.nanmin(sweep_rpms))
    new_time_data = np.asarray(new_radar.time['data'], dtype=float).copy()
    ray_ptr = 0
    for sweep in range(int(getattr(new_radar, 'nsweeps', 0) or 0)):
        start = int(new_radar.sweep_start_ray_index['data'][sweep])
        end = int(new_radar.sweep_end_ray_index['data'][sweep]) + 1
        nsweep_rays = max(1, end - start)
        if nsweep_rays < 2:
            continue
        target_duration = 60.0 / max(target_rpm, 1e-6)
        time_per_ray = target_duration / max(nsweep_rays - 1, 1)
        sweep_time = np.arange(nsweep_rays, dtype=float) * time_per_ray
        offset = 0.0 if sweep == 0 else (new_time_data[ray_ptr - 1] + time_per_ray)
        new_time_data[ray_ptr:ray_ptr + nsweep_rays] = sweep_time + offset
        ray_ptr += nsweep_rays
    new_radar.time['data'] = new_time_data.astype(np.float64)
    return new_radar


def _pyiris_quantity_name(moment: str) -> bytes:
    name = str(moment or '')
    low = name.lower()
    if 'dbt' in low:
        return np.bytes_('TX' if 'dbte' in low else 'TH')
    if 'dbz' in low:
        if 'dbze' in low:
            return np.bytes_('DBZX')
        if 'dbzv' in low:
            return np.bytes_('DBZV')
        return np.bytes_('DBZH')
    if 'vel' in low:
        return np.bytes_('VRADDH' if 'velc' in low else 'VRADH')
    if 'width' in low:
        return np.bytes_('WRADH')
    if 'zdr' in low:
        return np.bytes_('ZDR')
    if 'phidp' in low:
        return np.bytes_('PHIDP')
    if 'rhohv' in low:
        return np.bytes_('RHOHV')
    if 'kdp' in low:
        return np.bytes_('KDP')
    if 'sqi' in low:
        return np.bytes_('SQIH')
    if 'snr' in low:
        return np.bytes_('SNRH')
    if 'class' in low or 'hclass' in low:
        return np.bytes_('CLASS')
    return np.bytes_(name)


def _safe_masked_to_float(data: Any, fill_value: float = np.nan) -> np.ndarray:
    arr = np.ma.array(data, copy=False)
    if np.ma.isMaskedArray(arr):
        filled = arr.astype(np.float64).filled(fill_value)
    else:
        filled = np.asarray(arr, dtype=np.float64)
    return np.asarray(filled, dtype=float)


def _build_pyiris_hdf5_dataset(
    filepaths,
    filters=None,
    target_rays: int | None = 360,
    interpolate_missing: bool = False,
    normalize_rpm: bool = False,
):
    filters = filters or {}
    radar, warnings = _merge_radars(filepaths)

    if interpolate_missing:
        try:
            radar = _pyiris_fill_missing_rays_by_raycount(radar, target_rays=target_rays)
        except Exception as exc:
            warnings.append(f'missing-ray interpolation skipped: {exc}')

    if normalize_rpm:
        try:
            radar = _pyiris_rpm_check(radar)
        except Exception as exc:
            warnings.append(f'rpm normalization skipped: {exc}')

    for fname in list(radar.fields.keys()):
        try:
            raw_data = radar.fields[fname]['data'].copy()
            filtered_data, filter_warnings = _apply_pyiris_filters(radar, fname, raw_data, filters, load_pyiris_defaults())
            warnings.extend(filter_warnings)
            radar.fields[fname]['data'] = np.ma.masked_invalid(filtered_data)
        except Exception as exc:
            warnings.append(f'filter skipped for {fname}: {exc}')

    try:
        hclass_name = next((f for f in radar.fields.keys() if 'CLASS' in str(f).upper() or 'HCLASS' in str(f).upper()), None)
        if hclass_name:
            hclass_data = _safe_masked_to_float(radar.fields[hclass_name]['data'], fill_value=0).astype(np.int32)
            radar.fields[hclass_name]['data'] = np.ma.masked_invalid(np.bitwise_and(hclass_data, ~(0b11111 << 3)))
    except Exception:
        pass

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.h5')
    tmp.close()

    moments_out = list(radar.fields.keys())
    fixed_angle = np.asarray(getattr(radar, 'fixed_angle', {'data': []})['data'] if getattr(radar, 'fixed_angle', None) is not None else [], dtype=float)
    altitude_data = np.asarray(getattr(radar, 'altitude', {'data': [0.0]})['data'] if getattr(radar, 'altitude', None) is not None else [0.0], dtype=float)
    latitude = np.asarray(radar.latitude['data'], dtype=float)
    longitude = np.asarray(radar.longitude['data'], dtype=float)
    azimuth = np.asarray(radar.azimuth['data'], dtype=float)
    elevation = np.asarray(radar.elevation['data'], dtype=float)
    delta_time = np.asarray(radar.time['data'], dtype=float)
    nrays_sweep = np.asarray(radar.rays_per_sweep['data'], dtype=int) if getattr(radar, 'rays_per_sweep', None) is not None else np.array([], dtype=int)
    nbins = int(getattr(radar, 'ngates', 0) or 0)
    nsweeps = int(getattr(radar, 'nsweeps', 0) or 0)
    first_bin_range = np.asarray(radar.range.get('meters_to_center_of_first_gate', [0.0]), dtype=float)
    range_step = np.asarray(radar.range.get('meters_between_gates', [0.0]), dtype=float)
    a1gate = int(radar.range.get('a1gate', 0)) if isinstance(radar.range, dict) else 0

    start_units = str(radar.time.get('units', 'seconds since 1970-01-01T00:00:00Z'))
    try:
        start_scan_time = datetime.strptime(start_units, 'seconds since %Y-%m-%dT%H:%M:%SZ')
    except Exception:
        try:
            base = start_units.split('since', 1)[1].strip().replace('Z', '')
            start_scan_time = datetime.fromisoformat(base)
        except Exception:
            start_scan_time = datetime.utcnow()

    metadata = getattr(radar, 'metadata', {}) or {}
    instrument_parameters = getattr(radar, 'instrument_parameters', {}) or {}
    site_name = _safe_text(metadata.get('instrument_name')) or 'RADAR'
    task_name = _safe_text(metadata.get('sigmet_task_name') or metadata.get('task_name') or 'PYIRIS') or 'PYIRIS'
    polarization = _safe_text(metadata.get('polarization')) or 'unknown'
    scan_type = 'SCAN' if nsweeps == 1 else 'PVOL'
    wavelength = np.asarray(instrument_parameters.get('wavelength', {}).get('data', [0.0]), dtype=float)
    nyquist = np.asarray(instrument_parameters.get('nyquist_velocity', {}).get('data', [0.0]), dtype=float)
    prt_ratio = np.asarray(instrument_parameters.get('prt_ratio', {}).get('data', np.ones(max(1, azimuth.size))), dtype=float)
    beamwidth_h = np.asarray(instrument_parameters.get('radar_beam_width_h', {}).get('data', [0.0]), dtype=float)
    pulsewidth = np.asarray(instrument_parameters.get('pulse_width', {}).get('data', np.zeros(max(1, azimuth.size))), dtype=float)
    prt = np.asarray(instrument_parameters.get('prt', {}).get('data', np.ones(max(1, azimuth.size))), dtype=float)
    with np.errstate(divide='ignore', invalid='ignore'):
        prf = np.where(prt != 0, 1.0 / prt, 0.0)

    azimuth_start = np.asarray(radar.azimuth.get('start', azimuth), dtype=float)
    azimuth_stop = np.asarray(radar.azimuth.get('stop', azimuth), dtype=float)
    elevation_start = np.asarray(radar.elevation.get('start', elevation), dtype=float)
    elevation_stop = np.asarray(radar.elevation.get('stop', elevation), dtype=float)

    dt = np.dtype([('key','int64',(64)),('value','int64',(32))])
    hydrometeor_class = ['THRESHOLD','NON_MET','RAIN','WET_SNOW','SNOW','GRAUPEL','HAIL','GC/AP','BIO','PRECIPITATION','LARGE_DROPS','LIGHT_PRECIP','MODERATE_PRECIP','HEAVY_PRECIP','STATIFORM','CONVECTIVE','MELTING','NON_MELTING','AUX3','AUX4','AUX5','USER1','USER2','USER3','USER4','USER5']
    legend = np.zeros((26,), dtype=dt)
    for seq, h_class in enumerate(hydrometeor_class):
        key_arr = np.zeros(64, dtype='int64')
        val_arr = np.zeros(32, dtype='int64')
        for i, c in enumerate(h_class[:64]):
            key_arr[i] = ord(c)
        for i, c in enumerate(str(seq)[:32]):
            val_arr[i] = ord(c)
        legend[seq] = (key_arr, val_arr)

    offset_map = {}
    gain_map = {}
    for moment in moments_out:
        data = _safe_masked_to_float(radar.fields[moment]['data'])
        finite = data[np.isfinite(data)]
        offset = float(np.nanmin(finite)) if finite.size else 0.0
        low = str(moment).lower()
        if any(token in low for token in ('rhohv', 'phidp', 'sqi', 'pmi')):
            gain = abs(offset) if abs(offset) > 0 else 1.0
        elif 'class' in low or 'hclass' in low:
            gain = 1.0
            offset = 0.0
        else:
            gain = 0.01
        if not np.isfinite(gain) or gain == 0:
            gain = 1.0
        if not np.isfinite(offset):
            offset = 0.0
        offset_map[moment] = offset
        gain_map[moment] = gain

    with h5py.File(tmp.name, 'w') as hdf:
        hdf.attrs['Conventions'] = np.bytes_('ODIM_H5/V2_2')
        for sweep in range(nsweeps):
            start_ray = int(radar.get_start(sweep))
            end_ray = int(radar.get_end(sweep))
            slice_ray = radar.get_slice(sweep)
            ds_group = hdf.create_group(f'dataset{sweep+1}')
            d = 1
            for moment in moments_out:
                moment_data = _safe_masked_to_float(radar.fields[moment]['data'][slice_ray])
                order = np.argsort(np.asarray(azimuth_stop[slice_ray], dtype=float)) if np.asarray(azimuth_stop[slice_ray]).size else np.arange(moment_data.shape[0])
                az_start = np.asarray(azimuth_start[slice_ray], dtype=float)[order] if np.asarray(azimuth_start[slice_ray]).size else np.asarray(azimuth[slice_ray], dtype=float)[order]
                az_stop = np.asarray(azimuth_stop[slice_ray], dtype=float)[order] if np.asarray(azimuth_stop[slice_ray]).size else np.asarray(azimuth[slice_ray], dtype=float)[order]
                ordered = moment_data[order]
                encoded = np.where(np.isfinite(ordered), (ordered - offset_map[moment]) / gain_map[moment], 0.0)
                encoded = np.clip(np.rint(encoded), 0, np.iinfo(np.uint16).max).astype(np.uint16)
                main = ds_group.create_group(f'data{d}')
                main.create_dataset('data', data=encoded, chunks=encoded.shape, compression='gzip')
                data_dset = main['data']
                data_dset.attrs['CLASS'] = np.bytes_('IMAGE')
                data_dset.attrs['IMAGE_VERSION'] = np.bytes_('1.2')
                if 'class' in str(moment).lower() or 'hclass' in str(moment).lower():
                    main.create_dataset('legend', (26,), data=legend, dtype=dt)
                what = main.create_group('what')
                what.attrs['quantity'] = _pyiris_quantity_name(moment)
                what.attrs['gain'] = np.double(gain_map[moment])
                what.attrs['offset'] = np.double(offset_map[moment])
                what.attrs['nodata'] = np.double(0.0)
                what.attrs['undetect'] = np.double(0.0)
                d += 1

            ds_what = ds_group.create_group('what')
            ds_what.attrs['product'] = np.bytes_('SCAN')
            sweep_start_time = start_scan_time + timedelta(seconds=float(delta_time[start_ray]) if delta_time.size > start_ray else 0.0)
            sweep_end_time = start_scan_time + timedelta(seconds=float(delta_time[end_ray]) if delta_time.size > end_ray else 0.0)
            ds_what.attrs.create('startdate', sweep_start_time.strftime('%Y%m%d'), None, dtype='<S9')
            ds_what.attrs.create('starttime', sweep_start_time.strftime('%H%M%S'), None, dtype='<S7')
            ds_what.attrs.create('enddate', sweep_end_time.strftime('%Y%m%d'), None, dtype='<S9')
            ds_what.attrs.create('endtime', sweep_end_time.strftime('%H%M%S'), None, dtype='<S7')

            ds_how = ds_group.create_group('how')
            scan_time = start_scan_time.timestamp() + (delta_time[slice_ray] if delta_time.size else np.array([], dtype=float))
            duration = max((sweep_end_time - sweep_start_time).total_seconds(), 1.0)
            rpm = np.single((360.0 / duration) / 6.0).round(decimals=1)
            ds_how.attrs['scan_index'] = sweep + 1
            ds_how.attrs['pulsewidth'] = np.double(float(pulsewidth[start_ray] * 1e6) if pulsewidth.size > start_ray else 0.0)
            lowprf = float(prf[start_ray] / prt_ratio[start_ray]) if prf.size > start_ray and prt_ratio.size > start_ray and prt_ratio[start_ray] not in (0, np.nan) else 0.0
            ds_how.attrs['lowprf'] = np.double(lowprf)
            ds_how.attrs['highprf'] = np.double(float(prf[start_ray]) if prf.size > start_ray else 0.0)
            ds_how.attrs['NI'] = np.double(float(nyquist[start_ray]) if nyquist.size > start_ray else 0.0)
            ds_how.attrs['rpm'] = rpm
            ds_how.attrs['astart'] = np.float64(azimuth_start[start_ray] if azimuth_start.size > start_ray else azimuth[start_ray])
            ds_how.attrs['startazA'] = np.asarray(azimuth_start[slice_ray], dtype='float64') if azimuth_start.size else np.asarray(azimuth[slice_ray], dtype='float64')
            ds_how.attrs['stopazA'] = np.asarray(azimuth_stop[slice_ray], dtype='float64') if azimuth_stop.size else np.asarray(azimuth[slice_ray], dtype='float64')
            ds_how.attrs['startazT'] = np.asarray(scan_time, dtype='float64')
            ds_how.attrs['stopazT'] = np.asarray(scan_time, dtype='float64')
            ds_how.attrs['startelT'] = np.asarray(scan_time, dtype='float64')
            ds_how.attrs['stopelT'] = np.asarray(scan_time, dtype='float64')
            ds_how.attrs['startelA'] = np.asarray(elevation_start[slice_ray], dtype='float64') if elevation_start.size else np.asarray(elevation[slice_ray], dtype='float64')
            ds_how.attrs['stopelA'] = np.asarray(elevation_stop[slice_ray], dtype='float64') if elevation_stop.size else np.asarray(elevation[slice_ray], dtype='float64')

            ds_where = ds_group.create_group('where')
            ds_where.attrs['elangle'] = np.single(np.round(float(fixed_angle[sweep]) if fixed_angle.size > sweep else float(np.nanmedian(elevation[slice_ray])) if elevation[slice_ray].size else 0.0, 1))
            ds_where.attrs['nbins'] = np.int_(nbins)
            ds_where.attrs['rstart'] = np.double(float(first_bin_range[0]) if first_bin_range.size else 0.0)
            ds_where.attrs['rscale'] = np.double(float(range_step[0]) if range_step.size else 0.0)
            ds_where.attrs['nrays'] = np.int_(int(np.nanmax(nrays_sweep)) if interpolate_missing and nrays_sweep.size else int(nrays_sweep[sweep]) if nrays_sweep.size > sweep else len(slice_ray))
            ds_where.attrs['a1gate'] = np.int_(a1gate)

        how = hdf.create_group('how')
        how.attrs['task'] = _safe_string_attr(task_name, 'PYIRIS')
        how.attrs['beamwidth'] = np.double(float(beamwidth_h[0]) if beamwidth_h.size else 0.0)
        how.attrs['polarization'] = _safe_string_attr(polarization, 'unknown')
        how.attrs['scan_count'] = np.int_(nsweeps)
        how.attrs['wavelength'] = np.double(float(wavelength[0] / 100.0) if wavelength.size else 0.0)
        how.attrs['azmethod'] = np.bytes_('AVERAGE')
        how.attrs['binmethod'] = np.bytes_('AVERAGE')

        what = hdf.create_group('what')
        what.attrs['object'] = np.bytes_(scan_type)
        what.attrs['version'] = np.bytes_('H5rad 2.2')
        what.attrs['date'] = np.bytes_(start_scan_time.strftime('%Y%m%d'))
        what.attrs['time'] = np.bytes_(start_scan_time.strftime('%H%M%S'))
        what.attrs['source'] = np.bytes_(f'PLC:{site_name}')

        where = hdf.create_group('where')
        where.attrs['lon'] = np.double(float(longitude[0]) if longitude.size else 0.0)
        where.attrs['lat'] = np.double(float(latitude[0]) if latitude.size else 0.0)
        where.attrs['height'] = np.double(float(altitude_data[0]) if altitude_data.size else 0.0)

    return tmp.name, warnings, radar

def _build_export_dataset(
    filepaths,
    field,
    export_format='nc',
    filters=None,
    requested_sweep=None,
    requested_elevation=None,
    derived_product='PPI',
    cappi_height_km=2.0,
):
    filters = filters or {}
    radar, warnings = _merge_radars(filepaths)
    if field not in radar.fields:
        field = 'DBZ2' if 'DBZ2' in radar.fields else list(radar.fields.keys())[0]

    raw_data = radar.fields[field]['data'].copy()
    filtered_data, filter_warnings = _apply_pyiris_filters(radar, field, raw_data, filters, load_pyiris_defaults())
    warnings.extend(filter_warnings)
    radar.fields[field]['data'] = np.ma.masked_invalid(filtered_data)

    export_format = (export_format or 'nc').lower()
    if export_format not in ('nc', 'h5'):
        export_format = 'nc'

    product_used = (derived_product or 'PPI').upper()

    try:
        rng_km = float(radar.range['data'][-1]) / 1000.0
    except Exception:
        rng_km = 250.0

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.nc' if export_format == 'nc' else '.h5')
    tmp.close()

    if product_used == 'PPI':
        if export_format == 'nc':
            pyart.io.write_cfradial(tmp.name, radar)
        else:
            grid = pyart.map.grid_from_radars(
                (radar,),
                grid_shape=(1, 400, 400),
                grid_limits=((0.0, 0.0), (-rng_km * 1000.0, rng_km * 1000.0), (-rng_km * 1000.0, rng_km * 1000.0)),
                fields=[field],
                weighting_function='Barnes2',
            )
            pyart.io.write_grid(tmp.name, grid)
        return tmp.name, product_used, warnings

    grid_limit_xy = rng_km * 1000.0
    if product_used == 'MAX':
        z_top = max(12000.0, float(getattr(radar, 'nsweeps', 1) or 1) * 1000.0)
        grid = pyart.map.grid_from_radars(
            (radar,),
            grid_shape=(12, 400, 400),
            grid_limits=((0.0, z_top), (-grid_limit_xy, grid_limit_xy), (-grid_limit_xy, grid_limit_xy)),
            fields=[field],
            weighting_function='Barnes2',
        )
        arr3 = np.ma.filled(grid.fields[field]['data'], np.nan)
        grid.fields[field]['data'] = np.ma.masked_invalid(np.nanmax(arr3, axis=0, keepdims=True))
    elif product_used in ('ETOP', 'EBASE', 'ETHICK', 'LMEAN', 'VIL'):
        grid, arr3, z_levels = _build_cartesian_grid_for_product(radar, field, {'vertical_levels': 12, 'max_cappi_grid': 400}, rng_km, extra_z_levels=2)
        arr2, _, _, _ = _compute_vertical_derived_array(arr3, z_levels, product_used)
        grid.fields[field]['data'] = np.ma.masked_invalid(np.asarray(arr2, dtype=float)[np.newaxis, :, :])
    else:
        cappi_h = float(cappi_height_km) * 1000.0
        half = 250.0
        grid = pyart.map.grid_from_radars(
            (radar,),
            grid_shape=(3, 400, 400),
            grid_limits=((max(0.0, cappi_h-half), cappi_h+half), (-grid_limit_xy, grid_limit_xy), (-grid_limit_xy, grid_limit_xy)),
            fields=[field],
            weighting_function='Barnes2',
        )
        arr3 = np.ma.filled(grid.fields[field]['data'], np.nan)
        arr2 = np.nanmean(arr3, axis=0)
        arr2 = _fill_nan_nearest_2d(arr2)
        grid.fields[field]['data'] = np.ma.masked_invalid(arr2[np.newaxis, :, :])

    pyart.io.write_grid(tmp.name, grid)
    return tmp.name, product_used, warnings

def _build_scan_groups(scan_window_minutes: int):
    global radar_groups, available_fields, radar_extent, time_bounds
    window_sec = max(1, int(scan_window_minutes)) * 60
    collected = []
    for uploaded in uploaded_files_mem:
        uploaded_name = uploaded['filename']
        try:
            radar = _read_radar_fileentry(uploaded)
        except Exception as exc:
            current_app.logger.warning('Skipping %s: %s', uploaded_name, exc)
            continue
        if len(radar.sweep_start_ray_index['data']) == 0:
            current_app.logger.warning('Skipping %s: no sweeps found', uploaded_name)
            continue
        site = _safe_text(radar.metadata.get('instrument_name')) or 'UNKNOWN'
        raw_task = _safe_text(radar.metadata.get('task_name') or radar.metadata.get('sigmet_task_name') or '')
        base_task = _normalize_task_name(raw_task)
        ts_label, epoch = _get_radar_epoch_and_label(radar, uploaded_name)
        elev_text = 'N/A'
        try:
            elev_arr = np.asarray(radar.fixed_angle['data']).astype(float)
            elev_text = ', '.join(f'{v:g}°' for v in sorted(set(np.round(elev_arr, 2))))
        except Exception:
            pass
        sweep_code = _extract_sweep_code(uploaded_name, raw_task)
        subscan_letter = _extract_subscan_letter(raw_task, sweep_code, uploaded_name)
        collected.append({
            'fileentry': uploaded,
            'filename': uploaded_name,
            'site': site,
            'raw_task': raw_task or base_task,
            'base_task': base_task,
            'sweep_code': sweep_code,
            'subscan_letter': subscan_letter,
            'subscan_rank': _subscan_rank(subscan_letter),
            'time': ts_label,
            'epoch': epoch,
            'elevations_text': elev_text,
        })
    collected.sort(key=lambda x: (x['site'], x['base_task'], x['epoch'] if x['epoch'] is not None else float('inf'), x['filename']))
    grouped, current_by_group = {}, {}
    for item in collected:
        pair = (item['site'], item['base_task'])
        existing = current_by_group.get(pair)
        if existing is None:
            current_by_group[pair] = {'items': [item], 'start_epoch': item['epoch'], 'last_rank': item.get('subscan_rank', 999)}
            continue
        start_epoch = existing['start_epoch']
        within_window = item['epoch'] is not None and start_epoch is not None and (item['epoch'] - start_epoch) <= window_sec
        current_rank = item.get('subscan_rank', 999)
        previous_rank = existing.get('last_rank', 999)
        rank_reset = current_rank <= previous_rank
        if within_window and not rank_reset:
            existing['items'].append(item); existing['last_rank'] = current_rank
        else:
            key = f"{pair[0]} - {pair[1]}"
            grouped.setdefault(key, []).append(existing['items'])
            current_by_group[pair] = {'items': [item], 'start_epoch': item['epoch'], 'last_rank': current_rank}
    for pair, frame in current_by_group.items():
        key = f"{pair[0]} - {pair[1]}"
        grouped.setdefault(key, []).append(frame['items'])

    radar_groups, time_bounds = {}, {}
    for key, frames in grouped.items():
        clean_frames = []
        for frame_items in frames:
            preferred = sorted(frame_items, key=lambda x: (_subscan_rank(x.get('subscan_letter', '')), x.get('epoch') if x.get('epoch') is not None else float('inf'), x.get('filename', '')))
            anchor_item = preferred[0]
            sweeps, seen_sweeps, elevations, seen_elev, files = [], set(), [], set(), []
            for item in preferred:
                files.append(item['fileentry'])
                code = str(item.get('sweep_code') or '').strip()
                if code and code not in seen_sweeps:
                    seen_sweeps.add(code); sweeps.append(code)
                elev = str(item.get('elevations_text') or '').strip()
                if elev and elev not in seen_elev:
                    seen_elev.add(elev); elevations.append(elev)
            clean_frames.append({'files': files, 'file_count': len(files), 'time': anchor_item.get('time', 'N/A'), 'epoch': anchor_item.get('epoch'), 'sweeps': sweeps, 'elevations_text': ' | '.join(elevations)})
        clean_frames.sort(key=lambda x: x['epoch'] if x['epoch'] is not None else float('inf'))
        radar_groups[key] = clean_frames
        epochs_list = [f['epoch'] for f in clean_frames if f['epoch'] is not None]
        if epochs_list:
            time_bounds[key] = {'min': min(epochs_list), 'max': max(epochs_list)}

    available_fields = []
    if radar_groups:
        first_entry = next(iter(radar_groups.values()))[0]['files'][0]
        try:
            radar = _read_radar_fileentry(first_entry)
            available_fields[:] = list(radar.fields.keys())
            lat = radar.latitude['data'][0]; lon = radar.longitude['data'][0]
            rng = radar.range['data'][-1] / 1000.0; d = rng / 111.0
            radar_extent[:] = [[lat - d, lon - d], [lat + d, lon + d]]
        except Exception as exc:
            current_app.logger.warning('Could not read first file fields: %s', exc)
            available_fields[:] = []



@radar_bp.route('/precompute', methods=['POST'])
def radar_precompute():
    payload = request.get_json(silent=True) or {}
    group = payload.get('group')
    field = payload.get('field')
    cmap_override = payload.get('cmap', '')
    vmin_override = payload.get('vmin')
    vmax_override = payload.get('vmax')
    filters = payload.get('filters') or {}
    requested_sweep = payload.get('sweep')
    requested_elevation = payload.get('elevation')
    derived_product = (payload.get('derived_product') or 'PPI').upper()
    cappi_height_km = payload.get('cappi_height_km', 2.0)
    render_quality = _normalize_render_quality(payload.get('quality'))
    custom_cmap = payload.get('custom_cmap') or {}

    if group not in radar_groups:
        return {'ok': False, 'error': 'Group not found'}, 404
    if not field or field not in available_fields:
        if available_fields:
            field = available_fields[0]
        else:
            return {'ok': False, 'error': 'No radar fields available'}, 404

    try:
        vmin_override = None if vmin_override in ('', None) else float(vmin_override)
        vmax_override = None if vmax_override in ('', None) else float(vmax_override)
    except Exception:
        vmin_override = None
        vmax_override = None

    frames = radar_groups[group]
    built = 0
    errors = []

    for idx, frame_data in enumerate(frames):
        try:
            cache_key = _make_render_cache_key(
                group, idx, field, cmap_override, vmin_override, vmax_override,
                filters, requested_sweep, requested_elevation, derived_product, cappi_height_km, render_quality, custom_cmap
            )
            if _cache_get(render_cache, cache_key) is not None:
                continue

            png_buf, extent, radar, warnings, selected_sweep_idx, sweep_options, selected_elevation, product_used = _render_radar_png_from_files(
                frame_data['files'],
                field,
                cmap_override,
                vmin_override,
                vmax_override,
                filters,
                requested_sweep=requested_sweep,
                requested_elevation=requested_elevation,
                derived_product=derived_product,
                cappi_height_km=cappi_height_km,
                render_quality=render_quality,
                custom_cmap=custom_cmap,
            )
            cached = {
                'png_bytes': png_buf.getvalue(),
                'extent': extent,
                'timestamp': frame_data['time'],
                'file_count': frame_data.get('file_count', len(frame_data['files'])),
                'sweeps': ', '.join(frame_data.get('sweeps', [])),
                'elevations': frame_data.get('elevations_text', ''),
                'selected_sweep_idx': selected_sweep_idx,
                'selected_elevation': selected_elevation,
                'sweep_options': json.dumps(sweep_options),
                'warnings': '; '.join(_unique_warning_messages(warnings)) if warnings else '',
                'product_used': product_used,
                'frames_total': len(frames),
                'render_quality': render_quality,
                'filter_availability': json.dumps(_compute_filter_availability(radar, field)),
            }
            _cache_store(render_cache, cache_key, cached, CACHE_MAX_ITEMS)
            built += 1
        except Exception as exc:
            errors.append(f'frame {idx}: {exc}')

    return {'ok': True, 'built': built, 'total': len(frames), 'errors': errors[:5]}

@radar_bp.route('/export')
def radar_export():
    group = request.args.get('group')
    idx = int(request.args.get('frame', 0))
    field = request.args.get('field')
    export_format = (request.args.get('export_format') or 'nc').lower()
    requested_sweep = request.args.get('sweep')
    requested_elevation = request.args.get('elevation')
    derived_product = (request.args.get('derived_product') or 'PPI').upper()
    render_quality = _normalize_render_quality(request.args.get('quality'))
    custom_cmap_arg = request.args.get('custom_cmap')
    try:
        custom_cmap = json.loads(custom_cmap_arg) if custom_cmap_arg else {}
    except Exception:
        custom_cmap = {}
    try:
        cappi_height_km = float(request.args.get('cappi_height_km', '2.0') or '2.0')
    except Exception:
        cappi_height_km = 2.0
    filters_arg = request.args.get('filters')
    try:
        filters = json.loads(filters_arg) if filters_arg else {}
    except Exception:
        filters = {}
    interpolate = str(request.args.get('interpolate', '')).strip().lower() in ('1', 'true', 'yes', 'on')
    gap_fill = str(request.args.get('gap_fill', '')).strip().lower() in ('1', 'true', 'yes', 'on')
    if gap_fill:
        interpolate = True

    if not radar_groups or group not in radar_groups:
        return 'No radar files loaded', 404

    if not field or field not in available_fields:
        if 'DBZ2' in available_fields:
            field = 'DBZ2'
        elif available_fields:
            field = available_fields[0]
        else:
            return 'No radar fields available', 404

    frames = radar_groups[group]
    idx = idx % len(frames)
    frame_data = frames[idx]

    export_path, product_used, warnings = _build_export_dataset(
        frame_data['files'],
        field,
        export_format=export_format,
        filters=filters,
        requested_sweep=requested_sweep,
        requested_elevation=requested_elevation,
        derived_product=derived_product,
        cappi_height_km=cappi_height_km,
    )

    safe_group = group.replace(' ', '_').replace('/', '-')
    safe_time = frame_data['time'].replace(':', '-').replace(' ', '_')
    filename = f"{safe_group}_{safe_time}_{field}_{product_used}.{export_format}"
    response = send_file(export_path, as_attachment=True, download_name=filename)

    @response.call_on_close
    def _cleanup_tmp():
        try:
            os.remove(export_path)
        except Exception:
            pass

    warnings = _unique_warning_messages(warnings)
    if warnings:
        response.headers['X-Warnings'] = '; '.join(warnings)
    return response

@radar_bp.route('/convert_hdf5')
def radar_convert_hdf5():
    group = request.args.get('group')
    idx = int(request.args.get('frame', 0))
    filters_arg = request.args.get('filters')
    try:
        filters = json.loads(filters_arg) if filters_arg else {}
    except Exception:
        filters = {}

    interpolate_missing = str(request.args.get('interpolate_missing', '')).strip().lower() in ('1', 'true', 'yes', 'on')
    normalize_rpm = str(request.args.get('normalize_rpm', '')).strip().lower() in ('1', 'true', 'yes', 'on')
    try:
        target_rays = int(request.args.get('target_rays', '360') or '360')
    except Exception:
        target_rays = 360
    target_rays = max(1, target_rays)

    if not radar_groups or group not in radar_groups:
        return 'No radar files loaded', 404

    frames = radar_groups[group]
    idx = idx % len(frames)
    frame_data = frames[idx]

    export_path, warnings, radar = _build_pyiris_hdf5_dataset(
        frame_data['files'],
        filters=filters,
        target_rays=target_rays,
        interpolate_missing=interpolate_missing,
        normalize_rpm=normalize_rpm,
    )

    task_name = _normalize_task_name(_safe_text(getattr(radar, 'metadata', {}).get('sigmet_task_name') or getattr(radar, 'metadata', {}).get('task_name') or group))
    safe_time = str(frame_data.get('time') or 'frame').replace(':', '-').replace(' ', '_')
    filename = f"{task_name}_{safe_time}_pyiris.h5"
    response = send_file(export_path, as_attachment=True, download_name=filename)

    @response.call_on_close
    def _cleanup_tmp():
        try:
            os.remove(export_path)
        except Exception:
            pass

    warnings = _unique_warning_messages(warnings)
    if warnings:
        response.headers['X-Warnings'] = '; '.join(warnings)
    return response


@radar_bp.route('/cross_section')
def radar_cross_section():
    group = request.args.get('group')
    idx = int(request.args.get('frame', 0))
    field = request.args.get('field')
    cmap_override = request.args.get('cmap', '')
    vmin_override = float(request.args.get('vmin', '') or 'nan')
    vmax_override = float(request.args.get('vmax', '') or 'nan')
    vmin_override = None if np.isnan(vmin_override) else vmin_override
    vmax_override = None if np.isnan(vmax_override) else vmax_override
    render_quality = _normalize_render_quality(request.args.get('quality'))
    custom_cmap_arg = request.args.get('custom_cmap')
    try:
        custom_cmap = json.loads(custom_cmap_arg) if custom_cmap_arg else {}
    except Exception:
        custom_cmap = {}
    try:
        start_lat = float(request.args.get('start_lat'))
        start_lon = float(request.args.get('start_lon'))
        end_lat = float(request.args.get('end_lat'))
        end_lon = float(request.args.get('end_lon'))
    except Exception:
        return 'Invalid cross section coordinates', 400

    def _opt_float(value):
        try:
            return None if value in (None, '') else float(value)
        except Exception:
            return None

    x_min = _opt_float(request.args.get('x_min'))
    x_max = _opt_float(request.args.get('x_max'))
    y_min = _opt_float(request.args.get('y_min'))
    y_max = _opt_float(request.args.get('y_max'))
    interpolate = str(request.args.get('interpolate', '')).strip().lower() in ('1', 'true', 'yes', 'on')
    gap_fill = str(request.args.get('gap_fill', '')).strip().lower() in ('1', 'true', 'yes', 'on')
    smooth_interp = _opt_float(request.args.get('smooth_interp'))
    if smooth_interp is None:
        smooth_interp = _opt_float(request.args.get('smooth'))
    if smooth_interp is None:
        smooth_interp = 1.0

    smooth_gap = _opt_float(request.args.get('smooth_gap'))
    if smooth_gap is None:
        smooth_gap = smooth_interp

    filters_arg = request.args.get('filters')
    try:
        filters = json.loads(filters_arg) if filters_arg else {}
    except Exception:
        filters = {}
    interpolate = str(request.args.get('interpolate', '')).strip().lower() in ('1', 'true', 'yes', 'on')
    gap_fill = str(request.args.get('gap_fill', '')).strip().lower() in ('1', 'true', 'yes', 'on')
    if gap_fill:
        interpolate = True

    if not radar_groups or group not in radar_groups:
        return 'No radar files loaded', 404
    if not field or field not in available_fields:
        if 'DBZ2' in available_fields:
            field = 'DBZ2'
        elif available_fields:
            field = available_fields[0]
        else:
            return 'No radar fields available', 404

    frames = radar_groups[group]
    idx = idx % len(frames)
    frame_data = frames[idx]

    buf, warnings, meta = _render_cross_section_png_from_files(
        frame_data['files'],
        field,
        start_lat, start_lon, end_lat, end_lon,
        cmap_override=cmap_override,
        vmin_override=vmin_override,
        vmax_override=vmax_override,
        filters=filters,
        render_quality=render_quality,
        custom_cmap=custom_cmap,
        x_min=x_min,
        x_max=x_max,
        y_min=y_min,
        y_max=y_max,
        interpolate=interpolate,
        gap_fill=gap_fill,
        smooth_interp=smooth_interp,
        smooth_gap=smooth_gap,
    )
    response = send_file(buf, mimetype='image/png')
    response.headers['X-Cross-Distance-Km'] = f"{meta['line_length_km']:.3f}"
    response.headers['X-Cross-X-Min'] = f"{meta['x_min']:.6f}"
    response.headers['X-Cross-X-Max'] = f"{meta['x_max']:.6f}"
    response.headers['X-Cross-Y-Min'] = f"{meta['y_min']:.6f}"
    response.headers['X-Cross-Y-Max'] = f"{meta['y_max']:.6f}"
    response.headers['X-Cross-Default-X-Min'] = f"{meta['default_x_min']:.6f}"
    response.headers['X-Cross-Default-X-Max'] = f"{meta['default_x_max']:.6f}"
    response.headers['X-Cross-Default-Y-Min'] = f"{meta['default_y_min']:.6f}"
    response.headers['X-Cross-Default-Y-Max'] = f"{meta['default_y_max']:.6f}"
    response.headers['X-Cross-Interpolate'] = '1' if meta.get('interpolate') else '0'
    response.headers['X-Cross-Gap-Fill'] = '1' if meta.get('gap_fill') else '0'
    response.headers['X-Cross-Smooth-Interp'] = f"{meta.get('smooth_interp', 1.0):.3f}"
    response.headers['X-Cross-Smooth-Gap'] = f"{meta.get('smooth_gap', meta.get('smooth_interp', 1.0)):.3f}"
    response.headers['X-Cross-Smooth'] = f"{meta.get('smooth_interp', 1.0):.3f}"
    warnings = _unique_warning_messages(warnings)
    if warnings:
        response.headers['X-Warnings'] = '; '.join(warnings)
    return response




@radar_bp.route('/sample_point')
def radar_sample_point():
    group = request.args.get('group')
    idx = int(request.args.get('frame', 0))
    field = request.args.get('field')
    derived_product = (request.args.get('derived_product') or 'PPI').upper()
    requested_sweep = request.args.get('sweep')
    requested_elevation = request.args.get('elevation')
    try:
        cappi_height_km = float(request.args.get('cappi_height_km', '2.0') or '2.0')
    except Exception:
        cappi_height_km = 2.0
    try:
        lat = float(request.args.get('lat'))
        lon = float(request.args.get('lon'))
    except Exception:
        return jsonify({'ok': False, 'error': 'Invalid coordinates'}), 400

    filters_arg = request.args.get('filters')
    try:
        filters = json.loads(filters_arg) if filters_arg else {}
    except Exception:
        filters = {}
    interpolate = str(request.args.get('interpolate', '')).strip().lower() in ('1', 'true', 'yes', 'on')
    gap_fill = str(request.args.get('gap_fill', '')).strip().lower() in ('1', 'true', 'yes', 'on')
    if gap_fill:
        interpolate = True

    if not radar_groups or group not in radar_groups:
        return jsonify({'ok': False, 'error': 'No radar files loaded'}), 404

    if not field or field not in available_fields:
        if 'DBZ2' in available_fields:
            field = 'DBZ2'
        elif available_fields:
            field = available_fields[0]
        else:
            return jsonify({'ok': False, 'error': 'No radar fields available'}), 404

    frames = radar_groups[group]
    idx = idx % len(frames)
    frame_data = frames[idx]

    try:
        payload = _sample_product_value_from_files(
            frame_data['files'],
            field,
            lat,
            lon,
            filters=filters,
            requested_sweep=requested_sweep,
            requested_elevation=requested_elevation,
            derived_product=derived_product,
            cappi_height_km=cappi_height_km,
            accumulation_minutes=float(merge_window_minutes),
        )
        payload.update({
            'group': group,
            'frame': idx,
            'timestamp': frame_data.get('time'),
        })
        return jsonify(payload)
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500


@radar_bp.route('/', methods=['GET', 'POST'])
def radar_home():
    global merge_window_minutes
    defaults = load_pyiris_defaults()
    if request.method == 'POST':
        try:
            merge_window_minutes = int(request.form.get('scan_window_minutes') or request.args.get('scan_window') or merge_window_minutes or 5)
        except Exception:
            merge_window_minutes = 5
        uploaded_files_mem.clear()
        render_cache.clear()
        merged_radar_cache.clear()
        for uploaded in request.files.getlist('radarfiles'):
            if not uploaded or not uploaded.filename:
                continue
            uploaded_files_mem.append({
                'filename': uploaded.filename,
                'content': uploaded.read(),
            })
        _build_scan_groups(merge_window_minutes)
        return redirect(url_for('radar_bp.radar_home', scan_window=merge_window_minutes))
    try:
        requested_window = int(request.args.get('scan_window') or merge_window_minutes or 5)
    except Exception:
        requested_window = merge_window_minutes or 5
    if requested_window != merge_window_minutes or not radar_groups:
        merge_window_minutes = requested_window
        _build_scan_groups(merge_window_minutes)
    return render_template(
        'radar_map_tbr.html',
        fields=available_fields,
        extent=radar_extent,
        groups=list(radar_groups.keys()),
        times={g: [item['time'] for item in radar_groups[g]] for g in radar_groups},
        epochs={g: [item['epoch'] for item in radar_groups[g]] for g in radar_groups},
        bounds=time_bounds,
        default_configs=default_configs,
        scan_window_minutes=merge_window_minutes,
        pyiris_defaults=defaults,
    )

@radar_bp.route('/overlay')
def radar_overlay():
    group = request.args.get('group')
    idx = int(request.args.get('frame', 0))
    field = request.args.get('field')
    if not field or field not in available_fields:
        if 'DBZ2' in available_fields:
            field = 'DBZ2'
        elif available_fields:
            field = available_fields[0]
        else:
            return 'No radar fields available', 404
    cmap_override = request.args.get('cmap', '')
    vmin_override = float(request.args.get('vmin', '') or 'nan')
    vmax_override = float(request.args.get('vmax', '') or 'nan')
    vmin_override = None if np.isnan(vmin_override) else vmin_override
    vmax_override = None if np.isnan(vmax_override) else vmax_override
    requested_sweep = request.args.get('sweep')
    requested_elevation = request.args.get('elevation')
    derived_product = (request.args.get('derived_product') or 'PPI').upper()
    render_quality = _normalize_render_quality(request.args.get('quality'))
    custom_cmap_arg = request.args.get('custom_cmap')
    try:
        custom_cmap = json.loads(custom_cmap_arg) if custom_cmap_arg else {}
    except Exception:
        custom_cmap = {}
    try:
        cappi_height_km = float(request.args.get('cappi_height_km', '2.0') or '2.0')
    except Exception:
        cappi_height_km = 2.0
    filters_arg = request.args.get('filters')
    try:
        filters = json.loads(filters_arg) if filters_arg else {}
    except Exception:
        filters = {}
    interpolate = str(request.args.get('interpolate', '')).strip().lower() in ('1', 'true', 'yes', 'on')
    gap_fill = str(request.args.get('gap_fill', '')).strip().lower() in ('1', 'true', 'yes', 'on')
    if gap_fill:
        interpolate = True
    if not radar_groups or group not in radar_groups:
        return 'No radar files loaded', 404
    frames = radar_groups[group]
    idx = idx % len(frames)
    frame_data = frames[idx]
    cache_key = _make_render_cache_key(
        group, idx, field, cmap_override, vmin_override, vmax_override,
        filters, requested_sweep, requested_elevation, derived_product, cappi_height_km, render_quality, custom_cmap
    )
    cached = _cache_get(render_cache, cache_key)
    if cached is None:
        png_buf, extent, radar, warnings, selected_sweep_idx, sweep_options, selected_elevation, product_used = _render_radar_png_from_files(
            frame_data['files'],
            field,
            cmap_override,
            vmin_override,
            vmax_override,
            filters,
            requested_sweep=requested_sweep,
            requested_elevation=requested_elevation,
            derived_product=derived_product,
            cappi_height_km=cappi_height_km,
            render_quality=render_quality,
            custom_cmap=custom_cmap,
        )
        cached = {
            'png_bytes': png_buf.getvalue(),
            'extent': extent,
            'timestamp': frame_data['time'],
            'file_count': frame_data.get('file_count', len(frame_data['files'])),
            'sweeps': ', '.join(frame_data.get('sweeps', [])),
            'elevations': frame_data.get('elevations_text', ''),
            'selected_sweep_idx': selected_sweep_idx,
            'selected_elevation': selected_elevation,
            'sweep_options': json.dumps(sweep_options),
            'warnings': '; '.join(warnings) if warnings else '',
            'product_used': product_used,
            'frames_total': len(frames),
            'render_quality': render_quality,
            'filter_availability': json.dumps(_compute_filter_availability(radar, field)),
        }
        _cache_store(render_cache, cache_key, cached, CACHE_MAX_ITEMS)

    response = send_file(BytesIO(cached['png_bytes']), mimetype='image/png')
    extent = cached['extent']
    response.headers['X-Extent'] = f'{extent[0]},{extent[1]},{extent[2]},{extent[3]}'
    response.headers['X-Frames'] = str(cached['frames_total'])
    response.headers['X-Timestamp'] = cached['timestamp']
    response.headers['X-File-Count'] = str(cached['file_count'])
    response.headers['X-Sweeps'] = cached['sweeps']
    response.headers['X-Elevations'] = cached['elevations']
    response.headers['X-Active-Sweep-Index'] = str(cached['selected_sweep_idx'])
    response.headers['X-Active-Elevation'] = '' if cached['selected_elevation'] is None else str(cached['selected_elevation'])
    response.headers['X-Sweep-Options'] = cached['sweep_options']
    response.headers['X-Derived-Product'] = cached['product_used']
    response.headers['X-Render-Quality'] = cached.get('render_quality', 'high')
    response.headers['X-Filter-Availability'] = cached.get('filter_availability', '{}')
    if cached['warnings']:
        response.headers['X-Warnings'] = cached['warnings']
    return response

@radar_bp.route('/volume_data')
def radar_volume_data():
    group = request.args.get('group')
    idx = int(request.args.get('frame', 0))
    field = request.args.get('field')
    render_quality = _normalize_render_quality(request.args.get('quality'))

    try:
        max_points = int(request.args.get('max_points') or 18000)
    except Exception:
        max_points = 18000
    max_points = int(np.clip(max_points, 3000, 50000))

    filters_arg = request.args.get('filters')
    try:
        filters = json.loads(filters_arg) if filters_arg else {}
    except Exception:
        filters = {}
    interpolate = str(request.args.get('interpolate', '')).strip().lower() in ('1', 'true', 'yes', 'on')
    gap_fill = str(request.args.get('gap_fill', '')).strip().lower() in ('1', 'true', 'yes', 'on')
    if gap_fill:
        interpolate = True

    if group not in radar_groups:
        return jsonify({'ok': False, 'error': 'Group not found'}), 404

    frames = radar_groups[group]
    frame_data = frames[idx % len(frames)]
    radar, warnings = _merge_radars(frame_data['files'])

    if field not in radar.fields:
        field = _choose_reflectivity_field(radar)
    if not field or field not in radar.fields:
        return jsonify({'ok': False, 'error': 'No valid radar field available'}), 404

    try:
        raw_data = radar.fields[field]['data'].copy()
        filtered_data, filter_warnings = _apply_pyiris_filters(radar, field, raw_data, filters, load_pyiris_defaults())
        warnings.extend(filter_warnings)
        data = np.ma.filled(filtered_data, np.nan).astype(float)
    except Exception:
        data = np.ma.filled(radar.fields[field]['data'], np.nan).astype(float)

    data = _prepare_3d_volume_data(data, interpolate=interpolate, gap_fill=gap_fill)

    az = np.asarray(radar.azimuth['data'], dtype=float)
    elev = np.asarray(radar.elevation['data'], dtype=float)
    rng_km_full = np.asarray(radar.range['data'], dtype=float) / 1000.0

    if data.ndim != 2 or data.size == 0 or az.size == 0 or rng_km_full.size == 0:
        return jsonify({'ok': False, 'error': 'Radar volume is empty'}), 400

    qcfg = _quality_settings(render_quality)
    base_stride = 1
    if render_quality == 'medium':
        base_stride = 1
    elif render_quality == 'low':
        base_stride = 2
    az_stride = max(1, base_stride)
    rng_stride = max(1, base_stride)

    sample = data[::az_stride, ::rng_stride]
    az_sample = az[::az_stride]
    elev_sample = elev[::az_stride]
    rng_km = rng_km_full[::rng_stride]

    cfg = default_configs.get(field, {})
    finite_sample = sample[np.isfinite(sample)]
    base_min = float(cfg.get('vmin', -20.0))
    if 'DB' in str(field).upper():
        threshold = max(-5.0, base_min)
    else:
        threshold = float(np.nanpercentile(finite_sample, 65)) if finite_sample.size else base_min

    mask = np.isfinite(sample) & (sample >= threshold)
    if not np.any(mask):
        mask = np.isfinite(sample)
    if not np.any(mask):
        return jsonify({'ok': False, 'error': 'No finite radar voxels available'}), 400

    az_rad = np.deg2rad(az_sample)[:, None]
    elev_rad = np.deg2rad(elev_sample)[:, None]
    rr_km = rng_km[None, :]

    x_km = rr_km * np.sin(az_rad)
    y_km = rr_km * np.cos(az_rad)

    lat0 = float(radar.latitude['data'][0])
    lon0 = float(radar.longitude['data'][0])
    alt0_m = float(np.asarray(radar.altitude['data']).ravel()[0]) if getattr(radar, 'altitude', None) is not None else 0.0

    values = sample[mask]
    x_vals = x_km[mask]
    y_vals = y_km[mask]
    z_vals_km = np.maximum(0.0, (alt0_m + (rr_km * 1000.0 * np.sin(elev_rad)))[mask] / 1000.0)

    valid = np.isfinite(values) & np.isfinite(x_vals) & np.isfinite(y_vals) & np.isfinite(z_vals_km)
    values = values[valid]
    x_vals = x_vals[valid]
    y_vals = y_vals[valid]
    z_vals_km = z_vals_km[valid]

    if values.size == 0:
        return jsonify({'ok': False, 'error': 'No valid sampled voxels available'}), 400

    max_range_km = float(np.nanmax(rng_km_full)) if rng_km_full.size else 0.0
    max_height_km = float(np.nanmax(z_vals_km)) if z_vals_km.size else 0.0

    xy_step_km = {'low': 2.0, 'medium': 1.6, 'high': 1.2, 'ultra': 0.95}.get(render_quality, 1.2)
    z_step_km = {'low': 0.90, 'medium': 0.72, 'high': 0.55, 'ultra': 0.42}.get(render_quality, 0.55)
    if interpolate:
        xy_step_km *= 0.78
        z_step_km *= 0.82
    if gap_fill:
        xy_step_km *= 0.82
        z_step_km *= 0.86

    x_edges = np.arange(-max_range_km, max_range_km + xy_step_km, xy_step_km)
    y_edges = np.arange(-max_range_km, max_range_km + xy_step_km, xy_step_km)
    z_edges = np.arange(0.0, max(max_height_km + z_step_km, z_step_km * 2), z_step_km)
    nx = max(1, len(x_edges) - 1)
    ny = max(1, len(y_edges) - 1)
    nz = max(1, len(z_edges) - 1)

    ix = np.clip(np.searchsorted(x_edges, x_vals, side='right') - 1, 0, nx - 1)
    iy = np.clip(np.searchsorted(y_edges, y_vals, side='right') - 1, 0, ny - 1)
    iz = np.clip(np.searchsorted(z_edges, z_vals_km, side='right') - 1, 0, nz - 1)

    grid_max = np.full((nz, ny, nx), np.nan, dtype=float)
    grid_sum = np.zeros((nz, ny, nx), dtype=float)
    grid_count = np.zeros((nz, ny, nx), dtype=float)

    flat_idx = iz * (ny * nx) + iy * nx + ix
    order = np.argsort(flat_idx)
    flat_sorted = flat_idx[order]
    values_sorted = values[order]
    unique_bins, starts = np.unique(flat_sorted, return_index=True)
    ends = np.r_[starts[1:], len(flat_sorted)]
    for bin_idx, s, e in zip(unique_bins, starts, ends):
        vals = values_sorted[s:e]
        if vals.size == 0:
            continue
        z_i = int(bin_idx // (ny * nx))
        rem = int(bin_idx % (ny * nx))
        y_i = int(rem // nx)
        x_i = int(rem % nx)
        grid_max[z_i, y_i, x_i] = float(np.nanmax(vals))
        grid_sum[z_i, y_i, x_i] = float(np.nansum(vals))
        grid_count[z_i, y_i, x_i] = float(np.count_nonzero(np.isfinite(vals)))

    grid_mean = np.where(grid_count > 0, grid_sum / np.maximum(grid_count, 1.0), np.nan)
    base_grid = np.where(np.isfinite(grid_max), grid_max, grid_mean)

    valid_grid = np.isfinite(base_grid)
    if interpolate and np.any(valid_grid):
        sigma_xy = 0.95 if gap_fill else 0.70
        sigma_z = 0.85 if gap_fill else 0.60
        work = np.where(valid_grid, base_grid, 0.0)
        weights = valid_grid.astype(float)
        work_s = gaussian_filter(work, sigma=(sigma_z, sigma_xy, sigma_xy))
        weights_s = gaussian_filter(weights, sigma=(sigma_z, sigma_xy, sigma_xy))
        with np.errstate(invalid='ignore', divide='ignore'):
            smooth_grid = work_s / weights_s
        if gap_fill:
            support = weights_s > 0.015
            base_grid = np.where(support, smooth_grid, np.nan)
        else:
            support = weights_s > 0.05
            base_grid = np.where(valid_grid, np.where(np.isfinite(smooth_grid), smooth_grid, base_grid), np.where(support, smooth_grid, np.nan))

    z_centers = (z_edges[:-1] + z_edges[1:]) / 2.0
    y_centers = (y_edges[:-1] + y_edges[1:]) / 2.0
    x_centers = (x_edges[:-1] + x_edges[1:]) / 2.0
    zz, yy, xx = np.meshgrid(z_centers, y_centers, x_centers, indexing='ij')
    rr = np.sqrt(xx * xx + yy * yy)
    radial_mask = rr <= (max_range_km * 0.985)
    base_grid = np.where(radial_mask, base_grid, np.nan)

    finite_vals = base_grid[np.isfinite(base_grid)]
    if finite_vals.size == 0:
        return jsonify({'ok': False, 'error': 'No valid volumetric cells available'}), 400

    value_lo = float(np.nanmin(finite_vals))
    value_hi = float(np.nanmax(finite_vals))
    value_span = max(value_hi - value_lo, 1e-6)
    keep_threshold = value_lo + (0.08 * value_span)
    keep_mask = np.isfinite(base_grid) & (base_grid >= keep_threshold)

    z_idx, y_idx, x_idx = np.where(keep_mask)
    voxel_values = base_grid[keep_mask]
    voxel_count = voxel_values.size
    if voxel_count == 0:
        return jsonify({'ok': False, 'error': 'No volumetric cells survived thresholding'}), 400

    if voxel_count > max_points:
        order = np.argsort(voxel_values)[-max_points:]
        z_idx = z_idx[order]
        y_idx = y_idx[order]
        x_idx = x_idx[order]
        voxel_values = voxel_values[order]

    xy_radius_m = float(xy_step_km * 1000.0 * (0.62 if gap_fill else 0.56))
    height_m = float(z_step_km * 1000.0)

    voxels = []
    for zi, yi, xi, val in zip(z_idx.tolist(), y_idx.tolist(), x_idx.tolist(), voxel_values.tolist()):
        xk = float(x_centers[xi])
        yk = float(y_centers[yi])
        lat = lat0 + (yk / 111.32)
        lon = lon0 + (xk / (111.32 * max(np.cos(np.deg2rad(lat0)), 1e-6)))
        strength = float(np.clip((val - value_lo) / value_span, 0.0, 1.0))
        voxels.append({
            'lat': round(lat, 6),
            'lon': round(lon, 6),
            'base_m': round(float(z_edges[zi] * 1000.0), 1),
            'height_m': round(height_m, 1),
            'radius_m': round(xy_radius_m, 1),
            'value': round(float(val), 2),
            'strength': round(strength, 4),
            'mean_norm': round(strength, 4),
            'isCore': bool(strength >= 0.60),
            'isHot': bool(strength >= 0.82),
        })

    return jsonify({
        'ok': True,
        'field': field,
        'points': [],
        'voxels': voxels,
        'point_count': len(voxels),
        'stride': int(max(az_stride, rng_stride)),
        'center_lat': lat0,
        'center_lon': lon0,
        'max_range_km': max_range_km,
        'max_height_m': float(np.nanmax(z_edges) * 1000.0) if z_edges.size else alt0_m,
        'value_min': value_lo,
        'value_max': value_hi,
        'warnings': warnings,
        'quality': render_quality,
        'interpolate': bool(interpolate),
        'gap_fill': bool(gap_fill),
        'voxel_mode': True,
    })


@radar_bp.route('/accumulation_series')
def radar_accumulation_series():
    group = request.args.get('group')
    field = request.args.get('field')
    requested_sweep = request.args.get('sweep')
    requested_elevation = request.args.get('elevation')
    try:
        cappi_height_km = float(request.args.get('cappi_height_km', '2.0') or '2.0')
    except Exception:
        cappi_height_km = 2.0
    try:
        lat = float(request.args.get('lat'))
        lon = float(request.args.get('lon'))
    except Exception:
        return jsonify({'ok': False, 'error': 'Invalid coordinates'}), 400

    filters_arg = request.args.get('filters')
    try:
        filters = json.loads(filters_arg) if filters_arg else {}
    except Exception:
        filters = {}

    if not radar_groups or group not in radar_groups:
        return jsonify({'ok': False, 'error': 'No radar files loaded'}), 404

    if not field or field not in available_fields:
        if 'DBZ2' in available_fields:
            field = 'DBZ2'
        elif available_fields:
            field = available_fields[0]
        else:
            return jsonify({'ok': False, 'error': 'No radar fields available'}), 404

    try:
        series = _sample_accumulation_series_by_group(
            group,
            field,
            lat,
            lon,
            filters=filters,
            requested_sweep=requested_sweep,
            requested_elevation=requested_elevation,
            cappi_height_km=cappi_height_km,
        )
        return jsonify({
            'ok': True,
            'group': group,
            'field': field,
            'product': 'ACC',
            'unit': 'mm',
            'accumulation_minutes': float(merge_window_minutes),
            'lat': float(lat),
            'lon': float(lon),
            'series': series,
        })
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500

