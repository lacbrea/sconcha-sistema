"""Migración de una sola vez: copia los 1.800 comprobantes históricos de EL
TEMPLO desde la carpeta local de OneDrive a Google Drive (01_PROCESADO),
SIN pasar por el pipeline de procesamiento por modelo (costo $0).

Por qué existe separado de procesar.py: procesar.py extrae datos con el
modelo de Claude y escribe filas en los Sheets del negocio — correrlo sobre
1.800 comprobantes históricos que ya están cuadrados en el sistema contable
viejo costaría dinero y produciría filas duplicadas/falsas en los Sheets.
Este script hace solo la mitad "archivo" del trabajo: deja el PDF en el
mismo árbol de carpetas (AAAA-MM/EMPRESA/BOLETAS|FACTURAS) donde procesar.py
los habría dejado, para que quede un respaldo navegable en Drive. Nunca
llama a extractores.modelo ni a registro_sheets.

Origen verificado a mano (ver el encargo de este script para la tabla
completa de 17 carpetas de mes y sus conteos):
    C:\\Users\\luisa\\OneDrive\\SCONCHA\\Sconcha 2\\FACTURAS\\EL TEMPLO
    \\<AÑO>\\<CARPETA_MES>\\<SUBCARPETA>\\*.pdf

Destino en Drive:
    01_PROCESADO/<AAAA-MM>/EL_TEMPLO/<BOLETAS|FACTURAS>/<archivo original>

(La empresa se escribe con GUION BAJO en Drive, "EL_TEMPLO", igual que la
crea procesar.py vía nombre_empresa_carpeta() — en OneDrive es "EL TEMPLO"
con espacio.)

Uso:
    C:\\Python312\\python.exe migrar_comprobantes.py
        (dry-run: solo imprime el plan y los totales, no sube nada)
    C:\\Python312\\python.exe migrar_comprobantes.py --ejecutar
        (sube de verdad; requiere credenciales de Google ya autorizadas)

--dry-run es el comportamiento POR DEFECTO a propósito: correr esto dos
veces sin querer no debe poder duplicar 1.800 archivos. Ver
ejecutar_migracion() para cómo la idempotencia hace además que una corrida
real cortada a la mitad se pueda repetir sin duplicar nada.

Dos modos adicionales, agregados para el caso de julio 2026 (mes en
conciliación: sus facturas no van al archivo, van al buzón para que
procesar.py las lea con el modelo):

    --a-buzon <tipo>
        Modo alterno, NO el modo archivo de arriba: sube PLANO (sin árbol
        AAAA-MM/EMPRESA/BOLETAS-FACTURAS) el contenido de --origen a
        00_BUZON/<tipo> (id leído de config.yaml -> drive.carpetas.
        buzon_tipos.<tipo>). Pensado para un --origen que ya es una carpeta
        hoja (ej. "...\\2026\\JULIO\\FACTURAS"), no la raíz de tres niveles.
        Ver recorrer_arbol_plano() / ejecutar_migracion_a_buzon().

    --excluir <subruta> (repetible)
        Modo archivo: salta un subárbol completo (comparado contra la ruta
        relativa a --origen, tolerante a "/"/"\\" y a mayúsculas/minúsculas)
        y lo cuenta aparte como "excluido". Para cuando --a-buzon ya subió
        esa misma carpeta y no debe duplicarse en el archivo (ej.
        --excluir "2026/JULIO/FACTURAS"). Ver archivo_esta_excluido().
"""
from __future__ import annotations

import argparse
import dataclasses
import logging
import mimetypes
import pathlib
import sys

import yaml

from almacen_drive import AlmacenDrive

logger = logging.getLogger("migrar_comprobantes")

# -----------------------------------------------------------------------------
# Configuración fija de esta migración (script de un solo uso, para UNA sola
# empresa). No se lee de config.yaml a propósito: el alcance está cerrado
# (solo EL TEMPLO tiene archivos; ver el encargo de este script) y así el
# script no depende de que config.yaml no cambie mientras tanto.
# -----------------------------------------------------------------------------
RAIZ_ORIGEN_POR_DEFECTO = pathlib.Path(
    r"C:\Users\luisa\OneDrive\SCONCHA\Sconcha 2\FACTURAS\EL TEMPLO"
)

