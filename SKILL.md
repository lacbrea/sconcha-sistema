# Skill de conciliación de comprobantes — SCONCHA

## Qué hace

Procesa en una sola pasada los comprobantes (XML UBL, PDF, fotos) que caen
en la carpeta `00_BUZON` de Google Drive de un negocio: los lee (con
parseo determinístico si son XML, o con el modelo de Claude si son PDF o
imagen), identifica a qué empresa propia le corresponde cada uno, evita
registrar duplicados, empareja cada línea de ítem contra el catálogo de
insumos, escribe todo en dos Google Sheets y finalmente mueve el archivo
original a `01_PROCESADO/AAAA-MM/EMPRESA/` (o a `02_REVISAR/` si algo no
se pudo resolver automáticamente, junto con un archivo `.motivo.txt` que
explica por qué).

Ningún archivo se borra nunca. Un archivo que falla no detiene el lote.

## Arquitectura

```
procesar.py          orquestador: una pasada sobre 00_BUZON (este archivo)
almacen_drive.py       capa de almacenamiento: encapsula toda llamada a la API de Google Drive v3
auth_google.py        OAuth de usuario (Drive + Sheets + Gmail de solo lectura), token.json/credenciales.json
init_negocio.py        prepara un negocio nuevo: carpetas (por API de Drive) + Sheets con cabeceras
config.yaml             datos del negocio (empresas, RUCs, locales, IDs de carpetas de Drive y de Sheets)
config.ejemplo.yaml      plantilla comentada para dar de alta OTRO negocio

conciliar.py            envoltorio de conciliación bancaria: arma los argumentos del motor desde config.yaml y sube el .xlsx a Drive (ver "Conciliación bancaria" más abajo)
correo_gmail.py          lectura de Gmail de solo lectura, para bajar constancias de transferencia (y, cuando se implemente, EECC y comprobantes)
conciliacion/             motor de conciliación bancaria vendorizado (ver conciliacion/README.md)

esquema.py               (otro agente) dataclasses ComprobanteExtraido / ItemExtraido
extractores/xml_ubl.py   (otro agente) parseo determinístico de XML UBL / ZIP
extractores/modelo.py    (otro agente) lectura de PDF/imagen vía Claude
catalogo.py               (otro agente) emparejado de descripciones contra insumos.csv
registro_sheets.py        (otro agente) escritura en los dos Google Sheets
insumos.csv                (otro agente) catálogo de insumos (insumo, categoria, unidad_base, alias)
```

`procesar.py` e `init_negocio.py` nunca llaman a `googleapiclient` directamente:
reciben una instancia de `almacen_drive.AlmacenDrive` (construida sobre
`auth_google.servicio_drive()`) inyectada, y todo el trato con Drive —
listar el buzón, descargar un archivo a un temporal, mover/renombrar,
crear un `.motivo.txt`, obtener el link real de un archivo, asegurar que
una carpeta exista — pasa por esa clase. Así sus tests pueden sustituirla
por un doble en memoria sin tocar red ni credenciales (ver
`tests/test_almacen_drive.py` para las pruebas de la clase misma, contra
un doble del `Resource` de `drive` v3).

`procesar.py` importa `extractores.xml_ubl`, `extractores.modelo`,
`catalogo` y `registro_sheets` como módulos de nivel superior; nunca
importa `esquema` en tiempo de ejecución (usa *duck typing*: le basta con
que el objeto que devuelven los extractores tenga los atributos y métodos
del contrato — `clave()`, `validar()`, `.items`, etc. — sin necesidad de
que el tipo `ComprobanteExtraido` exista como import real). Esto permite
que `procesar.py` y sus tests corran aunque los otros módulos todavía
estén en construcción.

### Flujo por archivo

1. **Listar el buzón** por API (`AlmacenDrive.listar`) y **agrupar** los
   archivos por nombre (sin extensión). Si un grupo tiene exactamente un
   XML/ZIP, ese es el comprobante principal y el resto del grupo (por
   ejemplo, el PDF con el mismo nombre) se trata como respaldo enlazado: se
   mueve junto al XML pero no se procesa ni se registra como comprobante
   aparte ("XML gana siempre"). Ver la trampa sobre el criterio de agrupado
   más abajo.
