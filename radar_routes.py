
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timedelta
from io import BytesIO
from typing import Any

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import patch_pyart  # noqa: F401
import pyart
from flask import Blueprint, redirect, render_template, request, send_file, url_for

radar_bp = Blueprint(
    'radar_bp',
    __name__,
    template_folder='templates',
    static_folder='static',
    url_prefix='/radarviewer',
)

# In-memory session state only. Nothing is persisted to the server filesystem.
uploaded_radar_payloads: list[dict[str, Any]] = []
radar_groups: dict[str, list[dict[str, Any]]] = {}
available_fields: list[str] = []
radar_extent = [[-10, 100], [10, 120]]
time_bounds: dict[str, dict[str, float]] = {}
merge_window_minutes: int = 5

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
    m = re.fullmatch(r"[bB][\"'](.*)[\"']", text)
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
    digits = m.group(2)
    try:
        return datetime.strptime(digits, '%y%m%d%H%M%S')
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
    patterns = [r'[_-]([A-Z])$', r'(?<=\d)([A-Z])$']
    for pat in patterns:
        m = re.search(pat, task_text)
        if m:
            return m.group(1)

    sweep_text = _safe_text(sweep_code).upper()
    m = re.search(r'([A-Z])$', sweep_text)
    if m:
        return m.group(1)

    stem = os.path.basename(filename).split('.')[0].upper()
    m = re.search(r'([A-Z])$', stem)
    if m:
        return m.group(1)

    return ''


def _subscan_rank(letter: str) -> int:
    if not letter:
        return 999
    c = str(letter).upper()[0]
    if 'A' <= c <= 'Z':
        return ord(c) - ord('A') + 1
    return 999


def _read_radar(filepath: str):
    return pyart.io.read_sigmet(filepath, file_field_names=True, full_xhdr=True, time_ordered='full')


def _read_radar_from_bytes(content: bytes, filename_hint: str = 'upload.raw'):
    suffix = os.path.splitext(filename_hint)[1] or '.raw'
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        tmp.write(content)
        tmp.flush()
        tmp.close()
        return _read_radar(tmp.name)
    finally:
        try:
            os.remove(tmp.name)
        except Exception:
            pass


def _get_radar_epoch_and_label(radar, filename_hint: str) -> tuple[str, float | None]:
    try:
        start = radar.time['units']
        base = start.split('since')[-1].strip()
        base_dt = datetime.fromisoformat(base.replace('Z', ''))
        sweep_time = base_dt + timedelta(seconds=float(radar.time['data'][0]))
        return sweep_time.strftime('%Y-%m-%d %H:%M:%S'), sweep_time.timestamp()
    except Exception:
        file_dt = _extract_file_datetime(os.path.basename(filename_hint))
        if file_dt:
            return file_dt.strftime('%Y-%m-%d %H:%M:%S'), file_dt.timestamp()
        return 'N/A', None


def _finalize_frame_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    if not items:
        return {
            'files': [],
            'file_count': 0,
            'time': 'N/A',
            'epoch': None,
            'sweeps': [],
            'elevations_text': '',
        }

    preferred = sorted(
        items,
        key=lambda x: (
            _subscan_rank(x.get('subscan_letter', '')),
            x.get('epoch') if x.get('epoch') is not None else float('inf'),
            x.get('filename', ''),
        )
    )

    anchor_item = preferred[0]
    sweeps: list[str] = []
    seen_sweeps: set[str] = set()
    elevations: list[str] = []
    seen_elev: set[str] = set()
    files: list[dict[str, Any]] = []

    for item in preferred:
        files.append({'filename': item['filename'], 'content': item['content']})
        code = str(item.get('sweep_code') or '').strip()
        if code and code not in seen_sweeps:
            seen_sweeps.add(code)
            sweeps.append(code)
        elev = str(item.get('elevations_text') or '').strip()
        if elev and elev not in seen_elev:
            seen_elev.add(elev)
            elevations.append(elev)

    return {
        'files': files,
        'file_count': len(files),
        'time': anchor_item.get('time', 'N/A'),
        'epoch': anchor_item.get('epoch'),
        'sweeps': sweeps,
        'elevations_text': ' | '.join(elevations),
    }


def _merge_radars_from_payloads(file_payloads: list[dict[str, Any]]):
    warnings: list[str] = []
    radars = []
    for payload in file_payloads:
        try:
            radars.append(_read_radar_from_bytes(payload['content'], payload.get('filename', 'upload.raw')))
        except Exception as exc:
            warnings.append(f"{payload.get('filename', 'unknown')} failed: {exc}")

    if not radars:
        raise RuntimeError('No valid radar files in merged frame')

    merged = radars[0]
    for nxt in radars[1:]:
        try:
            merged = pyart.util.join_radar(merged, nxt)
        except Exception as exc:
            warnings.append(f'join failed: {exc}')
    return merged, warnings


