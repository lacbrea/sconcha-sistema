"""Registro de comprobantes procesados en las dos Google Sheets del skill.

Escribe una fila por comprobante en el sheet "contable" y una fila por linea
de item en el sheet "detalle". Ver Registro para el detalle de las reglas.

Interfaz de autenticacion esperada (la crea otro agente en auth_google.py,
no se importa a nivel de modulo para que este archivo se pueda importar y
usar en modo dry_run sin que auth_google.py exista todavia):

    def servicio_sheets()  # -> Resource de googleapiclient para sheets v4
"""
from __future__ import annotations

import csv
import datetime
import pathlib
from typing import TYPE_CHECKING, Any

from catalogo import Catalogo

if TYPE_CHECKING:  # pragma: no cover - solo para chequeo de tipos, no en runtime.
    from esquema import ComprobanteExtraido

RAIZ = pathlib.Path(__file__).resolve().parent

# --- Columnas del sheet contable ---------------------------------------------
# Las primeras 18 replican EXACTO el registro historico REGISTRO
# COMPROBANTES.xlsx (hoja 'COMPROBANTES') para que build_conciliacion.py siga
# consumiendolo sin cambios.
#
# OJO - columna 15: el Excel historico la llama LINK_COMPROBANTE, pero el
# motor de conciliacion (CONCILIACION/skill/scripts/build_conciliacion.py,
# lineas 732 y 742) lee la clave 'LINK_DRIVE'. Usamos aqui el nombre que
# espera el motor a proposito, para eliminar esa trampa de raiz. NO renombrar
# esta columna a LINK_COMPROBANTE "para que coincida con el Excel viejo": eso
# es exactamente el bug que se evito al escribir esto.
# OBSERVACIONES (columna 18) se escribe SIEMPRE VACIA al registrar: en el
# registro real de junio esa columna tiene notas humanas de valor (ej. el
# analisis completo del descuadre de compras del terminal pesquero), no
# trazabilidad de maquina. Mezclar un "Archivo: xxx.pdf" automatico ahi
# obligaria a quien anota a mano a escribir a continuacion del texto del
# skill. La trazabilidad del archivo origen va en ARCHIVO (columna nueva,
# al final), separada de las notas humanas — mismo criterio que ADVERTENCIAS
# (del skill) vs OBSERVACIONES (humana).
COLUMNAS_CONTABLE = [
    "FECHA_EMISION", "EMPRESA", "LOCAL", "PROVEEDOR", "RUC", "TIPO",
    "SERIE_NUMERO", "SUBTOTAL", "IGV", "TOTAL", "CONDICION", "ESTADO_PAGO",
    "FECHA_PAGO", "CAJA_CHICA", "LINK_DRIVE", "REGISTRADO_POR",
    "FECHA_REGISTRO", "OBSERVACIONES",
    # columnas nuevas, no existen en el Excel historico.
    "MONEDA", "TIPO_CAMBIO", "FECHA_VENCIMIENTO", "DETRACCION_PCT",
    "DETRACCION_MONTO", "RETENCION", "ICBPER", "DESCUENTO_GLOBAL",
    "CLIENTE_RUC", "DOC_REFERENCIA", "ORIGEN", "CONFIANZA", "ADVERTENCIAS",
    "ARCHIVO",
]

COLUMNAS_DETALLE = [
    "FECHA_EMISION", "EMPRESA", "LOCAL", "RUC", "SERIE_NUMERO", "ORDEN",
    "DESCRIPCION_FACTURA", "INSUMO", "CATEGORIA", "CANTIDAD", "UNIDAD",
    "PRECIO_UNITARIO", "TOTAL_LINEA", "MATCH", "FECHA_REGISTRO",
]

