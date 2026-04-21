import json
import uuid
from contextlib import contextmanager

from flask import Blueprint, current_app, jsonify, render_template, request

kotatua_bp = Blueprint(
    'kotatua',
    __name__,
    template_folder='templates'
)

_DRIVER = None
try:
    import psycopg2  # type: ignore
    _DRIVER = 'psycopg2'
except Exception:
    try:
        import psycopg  # type: ignore
        _DRIVER = 'psycopg'
    except Exception:
        _DRIVER = None


FALLBACK_KOTA_TUA = {
    'type': 'Feature',
    'properties': {'name': 'Kota Tua Jakarta (fallback)'},
    'geometry': {
        'type': 'Polygon',
        'coordinates': [[[106.80810, -6.13125], [106.81100, -6.13075], [106.81440, -6.13075], [106.81785, -6.13135], [106.82090, -6.13240], [106.82185, -6.13525], [106.82135, -6.13860], [106.82105, -6.14180], [106.82125, -6.14485], [106.82095, -6.14835], [106.81915, -6.15110], [106.81635, -6.15245], [106.81310, -6.15280], [106.80955, -6.15230], [106.80665, -6.15055], [106.80515, -6.14765], [106.80470, -6.14415], [106.80480, -6.14055], [106.80555, -6.13680], [106.80695, -6.13340], [106.80810, -6.13125]]]
    }
}

FALLBACK_KOTA_TUA_CORE = {
    'type': 'Feature',
    'properties': {'name': 'Kota Tua Heritage Core'},
    'geometry': {
        'type': 'Polygon',
        'coordinates': [[[106.7946671695357, -6.152042293120568], [106.8059037766916, -6.173228635843911], [106.8398289050148, -6.168063893769462], [106.8274619917771, -6.115470721202261], [106.8170630748180, -6.114669587333109], [106.8158981118654, -6.119125531511821], [106.8102510693856, -6.120462606884525], [106.8102508583425, -6.117700024255674], [106.8079203723969, -6.117521929656587], [106.8036173133190, -6.094619505112800], [106.7973434878632, -6.094620075267708], [106.7946547439501, -6.092481454154830], [106.7910698508049, -6.093016400173771], [106.7946671695357, -6.152042293120568]]]
    }
}

SEED_LAYERS = [
    {
        'id': 'dki',
        'name': 'DKI Jakarta Mainland',
        'geometry': FALLBACK_KOTA_TUA_CORE['geometry'],
        'visible': False,
        'style': {'color': '#2563EB', 'weight': 1.5, 'fillColor': '#2563EB', 'fillOpacity': 0.10},
        'layer_group': 'default',
        'source_type': 'seed',
    },
    {
        'id': 'focus',
        'name': 'Kota Tua Heritage Focus',
        'geometry': FALLBACK_KOTA_TUA['geometry'],
        'visible': False,
        'style': {'color': '#DC2626', 'weight': 2.4, 'fillColor': '#DC2626', 'fillOpacity': 0.10},
        'layer_group': 'default',
        'source_type': 'seed',
    },
    {
        'id': 'core',
        'name': 'Kota Tua Core Zone',
        'geometry': FALLBACK_KOTA_TUA_CORE['geometry'],
        'visible': True,
        'style': {'color': '#EF4444', 'weight': 4, 'fillColor': '#EF4444', 'fillOpacity': 0.04},
        'layer_group': 'default',
        'source_type': 'seed',
    },
]

CREATE_TABLE_SQL = '''
CREATE TABLE IF NOT EXISTS kotatua_map_layers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    geometry JSONB NOT NULL,
    visible BOOLEAN NOT NULL DEFAULT TRUE,
    style JSONB,
    layer_group TEXT NOT NULL DEFAULT 'custom',
    source_type TEXT NOT NULL DEFAULT 'draw',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
'''


class DatabaseUnavailable(RuntimeError):
    pass


@contextmanager
def get_conn():
    database_url = (current_app.config.get('DATABASE_URL') or '').strip()
    if not database_url:
        raise DatabaseUnavailable('DATABASE_URL belum terpasang.')
    if _DRIVER is None:
        raise DatabaseUnavailable('Driver PostgreSQL belum terpasang. Tambahkan psycopg2-binary pada environment Railway.')

    if _DRIVER == 'psycopg2':
        import psycopg2  # type: ignore
        conn = psycopg2.connect(database_url)
    else:
        import psycopg  # type: ignore
        conn = psycopg.connect(database_url)

    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_table_initialized = False


