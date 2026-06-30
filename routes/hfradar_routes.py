from __future__ import annotations

import json
from collections import OrderedDict
from datetime import datetime, timezone
from hashlib import md5
from io import BytesIO
from typing import Any

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import numpy as np
from flask import Blueprint, current_app, jsonify, redirect, render_template, request, send_file, url_for
from scipy.ndimage import gaussian_filter
from routes.auth_utils import project_required
try:
    import xarray as xr
except Exception:  # pragma: no cover
    xr = None

hfradar_bp = Blueprint(
    'hfradar_bp',
    __name__,
    template_folder='templates',
    static_folder='static',
    url_prefix='/hfradarviewer',
)
@hfradar_bp.before_request
@project_required("hfradar")
def restrict_hfradarviewer_to_admin():
    pass
uploaded_files_mem: list[dict[str, Any]] = []
hfradar_groups: dict[str, list[dict[str, Any]]] = {}
available_fields: list[str] = []
radar_extent = [[-10.0, 100.0], [10.0, 120.0]]
time_bounds: dict[str, dict[str, float]] = {}
frame_catalog: dict[str, list[dict[str, Any]]] = {}
render_cache: 'OrderedDict[str, dict[str, Any]]' = OrderedDict()
volume_cache: 'OrderedDict[str, dict[str, Any]]' = OrderedDict()
CACHE_MAX_ITEMS = 96

FIELD_CONFIGS = {
    'Hs': dict(vmin=0.0, vmax=6.0, cmap='turbo', unit='m', label='Significant Wave Height'),
    'Wdir': dict(vmin=0.0, vmax=360.0, cmap='twilight_shifted', unit='deg', label='Wave Direction'),
    'Tmean': dict(vmin=0.0, vmax=20.0, cmap='viridis', unit='s', label='Mean Wave Period'),
    'Tenergy': dict(vmin=0.0, vmax=20.0, cmap='plasma', unit='s', label='Energy Wave Period'),
    'U10': dict(vmin=0.0, vmax=25.0, cmap='viridis', unit='m/s', label='Wind Speed 10 m'),
    'Udir': dict(vmin=0.0, vmax=360.0, cmap='twilight_shifted', unit='deg', label='Wind Direction'),
    'ewct': dict(vmin=-2.5, vmax=2.5, cmap='coolwarm', unit='m/s', label='Eastward Current'),
    'nsct': dict(vmin=-2.5, vmax=2.5, cmap='coolwarm', unit='m/s', label='Northward Current'),
    'ewct_error': dict(vmin=0.0, vmax=1.0, cmap='magma', unit='m/s', label='Eastward Current Error'),
    'nsct_error': dict(vmin=0.0, vmax=1.0, cmap='magma', unit='m/s', label='Northward Current Error'),
    'CurrentSpeed': dict(vmin=0.0, vmax=2.5, cmap='turbo', unit='m/s', label='Current Speed'),
    'CurrentDir': dict(vmin=0.0, vmax=360.0, cmap='twilight_shifted', unit='deg', label='Current Direction'),
    'gdopx': dict(vmin=0.0, vmax=10.0, cmap='magma', unit='', label='GDOP X'),
    'gdopy': dict(vmin=0.0, vmax=10.0, cmap='magma', unit='', label='GDOP Y'),
    'qual': dict(vmin=0.0, vmax=127.0, cmap='gray', unit='', label='Quality Flag'),
    'kl_qual': dict(vmin=0.0, vmax=127.0, cmap='gray', unit='', label='Current Vector Quality'),
}

QUALITY_PRESETS = {
    'low': {'figsize': 5.2, 'dpi': 110, 'max_points': 6000},
    'medium': {'figsize': 6.2, 'dpi': 140, 'max_points': 11000},
    'high': {'figsize': 7.2, 'dpi': 180, 'max_points': 18000},
    'ultra': {'figsize': 8.4, 'dpi': 220, 'max_points': 26000},
}

QC_PRESETS = {
    'off': {
        'enabled': False,
        'use_qual': False,
        'qual_min': 1.0,
        'use_gdop_xy': False,
        'gdopx_max': 3.0,
        'gdopy_max': 3.0,
        'use_gdop_combined': False,
        'gdop_combined_max': 4.0,
        'use_hs_range': False,
        'hs_min': 0.0,
        'hs_max': 8.0,
        'use_tmean_range': False,
        'tmean_min': 1.0,
        'tmean_max': 20.0,
        'use_tenergy_range': False,
        'tenergy_min': 1.0,
        'tenergy_max': 25.0,
        'use_wdir_valid': False,
    },
    'loose': {
        'enabled': True,
        'use_qual': True,
        'qual_min': 1.0,
        'use_gdop_xy': True,
        'gdopx_max': 5.0,
        'gdopy_max': 5.0,
        'use_gdop_combined': False,
        'gdop_combined_max': 6.5,
        'use_hs_range': True,
        'hs_min': 0.0,
        'hs_max': 10.0,
        'use_tmean_range': True,
        'tmean_min': 0.5,
        'tmean_max': 25.0,
        'use_tenergy_range': True,
        'tenergy_min': 0.5,
        'tenergy_max': 28.0,
        'use_wdir_valid': True,
    },
    'normal': {
        'enabled': True,
        'use_qual': True,
        'qual_min': 2.0,
        'use_gdop_xy': True,
        'gdopx_max': 3.5,
        'gdopy_max': 3.5,
        'use_gdop_combined': False,
        'gdop_combined_max': 4.5,
        'use_hs_range': True,
        'hs_min': 0.0,
        'hs_max': 8.0,
        'use_tmean_range': True,
        'tmean_min': 1.0,
        'tmean_max': 20.0,
        'use_tenergy_range': True,
        'tenergy_min': 1.0,
        'tenergy_max': 22.0,
        'use_wdir_valid': True,
    },
    'strict': {
        'enabled': True,
        'use_qual': True,
        'qual_min': 3.0,
        'use_gdop_xy': True,
        'gdopx_max': 2.5,
        'gdopy_max': 2.5,
        'use_gdop_combined': True,
        'gdop_combined_max': 3.5,
        'use_hs_range': True,
        'hs_min': 0.0,
        'hs_max': 7.0,
        'use_tmean_range': True,
        'tmean_min': 1.5,
        'tmean_max': 18.0,
        'use_tenergy_range': True,
        'tenergy_min': 1.5,
        'tenergy_max': 20.0,
        'use_wdir_valid': True,
    },
}



