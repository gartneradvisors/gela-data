"""Exportaciones mensuales de Colombia (microdatos del DANE) para el WOI de GELA Tracker.

Corre en GitHub Actions una vez por semana (el DANE bloquea las conexiones
desde Europa, desde EE. UU. responde). Solo baja lo que cambió:

1. Busca en el catálogo de microdatos los conjuntos "EXPO" de los últimos años.
2. Para cada archivo descargable compara tamaño y fecha con dane/state.json;
   si no cambió, no lo baja.
3. Si cambió, lo lee (CSV/TXT, Excel o SPSS dentro del ZIP), agrega por
   producto (HS6) x país de destino x mes (USD FOB) y reemplaza esos meses en
   dane/expo_monthly.csv.gz. Los demás meses quedan como estaban.
4. Regenera dane/expo/<capítulo HS2>.json, que es lo que lee la app, y
   dane/meta.json (columnas, ejemplos, últimos meses) para revisar.

Uso local: python dane/update.py [--force] [--years 3]
"""
import argparse, csv, gzip, io, json, os, re, sys, zipfile
from collections import defaultdict
from datetime import datetime, timezone

import requests
import pandas as pd

BASE = 'https://microdatos.dane.gov.co/index.php'
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, 'state.json')
MASTER = os.path.join(HERE, 'expo_monthly.csv.gz')
OUT = os.path.join(HERE, 'expo')
META = os.path.join(HERE, 'meta.json')
UA = {'User-Agent': 'Mozilla/5.0 (gela-data; +https://github.com/gartneradvisors/gela-data)'}
# Catálogos conocidos por si la búsqueda del catálogo falla (EXPO - 2025 A 2026).
KNOWN = [859]  # 472 = EXPO 2011 A 2024 (lo encuentra la búsqueda)
# Códigos de país propios de la DIAN (columna PAIS) → ISO2. Se completa al ver meta.json;
# si la columna COD_PAI4 trae códigos ISO alfabéticos, esta tabla no hace falta.
DIAN = {}

S = requests.Session(); S.headers.update(UA)


LOG = []
def log(*a):
    line = ' '.join(str(x) for x in a)
    LOG.append(line)
    print(line, flush=True)


def load_json(p, default):
    try:
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def catalogs(kind, years):
    """Ids de los conjuntos EXPO/IMPO cuyo título nombra alguno de los últimos `years` años."""
    want = {datetime.now().year - k for k in range(years + 1)}
    ids = set()
    try:
        r = S.get(f'{BASE}/api/catalog/search', params={'sk': kind, 'ps': 100}, timeout=60)
        rows = (r.json().get('result') or {}).get('rows') or []
        for row in rows:
            title = (row.get('title') or '').upper()
            ys = {int(y) for y in re.findall(r'(19\d{2}|20\d{2})', title)}
            ye = int(row.get('year_end') or (max(ys) if ys else 0))
            if re.search(r'\b%s\b' % kind, title) and ye >= min(want):
                ids.add(int(row['id']))
    except Exception as e:  # el catálogo a veces no responde: se usan los conocidos
        log('Búsqueda del catálogo falló:', e)
    return sorted(ids | set(KNOWN))


def downloads(cid):
    """[(url, nombre)] de un catálogo. NADA pone los enlaces de varias formas
    (href, data-url, onclick); se buscan todos. Si no aparece ninguno, la
    página se guarda en dane/debug/ para revisarla."""
    html = S.get(f'{BASE}/catalog/{cid}/get-microdata', timeout=60).text
    found = {}
    for m in re.finditer(r'((?:https?://microdatos\.dane\.gov\.co)?(?:/index\.php)?/catalog/%d/download/(\d+))' % cid, html):
        rid = m.group(2)
        near = html[max(0, m.start() - 800): m.end() + 800]
        name = re.search(r'([\w\- ]+\.(?:zip|csv|txt|xlsx?|sav|dta))', near, re.I)
        found.setdefault(rid, name.group(1).strip() if name else None)
    if not found:
        os.makedirs(os.path.join(HERE, 'debug'), exist_ok=True)
        with open(os.path.join(HERE, 'debug', f'get-microdata-{cid}.html'), 'w', encoding='utf-8') as fh:
            fh.write(html)
        log(f'  sin enlaces de descarga: página guardada en dane/debug/get-microdata-{cid}.html ({len(html)} caracteres)')
    return [(f'{BASE}/catalog/{cid}/download/{rid}', name or f'{cid}-{rid}') for rid, name in found.items()]


