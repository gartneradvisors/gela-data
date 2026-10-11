"""Importaciones mensuales de Colombia (microdatos IMPO del DANE) para GELA Tracker.

Corre en GitHub Actions después de update.py (exportaciones). Igual que allí, solo
baja los archivos anuales que cambiaron (dane/state_impo.json).

Qué saca de cada declaración de importación:
  - valor FOB (VAFODO) y CIF (VACID) en USD, por producto HS6, país de ORIGEN
    (PAISGEN), país de PROCEDENCIA (PAISPRO) y mes;
  - el arancel aplicado (PORARA, ponderado por el valor CIF), por acuerdo
    comercial (ACUERDO). IMP1 es el IVA, no el arancel (se comprobó con los datos).

Con eso la app puede:
  1. cruzar lo que Colombia dice que importó de un país con lo que ese país dice
     que exportó a Colombia (espejo mensual con Eurostat Comext);
  2. ver la triangulación: mercancía de origen alemán que llega desde otro país
     (procedencia distinta del origen);
  3. ver el arancel que de verdad se cobra, por acuerdo, frente al general.

Salidas:
  dane/impo_master/flows_AAAA.csv.gz y rates_AAAA.csv.gz  (base agregada, un archivo por año)
  dane/impo/<HS2>/<HS4>.json                               (lo que lee la app)
  dane/meta_impo.json                                      (columnas, totales y controles)

Uso local: python dane/impo.py [--force] [--years 3]
"""
import argparse, glob, json, os, re
from collections import defaultdict
from datetime import datetime, timezone

import pandas as pd

import update as U
import paises as P

HERE = U.HERE
STATE = os.path.join(HERE, 'state_impo.json')
MASTER = os.path.join(HERE, 'impo_master')
OUT = os.path.join(HERE, 'impo')
META = os.path.join(HERE, 'meta_impo.json')
KNOWN = [856]  # IMPO 2025 A 2026; los años anteriores los encuentra la búsqueda del catálogo
COLS = {'FECH', 'NABAN', 'POSAR', 'PAISGEN', 'PAISPRO', 'PAISCOM', 'VAFODO', 'VACID', 'VACIP', 'IMP1',
        'PORARA', 'ACUERDO', 'REGIMEN', 'COPAEX', 'TIPOIM', 'CLASE'}
MONTHS_OUT = 36   # meses que se publican por producto
WINDOW = 12       # ventana de aranceles y rutas

LOG = []
U.LOG = LOG       # read_tables y compañía escriben en este mismo registro
log = U.log


def num(df, c):
    return U.fob_values(df[c]) if c in df.columns else pd.Series(0.0, index=df.index)


def codes(col):
    return col.astype(str).str.strip().str.replace(r'\.0+$', '', regex=True).str.lstrip('0')


def choose_map(df, cands):
    """El sistema de códigos de país cambia según el año (DIAN/ALADI o M49). Se prueba
    cada mapa sobre el archivo y se elige el que reconoce más valor con un reparto
    creíble (China + EE. UU. son entre el 20 y el 70 % de lo que importa Colombia)."""
    org = codes(df['PAISGEN'])
    cif = num(df, 'VACID')
    tot = float(cif.sum()) or 1.0
    best, bests, report = None, -1.0, {}
    for name, mp in cands.items():
        iso = org.map(mp)
        mapped = float(cif[iso.notna()].sum()) / tot
        cnus = float(cif[iso.isin(['CN', 'US'])].sum()) / tot
        score = mapped * (1.0 if 0.2 <= cnus <= 0.85 else 0.3)
        report[name] = {'mapped': round(mapped, 3), 'cnus': round(cnus, 3), 'score': round(score, 3)}
        if score > bests + 1e-6:  # en empate gana el primero (M49, el sistema actual)
            best, bests = name, score
    return best, report