FIELD_ALIASES = {
    'qual': ('qual', 'kl_qual'),
    'CurrentSpeed': ('CurrentSpeed',),
    'CurrentDir': ('CurrentDir',),
}

VECTOR_FIELD_MAP = {
    'Wdir': ('Hs', 'Wave Direction'),
    'Udir': ('U10', 'Wind Direction'),
    'CurrentDir': ('CurrentSpeed', 'Current Direction'),
}


def _resolve_quality_field(ds):
    if 'qual' in ds:
        return 'qual'
    if 'kl_qual' in ds:
        return 'kl_qual'
    return None


def _resolve_field_name(ds, field: str) -> str | None:
    if field in ds:
        return field
    if field == 'qual':
        return _resolve_quality_field(ds)
    if field in ('CurrentSpeed', 'CurrentDir'):
        return field
    for candidate in FIELD_ALIASES.get(field, (field,)):
        if candidate in ds:
            return candidate
    return None


def _detect_product_type(ds) -> str:
    vars_set = set(ds.data_vars)
    if {'Hs', 'Wdir', 'Tmean', 'Tenergy'} & vars_set:
        return 'wave'
    if {'U10', 'Udir'} & vars_set:
        return 'wind'
    if {'ewct', 'nsct'} & vars_set:
        return 'current'
    return 'generic'


def _preferred_field_for_vars(vars_here: list[str]) -> str:
    for candidate in ('Hs', 'U10', 'CurrentSpeed', 'ewct', 'Wdir', 'Udir', 'CurrentDir', 'gdopx', 'gdopy', 'qual', 'kl_qual'):
        if candidate in vars_here:
            return candidate
    return vars_here[0] if vars_here else 'Hs'


def _resolve_frame_field(frame: dict[str, Any], requested_field: str | None) -> str:
    fields_here = list(frame.get('fields') or [])
    if requested_field and requested_field in fields_here:
        return requested_field
    return frame.get('default_field') or _preferred_field_for_vars(fields_here)


def _cache_store(cache_obj: OrderedDict, key: str, value: Any, max_items: int = CACHE_MAX_ITEMS):
    cache_obj[key] = value
    cache_obj.move_to_end(key)
    while len(cache_obj) > max_items:
        cache_obj.popitem(last=False)


def _cache_get(cache_obj: OrderedDict, key: str):
    if key not in cache_obj:
        return None
    cache_obj.move_to_end(key)
    return cache_obj[key]


def _normalize_render_quality(value: Any) -> str:
    q = str(value or 'high').strip().lower()
    return q if q in QUALITY_PRESETS else 'high'




def _apply_orientation(lons, lats, arr):
    arr = np.asarray(arr, dtype=float)
    lons = np.asarray(lons, dtype=float)
    lats = np.asarray(lats, dtype=float)
    if arr.shape == (len(lons), len(lats)):
        arr = arr.T
    if len(lats) > 1 and lats[0] > lats[-1]:
        lats = lats[::-1]
        arr = arr[::-1, :]
    if len(lons) > 1 and lons[0] > lons[-1]:
        lons = lons[::-1]
        arr = arr[:, ::-1]
    return lons, lats, arr


def _to_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def _to_float(value: Any, default: float) -> float:
    try:
        if value in (None, ''):
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _parse_qc_config(req):
    preset = str(req.args.get('qc_preset', 'off') or 'off').strip().lower()
    base = dict(QC_PRESETS.get(preset, QC_PRESETS['off']))
    base['preset'] = preset if preset in QC_PRESETS else 'off'
    enabled = _to_bool(req.args.get('qc_enabled'), False)
    base['enabled'] = enabled
    for key in ('use_qual','use_gdop_xy','use_gdop_combined','use_hs_range','use_tmean_range','use_tenergy_range','use_wdir_valid'):
        base[key] = _to_bool(req.args.get(key), base.get(key, False)) if enabled else False
    for key in ('qual_min','gdopx_max','gdopy_max','gdop_combined_max','hs_min','hs_max','tmean_min','tmean_max','tenergy_min','tenergy_max'):
        base[key] = _to_float(req.args.get(key), base.get(key, 0.0))
    return base


