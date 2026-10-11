# gela-data

Datos públicos agregados que usa GELA Tracker y que no se pueden bajar desde Europa.

- `dane/update.py`: exportaciones mensuales de Colombia (microdatos del DANE) por producto (HS4/HS6) y país de destino, en USD FOB. Corre cada lunes en GitHub Actions (`.github/workflows/dane.yml`) y solo baja los archivos que cambiaron (`dane/state.json`).
- `dane/expo/<HS2>.json`: lo que lee la app (fuente `danexpo` en `electron/sources.cjs` de gela-tracker), desde `raw.githubusercontent.com`.
- `dane/meta.json`: columnas, ejemplos y meses cubiertos, para revisar que todo cuadra.

Solo estadística pública agregada: sin datos personales ni de clientes. Por eso el repo es público (la app lo lee sin token).
- `drafts/update.py`: proyectos de decreto, resolución y circular de Colombia publicados para comentarios (MinAmbiente, MinInterior, MinHacienda, MinCIT, MinEnergía, DIAN, DNP), con fecha de publicación y cierre de comentarios. Corre dos veces al día (`.github/workflows/drafts.yml`) con Chromium (Playwright).
- `drafts/co.json`: lo que lee el radar de la app (fuente `consultas`): abiertos, cerrados hace menos de 60 días y nuevos sin fecha. `drafts/sources` dentro del JSON dice si cada página respondió; `drafts/debug/` guarda la última página de cada fuente para revisar cambios de formato.
- `dane/impo.py`: importaciones mensuales de Colombia (microdatos IMPO del DANE): USD FOB y CIF por producto HS6, país de origen y de procedencia, y arancel aplicado y pagado por acuerdo comercial. Corre después de `update.py` en el mismo workflow. Base agregada en `dane/impo_master/` (un archivo por año) y lo que lee la app en `dane/impo/<HS2>/<HS4>.json` (fuente `daneimpo`). Controles en `dane/meta_impo.json`.
- `dane/paises.py` y `dane/ref/paises.json`: correlativa ALADI vs. M49 del DANE. Los archivos de 2023 y 2024 traen el país en código DIAN/ALADI y los de 2025 en adelante en M49; la correlativa también da el nombre de los códigos que no son países (serie 900).
- `arancel/update.py`: Arancel de Aduanas vigente (Decreto 1881 de 2021) desde el normograma de la DIAN, con los decretos que modifican cada subpartida. Corre cada lunes (`.github/workflows/arancel.yml`). La app lo lee en `arancel/<HS2>.json` (fuente `arancelco`).
