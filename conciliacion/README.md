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

## Qué quedó fuera, y por qué

`parse_constancias.py` NO se copió: rastreaba transcripciones de sesiones de
Claude usando rutas de Linux que no existen en esta máquina (Windows).
Quedó obsoleto — lo reemplaza el acceso real a Gmail (`correo_gmail.py`, en
la raíz del repo, que baja las constancias de Interbank directo del correo
de la cuenta del negocio).
