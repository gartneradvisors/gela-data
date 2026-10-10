# gela-data

Datos públicos agregados que usa GELA Tracker y que no se pueden bajar desde Europa.

- `dane/update.py`: exportaciones mensuales de Colombia (microdatos del DANE) por producto (HS4/HS6) y país de destino, en USD FOB. Corre cada lunes en GitHub Actions (`.github/workflows/dane.yml`) y solo baja los archivos que cambiaron (`dane/state.json`).
- `dane/expo/<HS2>.json`: lo que lee la app (fuente `danexpo` en `electron/sources.cjs` de gela-tracker), desde `raw.githubusercontent.com`.
- `dane/meta.json`: columnas, ejemplos y meses cubiertos, para revisar que todo cuadra.

Solo estadística pública agregada: sin datos personales ni de clientes. Por eso el repo es público (la app lo lee sin token).
- `drafts/update.py`: proyectos de decreto, resolución y circular de Colombia publicados para comentarios (MinAmbiente, MinInterior, MinHacienda, MinCIT, MinEnergía, DIAN, DNP), con fecha de publicación y cierre de comentarios. Corre dos veces al día (`.github/workflows/drafts.yml`) con Chromium (Playwright).
- `drafts/co.json`: lo que lee el radar de la app (fuente `consultas`): abiertos, cerrados hace menos de 60 días y nuevos sin fecha. `drafts/sources` dentro del JSON dice si cada página respondió; `drafts/debug/` guarda la última página de cada fuente para revisar cambios de formato.
