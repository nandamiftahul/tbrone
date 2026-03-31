from __future__ import annotations

import io
import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from typing import Any

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import patch_pyart  # noqa: F401
import pyart
from flask import Blueprint, current_app, g, redirect, render_template, request, send_file, url_for, jsonify
import psycopg
from psycopg.rows import dict_row

radar_bp = Blueprint(
    'radar_bp',
    __name__,
    template_folder='templates',
    static_folder='static',
    url_prefix='/radarviewer',
)

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


# =========================
# PostgreSQL helpers
# =========================
def get_db():
    if 'radar_db' not in g:
        dsn = current_app.config.get('DATABASE_URL') or os.getenv('DATABASE_URL')
        if not dsn:
            raise RuntimeError('DATABASE_URL not set. Add Railway PostgreSQL and ensure env is injected.')
        g.radar_db = psycopg.connect(dsn, row_factory=dict_row)
        g.radar_db.autocommit = False
    return g.radar_db


@radar_bp.teardown_app_request
def close_db(exception=None):
    db = g.pop('radar_db', None)
    if db is not None:
        try:
            if exception:
                db.rollback()
            else:
                db.commit()
        except Exception:
            pass
        db.close()



def init_db() -> None:
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS radar_raw (
                id BIGSERIAL PRIMARY KEY,
                filename TEXT NOT NULL,
                file_size BIGINT,
                site_name TEXT,
                task_name TEXT,
                group_name TEXT NOT NULL,
                radar_time_utc TIMESTAMPTZ,
                radar_date DATE,
                epoch DOUBLE PRECISION,
                extent JSONB,
                fields JSONB NOT NULL DEFAULT '[]'::jsonb,
                metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                radar_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                raw_bytes BYTEA NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (group_name, radar_time_utc, filename)
            );
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_radar_raw_date ON radar_raw(radar_date DESC, radar_time_utc DESC);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_radar_raw_group ON radar_raw(group_name, radar_time_utc);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_radar_raw_site_task ON radar_raw(site_name, task_name);")
    db.commit()


# =========================
# Filesystem helpers
# =========================
def _uploads_dir() -> str:
    root = current_app.config.get('RADAR_UPLOAD_FOLDER') or os.path.join(current_app.root_path, 'uploads', 'radarviewer')
    os.makedirs(root, exist_ok=True)
    return root



def _cache_dir() -> str:
    root = current_app.config.get('RADAR_RENDER_FOLDER') or os.path.join(current_app.root_path, 'static', 'radar')
    os.makedirs(root, exist_ok=True)
    return root


# =========================
# Serialization helpers
# =========================
def _safe_str(v: Any) -> str:
    return '' if v is None else str(v)


def _to_jsonable(value: Any):
    if value is None:
        return None

    if isinstance(value, bytes):
        try:
            return value.decode('utf-8')
        except Exception:
            return value.hex()

    if isinstance(value, np.ma.MaskedArray):
        return _to_jsonable(value.filled(np.nan))

    if isinstance(value, np.ndarray):
        if value.dtype.kind in ('S', 'a'):
            return [_to_jsonable(v) for v in value.tolist()]
        if value.dtype.kind == 'f':
            return [None if (isinstance(v, float) and (np.isnan(v) or np.isinf(v))) else _to_jsonable(v) for v in value.tolist()]
        return [_to_jsonable(v) for v in value.tolist()]

    if isinstance(value, np.generic):
        return _to_jsonable(value.item())

    if isinstance(value, float):
        if np.isnan(value) or np.isinf(value):
            return None
        return value

    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}

    if isinstance(value, (list, tuple, set)):
        return [_to_jsonable(v) for v in value]

    return value


def _serialize_field_dict(field_dict: dict[str, Any]) -> dict[str, Any]:
    return {str(key): _to_jsonable(value) for key, value in field_dict.items()}