def _extract_active_sweep_options(radar) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    try:
        fixed = np.asarray(radar.fixed_angle['data']).astype(float).tolist()
    except Exception:
        fixed = []

    if fixed:
        for idx, elev in enumerate(fixed):
            options.append({
                'index': idx,
                'label': f'{elev:g}°',
                'elevation': elev,
            })
    else:
        try:
            count = int(getattr(radar, 'nsweeps', 1) or 1)
        except Exception:
            count = 1
        for idx in range(max(1, count)):
            options.append({
                'index': idx,
                'label': f'Sweep {idx + 1}',
                'elevation': None,
            })
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
                chosen_elev = options[chosen_idx].get('elevation')
                return chosen_idx, options, chosen_elev
        except Exception:
            pass

    if requested_elevation not in (None, '', 'all'):
        try:
            target = float(requested_elevation)
            best = min(options, key=lambda opt: abs((opt.get('elevation') if opt.get('elevation') is not None else target) - target))
            chosen_idx = int(best['index'])
            chosen_elev = best.get('elevation')
            return chosen_idx, options, chosen_elev
        except Exception:
            pass

    return chosen_idx, options, chosen_elev


def _render_radar_png_from_payloads(file_payloads, field, cmap_override='', vmin_override=None, vmax_override=None, filters=None, requested_sweep=None, requested_elevation=None):
    if filters is None:
        filters = {}

    radar, warnings = _merge_radars_from_payloads(file_payloads)

    if field not in radar.fields:
        if 'DBZ2' in radar.fields:
            field = 'DBZ2'
        else:
            field = list(radar.fields.keys())[0]

    selected_sweep_idx, sweep_options, selected_elevation = _resolve_sweep_index(radar, requested_sweep, requested_elevation)

    data = radar.fields[field]['data'].copy()

    if filters.get('useSQI') and 'SQI2' in radar.fields:
        sqi_min = float(filters.get('sqi') or 0)
        sqi = radar.fields['SQI2']['data']
        data = np.ma.masked_where(sqi < sqi_min, data)
    elif filters.get('useSQI'):
        warnings.append('SQI2 field not available')

    if filters.get('usePMI') and 'PMI16' in radar.fields:
        pmi_min = float(filters.get('pmi') or 0)
        pmi = radar.fields['PMI16']['data']
        data = np.ma.masked_where(pmi < pmi_min, data)
    elif filters.get('usePMI'):
        warnings.append('PMI16 field not available')

    if filters.get('useLOG'):
        log_min = float(filters.get('log') or 0)
        data = np.ma.masked_where(data < log_min, data)

    if filters.get('clipRange') and vmin_override is not None and vmax_override is not None:
        data = np.clip(data, vmin_override, vmax_override)

    if filters.get('maskInvalid'):
        data = np.ma.masked_invalid(data)

    if filters.get('speckle'):
        from scipy.ndimage import median_filter
        data = median_filter(data, size=3)

    if filters.get('clutter'):
        data = np.ma.masked_where(data < 0, data)

    if filters.get('dealias') and field.startswith('VEL'):
        try:
            data = pyart.correct.dealias_region_based(radar, vel_field=field)
        except Exception as exc:
            warnings.append(f'Dealiasing failed: {exc}')

    radar.fields[field]['data'] = data

    display = pyart.graph.RadarDisplay(radar)
    fig, ax = plt.subplots(figsize=(6, 6))
    display.plot(
        field,
        selected_sweep_idx,
        ax=ax,
        colorbar_flag=False,
        vmin=vmin_override,
        vmax=vmax_override,
        cmap=(cmap_override or default_configs.get(field, {}).get('cmap', 'turbo')),
        title_flag=False,
    )
    ax.axis('off')

    buf = BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight', pad_inches=0, transparent=True)
    plt.close(fig)
    buf.seek(0)

    try:
        lat = radar.latitude['data'][0]
        lon = radar.longitude['data'][0]
        rng = radar.range['data'][-1] / 1000.0
        d = rng / 111.0
        extent = [lat - d, lon - d, lat + d, lon + d]
    except Exception:
        warnings.append('Extent calculation failed, using fallback box')
        extent = [-10, 100, 10, 120]

    return buf, extent, radar, warnings, selected_sweep_idx, sweep_options, selected_elevation