# Id de Drive de 01_PROCESADO (= config.yaml -> drive.carpetas.procesado).
CARPETA_PROCESADO_ID_POR_DEFECTO = "1lHxhikCbFFTl029vOIyk8r7W95yEbxZi"

EMPRESA_DESTINO = "EL_TEMPLO"

# Nombres de archivo que nunca son comprobantes (artefactos de OneDrive/
# Office), igual criterio que procesar.py: se ignoran en silencio, no
# abortan la migración (a diferencia de un nombre de mes o de subcarpeta
# desconocido, que sí aborta — ver clasificar_subcarpeta/resolver_anio_mes).
PREFIJOS_IGNORADOS = ("~$", ".")
NOMBRES_IGNORADOS = {"desktop.ini", "thumbs.db"}

# Extensiones que procesar.py sabe leer. Solo aplica al modo --a-buzon: un
# archivo que el pipeline no puede procesar termina en 02_REVISAR con un
# motivo, asi que se filtra ANTES de subirlo en vez de ensuciar la carpeta de
# revision. El modo archivo NO filtra por extension: ahi el objetivo es
# preservar el archivo historico completo, sea lo que sea.
EXTENSIONES_BUZON = {".pdf", ".xml", ".jpg", ".jpeg", ".png", ".heic"}

# -----------------------------------------------------------------------------
# Mapa de meses: EXPLÍCITO, no por parseo. El origen mezcla nombre completo,
# abreviatura ("OCT", "NOV", "DIC") y grafía peruana ("SETIEMBRE", no
# SEPTIEMBRE). Un nombre que no esté acá debe abortar la migración completa,
# nunca ignorarse: perder en silencio un mes entero de comprobantes es peor
# que frenar y corregir el nombre de la carpeta.
# -----------------------------------------------------------------------------
MAPA_MESES = {
    "ENERO": "01",
    "FEBRERO": "02",
    "MARZO": "03",
    "ABRIL": "04",
    "MAYO": "05",
    "JUNIO": "06",
    "JULIO": "07",
    "AGOSTO": "08",
    "SETIEMBRE": "09",
    "SEPTIEMBRE": "09",
    "OCTUBRE": "10",
    "OCT": "10",
    "NOVIEMBRE": "11",
    "NOV": "11",
    "DICIEMBRE": "12",
    "DIC": "12",
}


@dataclasses.dataclass(frozen=True)
class ArchivoOrigen:
    """Un comprobante local ya ubicado en el árbol de destino: sabe a qué
    AAAA-MM y a qué categoría (BOLETAS/FACTURAS) le corresponde, calculado
    una sola vez al recorrer el árbol, para que el resto del script no tenga
    que volver a mirar el nombre de las carpetas de origen."""

    ruta: pathlib.Path
    carpeta_anio: str
    carpeta_mes: str
    subcarpeta: str
    anio_mes: str  # "AAAA-MM"
    categoria: str  # "BOLETAS" | "FACTURAS"


@dataclasses.dataclass
class ResultadoMigracion:
    """Contadores de una corrida (real o dry-run), totales y por mes.

    'conteos' y 'por_mes' usan como clave el estado registrado por
    ejecutar_migracion(): "subido" | "ya_existia" | "error" en una corrida
    real, "se_subiria" en dry-run.
    """

    total_encontrados: int = 0
    conteos: dict[str, int] = dataclasses.field(default_factory=dict)
    por_mes: dict[str, dict[str, int]] = dataclasses.field(default_factory=dict)

    def registrar(self, anio_mes: str, estado: str) -> None:
        self.conteos[estado] = self.conteos.get(estado, 0) + 1
        contador_mes = self.por_mes.setdefault(anio_mes, {})
        contador_mes[estado] = contador_mes.get(estado, 0) + 1


# -----------------------------------------------------------------------------
# Lógica pura (sin Drive, sin red): clasificación de nombres y recorrido del
# árbol de origen. Separada de las llamadas a Drive para que los tests la
# ejerciten sin credenciales ni red (ver tests/test_migrar_comprobantes.py).
# -----------------------------------------------------------------------------
def normalizar_nombre_mes(nombre_carpeta_mes: str) -> str:
    """Quita el sufijo de año (si lo trae, ej. "ABRIL 2025" -> "ABRIL") y
    deja el nombre en mayúsculas, listo para buscar en MAPA_MESES.

    El año NUNCA sale de este sufijo (algunos meses no lo traen, ej. las
    carpetas de 2026: "ENERO", "FEBRERO", ...): sale de la carpeta padre.
    Este sufijo solo estorba para reconocer el nombre del mes.
    """
    texto = nombre_carpeta_mes.strip().upper()
    partes = texto.split()
    if partes and partes[-1].isdigit() and len(partes[-1]) == 4:
        partes = partes[:-1]
    return " ".join(partes)