2. **Clasificar por extensión**: `.xml`/`.zip` → `extractores.xml_ubl`;
   `.pdf` → `extractores.modelo(tipo='pdf')`; imágenes →
   `extractores.modelo(tipo='imagen')`; `.heic` → directo a revisar;
   cualquier otra extensión → directo a revisar.
3. **Descargar** el archivo principal a un temporal
   (`tempfile.TemporaryDirectory`, vía `AlmacenDrive.descargar`) y
   **extraer** los datos del comprobante desde esa copia local (los
   extractores siguen recibiendo un `pathlib.Path`, no cambian). Si la
   descarga o el extractor lanzan una excepción, o si `comp.validar()`
   devuelve advertencias, el archivo va a revisar con el motivo
   correspondiente. El temporal se borra solo al salir del `with`.
4. **Asignar empresa** buscando `comp.cliente_ruc` en `config['empresas']`.
   Si no hay RUC de cliente, o no coincide con ninguna empresa propia, va a
   revisar — nunca se adivina. Este es el chequeo que habría detectado los
   6 comprobantes de junio facturados a ILLAWARA pero pagados desde EL
   TEMPLO.
5. **Resolver el local**: si la empresa tiene 0 o 1 locales configurados,
   se asigna automáticamente (cadena vacía si no tiene ninguno). Si tiene
   más de uno (como EL TEMPLO: LINCE y CP), el comprobante no trae dato
   para decidir cuál, así que también va a revisar.
6. **Deduplicar**: se calcula `comp.clave()` y se compara contra
   `Registro.claves_existentes()` (lo ya escrito en el Sheet) y contra las
   claves ya procesadas en esta misma corrida. Un duplicado va a revisar y
   nunca se escribe dos veces.
7. **Emparejar ítems** contra el catálogo (ver trampa sobre atributos
   dinámicos).
8. **Escribir** con `Registro.escribir(comp, empresa, local, link_drive,
   archivo)`. `link_drive` sale de `AlmacenDrive.enlace(file_id)`
   (`webViewLink` real de los metadatos del archivo).
9. **Mover y renombrar por API**: `AlmacenDrive.asegurar_carpeta(...)`
   crea (o reutiliza) las subcarpetas `AAAA-MM` y `EMPRESA_CON_GUION_BAJO`
   dentro de `01_PROCESADO`, y `AlmacenDrive.mover(file_id, carpeta_destino,
   nuevo_nombre)` mueve y renombra el archivo dentro de Drive en una sola
   llamada, a `RUC_SERIE-NUMERO_TOTAL.ext`. Si el destino ya tiene un
   archivo con ese nombre, se agrega un sufijo numérico (`_2`, `_3`, ...);
   nunca se sobrescribe nada.

## Cómo se corre

```powershell
cd C:\Users\luisa\sconcha-sistema
C:\Python312\python.exe procesar.py --config config.yaml
C:\Python312\python.exe procesar.py --config config.yaml --dry-run --verbose
C:\Python312\python.exe procesar.py --solo F001-00123.xml
C:\Python312\python.exe procesar.py --limite 15
```

- `--dry-run`: no escribe en Sheets, no mueve archivos, no crea
  subcarpetas ni `.motivo.txt`; solo informa qué haría (sigue leyendo
  `Registro.claves_existentes()` de verdad, para que la vista previa de
  duplicados sea realista). **Sigue necesitando credenciales de Google
  válidas**: listar el buzón y descargar cada archivo para previsualizar
  el resultado son lecturas contra la API de Drive, no lectura de disco
  local — lo único que `--dry-run` evita son las escrituras.
- `--solo <archivo>`: procesa solo el archivo indicado (nombre exacto,
  incluida la extensión).
- `--limite N`: procesa como máximo N comprobantes (útil para la corrida
  de calibración inicial de 15 documentos, ver ONBOARDING.md).
- `--verbose`: log detallado (DEBUG) también en la consola.

El log completo siempre se escribe en `salida/procesar.log`, **relativo al
directorio de trabajo desde el que se invoca el comando** (no relativo a
`config.yaml` ni a la ubicación de `procesar.py`). Corre siempre el
comando desde la raíz del proyecto.

