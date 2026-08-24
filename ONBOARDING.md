# Guía de instalación en otro negocio

Esta guía es para instalar el sistema de conciliación de comprobantes en
**otro negocio** (otra cadena, otra empresa), reutilizando el mismo código.
No hace falta saber programar: es copiar archivos, pegar valores y correr
un par de comandos. No hace falta instalar nada de Google en la máquina
que corre el sistema: el motor habla directo con la API de Google Drive
(ver `almacen_drive.py`), así que no depende de tener Google Drive para
escritorio instalado ni de una letra de unidad de Windows. Los archivos
siguen entrando al buzón como siempre: arrastrándolos desde
drive.google.com, subiéndolos desde el celular, o mandándolos al bot.
Sigue los pasos en orden.

---

## Paso 0 — Cuenta de Google dedicada y exclusiva del negocio

Antes que nada, crea (o pide que te den acceso a) una cuenta de Gmail que
sea **exclusiva de este negocio**: no la cuenta personal de nadie, no una
cuenta compartida con otro negocio. Por ejemplo:
`administracion.<negocio>@gmail.com`.

Esto es lo que hace que el sistema sea replicable: todos los archivos,
carpetas y Google Sheets del negocio quedan asociados a esta cuenta, y
cualquier persona con las credenciales correctas puede administrarlos sin
depender de la cuenta personal de quien lo instaló.

---

## Paso 1 — Proyecto de Google Cloud y credenciales OAuth

