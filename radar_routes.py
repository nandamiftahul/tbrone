from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta
from typing import Any

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import patch_pyart
import pyart
from flask import Blueprint, current_app, redirect, render_template, request, send_file, url_for

radar_bp = Blueprint(
    'radar_bp',
    __name__,
    template_folder='templates',
    static_folder='static',
    url_prefix='/radarviewer',
)

# In-memory state per process.
radar_groups: dict[str, list[dict[str, Any]]] = {}
available_fields: list[str] = []
radar_extent = [[-10, 100], [10, 120]]
time_bounds: dict[str, dict[str, float]] = {}

# --- Default configs ---
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


def _uploads_dir() -> str:
    root = current_app.config.get('RADAR_UPLOAD_FOLDER') or os.path.join(current_app.root_path, 'uploads', 'radarviewer')
    os.makedirs(root, exist_ok=True)
    return root


def _cache_dir() -> str:
    root = current_app.config.get('RADAR_RENDER_FOLDER') or os.path.join(current_app.root_path, 'static', 'radar')
    os.makedirs(root, exist_ok=True)
    return root


def render_radar_png(filepath, field, cmap_override='', vmin_override=None, vmax_override=None, filters=None):
    if filters is None:
        filters = {}

    radar = pyart.io.read_sigmet(filepath, file_field_names=True, full_xhdr=True, time_ordered='full')
    warnings = []

    if field not in radar.fields:
        if 'DBZ2' in radar.fields:
            field = 'DBZ2'
        else:
            field = list(radar.fields.keys())[0]

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
        0,
        ax=ax,
        colorbar_flag=False,
        vmin=vmin_override,
        vmax=vmax_override,
        cmap=(cmap_override or default_configs.get(field, {}).get('cmap', 'turbo')),
        title_flag=False,
    )
    ax.axis('off')

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.png', dir=_cache_dir())
    plt.savefig(tmp.name, format='png', bbox_inches='tight', pad_inches=0, transparent=True)
    plt.close(fig)

    try:
        lat = radar.latitude['data'][0]
        lon = radar.longitude['data'][0]
        rng = radar.range['data'][-1] / 1000.0
        d = rng / 111.0
        extent = [lat - d, lon - d, lat + d, lon + d]
    except Exception:
        warnings.append('Extent calculation failed, using fallback box')
        extent = [-10, 100, 10, 120]

    return tmp.name, extent, radar, warnings


def get_radar_timestamp_and_epoch(radar):
    try:
        start = radar.time['units']
        base = start.split('since')[-1].strip()
        base_dt = datetime.fromisoformat(base.replace('Z', ''))
        sweep_time = base_dt + timedelta(seconds=float(radar.time['data'][0]))
        return sweep_time.strftime('%Y-%m-%d %H:%M:%S'), sweep_time.timestamp()
    except Exception:
        return 'N/A', None


@radar_bp.route('/', methods=['GET', 'POST'])
def radar_home():
    global radar_groups, available_fields, radar_extent, time_bounds

    if request.method == 'POST':
        radar_groups = {}
        time_bounds = {}

        for uploaded in request.files.getlist('radarfiles'):
            if not uploaded or not uploaded.filename:
                continue
            filepath = os.path.join(_uploads_dir(), uploaded.filename)
            uploaded.save(filepath)

            try:
                radar = pyart.io.read_sigmet(filepath, file_field_names=True, full_xhdr=True, time_ordered='full')
            except Exception as exc:
                current_app.logger.warning('Skipping %s: %s', uploaded.filename, exc)
                continue

            if len(radar.sweep_start_ray_index['data']) == 0:
                current_app.logger.warning('Skipping %s: no sweeps found', uploaded.filename)
                continue

            site = radar.metadata.get('instrument_name', 'UNKNOWN')
            task = radar.metadata.get('task_name') or radar.metadata.get('sigmet_task_name', 'UNKNOWN')
            key = f'{site} - {task}'

            ts_str, ts_epoch = get_radar_timestamp_and_epoch(radar)
            radar_groups.setdefault(key, []).append({'file': filepath, 'time': ts_str, 'epoch': ts_epoch})

        for key in radar_groups:
            radar_groups[key].sort(key=lambda item: item['file'])
            epochs_list = [item['epoch'] for item in radar_groups[key] if item['epoch'] is not None]
            if epochs_list:
                time_bounds[key] = {'min': min(epochs_list), 'max': max(epochs_list)}

        if radar_groups:
            first_file = list(radar_groups.values())[0][0]['file']
            try:
                radar = pyart.io.read_sigmet(first_file, file_field_names=True, full_xhdr=True, time_ordered='full')
                available_fields = list(radar.fields.keys())
                lat = radar.latitude['data'][0]
                lon = radar.longitude['data'][0]
                rng = radar.range['data'][-1] / 1000.0
                d = rng / 111.0
                radar_extent = [[lat - d, lon - d], [lat + d, lon + d]]
            except Exception as exc:
                current_app.logger.warning('Could not read first file fields: %s', exc)
                available_fields = []

        return redirect(url_for('radar_bp.radar_home'))

    return render_template(
        'radar_map_tbr.html',
        fields=available_fields,
        extent=radar_extent,
        groups=list(radar_groups.keys()),
        times={g: [item['time'] for item in radar_groups[g]] for g in radar_groups},
        epochs={g: [item['epoch'] for item in radar_groups[g]] for g in radar_groups},
        bounds=time_bounds,
        default_configs=default_configs,
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

    filters_arg = request.args.get('filters')
    try:
        filters = json.loads(filters_arg) if filters_arg else {}
    except Exception:
        filters = {}

    if not radar_groups or group not in radar_groups:
        return 'No radar files loaded', 404

    files = radar_groups[group]
    idx = idx % len(files)
    filepath = files[idx]['file']
    timestamp_str = files[idx]['time']

    png, extent, radar, warnings = render_radar_png(
        filepath,
        field,
        cmap_override,
        vmin_override,
        vmax_override,
        filters,
    )

    response = send_file(png, mimetype='image/png')
    response.headers['X-Extent'] = f'{extent[0]},{extent[1]},{extent[2]},{extent[3]}'
    response.headers['X-Frames'] = str(len(files))
    response.headers['X-Timestamp'] = timestamp_str
    if warnings:
        response.headers['X-Warnings'] = '; '.join(warnings)
    return response


# Example registration in TBRone app:
# from radar_routes import radar_bp
# app.register_blueprint(radar_bp)
