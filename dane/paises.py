"""Códigos de país de los microdatos del DANE.

Los archivos del DANE no usan siempre el mismo sistema en la columna de país:
- 2023 y 2024 traen los códigos de la DIAN/ALADI (249 = EE. UU., 580 = Panamá, 23 = Alemania).
- 2025 en adelante traen el M49 de la ONU, que es el ISO numérico (840 = EE. UU., 591 = Panamá, 276 = Alemania).
- Además hay códigos propios de la serie 900 (y otros sueltos) que no son países ISO.

El DANE publica una "Correlativa ALADI vs. M49" (catálogo 856, materiales relacionados).
Este módulo la baja, la guarda en dane/ref/paises.json y da tres mapas código → ISO2:
  m49      ISO numérico, sin tabla externa (pycountry)
  aladi    código DIAN/ALADI → M49 → ISO2, según la correlativa
  nombres  nombre oficial de cualquier código, también de los que no son países (serie 900)
"""
import io, json, os, re
from datetime import datetime, timezone

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
REF = os.path.join(HERE, 'ref', 'paises.json')
CORRELATIVA_URL = 'https://microdatos.dane.gov.co/index.php/catalog/856/download/24687'
ZF = re.compile(r'zona\s*franca|\bz\.?\s?f\.?\b|usuario\s+industrial|parque\s+industrial', re.I)


def norm(c):
    s = re.sub(r'\.0+$', '', str(c).strip())
    s = s.lstrip('0')
    return s if s.isdigit() else ''


def _iso_numeric():
    import pycountry
    return {norm(c.numeric): c.alpha_2 for c in pycountry.countries}, {norm(c.numeric): c.name for c in pycountry.countries}


def parse_correlativa(blob):
    """Lee la correlativa sin suponer su forma exacta: busca en cada hoja la fila de
    encabezados que nombra ALADI o M49 y toma las columnas de código y de nombre."""
    xl = pd.read_excel(io.BytesIO(blob), sheet_name=None, dtype=str, header=None)
    out = {'aladi': {}, 'm49': {}, 'sheets': []}
    for sh, df in xl.items():
        df = df.fillna('')
        hdr = None
        for i in range(min(25, len(df))):
            vals = ' | '.join(str(v).upper() for v in df.iloc[i].tolist())
            if 'ALADI' in vals or 'M49' in vals or 'M 49' in vals:
                hdr = i; break
        if hdr is None:
            out['sheets'].append({'sheet': sh, 'note': 'sin encabezado ALADI/M49', 'head': df.head(5).values.tolist()})
            continue
        cols = [str(v).strip() or f'col{j}' for j, v in enumerate(df.iloc[hdr].tolist())]
        body = df.iloc[hdr + 1:].copy(); body.columns = cols
        numeric = {c: body[c].astype(str).str.strip().str.fullmatch(r'\d{1,4}(\.0+)?').mean() for c in cols}
        code_cols = [c for c in cols if numeric[c] > 0.6]
        name_cols = [c for c in cols if numeric[c] < 0.2 and body[c].astype(str).str.len().mean() > 3]
        ala = next((c for c in code_cols if 'ALADI' in c.upper()), None)
        m49 = next((c for c in code_cols if re.search(r'M\s?49|ONU|UN\b|ISO', c.upper())), None)
        if not ala and len(code_cols) >= 1: ala = code_cols[0]
        if not m49 and len(code_cols) >= 2: m49 = next(c for c in code_cols if c != ala)
        ala_name = next((c for c in name_cols if 'ALADI' in c.upper()), name_cols[0] if name_cols else None)
        m49_name = next((c for c in name_cols if re.search(r'M\s?49|ONU', c.upper())), ala_name)
        out['sheets'].append({'sheet': sh, 'columns': cols, 'aladi': ala, 'm49': m49, 'names': name_cols, 'rows': int(len(body)), 'head': body.head(4).values.tolist()})
        for _, r in body.iterrows():
            a, m = norm(r.get(ala, '')) if ala else '', norm(r.get(m49, '')) if m49 else ''
            na = str(r.get(ala_name, '')).strip() if ala_name else ''
            nm = str(r.get(m49_name, '')).strip() if m49_name else na
            if a:
                out['aladi'].setdefault(a, {'m49': m or None, 'name': na or nm})
            if m:
                out['m49'].setdefault(m, {'aladi': a or None, 'name': nm or na})
    return out


def refresh(session, log=print):
    """Baja la correlativa y regenera dane/ref/paises.json. Si el DANE no responde,
    se queda con la versión anterior."""
    try:
        r = session.get(CORRELATIVA_URL, timeout=120)
        if r.status_code != 200 or 'html' in r.headers.get('Content-Type', '').lower():
            log(f'Correlativa ALADI-M49: el DANE respondió {r.status_code} {r.headers.get("Content-Type")}; sigo con la versión guardada')
            return load()
        corr = parse_correlativa(r.content)
    except Exception as e:  # noqa: BLE001
        log('Correlativa ALADI-M49: no se pudo leer', repr(e))
        return load()
    data = {'asOf': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'source': CORRELATIVA_URL, **corr}
    os.makedirs(os.path.dirname(REF), exist_ok=True)
    with open(REF, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    log(f'Correlativa ALADI-M49: {len(corr["aladi"])} códigos ALADI, {len(corr["m49"])} M49, hojas {[s.get("sheet") for s in corr["sheets"]]}')
    return data


def load():
    try:
        with open(REF, encoding='utf-8') as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {'aladi': {}, 'm49': {}, 'sheets': []}


def maps(ref=None):
    """{'m49': código→ISO2, 'aladi': código→ISO2} y nombres de cualquier código."""
    ref = ref or load()
    iso, iso_name = _iso_numeric()
    m49 = dict(iso)
    aladi = {}
    for a, v in (ref.get('aladi') or {}).items():
        m = norm(v.get('m49') or '')
        if m in iso:
            aladi[a] = iso[m]
    names = {}
    for a, v in (ref.get('aladi') or {}).items():
        if v.get('name'): names.setdefault(('aladi', a), v['name'])
    for m, v in (ref.get('m49') or {}).items():
        names.setdefault(('m49', m), v.get('name') or iso_name.get(m))
    for m, n in iso_name.items():
        names.setdefault(('m49', m), n)
    return {'m49': m49, 'aladi': aladi, 'names': names}


def describe(code, mp, prefer=('m49', 'aladi')):
    """Nombre y marca de zona franca para un código que no se pudo pasar a ISO2."""
    c = norm(code)
    for sys_ in prefer:
        n = mp['names'].get((sys_, c))
        if n:
            return {'name': n, 'system': sys_, 'zonaFranca': bool(ZF.search(n))}
    return None
