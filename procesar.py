"""Orquestador: una pasada sobre el buzón de comprobantes (00_BUZON) en
Google Drive, hablando directo con la API de Drive (ver almacen_drive.py).

Por cada archivo del buzón: lo descarga a un temporal, lo clasifica por
extensión, lo extrae (XML UBL de forma determinística, o PDF/imagen vía el
modelo de Claude), le asigna una empresa propia por el RUC del cliente, lo
deduplica contra lo ya registrado, empareja sus ítems contra el catálogo de
insumos, lo escribe en los Google Sheets del negocio y por último mueve el
archivo (por API, dentro de Drive) a 01_PROCESADO/AAAA-MM/EMPRESA/ (o a
02_REVISAR/ con un motivo, si algo no cuadra). Ningún archivo se borra
nunca.

Uso:
    C:\\Python312\\python.exe procesar.py --config config.yaml
    C:\\Python312\\python.exe procesar.py --config config.yaml --dry-run --verbose
    C:\\Python312\\python.exe procesar.py --solo F001-123.xml
    C:\\Python312\\python.exe procesar.py --limite 15

Nota sobre --dry-run: como listar y descargar el buzón son llamadas a la
API de Drive (ya no lectura de disco local), --dry-run SIGUE necesitando
credenciales de Google válidas. Lo que evita son las operaciones de
escritura: mover archivos, crear subcarpetas y crear los .motivo.txt.

Ver SKILL.md para la arquitectura completa y las trampas conocidas.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime
import logging
import pathlib
import re
import sys
import tempfile
from typing import TYPE_CHECKING

import yaml

from almacen_drive import AlmacenDrive

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
# comprobantes: son artefactos de Office o de una sincronización local de
# Drive para escritorio que alguien deje corriendo sobre la misma carpeta,
# aunque el propio skill ya no dependa de ella).
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


@dataclasses.dataclass(frozen=True)
class ArchivoDrive:
    """Un archivo del buzón, tal como lo reporta AlmacenDrive.listar().

    Expone .name/.stem/.suffix igual que pathlib.Path a propósito: así
    construir_planes() (el agrupado XML+PDF por nombre) no cambia su
    algoritmo, solo el tipo que fluye por él.
    """

    id: str
    name: str
    mime_type: str = ""
    size: int | None = None

    @property
    def stem(self) -> str:
        return pathlib.PurePosixPath(self.name).stem

    @property
    def suffix(self) -> str:
        return pathlib.PurePosixPath(self.name).suffix


# -----------------------------------------------------------------------------
# Listado y agrupación del buzón
# -----------------------------------------------------------------------------
def listar_buzon(almacen: AlmacenDrive, carpeta_buzon_id: str) -> list[ArchivoDrive]:
    """Lista los archivos directos de la carpeta de buzón en Drive.

    AlmacenDrive.listar() ya excluye carpetas y archivos en papelera; acá
    se aplica además el filtro de nombres que nunca son comprobantes
    (artefactos de Office o de la sincronización de escritorio, por si
    alguno llega a aparecer igual dentro de la carpeta de Drive).
    """
    crudos = almacen.listar(carpeta_buzon_id)
    archivos = []
    for f in crudos:
        nombre = f["name"]
        if nombre.lower() in NOMBRES_IGNORADOS:
            continue
        if nombre.startswith(PREFIJOS_IGNORADOS):
            continue
        archivos.append(ArchivoDrive(id=f["id"], name=nombre, mime_type=f.get("mimeType", ""), size=f.get("size")))
    return sorted(archivos, key=lambda a: a.name.lower())


def construir_planes(
    archivos: list[ArchivoDrive],
) -> list[tuple[ArchivoDrive, list[ArchivoDrive]]]:
    """Agrupa los archivos del buzón por nombre (sin extensión).

    Si un grupo contiene exactamente un XML/ZIP, ese es el comprobante
    principal y el resto del grupo se trata como respaldo enlazado (no se
    procesa por separado: "XML gana siempre"). En cualquier otro caso
    (ningún XML en el grupo, o más de uno) cada archivo se procesa de forma
    independiente, porque sin un XML de por medio no hay forma barata y
    segura de confirmar que dos archivos son el mismo comprobante.
    """
    grupos: dict[str, list[ArchivoDrive]] = {}
    for archivo in archivos:
        grupos.setdefault(archivo.stem.lower(), []).append(archivo)

    planes: list[tuple[ArchivoDrive, list[ArchivoDrive]]] = []
    for archivos_grupo in grupos.values():
        xmls = [a for a in archivos_grupo if a.suffix.lower() in EXT_XML]
        if len(xmls) == 1:
            principal = xmls[0]
            respaldos = [a for a in archivos_grupo if a != principal]
            planes.append((principal, respaldos))
        else:
            for a in archivos_grupo:
                planes.append((a, []))

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


def nombre_destino_unico(nombres_existentes: set[str], nombre: str) -> str:
    """Igual que antes (ruta_destino_unica), pero contra un set de nombres
    ya presentes en la carpeta destino en vez de Path.exists(): la carpeta
    destino ahora es una carpeta de Drive identificada por id, no una ruta
    local."""
    if nombre not in nombres_existentes:
        return nombre
    stem = pathlib.PurePosixPath(nombre).stem
    ext = pathlib.PurePosixPath(nombre).suffix
    contador = 2
    while True:
        candidato = f"{stem}_{contador}{ext}"
        if candidato not in nombres_existentes:
            return candidato
        contador += 1


def _nombre_unico_en_carpeta(
    almacen: AlmacenDrive,
    carpeta_destino_id: str,
    nombre_deseado: str,
    nombres_por_carpeta: dict[str, set[str]],
) -> str:
    """Calcula (y reserva) un nombre único dentro de carpeta_destino_id,
    usando nombres_por_carpeta como caché en memoria de "qué nombres ya hay
    en cada carpeta destino durante esta corrida".

    El caché se puebla de forma perezosa (un almacen.listar() la primera
    vez que se toca cada carpeta destino, no uno por archivo) y evita
    depender de la consistencia inmediata de Drive tras cada movimiento:
    dentro de una misma corrida, dos archivos que caen en la misma carpeta
    nunca chocan, porque el nombre reservado por el primero queda anotado
    en memoria antes de procesar el segundo.
    """
    if carpeta_destino_id not in nombres_por_carpeta:
        nombres_por_carpeta[carpeta_destino_id] = {a["name"] for a in almacen.listar(carpeta_destino_id)}
    existentes = nombres_por_carpeta[carpeta_destino_id]
    nombre_final = nombre_destino_unico(existentes, nombre_deseado)
    existentes.add(nombre_final)
    return nombre_final


def mover_archivo(
    almacen: AlmacenDrive | None,
    archivo: ArchivoDrive,
    carpeta_destino_id: str,
    nombre_deseado: str,
    nombres_por_carpeta: dict[str, set[str]],
    dry_run: bool,
) -> str | None:
    """Mueve 'archivo' (por su id de Drive) a 'carpeta_destino_id' sin
    sobrescribir nunca nada. Devuelve el nombre final usado, o None si el
    movimiento falló (el archivo queda intacto en su ubicación original; el
    llamador debe limitarse a registrar el problema, nunca reintentar
    borrando nada).

    En dry-run, carpeta_destino_id puede ser una clave sintética (no un id
    real de Drive: no se crearon las subcarpetas para no escribir nada) —
    solo se usa como llave del caché nombres_por_carpeta y para el mensaje
    de log, nunca se llama a Drive.
    """
    if dry_run:
        existentes = nombres_por_carpeta.setdefault(carpeta_destino_id, set())
        nombre_final = nombre_destino_unico(existentes, nombre_deseado)
        existentes.add(nombre_final)
        logger.info("[DRY-RUN] se movería %s -> carpeta '%s' como '%s'", archivo.name, carpeta_destino_id, nombre_final)
        return nombre_final

    try:
        nombre_final = _nombre_unico_en_carpeta(almacen, carpeta_destino_id, nombre_deseado, nombres_por_carpeta)
        almacen.mover(archivo.id, carpeta_destino_id, nombre_final)
        return nombre_final
    except Exception as exc:
        logger.error(
            "No se pudo mover '%s' a la carpeta '%s': %s. El archivo queda en el buzón; la "
            "deduplicación evitará que se registre dos veces en la próxima corrida.",
            archivo.name,
            carpeta_destino_id,
            exc,
        )
        return None


def mover_a_revisar(
    almacen: AlmacenDrive | None,
    archivos: list[ArchivoDrive],
    carpeta_revisar_id: str,
    motivo: str,
    nombres_por_carpeta: dict[str, set[str]],
    dry_run: bool,
) -> None:
    for archivo in archivos:
        nombre_final = mover_archivo(almacen, archivo, carpeta_revisar_id, archivo.name, nombres_por_carpeta, dry_run)
        if nombre_final is None:
            continue
        nombre_motivo = f"{nombre_final}.motivo.txt"
        if dry_run:
            logger.info("[DRY-RUN] se escribiría %s con motivo: %s", nombre_motivo, motivo)
        else:
            almacen.crear_texto(carpeta_revisar_id, nombre_motivo, motivo)


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
    principal: ArchivoDrive,
    respaldos: list[ArchivoDrive],
    *,
    config: dict,
    registro,
    catalogo_obj,
    almacen: AlmacenDrive,
    claves_procesadas_en_lote: set[str],
    nombres_por_carpeta: dict[str, set[str]],
    carpeta_procesado_id: str,
    carpeta_revisar_id: str,
    dry_run: bool,
) -> ResultadoArchivo:
    nombre = principal.name
    todos_los_archivos = [principal, *respaldos]
    extension = principal.suffix.lower()

    if extension in EXT_HEIC:
        mover_a_revisar(almacen, todos_los_archivos, carpeta_revisar_id, MOTIVO_HEIC, nombres_por_carpeta, dry_run)
        return ResultadoArchivo(nombre, "revisar", MOTIVO_HEIC)

    extensiones_soportadas = EXT_XML | EXT_PDF | EXT_IMAGEN
    if extension not in extensiones_soportadas:
        motivo = f"extensión no soportada: {extension or '(sin extensión)'}"
        mover_a_revisar(almacen, todos_los_archivos, carpeta_revisar_id, motivo, nombres_por_carpeta, dry_run)
        return ResultadoArchivo(nombre, "revisar", motivo)

    with tempfile.TemporaryDirectory(prefix="sconcha_") as carpeta_temporal:
        ruta_local = pathlib.Path(carpeta_temporal) / principal.name
        try:
            almacen.descargar(principal.id, ruta_local)
        except Exception as exc:
            motivo = f"no se pudo descargar el archivo desde Drive: {exc}"
            logger.exception("Error al descargar '%s' de Drive", principal.name)
            mover_a_revisar(almacen, todos_los_archivos, carpeta_revisar_id, motivo, nombres_por_carpeta, dry_run)
            return ResultadoArchivo(nombre, "revisar", motivo)

        try:
            comp, llamadas_modelo = extraer_comprobante(ruta_local, extension, config)
        except Exception as exc:
            motivo = f"error al extraer datos del comprobante: {exc}"
            logger.exception("Error al extraer '%s'", principal.name)
            mover_a_revisar(almacen, todos_los_archivos, carpeta_revisar_id, motivo, nombres_por_carpeta, dry_run)
            return ResultadoArchivo(nombre, "revisar", motivo)

        try:
            problemas = comp.validar() or []
        except Exception as exc:
            problemas = [f"no se pudo validar el comprobante: {exc}"]

        if problemas:
            motivo = "datos incompletos o inválidos: " + "; ".join(problemas)
            mover_a_revisar(almacen, todos_los_archivos, carpeta_revisar_id, motivo, nombres_por_carpeta, dry_run)
            return ResultadoArchivo(nombre, "revisar", motivo, llamadas_modelo=llamadas_modelo)

        empresa_cfg, motivo_empresa = resolver_empresa(config, comp)
        if empresa_cfg is None:
            mover_a_revisar(almacen, todos_los_archivos, carpeta_revisar_id, motivo_empresa, nombres_por_carpeta, dry_run)
            return ResultadoArchivo(nombre, "revisar", motivo_empresa, llamadas_modelo=llamadas_modelo)

        local, motivo_local = resolver_local(empresa_cfg)
        if local is None:
            mover_a_revisar(almacen, todos_los_archivos, carpeta_revisar_id, motivo_local, nombres_por_carpeta, dry_run)
            return ResultadoArchivo(nombre, "revisar", motivo_local, llamadas_modelo=llamadas_modelo)

        try:
            clave = comp.clave()
        except Exception as exc:
            motivo = f"no se pudo calcular la clave de deduplicación: {exc}"
            mover_a_revisar(almacen, todos_los_archivos, carpeta_revisar_id, motivo, nombres_por_carpeta, dry_run)
            return ResultadoArchivo(nombre, "revisar", motivo, llamadas_modelo=llamadas_modelo)

        if clave in claves_procesadas_en_lote or clave in registro.claves_existentes():
            motivo = f"comprobante duplicado (clave {clave} ya registrada)"
            mover_a_revisar(almacen, todos_los_archivos, carpeta_revisar_id, motivo, nombres_por_carpeta, dry_run)
            return ResultadoArchivo(nombre, "duplicado", motivo, llamadas_modelo=llamadas_modelo)

        emparejar_items(comp, catalogo_obj)

        nombre_final_deseado = nombre_destino(comp, extension)

        # El link de Drive ya no es "mejor esfuerzo": sale directo de los
        # metadatos del propio archivo. Es una lectura, no una escritura, así
        # que se obtiene también en --dry-run (a diferencia del código viejo,
        # que lo saltaba en dry-run porque antes dependía de una búsqueda por
        # nombre que sí tenía sentido evitar).
        try:
            link_drive = almacen.enlace(principal.id)
        except Exception as exc:
            logger.warning("No se pudo obtener el link de Drive de '%s': %s", principal.name, exc)
            link_drive = ""

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
        registro.escribir(comp, empresa_cfg["nombre_corto"], local, link_drive, nombre_final_deseado)

        claves_procesadas_en_lote.add(clave)

        # Las subcarpetas AAAA-MM/EMPRESA dentro de 01_PROCESADO se crean
        # sobre la marcha. En dry-run NO se llama a asegurar_carpeta (eso
        # crearía carpetas, y --dry-run no debe crear nada): se usa una
        # clave sintética solo para el caché de nombres y el mensaje de log.
        nombre_mes = anio_mes(comp)
        nombre_empresa = nombre_empresa_carpeta(empresa_cfg["nombre_corto"])
        if dry_run:
            carpeta_destino_id = f"[DRY-RUN] {carpeta_procesado_id}/{nombre_mes}/{nombre_empresa}"
        else:
            carpeta_mes_id = almacen.asegurar_carpeta(nombre_mes, carpeta_procesado_id)
            carpeta_destino_id = almacen.asegurar_carpeta(nombre_empresa, carpeta_mes_id)

        destino_nombre = mover_archivo(almacen, principal, carpeta_destino_id, nombre_final_deseado, nombres_por_carpeta, dry_run)
        if destino_nombre is None:
            logger.error(
                "'%s' se registró correctamente pero no se pudo mover; queda en el buzón.",
                principal.name,
            )
        for respaldo in respaldos:
            nombre_respaldo = nombre_destino(comp, respaldo.suffix.lower())
            mover_archivo(almacen, respaldo, carpeta_destino_id, nombre_respaldo, nombres_por_carpeta, dry_run)

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

    carpetas_cfg = config.get("drive", {}).get("carpetas") or {}
    carpeta_buzon_id = (carpetas_cfg.get("buzon") or "").strip()
    carpeta_procesado_id = (carpetas_cfg.get("procesado") or "").strip()
    carpeta_revisar_id = (carpetas_cfg.get("revisar") or "").strip()

    if not (carpeta_buzon_id and carpeta_procesado_id and carpeta_revisar_id):
        logger.error(
            "config.yaml no tiene los IDs de las carpetas de Drive (drive.carpetas.buzon/"
            "procesado/revisar). Corre 'init_negocio.py --config %s' primero.",
            ruta_config,
        )
        return 1

    logger.info("Iniciando corrida. Buzón (Drive id): %s", carpeta_buzon_id)
    if args.dry_run:
        logger.info(
            "Modo --dry-run: no se escribirá en los Sheets ni se moverá/creará nada en Drive. "
            "Sí se listará y descargará el buzón (lectura) para previsualizar el resultado."
        )

    try:
        import auth_google

        # Se llama SIEMPRE, incluso en --dry-run: listar y descargar el
        # buzón son lecturas contra la API de Drive, ya no lectura de disco
        # local. Lo que --dry-run evita son las escrituras (mover, crear
        # carpetas, crear los .motivo.txt), no la autenticación en sí.
        servicio_drive_obj = auth_google.servicio_drive()
    except Exception as exc:
        logger.error(str(exc))
        return 1

    almacen = AlmacenDrive(servicio_drive_obj)

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

    archivos = listar_buzon(almacen, carpeta_buzon_id)
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
    nombres_por_carpeta: dict[str, set[str]] = {}
    resultados: list[ResultadoArchivo] = []

    for principal, respaldos in planes:
        try:
            resultado = procesar_uno(
                principal,
                respaldos,
                config=config,
                registro=registro,
                catalogo_obj=catalogo_obj,
                almacen=almacen,
                claves_procesadas_en_lote=claves_procesadas_en_lote,
                nombres_por_carpeta=nombres_por_carpeta,
                carpeta_procesado_id=carpeta_procesado_id,
                carpeta_revisar_id=carpeta_revisar_id,
                dry_run=args.dry_run,
            )
        except Exception as exc:
            # Red de seguridad final: un archivo que falla de cualquier forma
            # no inesperada no debe detener el lote.
            logger.exception("Error inesperado procesando '%s'", principal.name)
            motivo = f"error inesperado: {exc}"
            mover_a_revisar(almacen, [principal, *respaldos], carpeta_revisar_id, motivo, nombres_por_carpeta, args.dry_run)
            resultado = ResultadoArchivo(principal.name, "revisar", motivo)

        logger.info("%s: %s%s", resultado.nombre, resultado.estado, f" ({resultado.motivo})" if resultado.motivo else "")
        resultados.append(resultado)

    imprimir_resumen(resultados)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