def _serialize_radar_for_db(radar) -> dict[str, Any]:
    fields_payload = {}
    for name, field_meta in radar.fields.items():
        fields_payload[name] = _serialize_field_dict(field_meta)

    def _attr_payload(name: str):
        try:
            attr = getattr(radar, name)
            if isinstance(attr, dict):
                return _serialize_field_dict(attr)
            return _to_jsonable(attr)
        except Exception:
            return None

    payload = {
        'metadata': _to_jsonable(dict(radar.metadata or {})),
        'instrument_parameters': _to_jsonable(getattr(radar, 'instrument_parameters', None)),
        'scan_type': _safe_str(getattr(radar, 'scan_type', None)),
        'nsweeps': int(getattr(radar, 'nsweeps', 0) or 0),
        'nrays': int(getattr(radar, 'nrays', 0) or 0),
        'ngates': int(getattr(radar, 'ngates', 0) or 0),
        'latitude': _attr_payload('latitude'),
        'longitude': _attr_payload('longitude'),
        'altitude': _attr_payload('altitude'),
        'range': _attr_payload('range'),
        'time': _attr_payload('time'),
        'azimuth': _attr_payload('azimuth'),
        'elevation': _attr_payload('elevation'),
        'fixed_angle': _attr_payload('fixed_angle'),
        'sweep_number': _attr_payload('sweep_number'),
        'sweep_mode': _attr_payload('sweep_mode'),
        'sweep_start_ray_index': _attr_payload('sweep_start_ray_index'),
        'sweep_end_ray_index': _attr_payload('sweep_end_ray_index'),
        'fields': fields_payload,
    }
    return _to_jsonable(payload)


def _make_extent(radar):
    lat = float(radar.latitude['data'][0])
    lon = float(radar.longitude['data'][0])
    rng = float(radar.range['data'][-1]) / 1000.0
    d = rng / 111.0
    return [lat - d, lon - d, lat + d, lon + d]



def _parse_radar_datetime(radar):
    try:
        start = radar.time['units']
        base = start.split('since')[-1].strip().replace('Z', '')
        base_dt = datetime.fromisoformat(base)
        if base_dt.tzinfo is None:
            base_dt = base_dt.replace(tzinfo=UTC)
        sweep_time = base_dt + timedelta(seconds=float(radar.time['data'][0]))
        return sweep_time.astimezone(UTC)
    except Exception:
        return None



def get_radar_timestamp_and_epoch(radar):
    dt = _parse_radar_datetime(radar)
    if dt is None:
        return 'N/A', None, None
    return dt.strftime('%Y-%m-%d %H:%M:%S UTC'), dt.timestamp(), dt.date().isoformat()


# =========================
# DB catalog helpers
# =========================
def _list_available_dates() -> list[str]:
    init_db()
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT radar_date::text AS radar_date
            FROM radar_raw
            WHERE radar_date IS NOT NULL
            ORDER BY radar_date DESC
            """
        )
        rows = cur.fetchall()
    return [r['radar_date'] for r in rows if r['radar_date']]



def _get_catalog(selected_date: str | None = None):
    init_db()
    db = get_db()
    params: list[Any] = []
    where = ''
    if selected_date:
        where = 'WHERE radar_date = %s'
        params.append(selected_date)

    with db.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, filename, site_name, task_name, group_name,
                   radar_time_utc, radar_date::text AS radar_date, epoch,
                   extent, fields
            FROM radar_raw
            {where}
            ORDER BY radar_time_utc ASC NULLS LAST, id ASC
            """,
            params,
        )
        rows = cur.fetchall()

    groups: dict[str, list[dict[str, Any]]] = {}
    available_fields: list[str] = []
    extent = [[-10, 100], [10, 120]]
    time_bounds: dict[str, dict[str, float]] = {}

    for row in rows:
        row_fields = row.get('fields') or []
        for fld in row_fields:
            if fld not in available_fields:
                available_fields.append(fld)
        group = row['group_name']
        ts = row['radar_time_utc']
        ts_text = ts.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S UTC') if ts else 'N/A'
        groups.setdefault(group, []).append({
            'id': row['id'],
            'file': row['filename'],
            'time': ts_text,
            'epoch': row['epoch'],
            'date': row.get('radar_date'),
        })
        ext = row.get('extent')
        if ext and extent == [[-10, 100], [10, 120]]:
            extent = [[ext[0], ext[1]], [ext[2], ext[3]]]

    for key in groups:
        groups[key].sort(key=lambda item: (item['epoch'] is None, item['epoch'] or 0, item['id']))
        epochs_list = [item['epoch'] for item in groups[key] if item['epoch'] is not None]
        if epochs_list:
            time_bounds[key] = {'min': min(epochs_list), 'max': max(epochs_list)}

    return {
        'groups': groups,
        'available_fields': available_fields,
        'radar_extent': extent,
        'time_bounds': time_bounds,
    }



