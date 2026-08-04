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
auth_google.py        OAuth de usuario (Drive + Sheets), token.json/credenciales.json
init_negocio.py        prepara un negocio nuevo: carpetas + Sheets con cabeceras
config.yaml             datos del negocio (empresas, RUCs, locales, carpetas, IDs de Sheets)
config.ejemplo.yaml      plantilla comentada para dar de alta OTRO negocio

esquema.py               (otro agente) dataclasses ComprobanteExtraido / ItemExtraido
extractores/xml_ubl.py   (otro agente) parseo determinístico de XML UBL / ZIP
extractores/modelo.py    (otro agente) lectura de PDF/imagen vía Claude
catalogo.py               (otro agente) emparejado de descripciones contra insumos.csv
registro_sheets.py        (otro agente) escritura en los dos Google Sheets
insumos.csv                (otro agente) catálogo de insumos (insumo, categoria, unidad_base, alias)
```

`procesar.py` importa `extractores.xml_ubl`, `extractores.modelo`,
`catalogo` y `registro_sheets` como módulos de nivel superior; nunca
importa `esquema` en tiempo de ejecución (usa *duck typing*: le basta con
que el objeto que devuelven los extractores tenga los atributos y métodos
del contrato — `clave()`, `validar()`, `.items`, etc. — sin necesidad de
que el tipo `ComprobanteExtraido` exista como import real). Esto permite
que `procesar.py` y sus tests corran aunque los otros módulos todavía
estén en construcción.

### Flujo por archivo

1. **Agrupar** los archivos del buzón por nombre (sin extensión). Si un
   grupo tiene exactamente un XML/ZIP, ese es el comprobante principal y el
   resto del grupo (por ejemplo, el PDF con el mismo nombre) se trata como
   respaldo enlazado: se mueve junto al XML pero no se procesa ni se
   registra como comprobante aparte ("XML gana siempre"). Ver la trampa
   sobre el criterio de agrupado más abajo.
2. **Clasificar por extensión**: `.xml`/`.zip` → `extractores.xml_ubl`;
   `.pdf` → `extractores.modelo(tipo='pdf')`; imágenes →
   `extractores.modelo(tipo='imagen')`; `.heic` → directo a revisar;
   cualquier otra extensión → directo a revisar.
3. **Extraer** los datos del comprobante. Si el extractor lanza una
   excepción, o si `comp.validar()` devuelve advertencias, el archivo va a
   revisar con el motivo correspondiente.
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
   archivo)`.
9. **Mover y renombrar** a
   `01_PROCESADO/AAAA-MM/EMPRESA_CON_GUION_BAJO/RUC_SERIE-NUMERO_TOTAL.ext`.
   Si el destino ya existe, se agrega un sufijo numérico (`_2`, `_3`, ...);
   nunca se sobrescribe nada.

## Cómo se corre

```powershell
cd C:\Users\luisa\sconcha-sistema
C:\Python312\python.exe procesar.py --config config.yaml
C:\Python312\python.exe procesar.py --config config.yaml --dry-run --verbose
C:\Python312\python.exe procesar.py --solo F001-00123.xml
C:\Python312\python.exe procesar.py --limite 15
```

- `--dry-run`: no escribe en Sheets ni mueve archivos; solo informa qué
  haría (sigue leyendo `Registro.claves_existentes()` de verdad, para que
  la vista previa de duplicados sea realista).
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

`init_negocio.py` crea ambos Sheets con estas cabeceras exactas (fila 1,
en negrita, congelada). Si algún día cambian, hay que actualizar
`init_negocio.py` y `registro_sheets.py` en conjunto.

**Sheet "contable"** — una fila por comprobante:

```
FECHA_PROCESO, EMPRESA, LOCAL, ORIGEN, CONFIANZA, PROVEEDOR_RUC,
PROVEEDOR_RAZON_SOCIAL, CLIENTE_RUC, CLIENTE_RAZON_SOCIAL, TIPO_DOCUMENTO,
SERIE_NUMERO, FECHA_EMISION, FECHA_VENCIMIENTO, CONDICION, MONEDA,
TIPO_CAMBIO, SUBTOTAL, IGV, ICBPER, DESCUENTO_GLOBAL, TOTAL,
DETRACCION_PCT, DETRACCION_MONTO, DETRACCION_CODIGO, RETENCION,
DOCUMENTO_REFERENCIA, CLAVE, ARCHIVO, LINK_DRIVE, ADVERTENCIAS
```

**Sheet "detalle"** — una fila por línea de ítem:

```
CLAVE_COMPROBANTE, PROVEEDOR_RUC, SERIE_NUMERO, FECHA_EMISION, EMPRESA,
LOCAL, ORDEN, DESCRIPCION, CANTIDAD, UNIDAD, PRECIO_UNITARIO, TOTAL_LINEA,
INSUMO_CATALOGO, CATEGORIA_CATALOGO, CONFIANZA_MATCH
```

## Trampas conocidas

- **La app de Google Cloud debe quedar "Published", no "Testing".** En
  modo Testing el permiso caduca a los 7 días y el sistema deja de poder
  escribir en Drive y Sheets **en silencio** (el error solo aparece la
  próxima vez que se intenta refrescar el token). Es el fallo más caro de
  diagnosticar; ver ONBOARDING.md paso 2.
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
- **`link_drive` es de mejor esfuerzo.** Se resuelve buscando el archivo
  por nombre en Drive ANTES de moverlo localmente (Drive para escritorio
  conserva el ID del archivo al detectar un movimiento local dentro del
  mismo Drive, así que el link sigue siendo válido después del
  movimiento). Puede volver vacío si: (a) Drive para escritorio todavía no
  sincronizó el archivo recién llegado al buzón, o (b) `drive.raiz` en
  config.yaml apunta a una Unidad compartida en vez de "Mi unidad" (el
  resolutor de carpetas solo sabe recorrer "Mi unidad"). En ambos casos no
  bloquea el registro del comprobante, solo queda sin link.
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
- **Si el movimiento final falla** (por ejemplo, un archivo bloqueado por
  otro proceso) después de una escritura exitosa, el archivo se queda en
  el buzón tal cual y se anota el error en el log; la deduplicación por
  `clave()` evita que la próxima corrida lo registre dos veces, así que es
  seguro volver a correr `procesar.py`.