def read_tables(blob, name):
    """DataFrames dentro de un ZIP (también ZIP dentro de ZIP) o archivo suelto.
    Formatos: CSV/TXT/DAT, Excel, SPSS (.sav), Stata (.dta), SAS (.sas7bdat), dBase (.dbf), Parquet."""
    def one(data, fname):
        low = fname.lower()
        if low.endswith(('.csv', '.txt', '.dat', '.tsv')):
            for enc in ('utf-8', 'latin-1'):
                try:
                    return pd.read_csv(io.BytesIO(data), sep=None, engine='python', dtype=str, encoding=enc)
                except UnicodeDecodeError:
                    continue
        if low.endswith(('.xlsx', '.xls')):
            return pd.read_excel(io.BytesIO(data), dtype=str)
        if low.endswith(('.sav', '.zsav', '.dta', '.sas7bdat', '.por')):
            import pyreadstat, tempfile
            reader = {'.sav': pyreadstat.read_sav, '.zsav': pyreadstat.read_sav, '.por': pyreadstat.read_por, '.dta': pyreadstat.read_dta, '.sas7bdat': pyreadstat.read_sas7bdat}[os.path.splitext(low)[1]]
            with tempfile.NamedTemporaryFile(suffix=os.path.splitext(low)[1]) as t:
                t.write(data); t.flush()
                df, _ = reader(t.name)
                return df.astype(str)
        if low.endswith('.dbf'):
            from dbfread import DBF
            import tempfile
            with tempfile.NamedTemporaryFile(suffix='.dbf') as t:
                t.write(data); t.flush()
                return pd.DataFrame(iter(DBF(t.name, encoding='latin-1', char_decode_errors='ignore'))).astype(str)
        if low.endswith('.parquet'):
            return pd.read_parquet(io.BytesIO(data)).astype(str)
        return None
    if zipfile.is_zipfile(io.BytesIO(blob)):
        z = zipfile.ZipFile(io.BytesIO(blob))
        log(f'    contenido de {name}: {[(i.filename, i.file_size) for i in z.infolist()][:20]}')
        for n in z.namelist():
            data = z.read(n)
            if n.lower().endswith('.zip') or zipfile.is_zipfile(io.BytesIO(data)) and not n.lower().endswith(('.xlsx',)):
                yield from read_tables(data, n)
                continue
            try:
                df = one(data, n)
            except Exception as e:
                log(f'    {n}: no se pudo leer ({e!r})'); df = None
            if df is not None:
                yield n, df
    else:
        log(f'    {name} no es ZIP; primeros bytes: {blob[:60]!r}')
        df = one(blob, name)
        if df is not None:
            yield name, df


def month_of(v):
    s = re.sub(r'\D', '', str(v))
    if len(s) == 4:   # AAMM
        return f'20{s[:2]}-{s[2:]}'
    if len(s) >= 6:   # AAAAMM o AAAAMMDD
        return f'{s[:4]}-{s[4:6]}'
    return None


def iso2_map():
    import pycountry
    m = {}
    for c in pycountry.countries:
        m[c.alpha_3] = c.alpha_2; m[c.alpha_2] = c.alpha_2
    return m