def _build_scan_groups(scan_window_minutes: int):
    global radar_groups, available_fields, radar_extent, time_bounds
    window_sec = max(1, int(scan_window_minutes)) * 60

    collected: list[dict[str, Any]] = []
    for uploaded in uploaded_radar_payloads:
        uploaded_name = uploaded['filename']
        content = uploaded['content']
        try:
            radar = _read_radar_from_bytes(content, uploaded_name)
        except Exception as exc:
            continue

        if len(radar.sweep_start_ray_index['data']) == 0:
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
            'content': content,
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

    grouped: dict[str, list[list[dict[str, Any]]]] = {}
    current_by_group: dict[tuple[str, str], dict[str, Any]] = {}

    for item in collected:
        pair = (item['site'], item['base_task'])
        existing = current_by_group.get(pair)

        if existing is None:
            current_by_group[pair] = {
                'items': [item],
                'start_epoch': item['epoch'],
                'last_rank': item.get('subscan_rank', 999),
            }
            continue

        start_epoch = existing['start_epoch']
        within_window = (
            item['epoch'] is not None
            and start_epoch is not None
            and (item['epoch'] - start_epoch) <= window_sec
        )

        current_rank = item.get('subscan_rank', 999)
        previous_rank = existing.get('last_rank', 999)
        rank_reset = current_rank <= previous_rank

        if within_window and not rank_reset:
            existing['items'].append(item)
            existing['last_rank'] = current_rank
        else:
            key = f"{pair[0]} - {pair[1]}"
            grouped.setdefault(key, []).append(existing['items'])
            current_by_group[pair] = {
                'items': [item],
                'start_epoch': item['epoch'],
                'last_rank': current_rank,
            }

    for pair, frame in current_by_group.items():
        key = f"{pair[0]} - {pair[1]}"
        grouped.setdefault(key, []).append(frame['items'])

    radar_groups = {}
    time_bounds = {}

    for key, frames in grouped.items():
        clean_frames = []
        for frame_items in frames:
            clean_frames.append(_finalize_frame_items(frame_items))

        clean_frames.sort(key=lambda x: x['epoch'] if x['epoch'] is not None else float('inf'))
        radar_groups[key] = clean_frames
        epochs_list = [f['epoch'] for f in clean_frames if f['epoch'] is not None]
        if epochs_list:
            time_bounds[key] = {'min': min(epochs_list), 'max': max(epochs_list)}

    available_fields = []
    if radar_groups:
        first_payload = next(iter(radar_groups.values()))[0]['files'][0]
        try:
            radar = _read_radar_from_bytes(first_payload['content'], first_payload['filename'])
            available_fields = list(radar.fields.keys())
            lat = radar.latitude['data'][0]
            lon = radar.longitude['data'][0]
            rng = radar.range['data'][-1] / 1000.0
            d = rng / 111.0
            radar_extent[:] = [[lat - d, lon - d], [lat + d, lon + d]]
        except Exception:
            available_fields = []


@radar_bp.route('/', methods=['GET', 'POST'])
def radar_home():
    global merge_window_minutes, uploaded_radar_payloads
    if request.method == 'POST':
        try:
            merge_window_minutes = int(request.form.get('scan_window_minutes') or request.args.get('scan_window') or merge_window_minutes or 5)
        except Exception:
            merge_window_minutes = 5

        new_payloads: list[dict[str, Any]] = []
        for uploaded in request.files.getlist('radarfiles'):
            if not uploaded or not uploaded.filename:
                continue
            content = uploaded.read()
            if not content:
                continue
            new_payloads.append({
                'filename': uploaded.filename,
                'content': content,
            })

        uploaded_radar_payloads = new_payloads
        _build_scan_groups(merge_window_minutes)
        return redirect(url_for('radar_bp.radar_home', scan_window=merge_window_minutes))

    try:
        requested_window = int(request.args.get('scan_window') or merge_window_minutes or 5)
    except Exception:
        requested_window = merge_window_minutes or 5
    if requested_window != merge_window_minutes or (uploaded_radar_payloads and not radar_groups):
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

    png_buf, extent, radar, warnings, selected_sweep_idx, sweep_options, selected_elevation = _render_radar_png_from_payloads(
        frame_data['files'],
        field,
        cmap_override,
        vmin_override,
        vmax_override,
        filters,
        requested_sweep=requested_sweep,
        requested_elevation=requested_elevation,
    )

    response = send_file(png_buf, mimetype='image/png')
    response.headers['X-Extent'] = f'{extent[0]},{extent[1]},{extent[2]},{extent[3]}'
    response.headers['X-Frames'] = str(len(frames))
    response.headers['X-Timestamp'] = frame_data['time']
    response.headers['X-File-Count'] = str(frame_data.get('file_count', len(frame_data['files'])))
    response.headers['X-Sweeps'] = ', '.join(frame_data.get('sweeps', []))
    response.headers['X-Elevations'] = frame_data.get('elevations_text', '')
    response.headers['X-Active-Sweep-Index'] = str(selected_sweep_idx)
    response.headers['X-Active-Elevation'] = '' if selected_elevation is None else str(selected_elevation)
    response.headers['X-Sweep-Options'] = json.dumps(sweep_options)
    if warnings:
        response.headers['X-Warnings'] = '; '.join(warnings)
    return response