def aggregate(df, cmap):
    prod = 'NABAN' if 'NABAN' in df.columns else 'POSAR'
    need = {'FECH', prod, 'PAISGEN', 'VACID'}
    if not need <= set(df.columns):
        raise ValueError(f'faltan columnas {need - set(df.columns)}; hay {list(df.columns)}')
    hs6 = df[prod].astype(str).str.replace(r'\D', '', regex=True).str.zfill(10).str[:6]
    month = df['FECH'].map(U.month_of)
    tag = lambda s: s.map(lambda c: cmap.get(c) or (f'DIAN:{c}' if c else None))
    org = tag(codes(df['PAISGEN']))
    pro = tag(codes(df['PAISPRO'])) if 'PAISPRO' in df.columns else org
    fob, cif, vacip, imp1, rate = num(df, 'VAFODO'), num(df, 'VACID'), num(df, 'VACIP'), num(df, 'IMP1'), num(df, 'PORARA')
    valid = rate[(cif > 0) & (rate > 0)]
    scale = 100.0 if len(valid) and valid.quantile(0.95) <= 1.0 and valid.max() <= 1.5 else 1.0
    rate = rate * scale
    acu = codes(df['ACUERDO']).replace('', '0') if 'ACUERDO' in df.columns else pd.Series('0', index=df.index)
    base = pd.DataFrame({'hs6': hs6, 'org': org, 'pro': pro.fillna(org), 'month': month})
    ok = base['org'].notna() & base['month'].notna()
    flows = base.assign(fob=fob, cif=cif)[ok].groupby(['hs6', 'org', 'pro', 'month'], as_index=False)[['fob', 'cif']].sum()
    rates = base.assign(acu=acu, cif=cif, rw=rate * cif, imp1=imp1, vacip=vacip)[ok].groupby(['hs6', 'org', 'acu', 'month'], as_index=False)[['cif', 'rw', 'imp1', 'vacip']].sum()
    return flows, rates, scale


def save_year(kind, year, new):
    p = os.path.join(MASTER, f'{kind}_{year}.csv.gz')
    old = pd.read_csv(p, dtype={'hs6': str, 'org': str, 'pro': str, 'acu': str, 'month': str}, keep_default_na=False) if os.path.exists(p) else None
    if old is not None:
        new = pd.concat([old[~old['month'].isin(set(new['month']))], new], ignore_index=True)
    new.sort_values(list(new.columns[:4])).to_csv(p, index=False, compression='gzip')