def resolver_anio_mes(carpeta_anio: str, carpeta_mes: str) -> str:
    """Devuelve "AAAA-MM" a partir de la carpeta de año (usada tal cual, sin
    validar formato: el árbol de origen ya está verificado a mano) y de
    carpeta_mes (buscada en MAPA_MESES tras normalizar_nombre_mes()).

    Aborta (ValueError) si el nombre de mes no está en la tabla: mejor
    frenar la corrida completa que adivinar o saltarse un mes en silencio.
    """
    nombre_normalizado = normalizar_nombre_mes(carpeta_mes)
    mes_numero = MAPA_MESES.get(nombre_normalizado)
    if mes_numero is None:
        raise ValueError(
            f"Nombre de mes no reconocido: carpeta '{carpeta_mes}' (normalizado "
            f"'{nombre_normalizado}') no está en MAPA_MESES. Corrige el nombre de "
            f"la carpeta en OneDrive o agrega la variante al mapa."
        )
    return f"{carpeta_anio.strip()}-{mes_numero}"


def clasificar_subcarpeta(nombre_subcarpeta: str) -> str:
    """Clasifica una subcarpeta como "BOLETAS" o "FACTURAS" por PREFIJO
    (tras normalizar a mayúsculas y quitar espacios de los extremos): el
    origen mezcla "FACTURA" singular, "FACTURAS" plural, y variantes con el
    mes pegado (ej. "BOLETAS OCTUBRE" dentro de la carpeta "OCT 2025"), así
    que una igualdad exacta contra un set de nombres conocidos no alcanza.

    Aborta (ValueError) ante cualquier otra subcarpeta: nunca se ignora en
    silencio, para no perder comprobantes por una carpeta mal nombrada o
    fuera de lugar.
    """
    normalizado = nombre_subcarpeta.strip().upper()
    if normalizado.startswith("BOLETA"):
        return "BOLETAS"
    if normalizado.startswith("FACTURA"):
        return "FACTURAS"
    raise ValueError(
        f"Subcarpeta no reconocida (no empieza con BOLETA ni FACTURA): "
        f"'{nombre_subcarpeta}'. Revisa el árbol de origen."
    )


def recorrer_arbol_origen(raiz: pathlib.Path) -> list[ArchivoOrigen]:
    """Recorre <raiz>/<AÑO>/<CARPETA_MES>/<SUBCARPETA>/*.* (tres niveles
    fijos, sin recursión adicional dentro de la subcarpeta) y devuelve la
    lista de archivos a migrar, ya clasificados por AAAA-MM y categoría.

    Cualquier nombre de mes o de subcarpeta que no calce con las tablas
    conocidas ABORTA la migración completa (deja subir la excepción): es
    preferible frenar y corregir el nombre de la carpeta en OneDrive que
    perder un comprobante sin darse cuenta, o que dejarlo mal ubicado en
    Drive.
    """
    if not raiz.exists():
        raise FileNotFoundError(f"No existe la carpeta de origen: {raiz}")

    archivos: list[ArchivoOrigen] = []
    for carpeta_anio_dir in sorted(p for p in raiz.iterdir() if p.is_dir()):
        for carpeta_mes_dir in sorted(p for p in carpeta_anio_dir.iterdir() if p.is_dir()):
            anio_mes = resolver_anio_mes(carpeta_anio_dir.name, carpeta_mes_dir.name)
            for subcarpeta_dir in sorted(p for p in carpeta_mes_dir.iterdir() if p.is_dir()):
                categoria = clasificar_subcarpeta(subcarpeta_dir.name)
                for archivo in sorted(p for p in subcarpeta_dir.iterdir() if p.is_file()):
                    if archivo.name.lower() in NOMBRES_IGNORADOS:
                        continue
                    if archivo.name.startswith(PREFIJOS_IGNORADOS):
                        continue
                    archivos.append(
                        ArchivoOrigen(
                            ruta=archivo,
                            carpeta_anio=carpeta_anio_dir.name,
                            carpeta_mes=carpeta_mes_dir.name,
                            subcarpeta=subcarpeta_dir.name,
                            anio_mes=anio_mes,
                            categoria=categoria,
                        )
                    )
    return archivos