def _extract_raw_field_2d(entry: dict[str, Any], field: str, time_index: int = 0):
    ds = _open_dataset_from_entry(entry)
    with ds:
        lon_name, lat_name, time_name = _get_xy_names(ds)
        resolved = _resolve_field_name(ds, field)
        lons = np.asarray(ds[lon_name].values, dtype=float)
        lats = np.asarray(ds[lat_name].values, dtype=float)

        if field == 'CurrentSpeed':
            if 'ewct' not in ds or 'nsct' not in ds:
                return None, None, None
            u = ds['ewct']
            v = ds['nsct']
            if time_name and time_name in u.dims:
                u_arr = np.asarray(u.isel({time_name: time_index}).values, dtype=float)
                v_arr = np.asarray(v.isel({time_name: time_index}).values, dtype=float)
            else:
                u_arr = np.asarray(u.values, dtype=float)
                v_arr = np.asarray(v.values, dtype=float)
            arr = np.sqrt(np.square(u_arr) + np.square(v_arr))
        elif field == 'CurrentDir':
            if 'ewct' not in ds or 'nsct' not in ds:
                return None, None, None
            u = ds['ewct']
            v = ds['nsct']
            if time_name and time_name in u.dims:
                u_arr = np.asarray(u.isel({time_name: time_index}).values, dtype=float)
                v_arr = np.asarray(v.isel({time_name: time_index}).values, dtype=float)
            else:
                u_arr = np.asarray(u.values, dtype=float)
                v_arr = np.asarray(v.values, dtype=float)
            arr = (np.degrees(np.arctan2(u_arr, v_arr)) + 360.0) % 360.0
        elif not resolved:
            return None, None, None
        else:
            var = ds[resolved]
            if time_name and time_name in var.dims:
                arr = np.asarray(var.isel({time_name: time_index}).values, dtype=float)
            else:
                arr = np.asarray(var.values, dtype=float)
        lons, lats, arr = _apply_orientation(lons, lats, arr)
        return lons, lats, arr


def _build_qc_mask(entry: dict[str, Any], time_index: int, qc_config: dict[str, Any] | None):
    cfg = dict(qc_config or {})
    if not cfg.get('enabled'):
        return None, []
    warnings = []
    _, _, qual = _extract_raw_field_2d(entry, 'qual', time_index)
    _, _, gdopx = _extract_raw_field_2d(entry, 'gdopx', time_index)
    _, _, gdopy = _extract_raw_field_2d(entry, 'gdopy', time_index)
    _, _, hs = _extract_raw_field_2d(entry, 'Hs', time_index)
    _, _, tmean = _extract_raw_field_2d(entry, 'Tmean', time_index)
    _, _, tenergy = _extract_raw_field_2d(entry, 'Tenergy', time_index)
    _, _, wdir = _extract_raw_field_2d(entry, 'Wdir', time_index)
    _, _, udir = _extract_raw_field_2d(entry, 'Udir', time_index)
    _, _, current_dir = _extract_raw_field_2d(entry, 'CurrentDir', time_index)
    direction_arr = next((a for a in (wdir, udir, current_dir) if isinstance(a, np.ndarray) and a.ndim == 2), None)
    shape = next((a.shape for a in (qual, gdopx, gdopy, hs, tmean, tenergy, direction_arr) if isinstance(a, np.ndarray) and a.ndim == 2), None)
    if shape is None:
        return None, ['No QC-supporting fields found']
    mask = np.ones(shape, dtype=bool)
    if cfg.get('use_qual'):
        if qual is None: warnings.append('quality field not found')
        else: mask &= np.isfinite(qual) & (qual >= cfg.get('qual_min', 1.0))
    if cfg.get('use_gdop_xy'):
        if gdopx is None or gdopy is None: warnings.append('gdopx/gdopy not found')
        else:
            mask &= np.isfinite(gdopx) & (gdopx <= cfg.get('gdopx_max', 3.0))
            mask &= np.isfinite(gdopy) & (gdopy <= cfg.get('gdopy_max', 3.0))
    if cfg.get('use_gdop_combined'):
        if gdopx is None or gdopy is None: warnings.append('combined GDOP unavailable')
        else:
            gdop_comb = np.sqrt(np.square(gdopx) + np.square(gdopy))
            mask &= np.isfinite(gdop_comb) & (gdop_comb <= cfg.get('gdop_combined_max', 4.0))
    if cfg.get('use_hs_range') and hs is not None:
        mask &= np.isfinite(hs) & (hs >= cfg.get('hs_min', 0.0)) & (hs <= cfg.get('hs_max', 8.0))
    elif cfg.get('use_hs_range'):
        warnings.append('Hs not found for QC')
    if cfg.get('use_tmean_range') and tmean is not None:
        mask &= np.isfinite(tmean) & (tmean >= cfg.get('tmean_min', 1.0)) & (tmean <= cfg.get('tmean_max', 20.0))
    elif cfg.get('use_tmean_range'):
        warnings.append('Tmean not found for QC')
    if cfg.get('use_tenergy_range') and tenergy is not None:
        mask &= np.isfinite(tenergy) & (tenergy >= cfg.get('tenergy_min', 1.0)) & (tenergy <= cfg.get('tenergy_max', 25.0))
    elif cfg.get('use_tenergy_range'):
        warnings.append('Tenergy not found for QC')
    if cfg.get('use_wdir_valid') and direction_arr is not None:
        mask &= np.isfinite(direction_arr) & (direction_arr >= 0.0) & (direction_arr <= 360.0)
    elif cfg.get('use_wdir_valid'):
        warnings.append('direction field not found')
    return mask, warnings