1. Con la cuenta del Paso 0, entra a [Google Cloud Console](https://console.cloud.google.com/)
   y crea un proyecto nuevo (o reutiliza uno si ya existe para este
   negocio).
2. En "APIs y servicios" → "Biblioteca", habilita:
   - **Google Drive API**
   - **Google Sheets API**
   - **Gmail API**
3. En "APIs y servicios" → "Pantalla de consentimiento OAuth":
   - Tipo de usuario: Externo (o Interno si la cuenta es de Google
     Workspace).
   - Completa los datos mínimos (nombre de la app, correo de soporte).
   - Agrega los ámbitos de Drive, Sheets y Gmail si te los pide.

   > ⚠️ Al dar el consentimiento en el navegador (Paso 5), Google va a
   > pedir permiso de **lectura** del correo de la cuenta del Paso 0. Es
   > para la fase de conciliación bancaria: bajar del correo del negocio
   > los estados de cuenta y las constancias de transferencia del banco.
   > El sistema nunca escribe, responde ni borra correo.

   > ⚠️ **Muy importante: al terminar, publica la aplicación ("Publish
   > App").** Si la dejas en modo **Testing**, el permiso de acceso
   > caduca a los **7 días** y el sistema deja de poder escribir en Drive
   > y en los Sheets **sin avisar de forma visible** — simplemente deja de
   > actualizarse, y el primer síntoma suele ser "los números no cuadran"
   > varios días después. Es el fallo más caro de diagnosticar de todo el
   > sistema. Verifica que el estado diga "In production" / "Publicada",
   > no "Testing".

4. En "APIs y servicios" → "Credenciales" → "Crear credenciales" →
   "ID de cliente de OAuth":
   - Tipo de aplicación: **Aplicación de escritorio**.
   - Dale un nombre (por ejemplo, el nombre del negocio).
5. Descarga el archivo JSON resultante. Renómbralo exactamente a
   `credenciales.json` y colócalo en la raíz del proyecto, junto a
   `procesar.py`.

   Este archivo es un secreto: no lo compartas por correo ni lo subas a
   ningún repositorio. Ya está incluido en `.gitignore`.

---

## Paso 2 — Instalar las dependencias de Python

Con Python 3.12 instalado, desde la raíz del proyecto:

```powershell
cd C:\Users\luisa\sconcha-sistema
C:\Python312\python.exe -m pip install -r requirements.txt
```

---

## Paso 3 — Rellenar la configuración del negocio

1. Copia `config.ejemplo.yaml` como `config.yaml` (en la misma carpeta).
2. Ábrelo y reemplaza cada valor de ejemplo por el dato real:
   - `negocio`: nombre corto del negocio.
   - `cuenta_google`: la cuenta del Paso 0.
   - `drive.raiz_nombre`: el nombre que va a tener la carpeta raíz del
     negocio dentro de "Mi unidad" de la cuenta del Paso 0 (por ejemplo,
     el mismo nombre corto del negocio). Es el único campo de texto de la
     sección `drive`: no hace falta ninguna ruta local ni letra de unidad
     de Windows — `init_negocio.py` (Paso 5) crea esa carpeta por API si
     no existe, junto con `00_BUZON`, `01_PROCESADO` y `02_REVISAR` dentro
     de ella, y guarda sus 4 ids en `drive.carpetas`.
   - `empresas`: una entrada por cada razón social (RUC) que factura a
     nombre de este negocio, con su `nombre_corto`, `razon_social`, `ruc`
     (entre comillas) y la lista de `locales` que factura ese RUC (puede
     quedar vacía `[]`).
     - `local_por_defecto`: **obligatorio si esa empresa tiene más de un
       local** en su lista de `locales`. Un comprobante nunca dice a qué
       local corresponde: se emite a nombre de la razón social, no del
       establecimiento — el local es conocimiento de quien compra, no un
       dato que esté en el documento. Sin `local_por_defecto`, TODOS los
       comprobantes de esa empresa caen en `02_REVISAR` pidiendo asignación
       manual y el sistema queda inservible para ella. Pon aquí el local
       que compra la mayor parte del tiempo; los pocos comprobantes que en
       realidad sean del otro local se corrigen a mano en el Sheet. Ver
       `config.ejemplo.yaml` para el formato exacto y la sección "Trampas
       conocidas" de `SKILL.md` para el caso real que motivó este campo.
   - `conciliacion` (opcional): solo si este negocio va a conciliar sus
     cuentas bancarias con el sistema. **Si no aplica, borra toda la
     sección** — `init_negocio.py` (Paso 5) no crea la carpeta
     `CONCILIACION` en Drive si no la encuentra en `config.yaml`. Si sí
     aplica:
     - `carpeta`: déjalo vacío (`""`); lo llena el Paso 5.
     - `empresas`: una entrada por cada empresa (de las de arriba) que
       tenga cuentas bancarias propias — una razón social que solo
       factura, sin cuenta propia, se omite (no se concilia), salvo que
       otra empresa del negocio pague sus compras desde la suya (ver
       `paga_comprobantes_de` más abajo, y el caso real de ILLAWARA en
       `SKILL.md`, sección "Conciliación bancaria").
       - `nombre_corto`: debe coincidir exactamente con el `nombre_corto`
         de la lista de `empresas` de arriba.
       - `nombre_motor`: el string EXACTO que recibe el motor vendorizado
         (`conciliacion/build_conciliacion.py`) como argumento posicional
         `empresa`. No es cosmético: el motor lo usa para decidir el local
         y el encargado de caja chica, y `conciliar.py` lo usa también
         para filtrar el CSV de comprobantes de esa empresa. Si aquí
         pusieras el `nombre_corto` en vez del nombre completo (por
         ejemplo `"INSTITUCION"` en vez de `"INSTITUCION CEVICHERA"`), el
         motor descartaría en silencio TODAS las filas de esa empresa — la
         conciliación saldría vacía sin ningún error visible. Ver la
         sección "Conciliación bancaria" de `SKILL.md`.
       - `caja_chica` (opcional): solo si esta entrada maneja caja chica
         por reposición semanal desde el banco (regla vigente desde
         ago-2026 — ver "Reporte de egresos de caja y caja chica" en
         `SKILL.md`). **Si no aplica, borra la subsección entera.** Si
         aplica:
         - `reposicion_semanal`: el monto que el banco repone cada semana
           a la caja chica. Nunca se hardcodea en el motor: sale de acá.
           Se cruza contra el "Reporte de Egresos" que el sistema de
           ventas del negocio exporta por local — súbelo (el `.xls`/
           `.htm`, con su carpeta hermana `_archivos` si aplica) a
           `CONCILIACION/AAAA-MM/EGRESOS/<nombre_corto>/` en Drive (la
           subcarpeta de ESTA empresa, con el `nombre_corto` tal cual; ver
           "Reporte de egresos de caja y caja chica" en SKILL.md sobre por
           qué es por empresa y no una sola carpeta compartida), o pásalo a
           mano con `conciliar.py --egresos <ruta>`. Sin este reporte,
           `conciliar.py` sigue con la regla vieja (fondo fijo de S/500)
           sin que haga falta tocar nada más.
       - `cuentas`: una entrada por cada cuenta bancaria de esta empresa,
         con `banco` y `moneda` (informativos) y:
         - `numero`: los ÚLTIMOS DÍGITOS con los que el banco nombra el
           archivo del estado de cuenta (ej. `EC_4134_062026.pdf` →
           `"4134"`), no el número completo de la cuenta. Es el criterio
           con el que `conciliar.py` sabe a qué cuenta pertenece cada EECC
           que encuentra en Drive.
         - `principal: true`: marca la cuenta que va como argumento
           posicional del motor; las demás entran con `--eecc`
           (repetible). Solo una cuenta por empresa puede ser `principal`.
       - `paga_comprobantes_de` (opcional): lista de `nombre_corto` de
         OTRAS empresas de este negocio que **no tienen cuentas bancarias
         propias** (razón social que solo factura, cuyas compras las paga
         esta empresa desde su propia cuenta). Caso real: ILLAWARA E.I.R.L.
         no tiene cuenta bancaria — EL TEMPLO paga sus compras desde la
         suya — así que la entrada de EL TEMPLO trae
         `paga_comprobantes_de: [ILLAWARA]`, e ILLAWARA aparece en
         `conciliacion.empresas` SIN `cuentas` (así se documenta que no se
         concilia por sí sola). Cada nombre listado debe existir en
         `conciliacion.empresas` y no tener `cuentas` propias — si no,
         `conciliar.py` no arranca (evita el doble conteo: una vez en la
         conciliación de la empresa referida, otra vez en la de quien
         declara `paga_comprobantes_de`). Ver "Conciliación bancaria" en
         `SKILL.md` para el detalle completo del caso ILLAWARA.
   - `correo` (opcional, solo si `conciliacion` aplica): déjalo tal como
     viene en `config.ejemplo.yaml` (`habilitado: false`) hasta tener a la
     vista un correo real de cada tipo con el cual confirmar las consultas
     de cada regla. Ver "Correo (Gmail, solo lectura)" en `SKILL.md` para
     qué hace y qué no hace todavía.
   - Deja `drive.carpetas.*`, `conciliacion.carpeta` y
     `sheets.contable`/`sheets.detalle` vacíos (`""`): los llena
     automáticamente el Paso 5.
3. `config.yaml` nunca se sube a un repositorio (contiene los IDs de las
   carpetas de Drive y de los Sheets una vez creados); ya está en
   `.gitignore`.

---

## Paso 4 — Clave de la API de Anthropic

El sistema necesita una `ANTHROPIC_API_KEY` para leer PDF e imágenes con
Claude. Se configura como variable de entorno de usuario en Windows, nunca
dentro de un archivo del proyecto:

```powershell
[Environment]::SetEnvironmentVariable('ANTHROPIC_API_KEY','valor-real-aqui','User')
```

Para comprobar que quedó configurada sin imprimir el valor:

```powershell
if ($env:ANTHROPIC_API_KEY) { 'SET' } else { 'NO SET' }
```

Después de configurarla, abre una terminal nueva (las variables de entorno
de usuario solo se leen al iniciar la sesión de la terminal).

---

## Paso 5 — Preparar el negocio (carpetas de Drive + Sheets)

```powershell
C:\Python312\python.exe init_negocio.py --config config.yaml
```

La primera vez te va a pedir iniciar sesión en el navegador con la cuenta
del Paso 0 y dar el consentimiento OAuth (queda guardado en `token.json`
para las próximas corridas). Este comando:

- Crea por API de Drive la carpeta raíz del negocio (`drive.raiz_nombre`)
  dentro de "Mi unidad", y las subcarpetas `00_BUZON`, `01_PROCESADO`,
  `02_REVISAR` dentro de ella (si ya existen, las reutiliza).
- Si `config.yaml` trae la sección `conciliacion` (Paso 3), crea además la
  carpeta `CONCILIACION` dentro de la raíz. Si no la trae, no la crea —
  es opcional a propósito.
- Crea los dos Google Sheets del negocio con sus cabeceras.
- Escribe de vuelta en `config.yaml` los ids de esas carpetas y de los 2
  Sheets (incluido `conciliacion.carpeta`, si aplica).

Puedes correrlo con `--dry-run` primero para ver qué haría sin tocar nada
(no necesita credenciales para el `--dry-run`):

```powershell
C:\Python312\python.exe init_negocio.py --config config.yaml --dry-run
```

Es seguro volver a correrlo más adelante: no duplica carpetas ni Sheets ya
creados.

---

## Paso 6 — Corrida de calibración con documentos representativos del flujo nuevo

Este sistema existe precisamente para dejar de escanear papel. No calibres
con comprobantes escaneados históricos: eso afinaría el sistema para un
problema que se está eliminando, no para el flujo real con el que va a
operar. En el flujo nuevo, las facturas y boletas llegan como **PDF
digital** o **XML de factura electrónica** (por correo o por el bot), y la
**foto con el celular** queda reservada solo para comprobantes
genuinamente físicos y sin versión digital (compras en el mercado, en el
terminal pesquero).

Antes de confiar el negocio al sistema, sube a la carpeta `00_BUZON` (del
Drive de la cuenta del Paso 0 — desde drive.google.com, desde la app del
celular, o mandándolos al bot; no hace falta ninguna app de escritorio)
**tres documentos representativos de ese flujo**:

1. Un **XML de factura electrónica** real de un proveedor.
2. Un **PDF digital** recibido por correo (no un escaneo).
3. Una **foto tomada con el celular** de una boleta de compra en el
   mercado (o de otro comprobante genuinamente físico).

Luego:

```powershell
C:\Python312\python.exe procesar.py --config config.yaml --limite 3 --verbose
```

Revisa **campo por campo contra el documento original**: RUC, serie y
número, fecha, montos (subtotal, IGV, total), y cada línea de ítem con su
emparejado de catálogo. Cualquier comprobante que haya caído en
`02_REVISAR` tiene su motivo en el archivo `.motivo.txt` correspondiente —
revísalo también, para confirmar que la razón es correcta y no un error
del sistema.

Si los tres calzan bien, puedes sumar más documentos reales de otras
empresas/locales configurados para ampliar la muestra antes del Paso 7,
siempre dentro de este mismo flujo (XML, PDF digital o foto de comprobante
físico) — nunca con escaneos históricos.

No continúes al Paso 7 hasta estar conforme con la precisión de esta
corrida.

---

## Paso 7 — Una semana en paralelo antes de confiar en el sistema nuevo

Corre el sistema nuevo en paralelo con el proceso anterior (manual o el
sistema previo) durante **al menos una semana**, sin dar de baja todavía
el proceso anterior. Compara los resultados. Solo cuando ambos coincidan
de forma consistente, deja de usar el proceso anterior.

---

## Resumen de lo que hay que copiar y dónde

| Qué | De dónde | A dónde |
|---|---|---|
| Todo el código (`.py`, `conciliacion/`, `requirements.txt`, `.gitignore`, `config.ejemplo.yaml`) | Este proyecto | Carpeta del negocio nuevo en el equipo que va a correrlo |
| `credenciales.json` | Google Cloud Console del Paso 1 (cuenta del negocio nuevo) | Raíz del proyecto (nunca al repositorio) |
| `config.yaml` | Se crea copiando `config.ejemplo.yaml` y rellenándolo (Paso 3) | Raíz del proyecto (nunca al repositorio) |
| `ANTHROPIC_API_KEY` | La entrega quien administra la cuenta de Anthropic | Variable de entorno de usuario de Windows (Paso 4) |
| `token.json` | Se genera solo, la primera vez que corre `init_negocio.py`, `procesar.py` o `conciliar.py` | Raíz del proyecto (nunca al repositorio) |

Nota: ya no hace falta instalar Google Drive para escritorio en ningún
equipo para que el sistema funcione — tanto el procesamiento de
comprobantes como la conciliación bancaria (`conciliar.py`) hablan directo
con la API de Drive. Lo único que sigue siendo manual es descargar el
estado de cuenta desde el portal del banco (ningún banco lo entrega por
API) y subirlo a la carpeta `CONCILIACION/AAAA-MM/EECC` de Drive; desde
ahí `conciliar.py` lo toma solo. Ver "Conciliación bancaria" en
`SKILL.md` para el detalle.
