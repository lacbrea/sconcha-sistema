# Guía de instalación en otro negocio

Esta guía es para instalar el sistema de conciliación de comprobantes en
**otro negocio** (otra cadena, otra empresa), reutilizando el mismo código.
No hace falta saber programar: es copiar archivos, pegar valores y correr
un par de comandos. Sigue los pasos en orden.

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

## Paso 1 — Google Drive para escritorio con esa cuenta

1. Instala "Google Drive para escritorio" en la computadora donde va a
   correr el sistema (o donde alguien lo va a correr periódicamente).
2. Inicia sesión con la cuenta del Paso 0.
3. Espera a que sincronice. Windows le asigna una letra de unidad a "Mi
   unidad" (por ejemplo `G:`); anótala, la vas a necesitar en el Paso 4.
4. Dentro de "Mi unidad", crea la carpeta raíz del negocio (por ejemplo
   `G:\Mi unidad\<NEGOCIO>`). `init_negocio.py` (Paso 6) crea las
   subcarpetas `00_BUZON`, `01_PROCESADO` y `02_REVISAR` dentro de esta
   carpeta automáticamente si no existen.

---

## Paso 2 — Proyecto de Google Cloud y credenciales OAuth

1. Con la cuenta del Paso 0, entra a [Google Cloud Console](https://console.cloud.google.com/)
   y crea un proyecto nuevo (o reutiliza uno si ya existe para este
   negocio).
2. En "APIs y servicios" → "Biblioteca", habilita:
   - **Google Drive API**
   - **Google Sheets API**
3. En "APIs y servicios" → "Pantalla de consentimiento OAuth":
   - Tipo de usuario: Externo (o Interno si la cuenta es de Google
     Workspace).
   - Completa los datos mínimos (nombre de la app, correo de soporte).
   - Agrega los ámbitos de Drive y Sheets si te los pide.

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

## Paso 3 — Instalar las dependencias de Python

Con Python 3.12 instalado, desde la raíz del proyecto:

```powershell
cd C:\Users\luisa\sconcha-sistema
C:\Python312\python.exe -m pip install -r requirements.txt
```

---

## Paso 4 — Rellenar la configuración del negocio

1. Copia `config.ejemplo.yaml` como `config.yaml` (en la misma carpeta).
2. Ábrelo y reemplaza cada valor de ejemplo por el dato real:
   - `negocio`: nombre corto del negocio.
   - `cuenta_google`: la cuenta del Paso 0.
   - `drive.raiz`: la ruta local a la carpeta raíz del Paso 1 (usa la letra
     de unidad real, por ejemplo `"G:/Mi unidad/<NEGOCIO>"`).
   - `empresas`: una entrada por cada razón social (RUC) que factura a
     nombre de este negocio, con su `nombre_corto`, `razon_social`, `ruc`
     (entre comillas) y la lista de `locales` que factura ese RUC (puede
     quedar vacía `[]`).
   - Deja `sheets.contable` y `sheets.detalle` vacíos (`""`): los llena
     automáticamente el Paso 6.
3. `config.yaml` nunca se sube a un repositorio (contiene los IDs de los
   Sheets una vez creados); ya está en `.gitignore`.

---

## Paso 5 — Clave de la API de Anthropic

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

## Paso 6 — Preparar el negocio (carpetas + Sheets)

```powershell
C:\Python312\python.exe init_negocio.py --config config.yaml
```

La primera vez te va a pedir iniciar sesión en el navegador con la cuenta
del Paso 0 y dar el consentimiento OAuth (queda guardado en `token.json`
para las próximas corridas). Este comando:

- Crea las subcarpetas `00_BUZON`, `01_PROCESADO`, `02_REVISAR` dentro de
  `drive.raiz` (si ya existen, las reutiliza).
- Crea los dos Google Sheets del negocio con sus cabeceras.
- Escribe los IDs de esos Sheets de vuelta en `config.yaml`.

Puedes correrlo con `--dry-run` primero para ver qué haría sin tocar nada:

```powershell
C:\Python312\python.exe init_negocio.py --config config.yaml --dry-run
```

Es seguro volver a correrlo más adelante: no duplica carpetas ni Sheets ya
creados.

---

## Paso 7 — Corrida de calibración con 15 documentos reales

Antes de confiar el negocio al sistema, coloca en `00_BUZON` unos 15
comprobantes reales que cubran los tres tipos de origen (XML, PDF y foto),
y de varias empresas/locales configurados. Luego:

```powershell
C:\Python312\python.exe procesar.py --config config.yaml --limite 15 --verbose
```

Revisa **campo por campo contra el documento original**: RUC, serie y
número, fecha, montos (subtotal, IGV, total), y cada línea de ítem con su
emparejado de catálogo. Cualquier comprobante que haya caído en
`02_REVISAR` tiene su motivo en el archivo `.motivo.txt` correspondiente —
revísalo también, para confirmar que la razón es correcta y no un error
del sistema.

No continúes al Paso 8 hasta estar conforme con la precisión de esta
corrida.

---

## Paso 8 — Una semana en paralelo antes de confiar en el sistema nuevo

Corre el sistema nuevo en paralelo con el proceso anterior (manual o el
sistema previo) durante **al menos una semana**, sin dar de baja todavía
el proceso anterior. Compara los resultados. Solo cuando ambos coincidan
de forma consistente, deja de usar el proceso anterior.

---

## Resumen de lo que hay que copiar y dónde

| Qué | De dónde | A dónde |
|---|---|---|
| Todo el código (`.py`, `requirements.txt`, `.gitignore`, `config.ejemplo.yaml`) | Este proyecto | Carpeta del negocio nuevo en el equipo que va a correrlo |
| `credenciales.json` | Google Cloud Console del Paso 2 (cuenta del negocio nuevo) | Raíz del proyecto (nunca al repositorio) |
| `config.yaml` | Se crea copiando `config.ejemplo.yaml` y rellenándolo (Paso 4) | Raíz del proyecto (nunca al repositorio) |
| `ANTHROPIC_API_KEY` | La entrega quien administra la cuenta de Anthropic | Variable de entorno de usuario de Windows (Paso 5) |
| `token.json` | Se genera solo, la primera vez que corre `init_negocio.py` o `procesar.py` | Raíz del proyecto (nunca al repositorio) |