def recorrer_arbol_plano(raiz: pathlib.Path) -> list[pathlib.Path]:
    """Recorre 'raiz' recursivamente y devuelve, ordenados, todos los
    archivos encontrados — para el modo --a-buzon.

    A diferencia de recorrer_arbol_origen(), esta función NO interpreta
    ningún nivel de carpeta como año/mes/subcarpeta: no llama a
    resolver_anio_mes() ni a clasificar_subcarpeta(), así que un --origen
    cuyo nombre no es un mes válido (el caso real: "...\\2026\\JULIO\\
    FACTURAS", una carpeta hoja) no aborta nada. Aplica los mismos filtros
    de artefactos que recorrer_arbol_origen (PREFIJOS_IGNORADOS,
    NOMBRES_IGNORADOS), y ademas descarta las extensiones que procesar.py no
    sabe leer (EXTENSIONES_BUZON): subir un .docx al buzon solo lograria que
    termine en 02_REVISAR con un motivo. Lo descartado se registra en el log,
    nunca en silencio.
    """
    if not raiz.exists():
        raise FileNotFoundError(f"No existe la carpeta de origen: {raiz}")

    archivos: list[pathlib.Path] = []
    for ruta in sorted(raiz.rglob("*")):
        if not ruta.is_file():
            continue
        if ruta.name.lower() in NOMBRES_IGNORADOS:
            continue
        if ruta.name.startswith(PREFIJOS_IGNORADOS):
            continue
        if ruta.suffix.lower() not in EXTENSIONES_BUZON:
            logger.warning(
                "%s: extension no soportada por procesar.py, no se sube al buzon.", ruta.name
            )
            continue
        archivos.append(ruta)
    return archivos


def _normalizar_ruta_exclusion(ruta: str) -> str:
    """Normaliza una subruta de --excluir para comparar: barras invertidas a
    barras normales, sin barra inicial/final, minúsculas. El origen real
    mezcla mayúsculas y minúsculas y en Windows es común pegar la ruta con
    "\\", así que la comparación no puede ser sensible a ninguna de las dos."""
    return ruta.strip().replace("\\", "/").strip("/").lower()


def archivo_esta_excluido(archivo: ArchivoOrigen, patrones_exclusion: list[str] | None) -> bool:
    """True si 'archivo' cae dentro de alguno de los subárboles de
    'patrones_exclusion' (--excluir, repetible).

    La ruta del archivo para comparar es su carpeta contenedora relativa a
    la raíz de origen: "<carpeta_anio>/<carpeta_mes>/<subcarpeta>" (los tres
    niveles fijos que recorrer_arbol_origen ya reconoció), normalizada igual
    que el patrón. Un patrón más corto que la ruta del archivo (ej. solo
    "2026/JULIO") excluye el mes completo; un patrón de los tres niveles
    (ej. "2026/JULIO/FACTURAS") excluye solo esa subcarpeta.
    """
    if not patrones_exclusion:
        return False
    segmentos_archivo = (
        _normalizar_ruta_exclusion(f"{archivo.carpeta_anio}/{archivo.carpeta_mes}/{archivo.subcarpeta}")
    ).split("/")
    for patron in patrones_exclusion:
        segmentos_patron = _normalizar_ruta_exclusion(patron).split("/")
        if segmentos_archivo[: len(segmentos_patron)] == segmentos_patron:
            return True
    return False


def ruta_destino(archivo: ArchivoOrigen) -> str:
    """Ruta destino en Drive, en texto, solo para mostrar/loguear (no es un
    id): 01_PROCESADO/<AAAA-MM>/EL_TEMPLO/<categoria>/<nombre original>."""
    return f"01_PROCESADO/{archivo.anio_mes}/{EMPRESA_DESTINO}/{archivo.categoria}/{archivo.ruta.name}"


