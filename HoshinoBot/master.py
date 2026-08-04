from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse
import re
import sqlite3
import tempfile

import requests


TEXT_TABLES = {
    'CHS', 'CHT', 'DEU', 'ENG', 'FRA', 'JPN', 'KOR', 'SPA', 'THA',
    'VIE',
}


def _qident(name):
    return '"' + name.replace('"', '""') + '"'


def _clean_ident(name, fallback):
    name = re.sub(r'\s+', '_', (name or '').strip())
    name = re.sub(r'[^0-9A-Za-z_\u4e00-\u9fff.-]', '_', name).strip('._-')
    return name or fallback


def _unique_names(names):
    used = defaultdict(int)
    result = []
    for index, name in enumerate(names, 1):
        base = _clean_ident(name, 'col_{}'.format(index))
        used[base] += 1
        result.append(base if used[base] == 1 else '{}_{}'.format(
            base, used[base]))
    return result


def _safe_text(value):
    if isinstance(value, bytes):
        return value.decode('utf-8-sig', errors='replace')
    if not isinstance(value, str):
        value = str(value)
    return value.encode('utf-8', errors='replace').decode('utf-8')


def _parse_table(text):
    lines = [line.rstrip('\r') for line in text.splitlines()
             if line.rstrip('\r')]
    if not lines:
        return [], []
    if '@' in lines[0]:
        headers = _unique_names(lines[0].rstrip('\r\n').split('@'))
        data_lines = lines[1:]
    else:
        headers = ['value']
        data_lines = lines

    width = len(headers)
    rows = []
    for line in data_lines:
        cells = [_safe_text(cell) for cell in line.rstrip('\r\n').split('@')]
        if len(cells) < width:
            cells += [''] * (width - len(cells))
        elif len(cells) > width:
            extra = len(cells) - width
            headers.extend('extra_{}'.format(i + 1) for i in range(extra))
            for row in rows:
                row.extend('' for _ in range(extra))
            width = len(headers)
        rows.append(cells)
    return headers, rows


def _catalog_name(info):
    name = info.get('NewCatalogName')
    return name.strip() if isinstance(name, str) and name.strip() else None


def _patch_domain(info):
    domains = []
    for key in ('PathDomain', 'PathDomains'):
        value = info.get(key)
        if isinstance(value, str):
            domains.extend(part.strip() for part in re.split(r'[,;|]', value)
                           if part.strip())
    return domains[0].rstrip('/') if domains else None


def _bundle_urls(catalog, patch_domain):
    urls = []
    for internal_id in catalog.get('m_InternalIds', []):
        if not isinstance(internal_id, str):
            continue
        name = Path(urlparse(internal_id).path).name.lower()
        if not name.endswith('.bundle') or not name.startswith(
                ('staticdata_', 'text_')):
            continue
        url = internal_id.replace('http://PatchDomain', patch_domain)
        url = url.replace('https://PatchDomain', patch_domain)
        if url.startswith('//'):
            url = 'https:' + url
        if not url.startswith(('http://', 'https://')):
            url = '{}/{}'.format(patch_domain, url.lstrip('/'))
        if url not in urls:
            urls.append(url)
    return urls


def _download_json(session, url):
    response = session.get(url, timeout=30)
    response.raise_for_status()
    return response.json()


def _download_file(session, url, path):
    with session.get(url, stream=True, timeout=180) as response:
        response.raise_for_status()
        with path.open('wb') as output:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    output.write(chunk)


def _insert_table(conn, table, source_file, asset_name, columns, rows):
    conn.execute('DROP TABLE IF EXISTS {}'.format(_qident(table)))
    conn.execute('CREATE TABLE {} ({})'.format(
        _qident(table),
        ', '.join('{} TEXT'.format(_qident(column)) for column in columns),
    ))
    if rows:
        column_sql = ', '.join(_qident(column) for column in columns)
        placeholders = ', '.join('?' for _ in columns)
        conn.executemany(
            'INSERT INTO {} ({}) VALUES ({})'.format(
                _qident(table), column_sql, placeholders),
            [tuple(row[:len(columns)]) for row in rows],
        )
    conn.execute(
        'INSERT INTO __table_manifest('
        'table_name, source_file, asset_name, row_count, column_count'
        ') VALUES (?, ?, ?, ?, ?)',
        (table, source_file, asset_name, len(rows), len(columns)),
    )