def _get_row_by_group_and_frame(group: str, frame: int, selected_date: str | None = None):
    init_db()
    db = get_db()
    params: list[Any] = [group]
    where_date = ''
    if selected_date:
        where_date = 'AND radar_date = %s'
        params.append(selected_date)

    with db.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, filename, radar_time_utc, extent, fields, raw_bytes
            FROM radar_raw
            WHERE group_name = %s {where_date}
            ORDER BY radar_time_utc ASC NULLS LAST, id ASC
            """,
            params,
        )
        rows = cur.fetchall()

    if not rows:
        return None, 0
    idx = frame % len(rows)
    return rows[idx], len(rows)


# =========================
# Rendering
# =========================
def render_radar_png_from_bytes(raw_bytes, field, cmap_override='', vmin_override=None, vmax_override=None, filters=None):
    if filters is None:
        filters = {}

    with tempfile.NamedTemporaryFile(delete=False, suffix='.raw', dir=_uploads_dir()) as tmp_raw:
        tmp_raw.write(raw_bytes)
        tmp_path = tmp_raw.name

    try:
        radar = pyart.io.read_sigmet(tmp_path, file_field_names=True, full_xhdr=True, time_ordered='full')
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

        tmp_png = tempfile.NamedTemporaryFile(delete=False, suffix='.png', dir=_cache_dir())
        plt.savefig(tmp_png.name, format='png', bbox_inches='tight', pad_inches=0, transparent=True)
        plt.close(fig)

        try:
            extent = _make_extent(radar)
        except Exception:
            warnings.append('Extent calculation failed, using fallback box')
            extent = [-10, 100, 10, 120]

        ts, _, _ = get_radar_timestamp_and_epoch(radar)
        return tmp_png.name, extent, ts, warnings
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass


# =========================
# Routes
# =========================
@radar_bp.route('/', methods=['GET', 'POST'])
def radar_home():
    init_db()

    if request.method == 'POST':
        db = get_db()
        inserted = 0
        skipped = 0

        with db.cursor() as cur:
            for uploaded in request.files.getlist('radarfiles'):
                if not uploaded or not uploaded.filename:
                    continue

                raw_bytes = uploaded.read()
                if not raw_bytes:
                    skipped += 1
                    continue

                upload_copy_path = os.path.join(_uploads_dir(), uploaded.filename)
                try:
                    with open(upload_copy_path, 'wb') as fp:
                        fp.write(raw_bytes)
                except Exception:
                    pass

                with tempfile.NamedTemporaryFile(delete=False, suffix='.raw', dir=_uploads_dir()) as tmp_raw:
                    tmp_raw.write(raw_bytes)
                    tmp_path = tmp_raw.name

                try:
                    radar = pyart.io.read_sigmet(tmp_path, file_field_names=True, full_xhdr=True, time_ordered='full')
                except Exception as exc:
                    current_app.logger.warning('Skipping %s: %s', uploaded.filename, exc)
                    skipped += 1
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass
                    continue

                try:
                    if len(radar.sweep_start_ray_index['data']) == 0:
                        current_app.logger.warning('Skipping %s: no sweeps found', uploaded.filename)
                        skipped += 1
                        continue

                    site = radar.metadata.get('instrument_name', 'UNKNOWN')
                    task = radar.metadata.get('task_name') or radar.metadata.get('sigmet_task_name', 'UNKNOWN')
                    group_name = f'{site} - {task}'
                    ts_str, ts_epoch, ts_date = get_radar_timestamp_and_epoch(radar)
                    ts_dt = _parse_radar_datetime(radar)
                    extent = _make_extent(radar)
                    radar_json = _serialize_radar_for_db(radar)
                    field_names = list(radar.fields.keys())
                    metadata_json = _to_jsonable({
                        'instrument_name': _safe_str(site),
                        'task_name': _safe_str(task),
                        'timestamp_text': ts_str,
                        'filename': uploaded.filename,
                        'file_size': len(raw_bytes),
                        'ray_count': int(getattr(radar, 'nrays', 0) or 0),
                        'gate_count': int(getattr(radar, 'ngates', 0) or 0),
                        'metadata': dict(radar.metadata or {}),
                    })

                    cur.execute(
                        """
                        INSERT INTO radar_raw(
                            filename, file_size, site_name, task_name, group_name,
                            radar_time_utc, radar_date, epoch, extent,
                            fields, metadata_json, radar_json, raw_bytes
                        )
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s)
                        ON CONFLICT (group_name, radar_time_utc, filename)
                        DO UPDATE SET
                            file_size = EXCLUDED.file_size,
                            site_name = EXCLUDED.site_name,
                            task_name = EXCLUDED.task_name,
                            radar_date = EXCLUDED.radar_date,
                            epoch = EXCLUDED.epoch,
                            extent = EXCLUDED.extent,
                            fields = EXCLUDED.fields,
                            metadata_json = EXCLUDED.metadata_json,
                            radar_json = EXCLUDED.radar_json,
                            raw_bytes = EXCLUDED.raw_bytes
                        """,
                        (
                            uploaded.filename,
                            len(raw_bytes),
                            site,
                            task,
                            group_name,
                            ts_dt,
                            ts_date,
                            ts_epoch,
                            json.dumps(extent),
                            json.dumps(field_names),
                            json.dumps(metadata_json),
                            json.dumps(radar_json),
                            raw_bytes,
                        ),
                    )
                    inserted += 1
                finally:
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass

        db.commit()
        current_app.logger.info('Radar upload complete. inserted=%s skipped=%s', inserted, skipped)
        selected_date = request.form.get('selected_date', '').strip()
        if selected_date:
            return redirect(url_for('radar_bp.radar_home', date=selected_date))
        return redirect(url_for('radar_bp.radar_home'))

    selected_date = (request.args.get('date') or '').strip() or None
    available_dates = _list_available_dates()
    if selected_date and selected_date not in available_dates:
        selected_date = None

    catalog = _get_catalog(selected_date)
    groups = catalog['groups']
    available_fields = catalog['available_fields']
    radar_extent = catalog['radar_extent']
    time_bounds = catalog['time_bounds']

    return render_template(
        'radar_map_tbr.html',
        fields=available_fields,
        extent=radar_extent,
        groups=list(groups.keys()),
        times={g: [item['time'] for item in groups[g]] for g in groups},
        epochs={g: [item['epoch'] for item in groups[g]] for g in groups},
        bounds=time_bounds,
        default_configs=default_configs,
        available_dates=available_dates,
        selected_date=selected_date or '',
        group_counts={g: len(groups[g]) for g in groups},
    )


@radar_bp.route('/overlay')
def radar_overlay():
    init_db()

    group = (request.args.get('group') or '').strip()
    selected_date = (request.args.get('date') or '').strip() or None
    try:
        idx = int(request.args.get('frame', 0))
    except Exception:
        idx = 0

    field = (request.args.get('field') or '').strip()
    catalog = _get_catalog(selected_date)
    available_fields = catalog['available_fields']
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

    row, total = _get_row_by_group_and_frame(group, idx, selected_date)
    if row is None:
        return 'No radar files loaded', 404

    png, extent, timestamp_str, warnings = render_radar_png_from_bytes(
        row['raw_bytes'],
        field,
        cmap_override,
        vmin_override,
        vmax_override,
        filters,
    )

    response = send_file(png, mimetype='image/png')
    response.headers['X-Extent'] = f'{extent[0]},{extent[1]},{extent[2]},{extent[3]}'
    response.headers['X-Frames'] = str(total)
    response.headers['X-Timestamp'] = timestamp_str
    response.headers['X-Radar-Id'] = str(row['id'])
    if warnings:
        response.headers['X-Warnings'] = '; '.join(warnings)
    return response


@radar_bp.route('/api/available-dates')
def radar_available_dates():
    init_db()
    return jsonify({'ok': True, 'dates': _list_available_dates()})