def aggregate(df, iso):
    df.columns = [str(c).strip().upper() for c in df.columns]
    need = {'FECH', 'POSAR', 'FOBDOL'}
    if not need <= set(df.columns):
        raise ValueError(f'faltan columnas {need - set(df.columns)}; hay {list(df.columns)[:30]}')
    alpha = 'COD_PAI4' in df.columns and df['COD_PAI4'].dropna().astype(str).str.fullmatch(r'[A-Za-z]{2,3}').mean() > 0.8
    if alpha:
        ctry = df['COD_PAI4'].astype(str).str.upper().map(iso)
    else:
        ctry = df['PAIS'].astype(str).str.strip().str.lstrip('0').map(lambda c: DIAN.get(c) or (f'DIAN:{c}' if c else None))
    hs6 = df['POSAR'].astype(str).str.replace(r'\D', '', regex=True).str.zfill(10).str[:6]
    # 1234.5, 1234,5 o 1.234,5 (separador de miles europeo)
    raw = df['FOBDOL'].astype(str).str.strip()
    raw = raw.where(~(raw.str.contains(r'\.') & raw.str.contains(',')), raw.str.replace('.', '', regex=False))
    fob = pd.to_numeric(raw.str.replace(',', '.', regex=False), errors='coerce').fillna(0)
    month = df['FECH'].map(month_of)
    g = pd.DataFrame({'hs6': hs6, 'ctry': ctry, 'month': month, 'fob': fob}).dropna(subset=['ctry', 'month'])
    return g.groupby(['hs6', 'ctry', 'month'], as_index=False)['fob'].sum(), alpha


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--years', type=int, default=3)
    a = ap.parse_args()
    state = load_json(STATE, {'files': {}})
    master = pd.read_csv(MASTER, dtype={'hs6': str, 'ctry': str, 'month': str}) if os.path.exists(MASTER) else pd.DataFrame(columns=['hs6', 'ctry', 'month', 'fob'])
    iso = iso2_map()
    meta = load_json(META, {})
    changed = False
    for cid in catalogs('EXPO', a.years):
        try:
            files = downloads(cid)
        except Exception as e:
            log(f'Catálogo {cid}: no se pudo leer ({e})'); continue
        log(f'Catálogo {cid}: {len(files)} archivos')
        for url, name in files:
            ys = [int(y) for y in re.findall(r'(20\d{2})', name)]
            if ys and max(ys) < datetime.now().year - a.years:
                log(f'  {name}: más viejo de lo necesario'); continue
            h = S.get(url, timeout=120, stream=True); h.close()
            cd = h.headers.get('Content-Disposition') or ''
            m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd)
            if m: name = m.group(1)
            sig = {'name': name, 'len': h.headers.get('Content-Length'), 'mod': h.headers.get('Last-Modified'), 'etag': h.headers.get('ETag')}
            prev = state['files'].get(url)
            if prev and not a.force and all(prev.get(k) == sig[k] for k in ('len', 'mod', 'etag')) and any(sig[k] for k in ('len', 'mod', 'etag')):
                log(f'  {name}: sin cambios'); continue
            log(f'  {name}: bajando…')
            r = S.get(url, timeout=600)
            ctype = r.headers.get('Content-Type', '')
            if 'html' in ctype.lower():
                os.makedirs(os.path.join(HERE, 'debug'), exist_ok=True)
                with open(os.path.join(HERE, 'debug', 'download-page.html'), 'w', encoding='utf-8') as fh:
                    fh.write(r.text)
                log(f'  {name}: el DANE devolvió una página, no el archivo (guardada en dane/debug/download-page.html): {r.text[:200]!r}'); continue
            parts = []
            for fname, df in read_tables(r.content, name):
                g, alpha = aggregate(df, iso)
                parts.append(g)
                meta.setdefault('files', {})[name] = {
                    'member': fname, 'rows': int(len(df)), 'columns': list(df.columns),
                    'sample': {c: df[c].dropna().astype(str).head(3).tolist() for c in df.columns},
                    'countryCodes': 'COD_PAI4 (ISO)' if alpha else 'PAIS (DIAN)',
                    'months': sorted(g['month'].unique().tolist()),
                }
                if not alpha:
                    top = df.assign(_f=pd.to_numeric(df['FOBDOL'], errors='coerce')).groupby(['PAIS', 'COD_PAI4'] if 'COD_PAI4' in df.columns else ['PAIS'])['_f'].sum().sort_values(ascending=False).head(60)
                    meta['files'][name]['topCountries'] = [[*(k if isinstance(k, tuple) else (k,)), round(float(v))] for k, v in top.items()]
            if not parts:
                log(f'  {name}: sin tablas legibles'); continue
            new = pd.concat(parts).groupby(['hs6', 'ctry', 'month'], as_index=False)['fob'].sum()
            months = set(new['month'])
            master = pd.concat([master[~master['month'].isin(months)], new], ignore_index=True)
            state['files'][url] = sig
            changed = True
            log(f'  {name}: {len(new)} filas, meses {min(months)} a {max(months)}')
    if not changed and (os.path.isdir(OUT) or master.empty) and not a.force:
        log('Nada nuevo.' if not master.empty else 'Sin datos todavía: revisa dane/debug/.'); return
    master = master.sort_values(['hs6', 'ctry', 'month'])
    master.to_csv(MASTER, index=False, compression='gzip')
    # Archivos por capítulo para la app: HS6 y HS4, serie mensual por país (USD FOB, enteros).
    os.makedirs(OUT, exist_ok=True)
    last = master['month'].max()
    asof = datetime.now(timezone.utc).isoformat(timespec='seconds')
    m4 = master.assign(hs4=master['hs6'].str[:4]).groupby(['hs4', 'ctry', 'month'], as_index=False)['fob'].sum().rename(columns={'hs4': 'hs'})
    both = pd.concat([master.rename(columns={'hs6': 'hs'}), m4])
    for ch, part in both.groupby(both['hs'].str[:2]):
        codes = defaultdict(lambda: defaultdict(dict))
        for hs, c, m, f in part[['hs', 'ctry', 'month', 'fob']].itertuples(index=False):
            if f > 0:
                codes[hs][c][m] = int(round(f))
        with open(os.path.join(OUT, f'{ch}.json'), 'w', encoding='utf-8') as fh:
            json.dump({'asOf': asof, 'lastMonth': last, 'unit': 'USD FOB', 'source': 'DANE, microdatos de exportaciones', 'codes': codes}, fh, separators=(',', ':'))
    meta.update({'asOf': asof, 'lastMonth': last, 'months': sorted(master['month'].unique().tolist()), 'rows': int(len(master)),
                 'unmappedCountries': sorted({c for c in master['ctry'].unique() if str(c).startswith('DIAN:')})[:200]})
    with open(META, 'w', encoding='utf-8') as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=1)
    with open(STATE, 'w', encoding='utf-8') as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
    log(f'Listo: {len(master)} filas, último mes {last}.')


if __name__ == '__main__':
    # El registro de cada corrida queda en el repo (dane/last_run.log) para revisarlo sin abrir Actions.
    try:
        main()
    except Exception as e:
        log('ERROR:', repr(e)); raise
    finally:
        with open(os.path.join(HERE, 'last_run.log'), 'w', encoding='utf-8') as fh:
            fh.write(datetime.now(timezone.utc).isoformat(timespec='seconds') + '\n' + '\n'.join(LOG) + '\n')