def _safe_iso_to_epoch(value: Any) -> float | None:
    try:
        if value is None:
            return None
        if isinstance(value, np.datetime64):
            return float(value.astype('datetime64[s]').astype(np.int64))
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return float(value.timestamp())
        text = str(value)
        if not text:
            return None
        if text.endswith('Z'):
            text = text[:-1] + '+00:00'
        return float(datetime.fromisoformat(text).timestamp())
    except Exception:
        return None


def _format_epoch(epoch: float | None) -> str:
    if epoch is None:
        return 'N/A'
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')


def _open_dataset_from_entry(entry: dict[str, Any]):
    if xr is None:
        raise RuntimeError('xarray is not installed')
    return xr.open_dataset(BytesIO(entry['content']))


def _get_xy_names(ds):
    lon_name = 'longitude' if 'longitude' in ds.variables else ('lon' if 'lon' in ds.variables else None)
    lat_name = 'latitude' if 'latitude' in ds.variables else ('lat' if 'lat' in ds.variables else None)
    time_name = 'time' if 'time' in ds.variables else None
    if not lon_name or not lat_name:
        raise RuntimeError('longitude/latitude variables not found')
    return lon_name, lat_name, time_name


def _extract_data_variables(ds) -> list[str]:
    lon_name, lat_name, time_name = _get_xy_names(ds)
    excluded = {lon_name, lat_name}
    if time_name:
        excluded.add(time_name)
    vars_out = []
    for name, var in ds.data_vars.items():
        if name in excluded:
            continue
        dims = set(var.dims)
        if lon_name in dims and lat_name in dims:
            vars_out.append(name)
    if 'ewct' in ds.data_vars and 'nsct' in ds.data_vars:
        vars_out.extend(['CurrentSpeed', 'CurrentDir'])
    if 'kl_qual' in ds.data_vars and 'qual' not in vars_out:
        vars_out.append('qual')
    preferred = ['Hs', 'Wdir', 'Tmean', 'Tenergy', 'U10', 'Udir', 'CurrentSpeed', 'CurrentDir', 'ewct', 'nsct', 'ewct_error', 'nsct_error', 'gdopx', 'gdopy', 'qual', 'kl_qual']
    ordered = [v for v in preferred if v in vars_out] + [v for v in vars_out if v not in preferred]
    return list(dict.fromkeys(ordered))


def _weighted_gaussian_nan_2d(arr: Any, sigma: float = 0.0):
    data = np.asarray(arr, dtype=float)
    if data.ndim != 2:
        return data
    sigma = float(sigma or 0.0)
    if sigma <= 0:
        return data
    valid = np.isfinite(data)
    if not np.any(valid):
        return data
    weighted = np.where(valid, data, 0.0)
    weights = valid.astype(float)
    smooth_data = gaussian_filter(weighted, sigma=sigma)
    smooth_weights = gaussian_filter(weights, sigma=sigma)
    with np.errstate(invalid='ignore', divide='ignore'):
        out = smooth_data / smooth_weights
    return np.where(smooth_weights > 0.03, out, np.nan)


def _interpolate_grid_2d(arr: Any, strength: float = 0.0):
    data = np.asarray(arr, dtype=float)
    strength = float(strength or 0.0)
    if data.ndim != 2 or strength <= 0:
        return data
    sigma = max(0.15, min(5.5, strength / 18.0))
    alpha = max(0.0, min(1.0, strength / 100.0))
    smooth = _weighted_gaussian_nan_2d(data, sigma=sigma)
    valid = np.isfinite(data)
    filled = np.where(valid, ((1.0 - alpha) * data) + (alpha * smooth), smooth)
    return np.where(np.isfinite(filled), filled, data)


def _apply_smoothing_2d(arr: Any, strength: float = 0.0):
    data = np.asarray(arr, dtype=float)
    strength = float(strength or 0.0)
    if data.ndim != 2 or strength <= 0:
        return data
    sigma = max(0.10, min(6.0, strength / 16.0))
    alpha = max(0.0, min(1.0, strength / 100.0))
    smooth = _weighted_gaussian_nan_2d(data, sigma=sigma)
    valid = np.isfinite(data)
    return np.where(valid, ((1.0 - alpha) * data) + (alpha * smooth), np.nan)