## Formato de los dos Google Sheets

La única fuente de verdad sobre las columnas de los dos Sheets son las
constantes `COLUMNAS_CONTABLE` (32 columnas) y `COLUMNAS_DETALLE` (15
columnas) en `registro_sheets.py`. Esta sección describe su estructura,
pero no las repite una por una a propósito: repetirlas aquí es lo que creó
este mismo problema — dos fuentes de verdad que divergen en silencio sin
que nada avise. `init_negocio.py` no copia esa lista: la importa
directamente de `registro_sheets.py` (`from registro_sheets import
COLUMNAS_CONTABLE, COLUMNAS_DETALLE`), así que si algún día cambian, basta
con tocar `registro_sheets.py` — pero conviene revisar `init_negocio.py`
en el mismo cambio, porque ahí vive el resto de la lógica de creación de
los Sheets (títulos, formato de cabecera).

**Sheet "contable"** — una fila por comprobante. Las primeras 18 columnas
replican EXACTO el registro histórico `REGISTRO COMPROBANTES.xlsx` (hoja
"COMPROBANTES"), para que el motor de conciliación (hoy vendorizado en
`conciliacion/`, ver "Conciliación bancaria") lo siga consumiendo sin
modificarlo. Las 14 columnas
nuevas del skill van después, al final. Dos trampas conocidas dentro de
ese bloque histórico:

- La columna 15 se llama `LINK_DRIVE` (no `LINK_COMPROBANTE`, como en el
  Excel histórico): es el nombre que espera el motor de conciliación
  (`build_conciliacion.py`), así que se usó a propósito ese nombre en vez
  del original para eliminar esa trampa de raíz.
- El motor de conciliación solo cruza filas con `ESTADO_PAGO = PAGADA`
  (`build_conciliacion.py`, línea 484). `registro_sheets.py` deja
  `ESTADO_PAGO` vacío a propósito al registrar un comprobante nuevo: lo
  llena después el flujo de pagos, y hasta entonces el comprobante no debe
  aparecer como conciliable.

**Sheet "detalle"** — una fila por línea de ítem del comprobante, sin
columnas históricas que replicar (es un sheet nuevo del skill).

> Nota: esta sección se corrigió porque listaba columnas inventadas
> (`FECHA_PROCESO`, `PROVEEDOR_RAZON_SOCIAL`, `CLAVE_COMPROBANTE`, entre
> otras) que no existen en el código.

## Conciliación bancaria