def _build_master_db(bundle_paths, output_path):
    try:
        import UnityPy
    except ImportError as exc:
        raise RuntimeError('缺少 UnityPy，请先执行 pip install UnityPy') from exc

    seen_tables = defaultdict(int)
    conn = sqlite3.connect(str(output_path))
    try:
        conn.execute('PRAGMA journal_mode=DELETE')
        conn.execute('PRAGMA synchronous=NORMAL')
        conn.execute(
            'CREATE TABLE __table_manifest('
            'table_name TEXT PRIMARY KEY, source_file TEXT, asset_name TEXT, '
            'row_count INTEGER, column_count INTEGER)'
        )
        for bundle_path in bundle_paths:
            env = UnityPy.load(str(bundle_path))
            for obj in env.objects:
                if obj.type.name != 'TextAsset':
                    continue
                try:
                    data = obj.read()
                    asset_name = _clean_ident(
                        getattr(data, 'm_Name', ''),
                        'textasset_{}'.format(obj.path_id),
                    )
                    if asset_name in TEXT_TABLES and asset_name != 'CHS':
                        continue
                    columns, rows = _parse_table(
                        _safe_text(getattr(data, 'm_Script', '')))
                    if not columns and not rows:
                        continue
                    seen_tables[asset_name] += 1
                    table = asset_name if seen_tables[asset_name] == 1 else \
                        '{}_{}'.format(asset_name, seen_tables[asset_name])
                    _insert_table(
                        conn, table, bundle_path.name, asset_name,
                        columns, rows)
                except Exception:
                    continue
        conn.commit()
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        missing = {'Role', 'CHS'} - tables
        if missing:
            raise RuntimeError('master.db 缺少必要表：{}'.format(
                '、'.join(sorted(missing))))
    finally:
        conn.close()


def _master_valid(path):
    path = Path(path)
    if not path.is_file():
        return False
    conn = None
    try:
        conn = sqlite3.connect(str(path))
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        return {'Role', 'CHS'}.issubset(tables)
    except sqlite3.Error:
        return False
    finally:
        if conn is not None:
            conn.close()


def update_master_db(bulletin, output_path, current_catalog=None,
                     session=None):
    """Update master.db atomically and keep no catalog/bundle files."""
    info = bulletin.get('Info', bulletin)
    if not isinstance(info, dict):
        raise RuntimeError('公告响应缺少 Info')
    catalog_name = _catalog_name(info)
    patch_domain = _patch_domain(info)
    if not catalog_name or not patch_domain:
        raise RuntimeError('公告响应缺少 catalog 或补丁域名')
    output_path = Path(output_path)
    if current_catalog == catalog_name and _master_valid(output_path):
        return catalog_name, False

    http = session or requests.Session()
    catalog_url = '{}/Android/{}.json'.format(patch_domain, catalog_name)
    catalog = _download_json(http, catalog_url)
    urls = _bundle_urls(catalog, patch_domain)
    if not urls:
        raise RuntimeError('catalog 中没有 staticdata/text bundle')

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
            prefix='gvg-master-', dir=str(output_path.parent)) as temp_dir:
        temp_dir = Path(temp_dir)
        bundles = []
        for index, url in enumerate(urls, 1):
            name = Path(urlparse(url).path).name or 'bundle_{}.bundle'.format(
                index)
            path = temp_dir / '{}_{}'.format(index, name)
            _download_file(http, url, path)
            bundles.append(path)
        temp_db = temp_dir / 'master.db'
        _build_master_db(bundles, temp_db)
        temp_db.replace(output_path)
    return catalog_name, True