def _build_groups():
    global hfradar_groups, available_fields, radar_extent, time_bounds, frame_catalog
    hfradar_groups = {}
    time_bounds = {}
    frame_catalog = {}
    available_fields = []
    render_cache.clear()
    volume_cache.clear()

    frames = []
    fields_seen: set[str] = set()
    extent_done = False
    for entry in uploaded_files_mem:
        try:
            ds = _open_dataset_from_entry(entry)
        except Exception as exc:
            current_app.logger.warning('Skipping %s: %s', entry.get('filename'), exc)
            continue
        with ds:
            lon_name, lat_name, time_name = _get_xy_names(ds)
            lons = np.asarray(ds[lon_name].values, dtype=float)
            lats = np.asarray(ds[lat_name].values, dtype=float)
            if lons.size == 0 or lats.size == 0:
                continue
            if not extent_done:
                radar_extent = [[float(np.nanmin(lats)), float(np.nanmin(lons))], [float(np.nanmax(lats)), float(np.nanmax(lons))]]
                extent_done = True
            vars_here = _extract_data_variables(ds)
            product_type = _detect_product_type(ds)
            default_field = _preferred_field_for_vars(vars_here)
            fields_seen.update(vars_here)

            if time_name and ds[time_name].size:
                time_values = np.atleast_1d(ds[time_name].values)
                for idx, t in enumerate(time_values):
                    epoch = _safe_iso_to_epoch(t)
                    frames.append({
                        'files': [entry],
                        'file_count': 1,
                        'time': _format_epoch(epoch),
                        'epoch': epoch,
                        'time_index': idx,
                        'filename': entry.get('filename', 'dataset.nc'),
                        'fields': list(vars_here),
                        'product_type': product_type,
                        'default_field': default_field,
                    })
            else:
                frames.append({
                    'files': [entry],
                    'file_count': 1,
                    'time': entry.get('filename', 'dataset.nc'),
                    'epoch': None,
                    'time_index': 0,
                    'filename': entry.get('filename', 'dataset.nc'),
                    'fields': list(vars_here),
                    'product_type': product_type,
                    'default_field': default_field,
                })

    frames.sort(key=lambda item: (item['epoch'] if item['epoch'] is not None else float('inf'), item['filename']))
    hfradar_groups = {'HF Radar Ocean Sensing': frames} if frames else {}
    frame_catalog = {'HF Radar Ocean Sensing': [
        {
            'time': item.get('time'),
            'epoch': item.get('epoch'),
            'filename': item.get('filename'),
            'fields': list(item.get('fields') or []),
            'product_type': item.get('product_type', 'generic'),
            'default_field': item.get('default_field'),
        }
        for item in frames
    ]} if frames else {}
    preferred_all = ['Hs', 'Wdir', 'Tmean', 'Tenergy', 'U10', 'Udir', 'CurrentSpeed', 'CurrentDir', 'ewct', 'nsct', 'ewct_error', 'nsct_error', 'gdopx', 'gdopy', 'qual', 'kl_qual']
    available_fields = [f for f in preferred_all if f in fields_seen] + [f for f in sorted(fields_seen) if f not in set(preferred_all)]
    epochs = [f['epoch'] for f in frames if f['epoch'] is not None]
    if epochs:
        time_bounds['HF Radar Ocean Sensing'] = {'min': min(epochs), 'max': max(epochs)}


def _get_frame(group: str, idx: int) -> dict[str, Any]:
    if group not in hfradar_groups:
        raise RuntimeError('Group not found')
    frames = hfradar_groups[group]
    if not frames:
        raise RuntimeError('No frames available')
    return frames[max(0, min(idx, len(frames) - 1))]


def _extract_field_2d(
    entry: dict[str, Any],
    field: str,
    time_index: int = 0,
    qc_enabled: bool = False,
    interp_strength: float = 0.0,
    smoothing_strength: float = 0.0,
    qc_config: dict[str, Any] | None = None,
):
    lons, lats, arr = _extract_raw_field_2d(entry, field, time_index)
    if arr is None:
        raise RuntimeError(f'Field {field} not found in dataset')
    _, _, qual = _extract_raw_field_2d(entry, 'qual', time_index)
    qc_mask = None
    qc_warnings = []
    if qc_config and qc_config.get('enabled'):
        qc_mask, qc_warnings = _build_qc_mask(entry, time_index, qc_config)
    elif qc_enabled and qual is not None and field != 'qual':
        qc_mask = np.isfinite(qual) & (qual > 0)
    if qc_mask is not None and field != 'qual':
        arr = np.where(qc_mask, arr, np.nan)
    if field != 'qual':
        arr = _interpolate_grid_2d(arr, interp_strength)
        arr = _apply_smoothing_2d(arr, smoothing_strength)
    return lons, lats, arr, qual, qc_warnings


def _field_unit(field: str) -> str:
    return FIELD_CONFIGS.get(field, {}).get('unit', '')


def _field_label(field: str) -> str:
    return FIELD_CONFIGS.get(field, {}).get('label', field)



def _build_matplotlib_cmap(custom_cmap_raw: str):
    if not custom_cmap_raw:
        return None
    try:
        spec = json.loads(custom_cmap_raw)
        if not isinstance(spec, dict):
            return None
        mode = 'ranges' if str(spec.get('mode', 'gradients')).lower() == 'ranges' else 'gradients'
        stops = []
        for stop in spec.get('stops', []):
            value = float(stop.get('value'))
            color = str(stop.get('color', '')).strip()
            if not color.startswith('#'):
                color = '#' + color
            if len(color) != 7:
                continue
            stops.append((value, color))
        if len(stops) < 2:
            return None
        stops.sort(key=lambda item: item[0])
        if mode == 'ranges':
            return mcolors.ListedColormap([color for _, color in stops], name='hfr_custom_ranges')
        min_v = stops[0][0]
        max_v = stops[-1][0]
        span = max(max_v - min_v, 1e-6)
        color_points = [((value - min_v) / span, color) for value, color in stops]
        return mcolors.LinearSegmentedColormap.from_list('hfr_custom_gradients', color_points)
    except Exception:
        return None


def _render_cache_key(
    group: str,
    idx: int,
    field: str,
    qc_enabled: bool,
    interp_strength: float,
    smoothing_strength: float,
    cmap: str,
    custom_cmap_raw: str,
    vmin: Any,
    vmax: Any,
    quality: str,
    qc_config: dict[str, Any] | None = None,
) -> str:
    raw = json.dumps({
        'g': group,
        'i': idx,
        'f': field,
        'qc': qc_enabled,
        'qc_cfg': qc_config or {},
        'interp': float(interp_strength),
        'smooth': float(smoothing_strength),
        'c': cmap,
        'cc': custom_cmap_raw,
        'vmin': vmin,
        'vmax': vmax,
        'q': quality,
    }, sort_keys=True, default=str)
    return md5(raw.encode('utf-8')).hexdigest()