# --- Pestaña RESPALDOS_CAJA ---------------------------------------------------
# Respaldos de gastos de caja chica (carpeta NOTAS_DE_VENTA del buzón, ver
# config.yaml -> drive.carpetas.buzon_tipos): fotos/PDF/xls de boletas de
# compras menores que NO se leen con el modelo -el dato de fondo (montos,
# insumos) vive en el reporte de egresos del sistema de ventas, no en este
# archivo-, así que solo se deja constancia de que el respaldo llegó. Va en
# una pestaña propia del MISMO spreadsheet contable (no un spreadsheet
# nuevo): es el mismo negocio, y un spreadsheet aparte solo agregaría un ID
# más que sincronizar en config.yaml sin necesidad real.
COLUMNAS_RESPALDOS_CAJA = ["FECHA", "EMPRESA", "LOCAL", "ARCHIVO", "LINK_DRIVE", "FECHA_REGISTRO"]

NOMBRE_HOJA_RESPALDOS_CAJA = "RESPALDOS_CAJA"

REGISTRADO_POR = "skill-comprobantes"


def _clave(ruc: Any, serie_numero: Any, total: Any) -> str | None:
    """'RUC|SERIE_NUMERO|TOTAL con 2 decimales', mismo formato que
    ComprobanteExtraido.clave() (esquema.py): ruc y serie en mayusculas y sin
    espacios, para que una clave leida del sheet siempre calce con la que
    genera comp.clave() aunque el dato venga con espacios o minusculas.
    Devuelve None si falta algun dato o TOTAL no es numerico (fila
    vacia/cabecera/basura)."""
    ruc_s = str(ruc or "").strip().upper().replace(" ", "")
    serie_s = str(serie_numero or "").strip().upper().replace(" ", "")
    if not ruc_s or not serie_s:
        return None
    try:
        total_f = float(str(total).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return f"{ruc_s}|{serie_s}|{total_f:.2f}"


def _clave_respaldo(archivo: Any, empresa: Any) -> str | None:
    """'ARCHIVO|EMPRESA' en mayúsculas y sin espacios, para que
    registrar_respaldo_caja() sea idempotente: registrar el mismo archivo dos
    veces para la misma empresa no duplica fila en RESPALDOS_CAJA.

    A diferencia de _clave() (RUC+SERIE+TOTAL), un respaldo de caja chica no
    tiene esos datos -no se lee con el modelo-, así que la única pareja de
    campos que dos registros del mismo archivo van a compartir siempre es su
    nombre y la empresa a la que se asignó. EMPRESA puede legítimamente venir
    vacía (ver resolver_empresa_local_nota_venta en procesar.py, cuando el
    negocio tiene más de una empresa y no se puede asignar automáticamente);
    solo ARCHIVO es obligatorio para calcular la clave.
    """
    archivo_s = str(archivo or "").strip().upper()
    empresa_s = str(empresa or "").strip().upper()
    if not archivo_s:
        return None
    return f"{archivo_s}|{empresa_s}"


class Registro:
    """Escribe comprobantes y sus items en los sheets contable y de detalle.

    config esperado (claves usadas por esta clase; el resto lo puede usar
    otro modulo del skill sin problema):

        {
          "sheets": {
              "contable": "<spreadsheet id>",
              "detalle": "<spreadsheet id>",
              "rango_contable": "A1:AF" (opcional, default "A1:AF"),
              "rango_detalle": "A1:O" (opcional, default "A1:O"),
          },
          "dry_run": True/False,
          "catalogo_csv": "ruta/a/insumos.csv" (opcional, default insumos.csv
              junto a este archivo),
          "salida_dir": "ruta/a/salida" (opcional, solo dry_run, default
              'salida' junto a este archivo),
        }

    Decision propia (no especificada en el contrato): ItemExtraido no trae
    insumo/categoria/score. procesar.py (otro agente) ya resuelve cada item
    contra el catalogo antes de llamar a escribir() y deja el resultado como
    atributos dinamicos en el item (insumo_catalogo/categoria_catalogo/
    confianza_match) — ver _resolver_match(). Por eso Registro tambien
    instancia su propio Catalogo: como red de respaldo, para poder resolver
    el item si esos atributos no vienen puestos (uso standalone, tests, o
    procesar.py sin catalogo cargado). La ruta del CSV es configurable via
    config['catalogo_csv'] por si el skill quiere apuntar a otro catalogo.
    """

    def __init__(self, config: dict, servicio: Any = None):
        self.config = config
        self.dry_run = bool(config.get("dry_run"))

        sheets_cfg = config.get("sheets") or {}
        self.id_contable = sheets_cfg.get("contable")
        self.id_detalle = sheets_cfg.get("detalle")
        # Rango amplio de columnas (31 en contable, 15 en detalle) sin fijar
        # cuantas filas: Sheets API acepta un rango abierto tipo 'A1:AF'
        # como destino de values().append/get, y usa la primera hoja del
        # spreadsheet cuando el rango no lleva nombre de hoja.
        self._rango_contable = sheets_cfg.get("rango_contable", "A1:AF")
        self._rango_detalle = sheets_cfg.get("rango_detalle", "A1:O")
        # RESPALDOS_CAJA vive en una pestaña propia del MISMO spreadsheet
        # contable (self.id_contable), así que su rango SÍ lleva el nombre de
        # hoja ("HOJA!A1:F"): a diferencia de rango_contable/rango_detalle,
        # que apuntan a la primera hoja de sus respectivos spreadsheets, acá
        # hace falta decirle a la API a cuál de las varias pestañas del mismo
        # spreadsheet escribir.
        self._rango_respaldos_caja = f"{NOMBRE_HOJA_RESPALDOS_CAJA}!A1:F"

        # Servicio de Sheets: si se inyecta (tests, doble de prueba) se usa
        # tal cual; si no, se crea de forma perezosa la primera vez que hace
        # falta, para que dry_run funcione sin que auth_google.py exista.
        self._servicio = servicio

        catalogo_csv = config.get("catalogo_csv")
        ruta_csv = pathlib.Path(catalogo_csv) if catalogo_csv else RAIZ / "insumos.csv"
        self.catalogo = Catalogo(ruta_csv)

        salida_dir = pathlib.Path(config.get("salida_dir", RAIZ / "salida"))
        self._csv_contable = salida_dir / "contable.csv"
        self._csv_detalle = salida_dir / "detalle.csv"
        self._csv_respaldos_caja = salida_dir / "respaldos_caja.csv"

    # -- API publica -----------------------------------------------------

    def claves_existentes(self) -> set[str]:
        """Claves ('RUC|SERIE_NUMERO|TOTAL') ya presentes en el sheet contable
        (o en salida/contable.csv si dry_run), para que el skill evite
        reprocesar un comprobante ya registrado."""
        if self.dry_run:
            return self._claves_desde_csv(self._csv_contable)

        servicio = self._obtener_servicio()
        resp = (
            servicio.spreadsheets()
            .values()
            .get(spreadsheetId=self.id_contable, range=self._rango_contable)
            .execute()
        )
        return self._claves_desde_filas(resp.get("values", []))

    def escribir(
        self,
        comp: "ComprobanteExtraido",
        empresa: str,
        local: str,
        link_drive: str,
        archivo: str,
    ) -> None:
        """Registra un comprobante: primero sus items en el sheet de detalle,
        despues la fila del sheet contable.

        Orden deliberado (items primero, contable al final): claves_existentes()
        solo lee el sheet CONTABLE. Si el proceso falla a media escritura
        (por ejemplo la llamada a la API de items funciona pero la de la fila
        contable falla), la clave del comprobante NO queda registrada y la
        siguiente corrida lo vuelve a intentar completo — el hueco es
        detectable y se autocorrige. El costo es que, en el caso mas raro de
        que falle justo despues de escribir la fila contable, un reintento
        podria duplicar los items; se prefiere ese riesgo (items duplicados,
        visibles y corregibles) al alternativo (fila contable "fantasma"
        marcada como registrada con items que nunca llegaron, un hueco
        silencioso en el inventario).
        """
        fecha_registro = datetime.datetime.now().isoformat(timespec="seconds")

        filas_detalle = self._filas_detalle(comp, empresa, local, fecha_registro)
        if filas_detalle:
            self._append(self.id_detalle, self._rango_detalle, COLUMNAS_DETALLE, filas_detalle, self._csv_detalle)

        fila_contable = self._fila_contable(comp, empresa, local, link_drive, archivo, fecha_registro)
        self._append(self.id_contable, self._rango_contable, COLUMNAS_CONTABLE, [fila_contable], self._csv_contable)

    def respaldos_existentes(self) -> set[str]:
        """Claves ('ARCHIVO|EMPRESA') ya presentes en la pestaña RESPALDOS_CAJA
        del spreadsheet contable (o en salida/respaldos_caja.csv si dry_run),
        para que registrar_respaldo_caja() sea idempotente.

        Si la pestaña todavía no existe (negocio recién migrado a
        NOTAS_DE_VENTA, primera corrida) la API devuelve un error al pedir un
        rango de una hoja inexistente; se interpreta como "todavía no hay
        nada registrado" en vez de propagar la excepción, porque
        _asegurar_hoja_respaldos_caja() la crea de todas formas en el primer
        registrar_respaldo_caja() que corra.
        """
        if self.dry_run:
            return self._respaldos_desde_csv(self._csv_respaldos_caja)

        servicio = self._obtener_servicio()
        try:
            resp = (
                servicio.spreadsheets()
                .values()
                .get(spreadsheetId=self.id_contable, range=self._rango_respaldos_caja)
                .execute()
            )
        except Exception:
            return set()
        return self._respaldos_desde_filas(resp.get("values", []))

    def registrar_respaldo_caja(
        self, fecha: str, empresa: str, local: str, archivo: str, link_drive: str
    ) -> bool:
        """Registra un respaldo de caja chica (NOTAS_DE_VENTA) en la pestaña
        RESPALDOS_CAJA del spreadsheet contable, SIN pasar por el modelo:
        costo S/0. El dato de fondo (montos, insumos) vive en el reporte de
        egresos del sistema de ventas — esto solo deja constancia de que el
        respaldo llegó, cuándo y a qué empresa/local se asignó.

        Idempotente por ARCHIVO+EMPRESA: si ya hay una fila con esa
        combinación, no escribe una nueva. Devuelve True si escribió una fila
        nueva, False si ya estaba registrado.
        """
        clave = _clave_respaldo(archivo, empresa)
        if clave is not None and clave in self.respaldos_existentes():
            return False

        fecha_registro = datetime.datetime.now().isoformat(timespec="seconds")
        fila = [fecha, empresa, local, archivo, link_drive, fecha_registro]

        if self.dry_run:
            self._append_csv(self._csv_respaldos_caja, COLUMNAS_RESPALDOS_CAJA, [fila])
            return True

        servicio = self._obtener_servicio()
        self._asegurar_hoja_respaldos_caja(servicio)
        self._asegurar_cabecera(servicio, self.id_contable, self._rango_respaldos_caja, COLUMNAS_RESPALDOS_CAJA)
        servicio.spreadsheets().values().append(
            spreadsheetId=self.id_contable,
            range=self._rango_respaldos_caja,
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [fila]},
        ).execute()
        return True

    def _asegurar_hoja_respaldos_caja(self, servicio) -> None:
        """Crea la pestaña RESPALDOS_CAJA en el spreadsheet contable si
        todavía no existe. Idempotente: consulta la lista de pestañas y solo
        crea si NOMBRE_HOJA_RESPALDOS_CAJA no aparece en ella."""
        info = (
            servicio.spreadsheets()
            .get(spreadsheetId=self.id_contable, fields="sheets.properties.title")
            .execute()
        )
        titulos = {hoja["properties"]["title"] for hoja in info.get("sheets", [])}
        if NOMBRE_HOJA_RESPALDOS_CAJA in titulos:
            return
        servicio.spreadsheets().batchUpdate(
            spreadsheetId=self.id_contable,
            body={"requests": [{"addSheet": {"properties": {"title": NOMBRE_HOJA_RESPALDOS_CAJA}}}]},
        ).execute()

    # -- Construccion de filas -------------------------------------------

    def _filas_detalle(self, comp, empresa: str, local: str, fecha_registro: str) -> list[list[Any]]:
        filas = []
        for item in comp.items:
            insumo, categoria, score = self._resolver_match(item)
            match_val = f"{score:.2f}" if insumo is not None else "SIN MATCH"
            filas.append(
                [
                    comp.fecha_emision,
                    empresa,
                    local,
                    comp.proveedor_ruc,
                    comp.serie_numero,
                    item.orden,
                    item.descripcion,
                    insumo or "",
                    categoria or "",
                    item.cantidad,
                    item.unidad,
                    item.precio_unitario,
                    item.total_linea,
                    match_val,
                    fecha_registro,
                ]
            )
        return filas

    # Atributo centinela para distinguir "procesar.py ya calculo el match y
    # lo guardo en el item" (aunque el valor guardado sea None) de "nadie lo
    # calculo todavia".
    _SIN_CALCULAR = object()

    def _resolver_match(self, item) -> tuple[str | None, str | None, float]:
        """Devuelve (insumo, categoria, score) para un item del comprobante.

        Integracion con procesar.py (otro agente, ya escrito): antes de
        llamar a Registro.escribir(), procesar.emparejar_items() ya corrio
        Catalogo.emparejar() sobre cada item y guardo el resultado como
        atributos dinamicos 'insumo_catalogo' / 'categoria_catalogo' /
        'confianza_match' (no son campos declarados de ItemExtraido; ver el
        comentario de emparejar_items en procesar.py). Si esos atributos
        estan presentes se reusan tal cual, para no volver a correr el
        matching dos veces. Si no estan (por ejemplo: Registro usado de
        forma standalone, en tests, o procesar.py no tenia catalogo cargado)
        se calcula aqui con el Catalogo propio de esta instancia, para que
        escribir() siga funcionando de forma autonoma.
        """
        insumo = getattr(item, "insumo_catalogo", self._SIN_CALCULAR)
        if insumo is self._SIN_CALCULAR:
            return self.catalogo.emparejar(item.descripcion)
        categoria = getattr(item, "categoria_catalogo", None)
        score = getattr(item, "confianza_match", 0.0)
        return insumo, categoria, score

    def _fila_contable(
        self,
        comp,
        empresa: str,
        local: str,
        link_drive: str,
        archivo: str,
        fecha_registro: str,
    ) -> list[Any]:
        # ESTADO_PAGO se deja vacio a proposito al registrar: lo llena
        # despues el flujo de pagos. El motor de conciliacion SOLO cruza
        # filas con ESTADO_PAGO == 'PAGADA' (build_conciliacion.py, linea
        # 484) — un comprobante recien registrado, todavia sin pagar, no
        # debe aparecer como conciliable.
        estado_pago = ""
        # FECHA_PAGO y CAJA_CHICA tampoco vienen en ComprobanteExtraido: los
        # completa el mismo flujo de pagos que llena ESTADO_PAGO.
        fecha_pago = ""
        caja_chica = ""
        advertencias = " | ".join(comp.advertencias) if comp.advertencias else ""
        # OBSERVACIONES se deja vacia a proposito al registrar: es una
        # columna de notas humanas (ver comentario junto a COLUMNAS_CONTABLE),
        # no de trazabilidad automatica. El nombre del archivo origen va en
        # la columna ARCHIVO, al final.
        observaciones = ""

        return [
            comp.fecha_emision,
            empresa,
            local,
            comp.proveedor_razon_social,
            comp.proveedor_ruc,
            comp.tipo_documento,
            comp.serie_numero,
            comp.subtotal,
            comp.igv,
            comp.total,
            comp.condicion,
            estado_pago,
            fecha_pago,
            caja_chica,
            link_drive,
            REGISTRADO_POR,
            fecha_registro,
            observaciones,
            comp.moneda,
            comp.tipo_cambio,
            comp.fecha_vencimiento,
            comp.detraccion_pct,
            comp.detraccion_monto,
            comp.retencion,
            comp.icbper,
            comp.descuento_global,
            comp.cliente_ruc,
            comp.documento_referencia,
            comp.origen,
            comp.confianza,
            advertencias,
            archivo,
        ]

    # -- Escritura (sheets reales o CSV en dry_run) -----------------------

    def _append(
        self,
        spreadsheet_id: str | None,
        rango: str,
        columnas: list[str],
        filas: list[list[Any]],
        csv_path: pathlib.Path,
    ) -> None:
        if self.dry_run:
            self._append_csv(csv_path, columnas, filas)
            return

        servicio = self._obtener_servicio()
        self._asegurar_cabecera(servicio, spreadsheet_id, rango, columnas)
        servicio.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=rango,
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": filas},
        ).execute()

    def _asegurar_cabecera(self, servicio, spreadsheet_id: str, rango: str, columnas: list[str]) -> None:
        """Escribe la fila de cabecera si el sheet esta vacio."""
        resp = (
            servicio.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=rango)
            .execute()
        )
        if resp.get("values"):
            return
        servicio.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=rango,
            valueInputOption="USER_ENTERED",
            body={"values": [columnas]},
        ).execute()

    def _append_csv(self, csv_path: pathlib.Path, columnas: list[str], filas: list[list[Any]]) -> None:
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        existe_con_datos = csv_path.exists() and csv_path.stat().st_size > 0
        with csv_path.open("a", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            if not existe_con_datos:
                writer.writerow(columnas)
            for fila in filas:
                writer.writerow(fila)

    # -- Lectura de claves existentes --------------------------------------

    def _obtener_servicio(self):
        if self._servicio is None:
            from auth_google import servicio_sheets

            self._servicio = servicio_sheets()
        return self._servicio

    def _claves_desde_csv(self, csv_path: pathlib.Path) -> set[str]:
        if not csv_path.exists():
            return set()
        claves: set[str] = set()
        with csv_path.open("r", encoding="utf-8", newline="") as f:
            for fila in csv.DictReader(f):
                clave = _clave(fila.get("RUC"), fila.get("SERIE_NUMERO"), fila.get("TOTAL"))
                if clave:
                    claves.add(clave)
        return claves

    def _claves_desde_filas(self, valores: list[list[Any]]) -> set[str]:
        if not valores:
            return set()
        header = valores[0]
        try:
            i_ruc = header.index("RUC")
            i_serie = header.index("SERIE_NUMERO")
            i_total = header.index("TOTAL")
        except ValueError:
            return set()

        claves: set[str] = set()
        for fila in valores[1:]:
            def valor(i: int) -> str:
                return fila[i] if i < len(fila) else ""

            clave = _clave(valor(i_ruc), valor(i_serie), valor(i_total))
            if clave:
                claves.add(clave)
        return claves

    def _respaldos_desde_csv(self, csv_path: pathlib.Path) -> set[str]:
        if not csv_path.exists():
            return set()
        claves: set[str] = set()
        with csv_path.open("r", encoding="utf-8", newline="") as f:
            for fila in csv.DictReader(f):
                clave = _clave_respaldo(fila.get("ARCHIVO"), fila.get("EMPRESA"))
                if clave:
                    claves.add(clave)
        return claves

    def _respaldos_desde_filas(self, valores: list[list[Any]]) -> set[str]:
        if not valores:
            return set()
        header = valores[0]
        try:
            i_archivo = header.index("ARCHIVO")
            i_empresa = header.index("EMPRESA")
        except ValueError:
            return set()

        claves: set[str] = set()
        for fila in valores[1:]:
            def valor(i: int) -> str:
                return fila[i] if i < len(fila) else ""

            clave = _clave_respaldo(valor(i_archivo), valor(i_empresa))
            if clave:
                claves.add(clave)
        return claves