def _mimetype_de(ruta: pathlib.Path) -> str:
    """application/pdf para .pdf (caso casi único en este origen); para
    cualquier otra extensión se deriva con mimetypes.guess_type, y si ni
    eso reconoce la extensión, se cae a application/octet-stream."""
    if ruta.suffix.lower() == ".pdf":
        return "application/pdf"
    tipo, _ = mimetypes.guess_type(ruta.name)
    return tipo or "application/octet-stream"


# -----------------------------------------------------------------------------
# Resolución de carpetas destino en Drive, con caché (34 carpetas destino:
# 17 meses x 2 categorías; no se llama asegurar_carpeta() ni listar() una
# vez por archivo, serían miles de llamadas de más sobre ~1.800 archivos).
# -----------------------------------------------------------------------------
def resolver_carpeta_destino(
    almacen: AlmacenDrive,
    anio_mes: str,
    categoria: str,
    carpeta_procesado_id: str,
    cache_meses: dict[str, str],
    cache_categorias: dict[str, str],
) -> str:
    """Devuelve el id de Drive de
    01_PROCESADO/<anio_mes>/EL_TEMPLO/<categoria>, creando las subcarpetas
    que falten (asegurar_carpeta es idempotente: nunca duplica).

    cache_meses cachea el id de 01_PROCESADO/<anio_mes>/EL_TEMPLO (se repite
    entre BOLETAS y FACTURAS del mismo mes); cache_categorias cachea el id
    final. Cada carpeta destino real solo se resuelve una vez por corrida.
    """
    if anio_mes not in cache_meses:
        carpeta_mes_id = almacen.asegurar_carpeta(anio_mes, carpeta_procesado_id)
        cache_meses[anio_mes] = almacen.asegurar_carpeta(EMPRESA_DESTINO, carpeta_mes_id)
    carpeta_empresa_id = cache_meses[anio_mes]

    clave_categoria = f"{anio_mes}/{categoria}"
    if clave_categoria not in cache_categorias:
        cache_categorias[clave_categoria] = almacen.asegurar_carpeta(categoria, carpeta_empresa_id)
    return cache_categorias[clave_categoria]


# -----------------------------------------------------------------------------
# Migración (real o dry-run)
# -----------------------------------------------------------------------------
def ejecutar_migracion(
    archivos: list[ArchivoOrigen],
    almacen: AlmacenDrive | None,
    carpeta_procesado_id: str,
    dry_run: bool,
    patrones_exclusion: list[str] | None = None,
) -> ResultadoMigracion:
    """Sube (o simula subir) cada archivo de 'archivos' a su carpeta destino
    en 01_PROCESADO, devolviendo los contadores de la corrida.

    IDEMPOTENCIA: antes de subir un archivo se revisa si su nombre ya está
    en la carpeta destino (usando un caché en memoria del listado de cada
    carpeta destino, no una llamada por archivo — ver
    resolver_carpeta_destino), y si ya está se cuenta como "ya_existia" y no
    se sube de nuevo. Esto hace la corrida reanudable: si se corta a la
    mitad, correrla de nuevo retoma donde quedó sin duplicar nada.

    Un archivo que falla al subir NO tumba la corrida completa: se anota
    como "error" y se sigue con el siguiente (mismo criterio que
    procesar.py y correo_gmail.py).

    En dry_run=True esta función NUNCA toca 'almacen' (puede venir en None):
    así el modo por defecto del script queda garantizado sin credenciales
    de Google y sin ningún riesgo de escritura, incluso si quien llama pasa
    por error una instancia real.

    'patrones_exclusion' (--excluir, repetible) se revisa ANTES que
    dry_run/idempotencia/subida: un archivo que cae en un subárbol excluido
    se cuenta como "excluido" y nunca llega a tocar Drive ni a evaluarse
    como "se_subiria" (ver archivo_esta_excluido()). Es lo que permite que
    --a-buzon suba una carpeta y el modo archivo, corrido después con
    --excluir sobre esa misma carpeta, no la duplique.
    """
    resultado = ResultadoMigracion(total_encontrados=len(archivos))
    cache_meses: dict[str, str] = {}
    cache_categorias: dict[str, str] = {}
    cache_nombres: dict[str, set[str]] = {}

    for archivo in archivos:
        destino_str = ruta_destino(archivo)

        if archivo_esta_excluido(archivo, patrones_exclusion):
            resultado.registrar(archivo.anio_mes, "excluido")
            logger.info("%s -> %s: excluido", archivo.ruta, destino_str)
            continue

        if dry_run:
            resultado.registrar(archivo.anio_mes, "se_subiria")
            logger.info("[DRY-RUN] %s -> %s", archivo.ruta, destino_str)
            continue

        try:
            carpeta_id = resolver_carpeta_destino(
                almacen, archivo.anio_mes, archivo.categoria, carpeta_procesado_id, cache_meses, cache_categorias
            )
            if carpeta_id not in cache_nombres:
                cache_nombres[carpeta_id] = {a["name"] for a in almacen.listar(carpeta_id)}
            nombres_existentes = cache_nombres[carpeta_id]

            if archivo.ruta.name in nombres_existentes:
                resultado.registrar(archivo.anio_mes, "ya_existia")
                logger.info("%s -> %s: ya_existia", archivo.ruta, destino_str)
                continue

            almacen.subir(carpeta_id, archivo.ruta.name, archivo.ruta, mimetype=_mimetype_de(archivo.ruta))
            nombres_existentes.add(archivo.ruta.name)
            resultado.registrar(archivo.anio_mes, "subido")
            logger.info("%s -> %s: subido", archivo.ruta, destino_str)
        except Exception as exc:
            resultado.registrar(archivo.anio_mes, "error")
            logger.error("%s -> %s: error (%s)", archivo.ruta, destino_str, exc)

    return resultado