def _render_overlay_png(
    group: str,
    idx: int,
    field: str,
    qc_enabled: bool,
    interp_strength: float = 0.0,
    smoothing_strength: float = 0.0,
    cmap_override: str = '',
    custom_cmap_raw: str = '',
    vmin_override=None,
    vmax_override=None,
    quality: str = 'high',
    qc_config: dict[str, Any] | None = None,
):
    frame = _get_frame(group, idx)
    entry = frame['files'][0]
    lons, lats, arr, qual, _qc_warn = _extract_field_2d(
        entry, field, frame.get('time_index', 0),
        qc_enabled=qc_enabled,
        interp_strength=interp_strength,
        smoothing_strength=smoothing_strength,
        qc_config=qc_config,
    )
    cfg = FIELD_CONFIGS.get(field, {})
    vmin = cfg.get('vmin') if vmin_override is None else vmin_override
    vmax = cfg.get('vmax') if vmax_override is None else vmax_override
    cmap = _build_matplotlib_cmap(custom_cmap_raw) or cmap_override or cfg.get('cmap', 'turbo')
    qcfg = QUALITY_PRESETS[_normalize_render_quality(quality)]

    fig, ax = plt.subplots(figsize=(qcfg['figsize'], qcfg['figsize']))
    if arr.ndim == 2:
        extent = [float(np.nanmin(lons)), float(np.nanmax(lons)), float(np.nanmin(lats)), float(np.nanmax(lats))]
        ax.imshow(arr, origin='lower', extent=extent, cmap=cmap, vmin=vmin, vmax=vmax, interpolation='nearest', aspect='auto')
    else:
        raise RuntimeError('Only 2D gridded data is supported')
    ax.axis('off')
    buf = BytesIO()
    plt.savefig(buf, format='png', dpi=qcfg['dpi'], bbox_inches='tight', pad_inches=0, transparent=True)
    plt.close(fig)
    buf.seek(0)
    bounds = [[float(np.nanmin(lats)), float(np.nanmin(lons))], [float(np.nanmax(lats)), float(np.nanmax(lons))]]
    meta = {
        'bounds': bounds,
        'time': frame.get('time', 'N/A'),
        'file_count': frame.get('file_count', 1),
        'filename': frame.get('filename', 'dataset.nc'),
        'qc_enabled': bool(qc_enabled),
        'warnings': list(_qc_warn) if qc_config and qc_config.get('enabled') else ([] if qual is not None or not qc_enabled else ['qual parameter not found, QC Data skipped']),
        'interp_strength': float(interp_strength),
        'smoothing_strength': float(smoothing_strength),
    }
    return buf.getvalue(), meta


def _build_volume_data(
    group: str,
    idx: int,
    field: str,
    qc_enabled: bool,
    interp_strength: float = 0.0,
    smoothing_strength: float = 0.0,
    quality: str = 'high',
    qc_config: dict[str, Any] | None = None,
):
    key = _render_cache_key(group, idx, field, qc_enabled, interp_strength, smoothing_strength, '', '', '', '', 'vol-' + quality, qc_config=qc_config)
    cached = _cache_get(volume_cache, key)
    if cached is not None:
        return cached
    frame = _get_frame(group, idx)
    entry = frame['files'][0]
    lons, lats, arr, qual, _qc_warn = _extract_field_2d(
        entry, field, frame.get('time_index', 0),
        qc_enabled=qc_enabled,
        interp_strength=interp_strength,
        smoothing_strength=smoothing_strength,
        qc_config=qc_config,
    )
    arr = np.asarray(arr, dtype=float)
    if arr.ndim != 2:
        raise RuntimeError('Only 2D gridded data is supported')
    lon_grid, lat_grid = np.meshgrid(lons, lats)
    valid = np.isfinite(arr)
    flat_vals = arr[valid]
    if flat_vals.size == 0:
        payload = {'voxels': [], 'points': [], 'value_min': 0, 'value_max': 1, 'max_height_m': 1}
        _cache_store(volume_cache, key, payload)
        return payload

    qcfg = QUALITY_PRESETS[_normalize_render_quality(quality)]
    max_points = qcfg['max_points']
    coords = np.column_stack([lon_grid[valid], lat_grid[valid], flat_vals])
    if coords.shape[0] > max_points:
        step = max(1, int(np.ceil(coords.shape[0] / max_points)))
        coords = coords[::step]

    vmin = float(np.nanmin(coords[:, 2]))
    vmax = float(np.nanmax(coords[:, 2]))
    span = max(1e-6, vmax - vmin)
    dx = abs(float(lons[1] - lons[0])) if len(lons) > 1 else 0.01
    dy = abs(float(lats[1] - lats[0])) if len(lats) > 1 else 0.01
    radius_m = max(150.0, ((dx + dy) * 0.5) * 111320.0 * 0.45)
    voxels = []
    points = []
    for lon, lat, val in coords:
        norm = (float(val) - vmin) / span
        height_m = 600.0 + norm * 8400.0
        alpha = 30 + int(norm * 225)
        voxels.append({
            'lon': float(lon),
            'lat': float(lat),
            'value': float(val),
            'base_m': 0.0,
            'height_m': float(height_m),
            'radius_m': radius_m,
            'strength': float(norm),
            'mean_norm': float(norm),
            'isCore': bool(norm >= 0.62),
            'isHot': bool(norm >= 0.82),
        })
        points.append({'lon': float(lon), 'lat': float(lat), 'value': float(val), 'alt_m': float(height_m), 'alpha': alpha})
    payload = {'voxels': voxels, 'points': points, 'value_min': vmin, 'value_max': vmax, 'max_height_m': 9000.0}
    _cache_store(volume_cache, key, payload)
    return payload