def load_all(kind):
    parts = [pd.read_csv(p, dtype={'hs6': str, 'org': str, 'pro': str, 'acu': str, 'month': str}, keep_default_na=False)
             for p in sorted(glob.glob(os.path.join(MASTER, f'{kind}_*.csv.gz')))]
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def build_outputs(meta):
    flows, rates = load_all('flows'), load_all('rates')
    if flows.empty:
        log('Sin datos de importaciones todavía.'); return
    months = sorted(flows['month'].unique())
    last = months[-1]
    pub = set(months[-MONTHS_OUT:])
    win = set(months[-WINDOW:])
    asof = datetime.now(timezone.utc).isoformat(timespec='seconds')
    mp = P.maps()
    unk = sorted({c for c in pd.concat([flows['org'], flows['pro']]).unique() if str(c).startswith('DIAN:')})
    names = {c: d for c in unk if (d := P.describe(c[5:], mp, prefer=('aladi', 'm49')))}

    f = flows[flows['month'].isin(pub)].copy()
    f['ind'] = f['cif'].where(f['pro'] != f['org'], 0.0)
    byo = f.groupby(['hs6', 'org', 'month'], as_index=False)[['fob', 'cif', 'ind']].sum()
    routes = flows[flows['month'].isin(win) & (flows['pro'] != flows['org'])].groupby(['hs6', 'org', 'pro'], as_index=False)['cif'].sum()
    r = rates[rates['month'].isin(win)].groupby(['hs6', 'org', 'acu'], as_index=False)[['cif', 'rw', 'imp1', 'vacip']].sum()

    out = defaultdict(lambda: {'codes': defaultdict(lambda: defaultdict(dict)), 'rates': defaultdict(dict), 'routes': defaultdict(dict)})
    for hs, o, m, fob, cif, ind in byo.itertuples(index=False):
        if cif > 0 or fob > 0:
            out[hs[:4]]['codes'][hs][o][m] = [int(round(fob)), int(round(cif)), int(round(ind))]
    for (hs, o), g in r.groupby(['hs6', 'org']):
        cif = float(g['cif'].sum())
        if cif <= 0:
            continue
        agr = g.assign(share=g['cif'] / cif, ap=(g['rw'] / g['cif'].where(g['cif'] > 0))).sort_values('cif', ascending=False).head(5)
        out[hs[:4]]['rates'][hs][o] = {
            'cif': int(round(cif)),
            'applied': round(float(g['rw'].sum()) / cif, 2),
            'agreements': {a: [round(float(s), 3), round(float(p), 2) if p == p else None] for a, s, p in agr[['acu', 'share', 'ap']].itertuples(index=False)},
        }
    for (hs, o), g in routes.groupby(['hs6', 'org']):
        top = g.sort_values('cif', ascending=False).head(5)
        out[hs[:4]]['routes'][hs][o] = [[p, int(round(c))] for p, c in top[['pro', 'cif']].itertuples(index=False)]

    os.makedirs(OUT, exist_ok=True)
    for hs4, d in out.items():
        os.makedirs(os.path.join(OUT, hs4[:2]), exist_ok=True)
        used = {c for v in d['codes'].values() for c in v} | {p for v in d['routes'].values() for lst in v.values() for p, _ in lst}
        with open(os.path.join(OUT, hs4[:2], f'{hs4}.json'), 'w', encoding='utf-8') as fh:
            json.dump({'asOf': asof, 'lastMonth': last, 'window': [min(win), max(win)], 'unit': 'USD',
                       'fields': ['fob', 'cif', 'cifIndirecta'], 'source': 'DANE, microdatos de importaciones',
                       'codes': d['codes'], 'rates': d['rates'], 'routes': d['routes'],
                       'names': {c: names[c] for c in sorted(used) if c in names}}, fh, separators=(',', ':'))

    # Controles para revisar sin abrir los datos
    yt = (flows.assign(y=flows['month'].str[:4]).groupby('y')['cif'].sum() / 1e6).round(0)
    lw = flows[flows['month'].isin(win)]
    top = (lw.groupby('org')['cif'].sum().sort_values(ascending=False).head(15) / 1e6).round(1)
    ind = lw[lw['pro'] != lw['org']]
    rr = rates[rates['month'].isin(win)]
    acu = rr.groupby('acu').agg(cif=('cif', 'sum'), rw=('rw', 'sum'), imp1=('imp1', 'sum'), vacip=('vacip', 'sum')).sort_values('cif', ascending=False).head(25)
    acu_org = rr.groupby(['acu', 'org'])['cif'].sum().reset_index().sort_values('cif', ascending=False)
    tot_cif = float(rr['cif'].sum()) or 1.0
    meta.pop('paidVsApplied', None)
    meta.update({
        'asOf': asof, 'lastMonth': last, 'months': months, 'rowsFlows': int(len(flows)), 'rowsRates': int(len(rates)),
        'cifUsdMillionsByYear': yt.to_dict(),
        'topOriginsLast12mUsdMillions': top.to_dict(),
        'indirectShareLast12m': round(float(ind['cif'].sum()) / max(1.0, float(lw['cif'].sum())), 3),
        'topIndirectRoutesLast12m': [[o, p, round(c / 1e6, 1)] for o, p, c in ind.groupby(['org', 'pro'])['cif'].sum().sort_values(ascending=False).head(20).reset_index().itertuples(index=False)],
        'agreementsLast12m': [{'acuerdo': a, 'cifShare': round(float(v.cif) / tot_cif, 3), 'applied': round(float(v.rw / v.cif), 2) if v.cif else None,
                               'topOrigins': acu_org[acu_org['acu'] == a].head(6)['org'].tolist()} for a, v in acu.iterrows()],
        # IMP1 resultó ser el IVA pagado (10/2026: 19 % sobre CIF en pesos + arancel), no el arancel; se guarda en la base pero no se publica.
        'ivaOverCifPct': round(float(rr['imp1'].sum() / rr['vacip'].sum() * 100), 2) if rr['vacip'].sum() else None,
        'unmapped': unk[:200], 'unmappedNames': names, 'paisesAsOf': P.load().get('asOf'),
    })
    with open(META, 'w', encoding='utf-8') as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=1)
    log(f'Listo: {len(flows)} filas de flujos, último mes {last}, {len(out)} archivos HS4.')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--years', type=int, default=3)
    a = ap.parse_args()
    state = U.load_json(STATE, {'files': {}})
    meta = U.load_json(META, {})
    os.makedirs(MASTER, exist_ok=True)
    mp = P.maps()
    learned = {k: v for k, v in (U.load_json(U.STATE, {}).get('dian') or {}).items()}
    cands = {'m49': mp['m49'], 'aladi': mp['aladi'], 'aprendido_expo': learned}
    U.KNOWN = KNOWN
    allfiles = []
    for cid in U.catalogs('IMPO', a.years):
        try:
            files = [(u, n) for u, n in U.downloads(cid) if 'impo' in str(n).lower() or str(n).startswith(f'{cid}-')]
        except Exception as e:  # noqa: BLE001
            log(f'Catálogo {cid}: no se pudo leer ({e})'); continue
        log(f'Catálogo {cid}: {len(files)} archivos')
        allfiles += files
    allfiles.sort(key=lambda f: max([int(y) for y in re.findall(r'(20\d{2})', f[1])] or [0]), reverse=True)
    changed = False
    for url, name in allfiles:
        ys = [int(y) for y in re.findall(r'(20\d{2})', name)]
        if ys and max(ys) < datetime.now().year - a.years:
            log(f'  {name}: más viejo de lo necesario'); continue
        h = U.S.get(url, timeout=120, stream=True); h.close()
        cd = h.headers.get('Content-Disposition') or ''
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd)
        if m: name = m.group(1)
        if 'expo' in name.lower():
            continue
        sig = {'name': name, 'len': h.headers.get('Content-Length'), 'mod': h.headers.get('Last-Modified'), 'etag': h.headers.get('ETag')}
        prev = state['files'].get(url)
        if prev and not a.force and all(prev.get(k) == sig[k] for k in ('len', 'mod', 'etag')) and any(sig[k] for k in ('len', 'mod', 'etag')):
            log(f'  {name}: sin cambios'); continue
        log(f'  {name}: bajando…')
        r = U.S.get(url, timeout=1200)
        if 'html' in r.headers.get('Content-Type', '').lower():
            log(f'  {name}: el DANE devolvió una página, no el archivo: {r.text[:200]!r}'); continue
        fparts, rparts = [], []
        for fname, df in U.read_tables(r.content, name, usecols=lambda c: c in COLS):
            df.columns = [str(c).replace('﻿', '').strip().upper() for c in df.columns]
            try:
                which, rep = choose_map(df, cands)
                # El mapa aprendido de exportaciones no tiene los países a los que Colombia no exporta
                # (Burundi, Bután…): en archivos con códigos DIAN/ALADI se completa con la correlativa.
                cmap = cands[which] if which == 'm49' else {**cands['aladi'], **cands[which]}
                fl, rt, scale = aggregate(df, cmap)
            except Exception as e:  # noqa: BLE001
                log(f'    {fname}: no se pudo agregar ({e})'); continue
            fparts.append(fl); rparts.append(rt)
            meta.setdefault('files', {})[f'{name}/{fname}'] = {
                'rows': int(len(df)), 'columns': list(df.columns), 'countryMap': which, 'mapTest': rep, 'poraraScale': scale,
                'sample': {c: df[c].astype(str).head(3).tolist() for c in df.columns},
                'cifUsd': round(float(fl['cif'].sum())), 'months': sorted(fl['month'].unique().tolist()),
            }
            log(f'    {fname}: {len(df)} filas, países con mapa "{which}" {rep[which]}')
        if not fparts:
            log(f'  {name}: sin tablas legibles'); continue
        fl = pd.concat(fparts).groupby(['hs6', 'org', 'pro', 'month'], as_index=False)[['fob', 'cif']].sum()
        rt = pd.concat(rparts).groupby(['hs6', 'org', 'acu', 'month'], as_index=False)[['cif', 'rw', 'imp1', 'vacip']].sum()
        for y in sorted({m[:4] for m in fl['month']}):
            save_year('flows', y, fl[fl['month'].str[:4] == y])
            save_year('rates', y, rt[rt['month'].str[:4] == y])
        state['files'][url] = sig
        changed = True
        log(f'  {name}: {len(fl)} filas, meses {fl["month"].min()} a {fl["month"].max()}')
    if not changed and os.path.isdir(OUT) and not a.force and meta.get('paisesAsOf') == P.load().get('asOf'):
        log('Nada nuevo.'); return
    build_outputs(meta)
    with open(STATE, 'w', encoding='utf-8') as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:  # noqa: BLE001
        log('ERROR:', repr(e)); raise
    finally:
        with open(os.path.join(HERE, 'last_run_impo.log'), 'w', encoding='utf-8') as fh:
            fh.write(datetime.now(timezone.utc).isoformat(timespec='seconds') + '\n' + '\n'.join(LOG) + '\n')