# -----------------------------------------------------------------------------
# Modo --a-buzon: subida PLANA a 00_BUZON/<tipo>, sin árbol de meses. A
# diferencia del resto del script (que no lee config.yaml a propósito, ver
# el docstring del módulo), este modo SÍ lo lee: es la única forma de
# resolver el id de la carpeta destino sin hardcodearlo.
# -----------------------------------------------------------------------------
def cargar_config(ruta_config: pathlib.Path) -> dict:
    """Carga config.yaml. Solo la usa el modo --a-buzon (el modo archivo
    sigue sin depender de config.yaml)."""
    if not ruta_config.exists():
        raise FileNotFoundError(
            f"No se encontró '{ruta_config}'. Pasa --config con la ruta correcta a config.yaml."
        )
    with ruta_config.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def resolver_carpeta_buzon(config: dict, tipo: str) -> str:
    """Devuelve el id de Drive de config.yaml -> drive.carpetas.buzon_tipos.
    <tipo> (ej. "facturas" -> id de 00_BUZON/FACTURAS).

    Aborta (ValueError) si 'tipo' no está en el config o si su id viene
    vacío: mejor frenar que subir 54 facturas al lugar equivocado por un
    nombre de tipo mal escrito.
    """
    buzon_tipos = (((config or {}).get("drive") or {}).get("carpetas") or {}).get("buzon_tipos") or {}
    if not isinstance(buzon_tipos, dict):
        buzon_tipos = {}
    carpeta_id = (buzon_tipos.get(tipo) or "").strip()
    if not carpeta_id:
        disponibles = ", ".join(sorted(buzon_tipos)) if buzon_tipos else "(ninguno configurado)"
        raise ValueError(
            f"Tipo de buzon '{tipo}' no está en config.yaml -> drive.carpetas.buzon_tipos "
            f"(disponibles: {disponibles}). No se subió nada."
        )
    return carpeta_id


def ejecutar_migracion_a_buzon(
    archivos: list[pathlib.Path],
    almacen: AlmacenDrive | None,
    carpeta_buzon_id: str,
    dry_run: bool,
) -> ResultadoMigracion:
    """Sube (o simula subir) cada archivo de 'archivos' PLANO a
    'carpeta_buzon_id' — sin subcarpetas de mes, empresa ni categoría.

    IDEMPOTENCIA: usa AlmacenDrive.buscar_por_nombre() en la carpeta destino
    antes de cada subida (a diferencia de ejecutar_migracion(), que cachea
    el listado completo de cada carpeta destino en memoria: acá hay una
    sola carpeta destino y un puñado de archivos —54 en el caso real de
    julio 2026—, así que una consulta por archivo es simple y de sobra
    suficiente). Correr esto dos veces sobre el mismo estado da 0 subidos y
    todo "ya_existia".

    Mismo criterio que ejecutar_migracion() para dry-run (nunca toca
    'almacen', que puede venir en None) y para errores (un archivo que
    falla se anota como "error" y no tumba la corrida).
    """
    resultado = ResultadoMigracion(total_encontrados=len(archivos))
    clave = "buzon"  # no hay anio_mes en este modo; una sola clave alcanza.

    for ruta in archivos:
        if dry_run:
            resultado.registrar(clave, "se_subiria")
            logger.info("[DRY-RUN] %s -> buzon", ruta)
            continue

        try:
            existente = almacen.buscar_por_nombre(carpeta_buzon_id, ruta.name)
            if existente is not None:
                resultado.registrar(clave, "ya_existia")
                logger.info("%s -> buzon: ya_existia", ruta)
                continue

            almacen.subir(carpeta_buzon_id, ruta.name, ruta, mimetype=_mimetype_de(ruta))
            resultado.registrar(clave, "subido")
            logger.info("%s -> buzon: subido", ruta)
        except Exception as exc:
            resultado.registrar(clave, "error")
            logger.error("%s -> buzon: error (%s)", ruta, exc)

    return resultado