@hfradar_bp.route('/precompute', methods=['POST'])
def hfradar_precompute():
    return jsonify({'ok': True})


@hfradar_bp.route('/sample_point')
def hfradar_sample_point():
    group = request.args.get('group') or 'HF Radar Ocean Sensing'
    idx = int(request.args.get('frame', 0))
    requested_field = request.args.get('field') or (available_fields[0] if available_fields else 'Hs')
    lat = float(request.args.get('lat'))
    lon = float(request.args.get('lon'))
    qc_enabled = str(request.args.get('qc_enabled', '0')).lower() in ('1', 'true', 'yes', 'on')
    qc_config = _parse_qc_config(request)
    interp_strength = float(request.args.get('interp_strength', 0) or 0)
    smoothing_strength = float(request.args.get('smoothing_strength', 0) or 0)

    frame = _get_frame(group, idx)
    field = _resolve_frame_field(frame, requested_field)
    entry = frame['files'][0]
    lons, lats, arr, qual, _qc_warn = _extract_field_2d(
        entry, field, frame.get('time_index', 0),
        qc_enabled=qc_enabled,
        interp_strength=interp_strength,
        smoothing_strength=smoothing_strength,
        qc_config=qc_config,
    )
    lon_idx = int(np.argmin(np.abs(np.asarray(lons, dtype=float) - lon)))
    lat_idx = int(np.argmin(np.abs(np.asarray(lats, dtype=float) - lat)))
    value = arr[lat_idx, lon_idx] if arr.ndim == 2 else np.nan
    qv = qual[lat_idx, lon_idx] if qual is not None and np.ndim(qual) == 2 else np.nan
    return jsonify({
        'ok': bool(np.isfinite(value)),
        'product': 'HF Radar Grid',
        'field': _field_label(field),
        'source_field': field,
        'unit': _field_unit(field),
        'lat': float(lats[lat_idx]),
        'lon': float(lons[lon_idx]),
        'value': None if not np.isfinite(value) else float(value),
        'details': {
            'grid_lat_index': lat_idx,
            'grid_lon_index': lon_idx,
            'qual': None if not np.isfinite(qv) else float(qv),
            'qc_enabled': qc_config.get('enabled', qc_enabled),
            'qc_preset': qc_config.get('preset', 'off'),
            'interp_strength': float(interp_strength),
            'smoothing_strength': float(smoothing_strength),
        },
        'warnings': list(_qc_warn) if qc_config and qc_config.get('enabled') else ([] if qual is not None or not qc_enabled else ['qual parameter not found, QC Data skipped']),
    })



@hfradar_bp.route('/vector_data')
def hfradar_vector_data():
    group = request.args.get('group') or 'HF Radar Ocean Sensing'
    idx = int(request.args.get('frame', 0))
    requested_field = request.args.get('field') or 'Wdir'
    quality = _normalize_render_quality(request.args.get('quality'))
    qc_enabled = str(request.args.get('qc_enabled', '0')).lower() in ('1', 'true', 'yes', 'on')
    qc_config = _parse_qc_config(request)
    interp_strength = float(request.args.get('interp_strength', 0) or 0)
    smoothing_strength = float(request.args.get('smoothing_strength', 0) or 0)

    frame = _get_frame(group, idx)
    field = _resolve_frame_field(frame, requested_field)
    entry = frame['files'][0]
    lons, lats, arr, qual, _qc_warn = _extract_field_2d(
        entry, field, frame.get('time_index', 0),
        qc_enabled=qc_enabled,
        interp_strength=interp_strength,
        smoothing_strength=smoothing_strength,
        qc_config=qc_config,
    )
    if arr.ndim != 2:
        return jsonify({'vectors': [], 'count': 0, 'field': field})

    lon_grid, lat_grid = np.meshgrid(lons, lats)
    valid = np.isfinite(arr)
    if not np.any(valid):
        return jsonify({'vectors': [], 'count': 0, 'field': field})

    target_count = {'low': 220, 'medium': 420, 'high': 700, 'ultra': 1000}.get(quality, 700)
    step_y = max(1, int(np.ceil(arr.shape[0] / np.sqrt(target_count))))
    step_x = max(1, int(np.ceil(arr.shape[1] / np.sqrt(target_count))))

    mag_field = VECTOR_FIELD_MAP.get(field, (None, None))[0]
    mag_arr = None
    try:
        if mag_field:
            _, _, mag_arr, _, _ = _extract_field_2d(
                entry, mag_field, frame.get('time_index', 0),
                qc_enabled=qc_enabled,
                interp_strength=interp_strength,
                smoothing_strength=smoothing_strength,
                qc_config=qc_config,
            )
    except Exception:
        mag_arr = None

    vectors = []
    for iy in range(0, arr.shape[0], step_y):
        for ix in range(0, arr.shape[1], step_x):
            if not np.isfinite(arr[iy, ix]):
                continue
            vectors.append({
                'lat': float(lat_grid[iy, ix]),
                'lon': float(lon_grid[iy, ix]),
                'angle': float(arr[iy, ix]),
                'magnitude': float(mag_arr[iy, ix]) if mag_arr is not None and np.isfinite(mag_arr[iy, ix]) else None,
                'qual': float(qual[iy, ix]) if qual is not None and np.isfinite(qual[iy, ix]) else None,
            })
    return jsonify({'vectors': vectors, 'count': len(vectors), 'field': field})



