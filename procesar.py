"""Orquestador: una pasada sobre el buzón de comprobantes (00_BUZON).

Por cada archivo del buzón: lo clasifica por extensión, lo extrae (XML UBL
de forma determinística, o PDF/imagen vía el modelo de Claude), le asigna
una empresa propia por el RUC del cliente, lo deduplica contra lo ya
registrado, empareja sus ítems contra el catálogo de insumos, lo escribe en
los Google Sheets del negocio y por último mueve el archivo a
01_PROCESADO/AAAA-MM/EMPRESA/ (o a 02_REVISAR/ con un motivo, si algo no
cuadra). Ningún archivo se borra nunca.

Uso:
    C:\\Python312\\python.exe procesar.py --config config.yaml
    C:\\Python312\\python.exe procesar.py --config config.yaml --dry-run --verbose
    C:\\Python312\\python.exe procesar.py --solo F001-123.xml
    C:\\Python312\\python.exe procesar.py --limite 15

Ver SKILL.md para la arquitectura completa y las trampas conocidas.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime
import logging
import pathlib
import re
import shutil
import sys
from typing import TYPE_CHECKING

import yaml

# Los tipos del contrato compartido (esquema.py) solo se usan para anotar;
# procesar.py nunca los importa en tiempo de ejecución (duck typing), así
# que puede correr aunque esquema.py todavía no exista.
if TYPE_CHECKING:
    from esquema import ComprobanteExtraido

# Los módulos de los otros dos agentes se importan a nivel de módulo para
# que los tests puedan sustituirlos por dobles de prueba (sys.modules)
# antes de importar procesar.py. No se tocan ni se crean aquí.
from extractores import modelo as extractor_modelo
from extractores import xml_ubl as extractor_xml
import catalogo as catalogo_mod
import registro_sheets as registro_mod

logger = logging.getLogger("procesar")

EXT_XML = {".xml", ".zip"}
EXT_PDF = {".pdf"}
EXT_IMAGEN = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
EXT_HEIC = {".heic"}

MOTIVO_HEIC = "formato HEIC no soportado por la API, convertir a JPEG"

# Nombres de archivo que se ignoran por completo al listar el buzón (no son
# comprobantes: son artefactos del sistema operativo o de la sincronización
# de Google Drive para escritorio).
PREFIJOS_IGNORADOS = ("~$", ".")
NOMBRES_IGNORADOS = {"desktop.ini", "thumbs.db"}

# Estimación aproximada del costo en USD de cada llamada al modelo (PDF o
# imagen). Es un valor de referencia, NO el precio real de la API de
# Anthropic: ajústalo en esta constante si cambia el precio del modelo o el
# nivel de esfuerzo configurado en config.yaml (modelo / esfuerzo).
COSTO_ESTIMADO_USD_POR_LLAMADA_MODELO = 0.02

FORMATOS_FECHA = ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y")


@dataclasses.dataclass
class ResultadoArchivo:
    """Resultado del procesamiento de un comprobante (o grupo XML+respaldo)."""

    nombre: str
    estado: str  # 'procesado' | 'revisar' | 'duplicado'
    motivo: str | None = None
    n_items: int = 0
    llamadas_modelo: int = 0


# -----------------------------------------------------------------------------
# Listado y agrupación del buzón
# -----------------------------------------------------------------------------
def listar_buzon(carpeta_buzon: pathlib.Path) -> list[pathlib.Path]:
    if not carpeta_buzon.exists():
        return []
    archivos = []
    for ruta in carpeta_buzon.iterdir():
        if not ruta.is_file():
            continue
        if ruta.name.lower() in NOMBRES_IGNORADOS:
            continue
        if ruta.name.startswith(PREFIJOS_IGNORADOS):
            continue
        archivos.append(ruta)
    return sorted(archivos, key=lambda p: p.name.lower())


def construir_planes(
    archivos: list[pathlib.Path],
) -> list[tuple[pathlib.Path, list[pathlib.Path]]]:
    """Agrupa los archivos del buzón por nombre (sin extensión).

    Si un grupo contiene exactamente un XML/ZIP, ese es el comprobante
    principal y el resto del grupo se trata como respaldo enlazado (no se
    procesa por separado: "XML gana siempre"). En cualquier otro caso
    (ningún XML en el grupo, o más de uno) cada archivo se procesa de forma
    independiente, porque sin un XML de por medio no hay forma barata y
    segura de confirmar que dos archivos son el mismo comprobante.
    """
    grupos: dict[str, list[pathlib.Path]] = {}
    for ruta in archivos:
        grupos.setdefault(ruta.stem.lower(), []).append(ruta)

    planes: list[tuple[pathlib.Path, list[pathlib.Path]]] = []
    for rutas_grupo in grupos.values():
        xmls = [r for r in rutas_grupo if r.suffix.lower() in EXT_XML]
        if len(xmls) == 1:
            principal = xmls[0]
            respaldos = [r for r in rutas_grupo if r != principal]
            planes.append((principal, respaldos))
        else:
            for r in rutas_grupo:
                planes.append((r, []))

    planes.sort(key=lambda plan: plan[0].name.lower())
    return planes


# -----------------------------------------------------------------------------
# Fechas y nombres de destino
# -----------------------------------------------------------------------------
def anio_mes(comp: "ComprobanteExtraido") -> str:
    valor = getattr(comp, "fecha_emision", None)
    if valor is None:
        return "SIN_FECHA"
    if isinstance(valor, (datetime.date, datetime.datetime)):
        return valor.strftime("%Y-%m")
    texto = str(valor).strip()
    for formato in FORMATOS_FECHA:
        try:
            return datetime.datetime.strptime(texto, formato).strftime("%Y-%m")
        except ValueError:
            continue
    coincidencia = re.match(r"(\d{4})-(\d{2})", texto)
    if coincidencia:
        return f"{coincidencia.group(1)}-{coincidencia.group(2)}"
    return "SIN_FECHA"


def nombre_empresa_carpeta(nombre_corto: str) -> str:
    return nombre_corto.strip().replace(" ", "_")


def nombre_destino(comp: "ComprobanteExtraido", extension: str) -> str:
    ruc = (getattr(comp, "proveedor_ruc", None) or "SINRUC").strip()
    serie_numero = (getattr(comp, "serie_numero", None) or "SINSERIE").strip()
    serie_numero = serie_numero.replace("/", "-").replace(" ", "")
    total = getattr(comp, "total", None)
    try:
        total_txt = f"{float(total):.2f}"
    except (TypeError, ValueError):
        total_txt = "0.00"
    return f"{ruc}_{serie_numero}_{total_txt}{extension.lower()}"


def ruta_destino_unica(carpeta: pathlib.Path, nombre: str) -> pathlib.Path:
    candidato = carpeta / nombre
    if not candidato.exists():
        return candidato
    stem = pathlib.Path(nombre).stem
    ext = pathlib.Path(nombre).suffix
    contador = 2
    while True:
        candidato = carpeta / f"{stem}_{contador}{ext}"
        if not candidato.exists():
            return candidato
        contador += 1


def mover_archivo(
    origen: pathlib.Path, carpeta_destino: pathlib.Path, nombre_deseado: str, dry_run: bool
) -> pathlib.Path | None:
    """Mueve 'origen' dentro de 'carpeta_destino' sin sobrescribir nunca nada.

    Devuelve la ruta final, o None si el movimiento falló (el archivo queda
    intacto en su ubicación original; el llamador debe limitarse a
    registrar el problema, nunca reintentar borrando nada).
    """
    if dry_run:
        destino = ruta_destino_unica(carpeta_destino, nombre_deseado) if carpeta_destino.exists() else carpeta_destino / nombre_deseado
        logger.info("[DRY-RUN] se movería %s -> %s", origen, destino)
        return destino

    try:
        carpeta_destino.mkdir(parents=True, exist_ok=True)
        destino = ruta_destino_unica(carpeta_destino, nombre_deseado)
        shutil.move(str(origen), str(destino))
        return destino
    except OSError as exc:
        logger.error(
            "No se pudo mover '%s' a '%s': %s. El archivo queda en el buzón; la "
            "deduplicación evitará que se registre dos veces en la próxima corrida.",
            origen,
            carpeta_destino,
            exc,
        )
        return None


def mover_a_revisar(
    rutas: list[pathlib.Path], carpeta_revisar: pathlib.Path, motivo: str, dry_run: bool
) -> None:
    for ruta in rutas:
        destino = mover_archivo(ruta, carpeta_revisar, ruta.name, dry_run)
        if destino is None:
            continue
        ruta_motivo = destino.with_name(destino.name + ".motivo.txt")
        if dry_run:
            logger.info("[DRY-RUN] se escribiría %s con motivo: %s", ruta_motivo, motivo)
        else:
            ruta_motivo.write_text(motivo, encoding="utf-8")


# -----------------------------------------------------------------------------
# Asignación de empresa y local
# -----------------------------------------------------------------------------
def resolver_empresa(config: dict, comp: "ComprobanteExtraido") -> tuple[dict | None, str | None]:
    cliente_ruc = (getattr(comp, "cliente_ruc", None) or "").strip()
    if not cliente_ruc:
        return None, "el comprobante no trae RUC de cliente; no se puede asignar la empresa automáticamente"

    for empresa_cfg in config.get("empresas", []):
        if str(empresa_cfg.get("ruc", "")).strip() == cliente_ruc:
            return empresa_cfg, None

    return None, (
        f"el RUC de cliente ({cliente_ruc}) no corresponde a ninguna empresa "
        f"configurada en config.yaml; asignar manualmente (puede ser un "
        f"comprobante facturado a una razón social pero pagado desde otra)"
    )


def resolver_local(empresa_cfg: dict) -> tuple[str | None, str | None]:
    """Decide el local de un comprobante ya asignado a una empresa.

    Un comprobante NUNCA dice a qué local corresponde: se emite a nombre de
    la razón social, no del establecimiento. El local es conocimiento de
    quien compra, no un dato del documento. Por eso una empresa con varios
    locales necesita `local_por_defecto` en config.yaml; sin él, todos sus
    comprobantes irían a revisión manual y el sistema sería inservible
    (EL TEMPLO tiene dos locales y concentra el 60% del volumen).

    El valor por defecto se corrige a mano en el Sheet cuando toque otro
    local. Para capturar el local en el origen —sin corregir después— la vía
    natural es usar subcarpetas por local dentro del buzón; está anotado en
    SKILL.md como mejora pendiente.
    """
    locales = empresa_cfg.get("locales") or []
    if len(locales) <= 1:
        return (locales[0] if locales else ""), None

    por_defecto = str(empresa_cfg.get("local_por_defecto") or "").strip()
    if por_defecto:
        if por_defecto not in locales:
            return None, (
                f"local_por_defecto '{por_defecto}' de la empresa "
                f"{empresa_cfg.get('nombre_corto')} no está en su lista de locales "
                f"({', '.join(locales)}); corregir config.yaml"
            )
        return por_defecto, None

    return None, (
        f"la empresa {empresa_cfg.get('nombre_corto')} tiene más de un local "
        f"configurado ({', '.join(locales)}) y el comprobante no indica a cuál "
        f"corresponde; define 'local_por_defecto' en config.yaml o asigna el "
        f"local manualmente"
    )


# -----------------------------------------------------------------------------
# Link de Drive (mejor esfuerzo; nunca bloquea el procesamiento)
# -----------------------------------------------------------------------------
class ResolutorLinkDrive:
    """Resuelve el link de Drive de un archivo del buzón buscándolo por nombre
    dentro de la carpeta de buzón, ANTES de moverlo. Drive para escritorio
    conserva el ID del archivo cuando se mueve localmente entre carpetas del
    mismo Drive (lo sincroniza como un cambio de carpeta padre, no como un
    borrado+creación), así que el link sigue siendo válido después del
    movimiento.

    Es un mejor esfuerzo: si no se puede resolver la carpeta de Drive
    correspondiente (por ejemplo, porque 'raiz' está en una Unidad
    compartida y no en 'Mi unidad', o porque Drive para escritorio todavía
    no sincronizó el archivo) devuelve cadena vacía y no interrumpe nada.
    """

    def __init__(self, servicio_drive, config: dict):
        self._servicio = servicio_drive
        self._config = config
        self._id_carpeta_buzon: str | None = None
        self._deshabilitado = servicio_drive is None

    def _segmentos_desde_mi_unidad(self) -> list[str] | None:
        raiz = str(self._config["drive"]["raiz"]).replace("\\", "/")
        coincidencia = re.search(r"Mi unidad/(.+)$", raiz, flags=re.IGNORECASE)
        if not coincidencia:
            return None
        segmentos = [s for s in coincidencia.group(1).split("/") if s]
        segmentos.append(self._config["drive"]["buzon"])
        return segmentos

    def _resolver_id_carpeta_buzon(self) -> str | None:
        if self._id_carpeta_buzon is not None:
            return self._id_carpeta_buzon
        segmentos = self._segmentos_desde_mi_unidad()
        if segmentos is None:
            self._deshabilitado = True
            return None
        padre_id = "root"
        try:
            for segmento in segmentos:
                nombre_escapado = segmento.replace("'", "\\'")
                query = (
                    f"name = '{nombre_escapado}' and "
                    "mimeType = 'application/vnd.google-apps.folder' and "
                    f"'{padre_id}' in parents and trashed = false"
                )
                resultado = self._servicio.files().list(
                    q=query, fields="files(id, name)", pageSize=5
                ).execute()
                encontrados = resultado.get("files", [])
                if not encontrados:
                    self._deshabilitado = True
                    return None
                padre_id = encontrados[0]["id"]
        except Exception as exc:
            logger.warning("No se pudo resolver la carpeta de Drive del buzón: %s", exc)
            self._deshabilitado = True
            return None
        self._id_carpeta_buzon = padre_id
        return padre_id

    def obtener(self, nombre_archivo: str) -> str:
        if self._deshabilitado:
            return ""
        carpeta_id = self._resolver_id_carpeta_buzon()
        if carpeta_id is None:
            return ""
        try:
            nombre_escapado = nombre_archivo.replace("'", "\\'")
            query = f"name = '{nombre_escapado}' and '{carpeta_id}' in parents and trashed = false"
            resultado = self._servicio.files().list(
                q=query, fields="files(id)", pageSize=1
            ).execute()
            archivos = resultado.get("files", [])
            if not archivos:
                logger.warning(
                    "No se encontró '%s' en Drive todavía (posible sincronización "
                    "pendiente de Drive para escritorio); se guarda sin link.",
                    nombre_archivo,
                )
                return ""
            return f"https://drive.google.com/file/d/{archivos[0]['id']}/view"
        except Exception as exc:
            logger.warning("No se pudo obtener el link de Drive de '%s': %s", nombre_archivo, exc)
            return ""


# -----------------------------------------------------------------------------
# Extracción
# -----------------------------------------------------------------------------
def extraer_comprobante(
    ruta: pathlib.Path, extension: str, config: dict | None = None
) -> tuple["ComprobanteExtraido | None", int]:
    """Devuelve (comprobante, llamadas_al_modelo). Puede lanzar excepción.

    `config` se pasa al extractor por modelo porque de ahí salen el modelo,
    el nivel de esfuerzo y las razones sociales propias del negocio. Sin
    config el extractor funciona igual, pero con los valores por defecto y
    sin poder decirle al modelo cuáles RUCs son del lado cliente — por eso
    conviene pasarlo siempre desde la corrida real.
    """
    if extension in EXT_XML:
        return extractor_xml.extraer(ruta), 0
    if extension in EXT_PDF:
        return extractor_modelo.extraer(ruta, tipo="pdf", config=config), 1
    if extension in EXT_IMAGEN:
        return extractor_modelo.extraer(ruta, tipo="imagen", config=config), 1
    raise ValueError(f"extensión no manejada por extraer_comprobante: {extension}")


# -----------------------------------------------------------------------------
# Catálogo de insumos
# -----------------------------------------------------------------------------
def emparejar_items(comp: "ComprobanteExtraido", catalogo_obj) -> None:
    """Enriquece cada ítem de comp con los atributos dinámicos
    'insumo_catalogo', 'categoria_catalogo' y 'confianza_match'.

    Estos atributos NO forman parte de los campos declarados en
    esquema.ItemExtraido (el contrato compartido es fijo); se añaden en
    tiempo de ejecución porque Registro.escribir(comp, ...) no recibe los
    resultados del emparejado por separado. registro_sheets.py debe leerlos
    con getattr(item, 'insumo_catalogo', None) y no asumir que siempre
    están presentes.
    """
    if catalogo_obj is None:
        return
    for item in getattr(comp, "items", None) or []:
        try:
            insumo, categoria, confianza = catalogo_obj.emparejar(item.descripcion)
        except Exception as exc:
            logger.warning("Fallo al emparejar '%s' contra el catálogo: %s", item.descripcion, exc)
            insumo, categoria, confianza = None, None, 0.0
        item.insumo_catalogo = insumo
        item.categoria_catalogo = categoria
        item.confianza_match = confianza


# -----------------------------------------------------------------------------
# Procesamiento de un comprobante (o grupo XML + respaldos)
# -----------------------------------------------------------------------------
def procesar_uno(
    principal: pathlib.Path,
    respaldos: list[pathlib.Path],
    *,
    config: dict,
    registro,
    catalogo_obj,
    resolutor_link,
    claves_procesadas_en_lote: set[str],
    carpeta_procesado: pathlib.Path,
    carpeta_revisar: pathlib.Path,
    dry_run: bool,
) -> ResultadoArchivo:
    nombre = principal.name
    todas_las_rutas = [principal, *respaldos]
    extension = principal.suffix.lower()

    if extension in EXT_HEIC:
        mover_a_revisar(todas_las_rutas, carpeta_revisar, MOTIVO_HEIC, dry_run)
        return ResultadoArchivo(nombre, "revisar", MOTIVO_HEIC)

    extensiones_soportadas = EXT_XML | EXT_PDF | EXT_IMAGEN
    if extension not in extensiones_soportadas:
        motivo = f"extensión no soportada: {extension or '(sin extensión)'}"
        mover_a_revisar(todas_las_rutas, carpeta_revisar, motivo, dry_run)
        return ResultadoArchivo(nombre, "revisar", motivo)

    try:
        comp, llamadas_modelo = extraer_comprobante(principal, extension, config)
    except Exception as exc:
        motivo = f"error al extraer datos del comprobante: {exc}"
        logger.exception("Error al extraer '%s'", principal)
        mover_a_revisar(todas_las_rutas, carpeta_revisar, motivo, dry_run)
        return ResultadoArchivo(nombre, "revisar", motivo)

    try:
        problemas = comp.validar() or []
    except Exception as exc:
        problemas = [f"no se pudo validar el comprobante: {exc}"]

    if problemas:
        motivo = "datos incompletos o inválidos: " + "; ".join(problemas)
        mover_a_revisar(todas_las_rutas, carpeta_revisar, motivo, dry_run)
        return ResultadoArchivo(nombre, "revisar", motivo, llamadas_modelo=llamadas_modelo)

    empresa_cfg, motivo_empresa = resolver_empresa(config, comp)
    if empresa_cfg is None:
        mover_a_revisar(todas_las_rutas, carpeta_revisar, motivo_empresa, dry_run)
        return ResultadoArchivo(nombre, "revisar", motivo_empresa, llamadas_modelo=llamadas_modelo)

    local, motivo_local = resolver_local(empresa_cfg)
    if local is None:
        mover_a_revisar(todas_las_rutas, carpeta_revisar, motivo_local, dry_run)
        return ResultadoArchivo(nombre, "revisar", motivo_local, llamadas_modelo=llamadas_modelo)

    try:
        clave = comp.clave()
    except Exception as exc:
        motivo = f"no se pudo calcular la clave de deduplicación: {exc}"
        mover_a_revisar(todas_las_rutas, carpeta_revisar, motivo, dry_run)
        return ResultadoArchivo(nombre, "revisar", motivo, llamadas_modelo=llamadas_modelo)

    if clave in claves_procesadas_en_lote or clave in registro.claves_existentes():
        motivo = f"comprobante duplicado (clave {clave} ya registrada)"
        mover_a_revisar(todas_las_rutas, carpeta_revisar, motivo, dry_run)
        return ResultadoArchivo(nombre, "duplicado", motivo, llamadas_modelo=llamadas_modelo)

    emparejar_items(comp, catalogo_obj)

    nombre_final = nombre_destino(comp, extension)
    link_drive = "" if dry_run else resolutor_link.obtener(principal.name)

    # La escritura se delega SIEMPRE en Registro, también en dry-run: Registro
    # lee config['dry_run'] y en ese modo escribe a salida/*.csv en vez de
    # llamar a la API de Sheets. Así la corrida de prueba ejercita el mismo
    # camino de código que la real (construcción de filas, orden de columnas,
    # emparejado de ítems) y deja un resultado revisable, en vez de solo
    # anunciar en el log lo que habría hecho.
    logger.info(
        "%sescribiendo registro: empresa=%s local=%s clave=%s items=%d",
        "[DRY-RUN] " if dry_run else "",
        empresa_cfg["nombre_corto"],
        local,
        clave,
        len(getattr(comp, "items", None) or []),
    )
    registro.escribir(comp, empresa_cfg["nombre_corto"], local, link_drive, nombre_final)

    claves_procesadas_en_lote.add(clave)

    carpeta_destino = carpeta_procesado / anio_mes(comp) / nombre_empresa_carpeta(empresa_cfg["nombre_corto"])
    destino = mover_archivo(principal, carpeta_destino, nombre_final, dry_run)
    if destino is None:
        logger.error(
            "'%s' se registró correctamente pero no se pudo mover; queda en el buzón.",
            principal,
        )
    for respaldo in respaldos:
        nombre_respaldo = nombre_destino(comp, respaldo.suffix.lower())
        mover_archivo(respaldo, carpeta_destino, nombre_respaldo, dry_run)

    return ResultadoArchivo(
        nombre, "procesado", None, n_items=len(getattr(comp, "items", None) or []), llamadas_modelo=llamadas_modelo
    )


# -----------------------------------------------------------------------------
# CLI y orquestación
# -----------------------------------------------------------------------------
def cargar_config(ruta_config: pathlib.Path) -> dict:
    if not ruta_config.exists():
        sys.exit(
            f"No se encontró '{ruta_config}'. Copia config.ejemplo.yaml como "
            f"config.yaml y rellénalo (ver ONBOARDING.md)."
        )
    with ruta_config.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def configurar_logging(carpeta_salida: pathlib.Path, verbose: bool) -> None:
    carpeta_salida.mkdir(parents=True, exist_ok=True)
    handler_archivo = logging.FileHandler(carpeta_salida / "procesar.log", encoding="utf-8")
    handler_archivo.setLevel(logging.DEBUG)
    handler_consola = logging.StreamHandler()
    handler_consola.setLevel(logging.DEBUG if verbose else logging.INFO)

    formato = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler_archivo.setFormatter(formato)
    handler_consola.setFormatter(formato)

    raiz = logging.getLogger("procesar")
    raiz.setLevel(logging.DEBUG)
    raiz.handlers.clear()
    raiz.addHandler(handler_archivo)
    raiz.addHandler(handler_consola)


def imprimir_resumen(resultados: list[ResultadoArchivo]) -> None:
    procesados = [r for r in resultados if r.estado == "procesado"]
    duplicados = [r for r in resultados if r.estado == "duplicado"]
    a_revisar = [r for r in resultados if r.estado == "revisar"]

    por_motivo: dict[str, int] = {}
    for r in a_revisar:
        por_motivo[r.motivo or "(sin motivo)"] = por_motivo.get(r.motivo or "(sin motivo)", 0) + 1

    total_items = sum(r.n_items for r in procesados)
    total_llamadas_modelo = sum(r.llamadas_modelo for r in resultados)
    costo_estimado = total_llamadas_modelo * COSTO_ESTIMADO_USD_POR_LLAMADA_MODELO

    print("\n" + "=" * 60)
    print("RESUMEN DE LA CORRIDA")
    print("=" * 60)
    print(f"Procesados:   {len(procesados)}")
    print(f"Duplicados:   {len(duplicados)}")
    print(f"A revisar:    {len(a_revisar)}")
    if por_motivo:
        for motivo, cantidad in sorted(por_motivo.items(), key=lambda x: -x[1]):
            print(f"    - ({cantidad}) {motivo}")
    print(f"Total de ítems escritos: {total_items}")
    print(
        f"Costo estimado (aprox., {total_llamadas_modelo} llamada(s) al modelo x "
        f"${COSTO_ESTIMADO_USD_POR_LLAMADA_MODELO:.2f}): ${costo_estimado:.2f}"
    )
    print("=" * 60)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Procesa el buzón de comprobantes de SCONCHA.")
    parser.add_argument("--config", default="config.yaml", help="Ruta al config.yaml del negocio.")
    parser.add_argument("--dry-run", action="store_true", help="No escribe ni mueve nada; solo informa.")
    parser.add_argument("--solo", default=None, help="Procesa solo el archivo indicado (nombre exacto).")
    parser.add_argument("--limite", type=int, default=None, help="Procesa como máximo N comprobantes.")
    parser.add_argument("--verbose", action="store_true", help="Log detallado (DEBUG) también en consola.")
    args = parser.parse_args(argv)

    ruta_config = pathlib.Path(args.config)
    config = cargar_config(ruta_config)

    # Registro lee el modo de prueba de config['dry_run'] para decidir si
    # escribe en los Sheets reales o en salida/*.csv. Sin esta línea,
    # --dry-run seguiría llamando a la API de Google: la bandera se
    # propaga aquí, en un solo lugar, y no como parámetro suelto.
    config["dry_run"] = args.dry_run

    carpeta_salida = pathlib.Path("salida")
    configurar_logging(carpeta_salida, args.verbose)

    raiz = pathlib.Path(config["drive"]["raiz"])
    carpeta_buzon = raiz / config["drive"]["buzon"]
    carpeta_procesado = raiz / config["drive"]["procesado"]
    carpeta_revisar = raiz / config["drive"]["revisar"]

    logger.info("Iniciando corrida. Buzón: %s", carpeta_buzon)
    if args.dry_run:
        logger.info("Modo --dry-run: no se escribirá ni se moverá nada.")

    try:
        import auth_google

        servicio_drive_obj = None if args.dry_run else auth_google.servicio_drive()
    except Exception as exc:
        logger.error(str(exc))
        return 1

    try:
        registro = registro_mod.Registro(config)
    except Exception as exc:
        logger.error("No se pudo inicializar el registro (Google Sheets): %s", exc)
        return 1

    ruta_catalogo = pathlib.Path(__file__).resolve().parent / "insumos.csv"
    catalogo_obj = None
    if ruta_catalogo.exists():
        try:
            catalogo_obj = catalogo_mod.Catalogo(ruta_catalogo)
        except Exception as exc:
            logger.warning("No se pudo cargar el catálogo de insumos (%s); se seguirá sin emparejar ítems.", exc)
    else:
        logger.warning("No se encontró '%s'; se seguirá sin emparejar ítems contra el catálogo.", ruta_catalogo)

    resolutor_link = ResolutorLinkDrive(servicio_drive_obj, config)

    archivos = listar_buzon(carpeta_buzon)
    planes = construir_planes(archivos)

    if args.solo:
        objetivo = args.solo.strip().lower()
        planes = [
            plan
            for plan in planes
            if plan[0].name.lower() == objetivo or any(r.name.lower() == objetivo for r in plan[1])
        ]
        if not planes:
            logger.warning("--solo %s: no se encontró ese archivo en el buzón.", args.solo)

    if args.limite is not None:
        planes = planes[: args.limite]

    claves_procesadas_en_lote: set[str] = set()
    resultados: list[ResultadoArchivo] = []

    for principal, respaldos in planes:
        try:
            resultado = procesar_uno(
                principal,
                respaldos,
                config=config,
                registro=registro,
                catalogo_obj=catalogo_obj,
                resolutor_link=resolutor_link,
                claves_procesadas_en_lote=claves_procesadas_en_lote,
                carpeta_procesado=carpeta_procesado,
                carpeta_revisar=carpeta_revisar,
                dry_run=args.dry_run,
            )
        except Exception as exc:
            # Red de seguridad final: un archivo que falla de cualquier forma
            # no inesperada no debe detener el lote.
            logger.exception("Error inesperado procesando '%s'", principal)
            motivo = f"error inesperado: {exc}"
            mover_a_revisar([principal, *respaldos], carpeta_revisar, motivo, args.dry_run)
            resultado = ResultadoArchivo(principal.name, "revisar", motivo)

        logger.info("%s: %s%s", resultado.nombre, resultado.estado, f" ({resultado.motivo})" if resultado.motivo else "")
        resultados.append(resultado)

    imprimir_resumen(resultados)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