def imprimir_resumen_buzon(resultado: ResultadoMigracion, tipo: str, dry_run: bool) -> None:
    print("\n" + "=" * 60)
    print(f"RESUMEN DE LA SUBIDA AL BUZON ({tipo})" + (" (DRY-RUN, no se subio nada)" if dry_run else ""))
    print("=" * 60)
    print(f"Total encontrados: {resultado.total_encontrados}")
    if dry_run:
        print(f"Se subirian:       {resultado.conteos.get('se_subiria', 0)}")
    else:
        print(f"Subidos:           {resultado.conteos.get('subido', 0)}")
        print(f"Ya existian:       {resultado.conteos.get('ya_existia', 0)}")
        print(f"Errores:           {resultado.conteos.get('error', 0)}")
    print("=" * 60)


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------
def configurar_logging(carpeta_salida: pathlib.Path, verbose: bool) -> logging.Logger:
    carpeta_salida.mkdir(parents=True, exist_ok=True)
    handler_archivo = logging.FileHandler(carpeta_salida / "migracion_comprobantes.log", encoding="utf-8")
    handler_archivo.setLevel(logging.DEBUG)
    handler_consola = logging.StreamHandler()
    handler_consola.setLevel(logging.DEBUG if verbose else logging.INFO)

    formato = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler_archivo.setFormatter(formato)
    handler_consola.setFormatter(formato)

    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.addHandler(handler_archivo)
    logger.addHandler(handler_consola)
    return logger