def ensure_table() -> None:
    global _table_initialized
    if _table_initialized:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
            cur.execute('SELECT COUNT(*) FROM kotatua_map_layers')
            count = cur.fetchone()[0]
            if int(count or 0) == 0:
                for row in SEED_LAYERS:
                    cur.execute(
                        '''
                        INSERT INTO kotatua_map_layers
                        (id, name, geometry, visible, style, layer_group, source_type)
                        VALUES (%s, %s, %s::jsonb, %s, %s::jsonb, %s, %s)
                        ''',
                        (
                            row['id'],
                            row['name'],
                            json.dumps(row['geometry']),
                            row['visible'],
                            json.dumps(row['style']),
                            row['layer_group'],
                            row['source_type'],
                        )
                    )
    _table_initialized = True


def serialize_row(row):
    area_id, name, geometry, visible, style, layer_group, source_type, created_at, updated_at = row
    if isinstance(geometry, str):
        geometry = json.loads(geometry)
    if isinstance(style, str):
        style = json.loads(style)
    return {
        'id': area_id,
        'name': name,
        'geometry': geometry,
        'visible': bool(visible),
        'style': style or {},
        'layer_group': layer_group or 'custom',
        'source_type': source_type or 'draw',
        'created_at': created_at.isoformat() if created_at else None,
        'updated_at': updated_at.isoformat() if updated_at else None,
    }


def normalize_geometry(value):
    if not isinstance(value, dict):
        raise ValueError('Geometry harus berupa object GeoJSON.')

    geo_type = value.get('type')

    if geo_type == 'Feature':
        geometry = value.get('geometry')
        if not isinstance(geometry, dict):
            raise ValueError('Geometry pada Feature tidak valid.')
        props = value.get('properties') if isinstance(value.get('properties'), dict) else {}
        return {
            'type': 'FeatureCollection',
            'features': [{
                'type': 'Feature',
                'properties': props,
                'geometry': normalize_geometry(geometry)['features'][0]['geometry']
            }]
        }

    if geo_type == 'FeatureCollection':
        features = value.get('features') or []
        if not isinstance(features, list) or not features:
            raise ValueError('FeatureCollection kosong.')

        normalized_features = []
        for feature in features:
            if not isinstance(feature, dict) or feature.get('type') != 'Feature':
                raise ValueError('Semua item pada FeatureCollection harus berupa Feature yang valid.')
            geometry = feature.get('geometry')
            if not isinstance(geometry, dict):
                raise ValueError('Geometry pada FeatureCollection tidak valid.')
            normalized_geom = normalize_geometry(geometry)['features'][0]['geometry']
            normalized_features.append({
                'type': 'Feature',
                'properties': feature.get('properties') if isinstance(feature.get('properties'), dict) else {},
                'geometry': normalized_geom,
            })

        return {
            'type': 'FeatureCollection',
            'features': normalized_features,
        }

    if geo_type not in ('Polygon', 'MultiPolygon'):
        raise ValueError('Hanya Polygon, MultiPolygon, Feature, atau FeatureCollection yang didukung.')
    if value.get('coordinates') is None:
        raise ValueError('Geometry GeoJSON tidak valid.')

    return {
        'type': 'FeatureCollection',
        'features': [{
            'type': 'Feature',
            'properties': {},
            'geometry': {
                'type': geo_type,
                'coordinates': value.get('coordinates'),
            }
        }]
    }


def normalize_layer_group(value):
    value = str(value or 'custom').strip().lower()
    if value not in ('default', 'custom'):
        value = 'custom'
    return value


def validate_payload(payload, partial=False):
    if not isinstance(payload, dict):
        raise ValueError('Payload harus JSON object.')

    cleaned = {}

    if not partial or 'name' in payload:
        name = str(payload.get('name', '')).strip()
        if not name:
            raise ValueError('Nama area wajib diisi.')
        cleaned['name'] = name

    if not partial or 'geometry' in payload:
        cleaned['geometry'] = normalize_geometry(payload.get('geometry'))

    if 'visible' in payload or not partial:
        cleaned['visible'] = bool(payload.get('visible', True))

    if 'style' in payload:
        style = payload.get('style')
        if style is None:
            style = {}
        if not isinstance(style, dict):
            raise ValueError('Style harus object.')
        cleaned['style'] = style
    elif not partial:
        cleaned['style'] = {}

    if 'layer_group' in payload or not partial:
        cleaned['layer_group'] = normalize_layer_group(payload.get('layer_group', 'custom'))

    if 'source_type' in payload or not partial:
        cleaned['source_type'] = str(payload.get('source_type', 'draw') or 'draw').strip().lower()

    return cleaned


@kotatua_bp.route('/kotatuamap')
def kotatua_map():
    return render_template('project/kota_tua_dki_highlight_totaldki_with_toggles.html')