Cruza los estados de cuenta del banco, las constancias de transferencia y
los comprobantes ya registrados en el Sheet contable, y genera un `.xlsx`
de conciliación por empresa y por mes. El motor que hace ese cruce vive en
`conciliacion/` (`build_conciliacion.py`, `parsers_eecc.py`,
`heredar_categorias.py`, `notificar_pendientes.py`): es código
**vendorizado**, copiado tal cual desde
`C:\Users\luisa\OneDrive\SCONCHA\AUTO\CONCILIACION\skill\scripts\`, sin
modificar una línea. Si hay que arreglar algo se arregla en el original y
se vuelve a copiar — no se edita dentro de `conciliacion/`. Ver
`conciliacion/README.md` para el detalle de cada archivo y por qué no se
refactoriza (no se repite aquí).

`conciliar.py`, en la raíz del repo, es el envoltorio: arma los argumentos
del motor desde `config.yaml`, junta EECC/constancias/comprobantes desde
Drive y el Sheet contable, invoca el motor **como subproceso** (nunca por
import: `build_conciliacion.py` hace `argparse.parse_args()` a nivel de
módulo) y sube el `.xlsx` resultante de vuelta a Drive.

### Cómo se corre

```powershell
cd C:\Users\luisa\sconcha-sistema
C:\Python312\python.exe conciliar.py --empresa "EL TEMPLO" --mes 2026-06
C:\Python312\python.exe conciliar.py --empresa "EL TEMPLO" --mes 2026-06 --dry-run --verbose
C:\Python312\python.exe conciliar.py --empresa "EL TEMPLO" --mes 2026-06 --sin-heredar
C:\Python312\python.exe conciliar.py --empresa "EL TEMPLO" --mes 2026-06 --comprobantes facturas.csv
```

- `--empresa`: el `nombre_corto` tal como aparece en `conciliacion.empresas`
  de `config.yaml` (no el `nombre_motor` — ver la trampa más abajo).
- `--mes`: formato `AAAA-MM`.
- `--dry-run`: genera el `.xlsx` localmente para revisarlo, pero no sube
  nada a Drive **ni consulta el correo**, aunque `correo.habilitado` esté
  en `true`.
- `--sin-heredar`: no intenta heredar del `.xlsx` del mes anterior de la
  misma empresa la depuración manual de Proveedor/Categoría (por defecto
  sí lo intenta; si no encuentra nada, sigue sin heredar y lo anota en el
  log — no es error).
- `--comprobantes <ruta.csv>`: usa ese CSV en vez de derivarlo del Sheet
  contable.

El log de cada corrida queda en `salida/conciliar.log`. Los archivos
intermedios (EECC descargados, constancias fusionadas, el CSV de
comprobantes, el `.xlsx` antes de subirlo) quedan en
`salida/conciliacion/<nombre_corto>/<mes>/`.

### Carpetas de Drive

```
<RAIZ>/CONCILIACION/AAAA-MM/EECC/         estados de cuenta del banco (uno por cuenta configurada)
<RAIZ>/CONCILIACION/AAAA-MM/CONSTANCIAS/  cons_<numero_cuenta>.json con las constancias de transferencia
<RAIZ>/CONCILIACION/AAAA-MM/              el .xlsx de salida: "CONCILIACION <nombre_corto> - <MES> <AÑO>.xlsx"
```

`init_negocio.py` crea la carpeta `CONCILIACION` (y guarda su id en
`conciliacion.carpeta`) solo si `config.yaml` trae la sección
`conciliacion` — es opcional a propósito (ver ONBOARDING.md, paso 3). Las
subcarpetas `AAAA-MM`, `EECC` y `CONSTANCIAS` las crea `conciliar.py`
mismo, por API, la primera vez que corre para ese mes.

`conciliar.py` decide qué EECC (o constancia) pertenece a qué cuenta por
el campo `numero` de `config.yaml` — los últimos dígitos con los que el
banco nombra el archivo del estado de cuenta (ej. `EC_4134_062026.pdf` →
`"4134"`), no el número completo de la cuenta. Un archivo en `EECC` que no
contiene el `numero` de ninguna cuenta configurada de esa empresa se
ignora, siempre con una advertencia en el log (nunca en silencio). La
cuenta marcada `principal: true` va como argumento posicional del motor;
las demás entran con `--eecc` (repetible) — solo una cuenta por empresa
puede ser `principal`.

### Correo (Gmail, solo lectura)

`correo_gmail.py` lee Gmail de la cuenta del negocio (ámbito
`gmail.readonly`, agregado en `auth_google.py`) para bajar constancias de
transferencia a `CONSTANCIAS`. Nunca escribe, responde ni borra correo, y
nunca guarda el cuerpo de un mensaje (ver el docstring de
`correo_gmail.py` para el detalle de esa garantía). `conciliar.py` lo
invoca antes de juntar los archivos, solo si `correo.habilitado` es
`true` y no se pasó `--dry-run`; si falla, no aborta la conciliación —
sigue con lo que ya haya en Drive.

Estado actual, sin ambigüedad:

- El ámbito `gmail.readonly` ya está en el código, pero **nadie ha dado el
  consentimiento todavía**: no existe `token.json`.
- `correo.habilitado` está en `false` en `config.yaml`; mientras esté así,
  `conciliar.py` nunca llama a Gmail.
- De las reglas declaradas en `config.yaml -> correo.reglas`, **solo el
  tipo `constancia_interbank` está implementado**. Las reglas de tipo
  `adjunto` (EECC de Interbank y BBVA, comprobantes de proveedores por
  correo) están declaradas pero sin implementar, a propósito: sus
  consultas de remitente y asunto son una suposición mientras no llegue
  correo real a la cuenta del negocio (creada el 2026-08-03, todavía
  vacía) con el que confirmarlas. Una regla de un tipo no implementado se
  salta con una advertencia que la nombra, nunca en silencio.
- El permiso de Gmail abarca **todo el buzón** — no existe permiso por
  etiqueta ni por remitente —, así que la cuenta debe ser exclusiva del
  negocio y el acotamiento (qué se lee, cuánto) se hace en el código y en
  las consultas de `config.yaml`.

### Dato verificado

El motor vendorizado se corrió contra los datos reales de junio 2026 de EL
TEMPLO (3 estados de cuenta) y reprodujo la corrida original: cuadre
exacto de saldos en las dos cuentas con movimiento — Interbank 4134
(`15,614.23 + 135,990.30 − 149,337.14 = 2,267.39`) y BBVA 8579
(`6,171.77 + 10,154.94 − 12,032.07 = 4,294.64`), ambas iguales al saldo
final del propio estado de cuenta — y el mismo conteo de conciliación:
122/220 (CARGOS 101/186 + CARGOS BBVA 21/34).

## Trampas conocidas

- **La app de Google Cloud debe quedar "Published", no "Testing".** En
  modo Testing el permiso caduca a los 7 días y el sistema deja de poder
  escribir en Drive y Sheets **en silencio** (el error solo aparece la
  próxima vez que se intenta refrescar el token). Es el fallo más caro de
  diagnosticar; ver ONBOARDING.md paso 1.
- **Una empresa con varios locales necesita `local_por_defecto`.** Un
  comprobante no dice nunca a qué local corresponde: se emite a nombre de
  la razón social, no del establecimiento — el local es conocimiento de
  quien compra, no un dato del documento. Sin `local_por_defecto` en
  `config.yaml`, TODOS los comprobantes de esa empresa se van a
  `02_REVISAR` pidiendo asignación manual. En SCONCHA eso afectaría a EL
  TEMPLO (locales LINCE y CP), que concentra alrededor del 60% del
  volumen, así que el sistema quedaría inservible. Los comprobantes que en
  realidad correspondan al otro local se corrigen a mano en el Sheet.
  Mejora pendiente que eliminaría la corrección manual: **subcarpetas por
  local dentro del buzón** (`00_BUZON/LINCE/`, `00_BUZON/MIRAFLORES/`),
  para capturar el local en el momento de dejar el archivo.
- **`clave()` calculada sobre `proveedor_ruc`** (el RUC del
  emisor), no sobre `cliente_ruc`: `"RUC_EMISOR|SERIE-NUMERO|TOTAL"`.
  Verificado en corrida real: el archivo destino sale como
  `20100066603_F001-00012345_1180.00.xml`, con el RUC del emisor. Es lo
  que identifica de forma única a un comprobante independientemente de a
  qué empresa propia se le asignó. Si `esquema.py` calcula `clave()` con
  otro criterio, la deduplicación y el nombre de archivo de destino
  (`RUC_SERIE-NUMERO_TOTAL.ext`) van a usar RUCs distintos entre sí —
  avisar si el criterio real difiere de este supuesto.
- **Atributos dinámicos de emparejado de catálogo.** Como
  `Registro.escribir(comp, empresa, local, link_drive, archivo)` no recibe
  el resultado del emparejado por separado, `procesar.py` le añade a cada
  `item` de `comp.items`, en tiempo de ejecución, los atributos
  `insumo_catalogo`, `categoria_catalogo` y `confianza_match` (no forman
  parte de los campos declarados en `esquema.ItemExtraido`).
  `registro_sheets.py` debe leerlos con
  `getattr(item, "insumo_catalogo", None)` y no asumir que siempre están
  presentes (si `insumos.csv` no existe todavía, `procesar.py` sigue sin
  emparejar y esos atributos quedan sin asignar).
- **El agrupado XML+PDF es por nombre de archivo, no por `clave()`
  extraída.** Confirmar la "misma clave" de verdad requeriría extraer
  también el PDF (con costo de API) antes de saber si hace falta. En la
  práctica, SUNAT y los facturadores nombran el XML y el PDF de un mismo
  comprobante con el mismo nombre base, así que `procesar.py` agrupa por
  ese nombre. Si algún proveedor nombra distinto el XML y su PDF, el PDF
  se procesará como comprobante aparte; si extrae la misma `clave()` que
  el XML, la deduplicación lo mandará a revisar en vez de duplicarlo (no
  se pierde el dato, pero tampoco queda enlazado como respaldo).
- **`--dry-run` de `procesar.py` sigue necesitando credenciales de
  Google.** Antes de este cambio, `--dry-run` leía el buzón directo del
  disco (Drive para escritorio ya lo tenía sincronizado ahí) y no
  necesitaba autenticarse. Ahora listar el buzón y descargar cada archivo
  a un temporal para poder extraerlo y previsualizar el resultado son
  llamadas de LECTURA contra la API de Drive, así que `auth_google.
  servicio_drive()` se llama siempre, dry-run incluido. Lo único que
  `--dry-run` sigue evitando son las ESCRITURAS: mover archivos, crear
  subcarpetas (`AlmacenDrive.asegurar_carpeta`) y crear los
  `.motivo.txt`. `init_negocio.py --dry-run` es la excepción: no llama a
  ninguna API de Google (ni Sheets ni Drive), porque solo anuncia la
  intención sin verificar nada contra la cuenta real.
- **`fecha_emision` sin formato reconocido** → la carpeta de destino usa
  `SIN_FECHA` en vez de `AAAA-MM`. Debería ser raro si `comp.validar()`
  ya exige la fecha, pero es un resguardo adicional.
- **El costo estimado del resumen final es una aproximación**, no el
  precio real de la API de Anthropic: cuenta llamadas a
  `extractores.modelo.extraer` (una por PDF o imagen; los XML son
  gratis porque no llaman al modelo) y las multiplica por la constante
  `COSTO_ESTIMADO_USD_POR_LLAMADA_MODELO` en `procesar.py`. Ajustar esa
  constante si cambia el precio o el modelo/esfuerzo configurado.
- **Archivos que se ignoran al listar el buzón** (no se procesan ni van a
  revisar, porque no son comprobantes): los que empiezan con `~$` o `.`, y
  `desktop.ini` / `Thumbs.db` — artefactos de Office o de Windows/Drive.
- **Si el movimiento final falla** (por ejemplo, un error transitorio de
  la API de Drive) después de una escritura exitosa, el archivo se queda
  en el buzón tal cual y se anota el error en el log; la deduplicación por
  `clave()` evita que la próxima corrida lo registre dos veces, así que es
  seguro volver a correr `procesar.py`.
- **La conciliación bancaria ya integra con Drive**: ya no es la "fase
  posterior, fuera de este skill" que leía archivos locales. `conciliar.py`
  baja los EECC y las constancias directo de
  `CONCILIACION/AAAA-MM/{EECC,CONSTANCIAS}` por API, arma el CSV de
  comprobantes desde el Sheet contable, y sube el `.xlsx` resultante de
  vuelta a Drive. Lo único que sigue siendo manual es descargar el estado
  de cuenta desde el portal del banco (ningún banco lo entrega por API) y
  subirlo a `EECC`; desde ahí `conciliar.py` lo toma solo. Ver
  "Conciliación bancaria" más arriba.
- **La columna EMPRESA del CSV de comprobantes que arma `conciliar.py`
  lleva `nombre_motor`, no `nombre_corto`.** El motor filtra las filas con
  `EMP_KEY not in norm(row['EMPRESA'])`, donde `EMP_KEY` es `'TEMPLO'` o
  `'CEVICHERA'` según si esa palabra está contenida en el argumento
  posicional `empresa` (`conciliacion/build_conciliacion.py:90` y `:454`).
  Si el CSV trajera `nombre_corto` en vez de `nombre_motor` — por ejemplo
  `"INSTITUCION"` en vez de `"INSTITUCION CEVICHERA"` — `EMP_KEY`
  (`CEVICHERA`) no estaría contenido en `"INSTITUCION"`, y el filtro
  descartaría TODAS las filas de esa empresa **en silencio**: la
  conciliación saldría vacía sin ningún error visible. Por eso
  `filtrar_y_escribir_csv()` en `conciliar.py` reescribe la columna
  `EMPRESA` con `nombre_motor` antes de escribir el CSV.