def imprimir_resumen(resultado: ResultadoMigracion, dry_run: bool) -> None:
    print("\n" + "=" * 60)
    print("RESUMEN DE LA MIGRACION" + (" (DRY-RUN, no se subio nada)" if dry_run else ""))
    print("=" * 60)
    print(f"Total encontrados: {resultado.total_encontrados}")
    if dry_run:
        print(f"Se subirian:       {resultado.conteos.get('se_subiria', 0)}")
    else:
        print(f"Subidos:           {resultado.conteos.get('subido', 0)}")
        print(f"Ya existian:       {resultado.conteos.get('ya_existia', 0)}")
        print(f"Errores:           {resultado.conteos.get('error', 0)}")
    if resultado.conteos.get("excluido"):
        print(f"Excluidos:         {resultado.conteos['excluido']} (--excluir, no se tocaron)")
    print("\nDesglose por mes:")
    for anio_mes in sorted(resultado.por_mes):
        contadores = resultado.por_mes[anio_mes]
        detalle = ", ".join(f"{estado}={cantidad}" for estado, cantidad in sorted(contadores.items()))
        print(f"  {anio_mes}: {detalle}")
    print("=" * 60)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Migracion de una sola vez: copia los comprobantes historicos de EL "
            "TEMPLO desde OneDrive a Drive (01_PROCESADO), sin pasar por el "
            "modelo. Por defecto es dry-run; usa --ejecutar para subir de verdad."
        )
    )
    parser.add_argument(
        "--origen", default=str(RAIZ_ORIGEN_POR_DEFECTO), help="Carpeta raiz de origen en OneDrive (EL TEMPLO)."
    )
    parser.add_argument(
        "--carpeta-procesado-id",
        default=None,
        help=(
            f"Id de Drive de 01_PROCESADO (por defecto, {CARPETA_PROCESADO_ID_POR_DEFECTO!r}). "
            "Incompatible con --a-buzon."
        ),
    )
    parser.add_argument(
        "--a-buzon",
        dest="a_buzon",
        default=None,
        metavar="TIPO",
        help=(
            "Modo alterno: sube PLANO (sin arbol AAAA-MM/EMPRESA/BOLETAS-FACTURAS) el "
            "contenido de --origen a 00_BUZON/<TIPO>. <TIPO> debe estar en config.yaml -> "
            "drive.carpetas.buzon_tipos (ej. 'facturas', 'notas_venta', 'liquidaciones', "
            "'otros'). Incompatible con --carpeta-procesado-id."
        ),
    )
    parser.add_argument(
        "--excluir",
        dest="excluir",
        action="append",
        default=None,
        metavar="SUBRUTA",
        help=(
            "Solo para el modo archivo (ignorado con --a-buzon): subruta relativa a --origen "
            "a excluir del recorrido (ej. '2026/JULIO/FACTURAS'), contada aparte como "
            "'excluido'. Repetible. Tolerante a '/' y '\\\\' y a mayusculas/minusculas."
        ),
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Ruta a config.yaml (solo la usa --a-buzon, para resolver drive.carpetas.buzon_tipos).",
    )
    parser.add_argument(
        "--ejecutar",
        action="store_true",
        help="Sube de verdad a Drive. Sin este flag, solo se imprime el plan (dry-run, por defecto).",
    )
    parser.add_argument("--verbose", action="store_true", help="Log detallado (DEBUG) tambien en consola.")
    args = parser.parse_args(argv)

    carpeta_salida = pathlib.Path("salida")
    configurar_logging(carpeta_salida, args.verbose)

    if args.a_buzon and args.carpeta_procesado_id is not None:
        logger.error("--a-buzon es incompatible con --carpeta-procesado-id.")
        return 1

    raiz = pathlib.Path(args.origen)
    dry_run = not args.ejecutar

    if args.a_buzon:
        if args.excluir:
            logger.warning("--excluir se ignora en modo --a-buzon (no aplica: la subida ya es plana).")

        try:
            config = cargar_config(pathlib.Path(args.config))
            carpeta_buzon_id = resolver_carpeta_buzon(config, args.a_buzon)
        except (FileNotFoundError, ValueError) as exc:
            logger.error(str(exc))
            return 1

        try:
            archivos_buzon = recorrer_arbol_plano(raiz)
        except FileNotFoundError as exc:
            logger.error(str(exc))
            return 1

        logger.info(
            "Encontrados %d archivo(s) en '%s' para el buzon (tipo=%s).", len(archivos_buzon), raiz, args.a_buzon
        )

        almacen: AlmacenDrive | None = None
        if args.ejecutar:
            try:
                import auth_google  # import diferido: dry-run no necesita credenciales

                almacen = AlmacenDrive(auth_google.servicio_drive())
            except Exception as exc:
                logger.error(str(exc))
                return 1
        else:
            logger.info("Modo dry-run (por defecto): no se sube nada. Usa --ejecutar para subir de verdad.")

        resultado_buzon = ejecutar_migracion_a_buzon(archivos_buzon, almacen, carpeta_buzon_id, dry_run=dry_run)
        imprimir_resumen_buzon(resultado_buzon, args.a_buzon, dry_run=dry_run)
        return 0

    carpeta_procesado_id = args.carpeta_procesado_id or CARPETA_PROCESADO_ID_POR_DEFECTO

    try:
        archivos = recorrer_arbol_origen(raiz)
    except (FileNotFoundError, ValueError) as exc:
        logger.error(str(exc))
        return 1

    logger.info("Encontrados %d archivo(s) en '%s'.", len(archivos), raiz)

    almacen: AlmacenDrive | None = None
    if args.ejecutar:
        try:
            import auth_google  # import diferido: dry-run no necesita credenciales

            almacen = AlmacenDrive(auth_google.servicio_drive())
        except Exception as exc:
            logger.error(str(exc))
            return 1
    else:
        logger.info("Modo dry-run (por defecto): no se sube nada. Usa --ejecutar para subir de verdad.")

    resultado = ejecutar_migracion(
        archivos, almacen, carpeta_procesado_id, dry_run=dry_run, patrones_exclusion=args.excluir
    )
    imprimir_resumen(resultado, dry_run=dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