@hfradar_bp.route('/', methods=['GET', 'POST'])
def hfradar_home():
    if request.method == 'POST':
        uploaded_files_mem.clear()
        for uploaded in request.files.getlist('radarfiles'):
            if not uploaded or not uploaded.filename:
                continue
            uploaded_files_mem.append({'filename': uploaded.filename, 'content': uploaded.read()})
        _build_groups()
        return redirect(url_for('hfradar_bp.hfradar_home'))

    if not hfradar_groups and uploaded_files_mem:
        _build_groups()

    return render_template(
        'project/hfradar_map_tbr.html',
        fields=available_fields,
        extent=radar_extent,
        groups=list(hfradar_groups.keys()),
        times={g: [item['time'] for item in hfradar_groups[g]] for g in hfradar_groups},
        epochs={g: [item['epoch'] for item in hfradar_groups[g]] for g in hfradar_groups},
        bounds=time_bounds,
        default_configs=FIELD_CONFIGS,
        qc_presets=QC_PRESETS,
        frame_catalog=frame_catalog,
    )


@hfradar_bp.route('/overlay')
def hfradar_overlay():
    group = request.args.get('group') or 'HF Radar Ocean Sensing'
    idx = int(request.args.get('frame', 0))
    requested_field = request.args.get('field') or (available_fields[0] if available_fields else 'Hs')
    cmap_override = request.args.get('cmap', '')
    custom_cmap_raw = request.args.get('custom_cmap', '')
    quality = _normalize_render_quality(request.args.get('quality'))
    qc_enabled = str(request.args.get('qc_enabled', '0')).lower() in ('1', 'true', 'yes', 'on')
    qc_config = _parse_qc_config(request)
    interp_strength = float(request.args.get('interp_strength', 0) or 0)
    smoothing_strength = float(request.args.get('smoothing_strength', 0) or 0)
    try:
        vmin_override = float(request.args.get('vmin', '')) if request.args.get('vmin', '') != '' else None
    except Exception:
        vmin_override = None
    try:
        vmax_override = float(request.args.get('vmax', '')) if request.args.get('vmax', '') != '' else None
    except Exception:
        vmax_override = None

    frame_obj = _get_frame(group, idx)
    field = _resolve_frame_field(frame_obj, requested_field)
    key = _render_cache_key(group, idx, field, qc_enabled, interp_strength, smoothing_strength, cmap_override, custom_cmap_raw, vmin_override, vmax_override, quality, qc_config=qc_config)
    cached = _cache_get(render_cache, key)
    if cached is None:
        png_bytes, meta = _render_overlay_png(
            group, idx, field, qc_enabled,
            interp_strength=interp_strength,
            smoothing_strength=smoothing_strength,
            cmap_override=cmap_override,
            custom_cmap_raw=custom_cmap_raw,
            vmin_override=vmin_override,
            vmax_override=vmax_override,
            quality=quality,
            qc_config=qc_config,
        )
        cached = {'png': png_bytes, 'meta': meta}
        _cache_store(render_cache, key, cached)

    response = send_file(BytesIO(cached['png']), mimetype='image/png')
    response.headers['X-Bounds'] = json.dumps(cached['meta']['bounds'])
    response.headers['X-Time'] = cached['meta']['time']
    response.headers['X-File-Count'] = str(cached['meta']['file_count'])
    response.headers['X-Filename'] = cached['meta']['filename']
    response.headers['X-Warnings'] = '; '.join(cached['meta']['warnings'])
    response.headers['X-Resolved-Field'] = field
    response.headers['X-Available-Fields'] = json.dumps(frame_obj.get('fields') or [])
    response.headers['X-Product-Type'] = str(frame_obj.get('product_type', 'generic'))
    response.headers['X-Interp-Strength'] = str(cached['meta'].get('interp_strength', 0))
    response.headers['X-Smoothing-Strength'] = str(cached['meta'].get('smoothing_strength', 0))
    return response



@hfradar_bp.route('/volume_data')
def hfradar_volume_data():
    group = request.args.get('group') or 'HF Radar Ocean Sensing'
    idx = int(request.args.get('frame', 0))
    requested_field = request.args.get('field') or (available_fields[0] if available_fields else 'Hs')
    quality = _normalize_render_quality(request.args.get('quality'))
    qc_enabled = str(request.args.get('qc_enabled', '0')).lower() in ('1', 'true', 'yes', 'on')
    qc_config = _parse_qc_config(request)
    interp_strength = float(request.args.get('interp_strength', 0) or 0)
    smoothing_strength = float(request.args.get('smoothing_strength', 0) or 0)
    frame_obj = _get_frame(group, idx)
    field = _resolve_frame_field(frame_obj, requested_field)
    payload = _build_volume_data(
        group, idx, field, qc_enabled,
        interp_strength=interp_strength,
        smoothing_strength=smoothing_strength,
        quality=quality,
        qc_config=qc_config,
    )
    return jsonify(payload)
