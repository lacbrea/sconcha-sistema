# conciliacion/ — motor de conciliación bancaria (vendorizado)

Estos 4 archivos se copiaron TAL CUAL, sin modificar una sola línea, el
2026-08-04 desde:

```
C:\Users\luisa\OneDrive\SCONCHA\AUTO\CONCILIACION\skill\scripts\
```

- `build_conciliacion.py` — el motor (~1.200 líneas). Genera el `.xlsx` de
  conciliación de una empresa/mes: cruza estados de cuenta, constancias de
  transferencia y comprobantes, arma CARGOS/ABONOS/FLUJO CAJA/EGP/CAJA
  CHICA/VERIFICACION/EECC, y valida el cuadre de saldos.
- `parsers_eecc.py` — parsers de los 3 formatos de estado de cuenta que
  acepta el motor (export Excel Interbank, PDF oficial Interbank, .xls-que-
  es-HTML de BBVA).
- `heredar_categorias.py` — lee un `.xlsx` de una corrida anterior para que
  el motor pueda heredar la depuración manual de Proveedor/Categoría
  (opción `--heredar` del motor).
- `notificar_pendientes.py` — CLI aparte que envía por webhook el
  `pendientes.json` que el motor puede generar con `--pendientes`. No lo
  invoca `conciliar.py` (que solo genera el JSON); queda vendorizado para
  cuando haga falta.

## Por qué se copia tal cual, sin refactorizar

Es código estilo script con estado global a nivel de módulo, pero está
validado contra los 5 estados de cuenta reales de junio 2026 con cuadre
EXACTO de saldos (ver las hojas VERIFICACION de esos archivos). Refactorizar
esto es exactamente donde aparecerían regresiones silenciosas, y la ganancia
sería puramente estética. Por eso queda como código vendorizado: si hay que
arreglar algo, se arregla en el original
(`C:\Users\luisa\OneDrive\SCONCHA\AUTO\CONCILIACION\skill\scripts\`) y se
vuelve a copiar aquí — no se edita directamente en este directorio.

## Cómo se invoca

Como **subproceso** (`python conciliacion/build_conciliacion.py ...`), nunca
como import: `build_conciliacion.py` hace `argparse.parse_args()` a nivel de
módulo, así que importarlo dispararía el parseo de argumentos de inmediato.
El envoltorio que arma esos argumentos desde `config.yaml` y Google Drive es
`conciliar.py`, en la raíz del repo.

## Cambios posteriores a la copia inicial

- **2026-08-05 — `--pdf-password`**: los EECC en PDF de Interbank de julio
  2026 llegaron cifrados (los de junio no lo estaban). La contraseña que usa
  Interbank para los PDF de "Cuenta Negocio" es el RUC del titular. Se agregó
  un flag opcional `--pdf-password` a `build_conciliacion.py` que se pasa a
  `parsers_eecc.parse_eecc(path, password=...)`; si el PDF no está cifrado,
  el parámetro se ignora sin error. `conciliar.py` lo arma automáticamente
  buscando el RUC de la empresa en `config['empresas']` (la lista de
  comprobantes, no `config['conciliacion']['empresas']`, que no tiene RUC).
  Arreglado primero en el original
  (`CONCILIACION\skill\scripts\`) y vuelto a copiar aquí, siguiendo la
  convención de este directorio.

- **2026-08-06 — parser de BBVA en PDF y guarda contra el parseo vacío**: desde
  julio 2026 el estado de cuenta de BBVA llega en PDF (hasta junio llegaba como
  `.xls`, que es HTML disfrazado). Como el despachador mandaba todo PDF al
  parser de Interbank, un EECC de BBVA salía con **0 movimientos,
  `banco: 'INTERBANK'` y `anchor_ok: True`**: la conciliación reportaba que la
  cuenta cuadraba mientras ignoraba el mes entero. Se agregó
  `parse_eecc_bbva_pdf`, la detección de banco por CONTENIDO del PDF (no por el
  nombre del archivo, que lo pone quien descarga: el de julio llegó como
  `EC_Julio 2026.pdf`), y `_validar_parseo`, que hace fallar cualquier EECC que
  se parsee a 0 movimientos SIN saldos — distinguiéndolo de una cuenta
  legítimamente sin movimientos, que sí trae sus saldos. Verificado contra el
  estado real de julio de la cuenta 8579: 45 movimientos, la cadena de saldos
  cierra fila a fila, y el cuadre da exacto
  (`4,294.64 + 9,543.29 − 13,828.49 = 9.44`). Arreglado primero en el original y
  vuelto a copiar aquí. Tests en `tests/test_parsers_eecc.py`.

- **2026-08-09 — columna CONCEPTO en los depósitos de CAJA CHICA**: el reporte
  de egresos de caja mezcla dos conceptos distintos dentro de "depósitos"
  (plata que sale de la caja hacia la cuenta del negocio): venta y propina en
  efectivo. Hallazgo real contra MIRAFLORES julio 2026: 3 de sus 5 depósitos
  son propina (`PROPINA EN EFECTIVO 26 AL 02`, `propinas en efectivo 17 al
  23`, `propinas en efectivo del 24 al 30`), no venta. `egresos_caja.py`
  (fuera del motor, se toca libre) ahora clasifica cada depósito con
  `_clasificar_concepto(motivo)` → `"propina"` | `"venta"` | `"indeterminado"`
  (los dos `DEPOSITO`/`400` de MIRAFLORES, donde el motivo no alcanza para
  decidir, quedan `"indeterminado"` a propósito — no se adivina). El campo
  `concepto` viaja en el JSON de `--egresos` (solo para depósitos; los gastos
  no lo llevan) y el motor lo agrega como columna en la sección
  `DEPOSITOS DE VENTA EN EFECTIVO (REPORTE) vs ABONOS DEL BANCO` de la hoja
  CAJA CHICA (reutiliza la columna E, antes solo de notas por fila, ya que
  esas filas no traían nota) y en la sub-lista de depósitos sin abono que
  calce. El cruce depósito↔abono NO cambia: sigue siendo solo por monto y
  fecha, sin importar el concepto — cuadrar propinas es una fase posterior,
  fuera de alcance. Arreglado primero en el original y vuelto a copiar aquí.
  Tests en `tests/test_egresos_caja.py` y `tests/test_conciliar.py`.

## Qué quedó fuera, y por qué

`parse_constancias.py` NO se copió: rastreaba transcripciones de sesiones de
Claude usando rutas de Linux que no existen en esta máquina (Windows).
Quedó obsoleto — lo reemplaza el acceso real a Gmail (`correo_gmail.py`, en
la raíz del repo, que baja las constancias de Interbank directo del correo
de la cuenta del negocio).