@kotatua_bp.route('/api/kotatua/layers', methods=['GET'])
def list_layers():
    try:
        ensure_table()
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    '''
                    SELECT id, name, geometry, visible, style, layer_group, source_type, created_at, updated_at
                    FROM kotatua_map_layers
                    ORDER BY CASE WHEN layer_group = 'default' THEN 0 ELSE 1 END, created_at ASC, name ASC
                    '''
                )
                rows = cur.fetchall()
        return jsonify({'ok': True, 'rows': [serialize_row(row) for row in rows]})
    except DatabaseUnavailable as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500
    except Exception as exc:
        return jsonify({'ok': False, 'error': f'Gagal load layer: {exc}'}), 500


@kotatua_bp.route('/api/kotatua/layers', methods=['POST'])
def create_layer():
    try:
        ensure_table()
        raw = request.get_json(silent=True) or {}
        payload = validate_payload(raw, partial=False)
        area_id = str(raw.get('id') or uuid.uuid4())
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    '''
                    INSERT INTO kotatua_map_layers (id, name, geometry, visible, style, layer_group, source_type)
                    VALUES (%s, %s, %s::jsonb, %s, %s::jsonb, %s, %s)
                    RETURNING id, name, geometry, visible, style, layer_group, source_type, created_at, updated_at
                    ''',
                    (
                        area_id,
                        payload['name'],
                        json.dumps(payload['geometry']),
                        payload['visible'],
                        json.dumps(payload.get('style', {})),
                        payload['layer_group'],
                        payload['source_type'],
                    )
                )
                row = cur.fetchone()
        return jsonify({'ok': True, 'row': serialize_row(row)})
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    except DatabaseUnavailable as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500
    except Exception as exc:
        return jsonify({'ok': False, 'error': f'Gagal membuat layer: {exc}'}), 500


@kotatua_bp.route('/api/kotatua/layers/<layer_id>', methods=['PUT'])
def update_layer(layer_id):
    try:
        ensure_table()
        payload = validate_payload(request.get_json(silent=True) or {}, partial=True)
        fields = []
        values = []

        if 'name' in payload:
            fields.append('name = %s')
            values.append(payload['name'])
        if 'geometry' in payload:
            fields.append('geometry = %s::jsonb')
            values.append(json.dumps(payload['geometry']))
        if 'visible' in payload:
            fields.append('visible = %s')
            values.append(payload['visible'])
        if 'style' in payload:
            fields.append('style = %s::jsonb')
            values.append(json.dumps(payload['style']))
        if 'layer_group' in payload:
            fields.append('layer_group = %s')
            values.append(payload['layer_group'])
        if 'source_type' in payload:
            fields.append('source_type = %s')
            values.append(payload['source_type'])

        if not fields:
            return jsonify({'ok': False, 'error': 'Tidak ada field yang diupdate.'}), 400

        fields.append('updated_at = NOW()')
        values.append(layer_id)

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f'''
                    UPDATE kotatua_map_layers
                    SET {', '.join(fields)}
                    WHERE id = %s
                    RETURNING id, name, geometry, visible, style, layer_group, source_type, created_at, updated_at
                    ''',
                    tuple(values)
                )
                row = cur.fetchone()
                if not row:
                    return jsonify({'ok': False, 'error': 'Layer tidak ditemukan.'}), 404
        return jsonify({'ok': True, 'row': serialize_row(row)})
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    except DatabaseUnavailable as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500
    except Exception as exc:
        return jsonify({'ok': False, 'error': f'Gagal update layer: {exc}'}), 500


@kotatua_bp.route('/api/kotatua/layers/<layer_id>', methods=['DELETE'])
def delete_layer(layer_id):
    try:
        ensure_table()
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute('DELETE FROM kotatua_map_layers WHERE id = %s RETURNING id', (layer_id,))
                row = cur.fetchone()
                if not row:
                    return jsonify({'ok': False, 'error': 'Layer tidak ditemukan.'}), 404
        return jsonify({'ok': True, 'deleted_id': layer_id})
    except DatabaseUnavailable as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500
    except Exception as exc:
        return jsonify({'ok': False, 'error': f'Gagal hapus layer: {exc}'}), 500


# Backward compatibility with previous custom-areas endpoints.
@kotatua_bp.route('/api/kotatua/custom-areas', methods=['GET'])
def list_custom_areas():
    return list_layers()


@kotatua_bp.route('/api/kotatua/custom-areas', methods=['POST'])
def create_custom_area():
    return create_layer()


@kotatua_bp.route('/api/kotatua/custom-areas/<area_id>', methods=['PUT'])
def update_custom_area(area_id):
    return update_layer(area_id)


@kotatua_bp.route('/api/kotatua/custom-areas/<area_id>', methods=['DELETE'])
def delete_custom_area(area_id):
    return delete_layer(area_id)
