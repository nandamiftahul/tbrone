
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
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import patch_pyart  # noqa: F401
import pyart
from flask import Blueprint, current_app, redirect, render_template, request, send_file, url_for

from scipy.ndimage import generic_filter, median_filter, distance_transform_edt
from scipy.ndimage.measurements import variance
from scipy.ndimage.filters import uniform_filter
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

def _apply_pyiris_filters(radar, field_name: str, data_in, filters: dict[str, Any], defaults: dict[str, Any]):
    data = np.ma.filled(np.ma.array(data_in).copy(), np.nan).astype(float)
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
):
    filters = filters or {}
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
    cmap_obj = plt.get_cmap(cmap_override or default_configs.get(field, {}).get('cmap', 'turbo')).copy()
    try:
        cmap_obj.set_bad(alpha=0.0)
    except Exception:
        pass

    try:
        lat0 = float(radar.latitude['data'][0])
        lon0 = float(radar.longitude['data'][0])
    except Exception:
        lat0, lon0 = 0.0, 0.0

    try:
        rng_km = float(radar.range['data'][-1]) / 1000.0
    except Exception:
        rng_km = 250.0

    fig, ax = plt.subplots(figsize=(6, 6))
    extent = None

    if product_used in ('MAX', 'CAPPI'):
        try:
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
                arr2 = np.nanmax(arr3, axis=0)
                arr2 = np.ma.masked_invalid(arr2)
            else:
                # CAPPI estimated from vertically interpolated gridded volume,
                # then masked to radar range so the plot keeps a circular footprint.
                cappi_h = float(cappi_height_km) * 1000.0
                z_top = max(12000.0, float(getattr(radar, 'nsweeps', 1) or 1) * 1000.0)
                grid = pyart.map.grid_from_radars(
                    (radar,),
                    grid_shape=(12, 400, 400),
                    grid_limits=((0.0, z_top), (-grid_limit_xy, grid_limit_xy), (-grid_limit_xy, grid_limit_xy)),
                    fields=[field],
                    weighting_function='Barnes2',
                )
                arr3 = np.ma.filled(grid.fields[field]['data'], np.nan)

                try:
                    z_levels = np.asarray(grid.z['data'], dtype=float)
                except Exception:
                    z_levels = np.linspace(0.0, z_top, arr3.shape[0])

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

                ny, nx = arr2.shape
                x = np.linspace(-rng_km, rng_km, nx)
                y = np.linspace(-rng_km, rng_km, ny)
                xx, yy = np.meshgrid(x, y)
                rr = np.sqrt(xx**2 + yy**2)
                arr2 = np.where(rr <= rng_km, arr2, np.nan)
                arr2 = np.ma.masked_invalid(arr2)

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
    plt.savefig(buf, format='png', bbox_inches='tight', pad_inches=0, transparent=True)
    plt.close(fig)
    buf.seek(0)
    return buf, extent, radar, warnings, selected_sweep_idx, sweep_options, selected_elevation, product_used



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
                filters, requested_sweep, requested_elevation, derived_product, cappi_height_km
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
    try:
        cappi_height_km = float(request.args.get('cappi_height_km', '2.0') or '2.0')
    except Exception:
        cappi_height_km = 2.0
    filters_arg = request.args.get('filters')
    try:
        filters = json.loads(filters_arg) if filters_arg else {}
    except Exception:
        filters = {}

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

    if warnings:
        response.headers['X-Warnings'] = '; '.join(warnings)
    return response

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
    try:
        cappi_height_km = float(request.args.get('cappi_height_km', '2.0') or '2.0')
    except Exception:
        cappi_height_km = 2.0
    filters_arg = request.args.get('filters')
    try:
        filters = json.loads(filters_arg) if filters_arg else {}
    except Exception:
        filters = {}
    if not radar_groups or group not in radar_groups:
        return 'No radar files loaded', 404
    frames = radar_groups[group]
    idx = idx % len(frames)
    frame_data = frames[idx]
    cache_key = _make_render_cache_key(
        group, idx, field, cmap_override, vmin_override, vmax_override,
        filters, requested_sweep, requested_elevation, derived_product, cappi_height_km
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
    if cached['warnings']:
        response.headers['X-Warnings'] = cached['warnings']
    return response
