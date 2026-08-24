"""Tests de conciliar.py.

Corren sin red, sin credenciales y sin tocar Drive/Gmail de verdad:

- El trato con Google Drive (almacen_drive.AlmacenDrive) se sustituye por
  FakeAlmacen, un doble en memoria con la misma interfaz que usan las
  funciones de conciliar.py (listar, descargar, asegurar_carpeta,
  buscar_por_nombre, subir, enlace). El doble del Resource real de
  googleapiclient ya se prueba aparte en tests/test_almacen_drive.py; acá lo
  que se prueba es que conciliar.py orquesta correctamente esas llamadas,
  mismo criterio que usa tests/test_procesar.py con AlmacenDriveFalso.
- auth_google.servicio_drive/servicio_sheets se monkeypatchean para no
  intentar el flujo OAuth real.
- conciliar.invocar_motor (que lanza build_conciliacion.py como subproceso)
  se monkeypatchea para no correr el motor real.
- correo_gmail se sustituye por un módulo doble inyectado en sys.modules
  (conciliar.py lo importa de forma diferida, dentro de main()), para poder
  verificar si se llamó o no sin depender del correo_gmail real.

Correr con:
    C:\\Python312\\python.exe -m pytest tests/test_conciliar.py -q
"""
from __future__ import annotations

import csv
import json
import logging
import pathlib
import sys
import types

import yaml

RAIZ_PROYECTO = pathlib.Path(__file__).resolve().parent.parent
if str(RAIZ_PROYECTO) not in sys.path:
    sys.path.insert(0, str(RAIZ_PROYECTO))

import auth_google  # noqa: E402
import conciliar  # noqa: E402
from registro_sheets import COLUMNAS_CONTABLE  # noqa: E402


# -----------------------------------------------------------------------------
# Doble en memoria de AlmacenDrive (mismo espíritu que AlmacenDriveFalso en
# tests/test_procesar.py, adaptado a lo que necesitan las funciones de
# conciliar.py: listar/descargar por carpeta, asegurar_carpeta idempotente,
# buscar_por_nombre + subir sin sobrescribir, y enlace()).
# -----------------------------------------------------------------------------
class FakeAlmacen:
    def __init__(self):
        self._contador = 0
        self.archivos: dict[str, dict] = {}
        self.carpetas: dict[str, dict] = {}
        self.subidas: list[tuple[str, str]] = []

    def _nuevo_id(self, prefijo: str) -> str:
        self._contador += 1
        return f"{prefijo}-{self._contador}"

    # -- helpers de test para poblar estado -------------------------------
    def agregar_archivo(self, carpeta_id: str, nombre: str, contenido: bytes = b"contenido de prueba") -> str:
        file_id = self._nuevo_id("archivo")
        self.archivos[file_id] = {"name": nombre, "parent": carpeta_id, "contenido": contenido}
        return file_id

    def agregar_carpeta(self, nombre: str, padre_id: str | None = None) -> str:
        carpeta_id = self._nuevo_id("carpeta")
        self.carpetas[carpeta_id] = {"nombre": nombre, "padre_id": padre_id}
        return carpeta_id

    # -- interfaz que usa conciliar.py -------------------------------------
    def listar(self, carpeta_id: str) -> list[dict]:
        return [{"id": fid, "name": f["name"]} for fid, f in self.archivos.items() if f["parent"] == carpeta_id]

    def descargar(self, file_id: str, destino: pathlib.Path) -> pathlib.Path:
        destino = pathlib.Path(destino)
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(self.archivos[file_id]["contenido"])
        return destino

    def asegurar_carpeta(self, nombre: str, padre_id: str | None = None) -> str:
        for cid, c in self.carpetas.items():
            if c["nombre"] == nombre and c["padre_id"] == padre_id:
                return cid
        return self.agregar_carpeta(nombre, padre_id)

    def buscar_por_nombre(self, carpeta_id: str, nombre: str) -> dict | None:
        for fid, f in self.archivos.items():
            if f["parent"] == carpeta_id and f["name"] == nombre:
                return {"id": fid, "name": nombre}
        return None

    def subir(self, carpeta_id: str, nombre: str, origen, mimetype: str | None = None) -> str:
        if isinstance(origen, (bytes, bytearray)):
            contenido = bytes(origen)
        else:
            contenido = pathlib.Path(origen).read_bytes()
        file_id = self.agregar_archivo(carpeta_id, nombre, contenido)
        self.subidas.append((carpeta_id, nombre))
        return file_id

    def enlace(self, file_id: str) -> str:
        return f"https://drive.google.com/file/d/{file_id}/view"


# -----------------------------------------------------------------------------
# Doble en memoria del Resource de sheets v4, solo para leer_filas_sheet_contable.
# -----------------------------------------------------------------------------
class FakeServicioSheets:
    """Doble del Resource de sheets v4, solo lo que usa conciliar.py.

    Registra las opciones de renderizado con las que se pidieron los valores:
    no son cosméticas, son correctitud de los importes. Leer con el modo por
    defecto devuelve los números según el idioma del Sheet ("686,44" con coma),
    y el motor los parsea con `float(str(x).replace(',', ''))`, que sobre
    "1507,16" da 150716.0 — el importe queda inflado x100 y ningún cargo cruza.
    Ver el docstring de leer_filas_sheet_contable().
    """

    def __init__(self, valores: list[list[str]]):
        self._valores = valores
        self.opciones_ultima_lectura: dict = {}

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, spreadsheetId, range, valueRenderOption=None, dateTimeRenderOption=None):  # noqa: N803, A002
        self.opciones_ultima_lectura = {
            "valueRenderOption": valueRenderOption,
            "dateTimeRenderOption": dateTimeRenderOption,
        }
        return self

    def execute(self):
        return {"values": self._valores}


# -----------------------------------------------------------------------------
# Selección de EECC (descargar_eecc)
# -----------------------------------------------------------------------------
def test_descargar_eecc_asocia_por_numero_de_cuenta(tmp_path):
    almacen = FakeAlmacen()
    carpeta_eecc_id = almacen.agregar_carpeta("EECC")
    almacen.agregar_archivo(carpeta_eecc_id, "EC_4134_062026.pdf", b"contenido del EECC")
    cuentas = [{"banco": "interbank", "numero": "4134", "moneda": "PEN", "principal": True}]

    por_cuenta, ignorados = conciliar.descargar_eecc(almacen, carpeta_eecc_id, cuentas, tmp_path)

    assert ignorados == []
    assert list(por_cuenta.keys()) == ["4134"]
    ruta = por_cuenta["4134"][0]
    assert ruta == tmp_path / "EC_4134_062026.pdf"
    assert ruta.read_bytes() == b"contenido del EECC"


def test_descargar_eecc_de_otra_empresa_no_se_descarga_y_queda_en_ignorados(tmp_path):
    """Caso importante: un EECC cuyo número no calza con ninguna cuenta
    configurada de ESTA empresa nunca se descarta en silencio, queda listado
    en 'ignorados' (y logueado) para que alguien lo revise."""
    almacen = FakeAlmacen()
    carpeta_eecc_id = almacen.agregar_carpeta("EECC")
    almacen.agregar_archivo(carpeta_eecc_id, "EC_9999_062026.pdf")  # cuenta de otra empresa
    cuentas = [{"banco": "interbank", "numero": "4134", "moneda": "PEN", "principal": True}]

    por_cuenta, ignorados = conciliar.descargar_eecc(almacen, carpeta_eecc_id, cuentas, tmp_path)

    assert por_cuenta == {}
    assert ignorados == ["EC_9999_062026.pdf"]
    assert not (tmp_path / "EC_9999_062026.pdf").exists()  # nunca se descargó


# -----------------------------------------------------------------------------
# Principal vs adicionales (separar_principal)
# -----------------------------------------------------------------------------
def test_separar_principal_va_como_posicional_y_el_resto_como_adicionales():
    cuentas = [
        {"numero": "4134", "principal": True},
        {"numero": "4388"},
        {"numero": "8579"},
    ]
    por_cuenta = {
        "4134": [pathlib.Path("EC_4134.pdf")],
        "4388": [pathlib.Path("EC_4388.pdf")],
        "8579": [pathlib.Path("EC_8579.xls")],
    }

    principal, adicionales = conciliar.separar_principal(por_cuenta, cuentas)

    assert principal == pathlib.Path("EC_4134.pdf")
    assert set(adicionales) == {pathlib.Path("EC_4388.pdf"), pathlib.Path("EC_8579.xls")}


def test_separar_principal_sin_archivo_de_la_cuenta_principal_deja_none():
    cuentas = [
        {"numero": "4134", "principal": True},  # esta cuenta no trajo EECC este mes
        {"numero": "8579"},
    ]
    por_cuenta = {
        "8579": [pathlib.Path("EC_8579.xls")],
    }

    principal, adicionales = conciliar.separar_principal(por_cuenta, cuentas)

    assert principal is None  # el motor acepta 'none' como posicional
    assert adicionales == [pathlib.Path("EC_8579.xls")]


# -----------------------------------------------------------------------------
# Argumentos del motor (construir_argumentos_motor)
# -----------------------------------------------------------------------------
def test_construir_argumentos_motor_orden_posicional_y_flags():
    argumentos = conciliar.construir_argumentos_motor(
        eecc_principal=pathlib.Path("EC_4134.pdf"),
        eecc_adicionales=[pathlib.Path("EC_4388.pdf")],
        constancias=pathlib.Path("constancias.json"),
        salida_xlsx=pathlib.Path("CONCILIACION EL TEMPLO - JUNIO 2026.xlsx"),
        nombre_motor="EL TEMPLO",
        comprobantes_csv=pathlib.Path("comprobantes.csv"),
        pendientes_json=pathlib.Path("pendientes.json"),
        heredar_xlsx=pathlib.Path("heredar.xlsx"),
    )

    # <eecc_principal|none> <constancias|none> <salida.xlsx> <EMPRESA>
    assert argumentos[:4] == [
        "EC_4134.pdf",
        "constancias.json",
        "CONCILIACION EL TEMPLO - JUNIO 2026.xlsx",
        "EL TEMPLO",
    ]
    assert argumentos[4:6] == ["--eecc", "EC_4388.pdf"]
    assert "--comprobantes" in argumentos
    assert argumentos[argumentos.index("--comprobantes") + 1] == "comprobantes.csv"
    assert "--pendientes" in argumentos
    assert argumentos[argumentos.index("--pendientes") + 1] == "pendientes.json"
    assert "--heredar" in argumentos
    assert argumentos[argumentos.index("--heredar") + 1] == "heredar.xlsx"


def test_construir_argumentos_motor_pdf_password_opcional():
    """Bug real encontrado el 2026-08-05: los EECC en PDF de Interbank de
    julio llegaron cifrados con el RUC del titular como contrasena (los de
    junio no lo estaban). --pdf-password se agrega solo si hay un valor;
    con None o cadena vacia no se agrega el flag (el motor no necesita
    contrasena para PDFs sin cifrar ni para los otros formatos)."""
    base = dict(
        eecc_principal=pathlib.Path("EC_4134.pdf"),
        eecc_adicionales=[],
        constancias=None,
        salida_xlsx=pathlib.Path("salida.xlsx"),
        nombre_motor="EL TEMPLO",
        comprobantes_csv=None,
        pendientes_json=pathlib.Path("pendientes.json"),
        heredar_xlsx=None,
    )
    con_password = conciliar.construir_argumentos_motor(**base, pdf_password="20608901494")
    assert "--pdf-password" in con_password
    assert con_password[con_password.index("--pdf-password") + 1] == "20608901494"

    sin_password = conciliar.construir_argumentos_motor(**base, pdf_password=None)
    assert "--pdf-password" not in sin_password

    password_vacio = conciliar.construir_argumentos_motor(**base, pdf_password="")
    assert "--pdf-password" not in password_vacio


def test_resolver_ruc_empresa_busca_en_config_empresas_no_en_conciliacion_empresas():
    """El RUC vive en config['empresas'] (la lista de comprobantes), no en
    config['conciliacion']['empresas'] (que solo tiene nombre_corto/
    nombre_motor/cuentas, sin RUC) — son dos listas distintas con el mismo
    nombre_corto como unica clave compartida."""
    config = {
        "empresas": [
            {"nombre_corto": "EL TEMPLO", "ruc": "20608901494"},
            {"nombre_corto": "INSTITUCION", "ruc": "20612506036"},
        ],
        "conciliacion": {"empresas": [{"nombre_corto": "EL TEMPLO", "nombre_motor": "EL TEMPLO"}]},
    }
    assert conciliar.resolver_ruc_empresa(config, "EL TEMPLO") == "20608901494"
    assert conciliar.resolver_ruc_empresa(config, "INSTITUCION") == "20612506036"
    assert conciliar.resolver_ruc_empresa(config, "NO EXISTE") is None


def test_construir_argumentos_motor_none_cuando_falta_principal_o_constancias():
    argumentos = conciliar.construir_argumentos_motor(
        eecc_principal=None,
        eecc_adicionales=[],
        constancias=None,
        salida_xlsx=pathlib.Path("salida.xlsx"),
        nombre_motor="EL TEMPLO",
        comprobantes_csv=None,
        pendientes_json=pathlib.Path("pendientes.json"),
        heredar_xlsx=None,
    )

    assert argumentos[0] == "none"
    assert argumentos[1] == "none"
    assert "--comprobantes" not in argumentos
    assert "--heredar" not in argumentos


def test_construir_argumentos_motor_usa_nombre_motor_no_nombre_corto():
    """CRÍTICO: el motor filtra el CSV con `EMP_KEY not in norm(row['EMPRESA'])`
    (conciliacion/build_conciliacion.py:454), donde EMP_KEY sale de si 'TEMPLO'
    está en el argumento posicional 'empresa' que se le pasa acá
    (build_conciliacion.py:90: EMP_KEY = 'TEMPLO' if 'TEMPLO' in EMP else
    'CEVICHERA'). nombre_corto de INSTITUCION es "INSTITUCION", que NO
    contiene "CEVICHERA": si este argumento posicional llevara nombre_corto
    en vez de nombre_motor, el motor descartaría TODAS las filas de
    INSTITUCION en silencio. Por eso el 4to argumento tiene que ser
    empresa_cfg['nombre_motor'] ("INSTITUCION CEVICHERA"), nunca
    empresa_cfg['nombre_corto'] ("INSTITUCION")."""
    argumentos = conciliar.construir_argumentos_motor(
        eecc_principal=None,
        eecc_adicionales=[],
        constancias=None,
        salida_xlsx=pathlib.Path("salida.xlsx"),
        nombre_motor="INSTITUCION CEVICHERA",
        comprobantes_csv=None,
        pendientes_json=pathlib.Path("pendientes.json"),
        heredar_xlsx=None,
    )

    assert argumentos[3] == "INSTITUCION CEVICHERA"
    assert "CEVICHERA" in argumentos[3]
    assert argumentos[3] != "INSTITUCION"  # nombre_corto, el que NO debe ir acá


# -----------------------------------------------------------------------------
# Egresos de caja: descarga por empresa (descargar_egresos, advertir_egresos_sueltos)
#
# CONCILIACION/<mes>/EGRESOS/ es una sola carpeta por mes compartida por
# todas las empresas; conciliar.py ahora crea (idempotente, con
# asegurar_carpeta) una subcarpeta EGRESOS/<nombre_corto>/ por empresa, para
# que descargar_egresos() nunca se lleve el reporte de otra empresa. Caso
# real que motivó el cambio (verificado con julio 2026): 'Egresos (18).xls'
# (local LINCE, 61 gastos, S/2,597.60) es de EL TEMPLO, y
# 'Egresos (19).xls' (local MIRAFLORES, 171 gastos, S/5,114.22) es de
# INSTITUCION — con una sola carpeta EGRESOS/ compartida, cada conciliación
# se hubiera tragado los gastos de la otra empresa en su hoja CAJA CHICA.
# -----------------------------------------------------------------------------
def test_descargar_egresos_de_la_subcarpeta_de_empresa_trae_solo_los_de_esa_empresa(tmp_path):
    almacen = FakeAlmacen()
    carpeta_egresos_id = almacen.agregar_carpeta("EGRESOS")
    carpeta_el_templo_id = almacen.asegurar_carpeta("EL TEMPLO", carpeta_egresos_id)
    almacen.agregar_archivo(carpeta_el_templo_id, "Egresos (18).xls", b"reporte LINCE / EL TEMPLO")

    rutas = conciliar.descargar_egresos(almacen, carpeta_el_templo_id, tmp_path)

    assert len(rutas) == 1
    assert rutas[0] == tmp_path / "Egresos (18).xls"
    assert rutas[0].read_bytes() == b"reporte LINCE / EL TEMPLO"


def test_advertir_egresos_sueltos_ignora_y_advierte_archivo_en_la_raiz(tmp_path, caplog):
    """Un archivo directamente en EGRESOS/ (layout viejo, sin subcarpeta de
    empresa) no se puede atribuir a ninguna empresa: se ignora, pero SIEMPRE
    con una advertencia clara en el log, nunca en silencio."""
    almacen = FakeAlmacen()
    carpeta_egresos_id = almacen.agregar_carpeta("EGRESOS")
    almacen.agregar_archivo(carpeta_egresos_id, "Egresos (20).xls", b"suelto, layout viejo")

    with caplog.at_level(logging.WARNING, logger="procesar.conciliar"):
        sueltos = conciliar.advertir_egresos_sueltos(almacen, carpeta_egresos_id)

    assert sueltos == ["Egresos (20).xls"]
    mensajes = [r.message for r in caplog.records]
    assert any("Egresos (20).xls" in m and "suelto" in m for m in mensajes)
    # nunca se descarga: advertir_egresos_sueltos() solo lista y advierte
    assert not (tmp_path / "Egresos (20).xls").exists()


def test_advertir_egresos_sueltos_no_ve_lo_que_hay_dentro_de_subcarpetas_de_empresa():
    """Un archivo que sí está dentro de EGRESOS/<empresa>/ no debe generar la
    advertencia de 'archivo suelto': almacen.listar() no es recursivo y
    excluye carpetas, así que la subcarpeta de empresa ni su contenido
    aparecen al listar la carpeta EGRESOS/ raíz."""
    almacen = FakeAlmacen()
    carpeta_egresos_id = almacen.agregar_carpeta("EGRESOS")
    carpeta_el_templo_id = almacen.asegurar_carpeta("EL TEMPLO", carpeta_egresos_id)
    almacen.agregar_archivo(carpeta_el_templo_id, "Egresos (18).xls")

    sueltos = conciliar.advertir_egresos_sueltos(almacen, carpeta_egresos_id)

    assert sueltos == []


def test_descargar_egresos_dos_empresas_cada_una_recibe_solo_lo_propio(tmp_path):
    """Dos empresas, cada una con su propio reporte en su subcarpeta: cada
    llamada a descargar_egresos() con la subcarpeta correspondiente trae
    solo el archivo de esa empresa, nunca el de la otra."""
    almacen = FakeAlmacen()
    carpeta_egresos_id = almacen.agregar_carpeta("EGRESOS")
    carpeta_el_templo_id = almacen.asegurar_carpeta("EL TEMPLO", carpeta_egresos_id)
    carpeta_institucion_id = almacen.asegurar_carpeta("INSTITUCION", carpeta_egresos_id)
    almacen.agregar_archivo(carpeta_el_templo_id, "Egresos (18).xls", b"LINCE / EL TEMPLO")
    almacen.agregar_archivo(carpeta_institucion_id, "Egresos (19).xls", b"MIRAFLORES / INSTITUCION")

    destino_el_templo = tmp_path / "EL TEMPLO"
    destino_institucion = tmp_path / "INSTITUCION"
    rutas_el_templo = conciliar.descargar_egresos(almacen, carpeta_el_templo_id, destino_el_templo)
    rutas_institucion = conciliar.descargar_egresos(almacen, carpeta_institucion_id, destino_institucion)

    assert [r.name for r in rutas_el_templo] == ["Egresos (18).xls"]
    assert rutas_el_templo[0].read_bytes() == b"LINCE / EL TEMPLO"
    assert [r.name for r in rutas_institucion] == ["Egresos (19).xls"]
    assert rutas_institucion[0].read_bytes() == b"MIRAFLORES / INSTITUCION"


def test_descargar_egresos_extension_no_reconocida_dentro_de_la_subcarpeta_se_ignora_con_advertencia(
    tmp_path, caplog
):
    """Mismo comportamiento de siempre para extensión no reconocida, ahora
    dentro de la subcarpeta de empresa en vez de EGRESOS/ directo."""
    almacen = FakeAlmacen()
    carpeta_egresos_id = almacen.agregar_carpeta("EGRESOS")
    carpeta_el_templo_id = almacen.asegurar_carpeta("EL TEMPLO", carpeta_egresos_id)
    almacen.agregar_archivo(carpeta_el_templo_id, "notas.txt", b"no es un reporte de egresos")

    with caplog.at_level(logging.WARNING, logger="procesar.conciliar"):
        rutas = conciliar.descargar_egresos(almacen, carpeta_el_templo_id, tmp_path)

    assert rutas == []
    mensajes = [r.message for r in caplog.records]
    assert any("notas.txt" in m and "extensión no reconocida" in m for m in mensajes)
    assert not (tmp_path / "notas.txt").exists()


# -----------------------------------------------------------------------------
# Egresos de caja: JSON intermedio (construir_json_egresos)
# -----------------------------------------------------------------------------
def _tabla_egresos_htm(*filas) -> str:
    """Mismo shape de 10 <td> por fila de datos que egresos_caja.py espera
    (Fecha, Usuario, Categoria, Caja, Motivo, Entregado A, Moneda, Tarjeta,
    Estado, Monto) — ver tests/test_egresos_caja.py, que prueba el parser en
    detalle. Acá solo hace falta lo mínimo para que parsear_egresos() separe
    depósitos con su 'concepto'; el parseo en sí no es lo que se prueba."""
    return "<html><body><table>" + "".join(filas) + "</table></body></html>"


def _fila_egreso(fecha, motivo, entregado_a, monto) -> str:
    return (
        f"<tr><td>{fecha}</td><td>CAJA.MIRAFLORES</td><td colspan=2>Otros</td><td>Caja 01</td>"
        f"<td colspan=4>{motivo}</td><td>{entregado_a}</td><td>Soles</td><td>-</td>"
        f"<td>ACTIVO</td><td>{monto}</td></tr>"
    )


def test_construir_json_egresos_propaga_el_concepto_de_cada_deposito(tmp_path):
    """El campo 'concepto' ('propina'/'venta'/'indeterminado') que
    egresos_caja.parsear_egresos() agrega a cada depósito tiene que viajar
    tal cual en el JSON intermedio que consume --egresos del motor, sin
    cambiar el resto del formato documentado en el comentario de arriba de
    esta función y en conciliacion/README.md."""
    filas = (
        _fila_egreso("02/07/2026 16:17", "PROPINA EN EFECTIVO 26 AL 02", "CTA DE LA EMPRESA", "120"),
        _fila_egreso("15/07/2026 16:22", "DEPOSITO", "INTERBANK", "400"),
        _fila_egreso("29/07/2026 14:16", "DEPOSITO DE VENTA EN EFECTIVO", "BANCO", "1400"),
    )
    ruta = tmp_path / "tabla.htm"
    ruta.write_text(_tabla_egresos_htm(*filas), encoding="utf-8")
    destino = tmp_path / "egresos.json"

    resumen = conciliar.construir_json_egresos([ruta], 250.0, destino)

    assert resumen["n_depositos"] == 3
    datos = json.loads(destino.read_text(encoding="utf-8"))
    conceptos = {d["motivo"]: d["concepto"] for d in datos["depositos"]}
    assert conceptos["PROPINA EN EFECTIVO 26 AL 02"] == "propina"
    assert conceptos["DEPOSITO"] == "indeterminado"
    assert conceptos["DEPOSITO DE VENTA EN EFECTIVO"] == "venta"
    # el resto del shape del JSON no cambia
    assert set(datos.keys()) == {"gastos", "depositos", "reposicion_semanal"}
    assert datos["reposicion_semanal"] == 250.0


def test_construir_json_egresos_los_gastos_no_llevan_concepto(tmp_path):
    ruta = tmp_path / "tabla.htm"
    ruta.write_text(_tabla_egresos_htm(_fila_egreso("01/07/2026 09:00", "COMPRA DE VERDURAS", "EDWIN", "30")), encoding="utf-8")
    destino = tmp_path / "egresos.json"

    conciliar.construir_json_egresos([ruta], 250.0, destino)

    datos = json.loads(destino.read_text(encoding="utf-8"))
    assert len(datos["gastos"]) == 1
    assert "concepto" not in datos["gastos"][0]


# -----------------------------------------------------------------------------
# CSV derivado (filtrar_y_escribir_csv)
# -----------------------------------------------------------------------------
def _fila_contable(**overrides) -> dict[str, str]:
    fila = {columna: "" for columna in COLUMNAS_CONTABLE}
    fila.update(overrides)
    return fila


def test_filtrar_y_escribir_csv_columnas_exactas_de_columnas_contable(tmp_path):
    fila = _fila_contable(
        EMPRESA="EL TEMPLO",
        FECHA_EMISION="15/06/2026",
        LINK_DRIVE="https://drive.google.com/file/d/abc/view",
    )
    destino = tmp_path / "comprobantes.csv"

    n = conciliar.filtrar_y_escribir_csv([fila], "EL TEMPLO", "EL TEMPLO", "2026-06", destino)

    assert n == 1
    with destino.open("r", encoding="utf-8-sig", newline="") as f:
        lector = csv.DictReader(f)
        assert lector.fieldnames == COLUMNAS_CONTABLE  # exactamente estas, en este orden
        filas_leidas = list(lector)
    assert filas_leidas[0]["LINK_DRIVE"] == "https://drive.google.com/file/d/abc/view"


def test_filtrar_y_escribir_csv_filtra_por_nombre_corto_pero_escribe_nombre_motor(tmp_path):
    """Mismo motivo crítico que construir_argumentos_motor: si la columna
    EMPRESA del CSV trajera nombre_corto='INSTITUCION' en vez de
    nombre_motor='INSTITUCION CEVICHERA', el filtro EMP_KEY del motor
    (build_conciliacion.py:454) descartaría la fila en silencio porque
    'INSTITUCION' no contiene 'CEVICHERA'."""
    fila_institucion = _fila_contable(EMPRESA="INSTITUCION", FECHA_EMISION="10/06/2026")
    fila_otra_empresa = _fila_contable(EMPRESA="EL TEMPLO", FECHA_EMISION="10/06/2026")
    destino = tmp_path / "comprobantes.csv"

    n = conciliar.filtrar_y_escribir_csv(
        [fila_institucion, fila_otra_empresa], "INSTITUCION", "INSTITUCION CEVICHERA", "2026-06", destino
    )

    assert n == 1  # solo la fila de INSTITUCION, la de EL TEMPLO se filtró afuera
    with destino.open("r", encoding="utf-8-sig", newline="") as f:
        filas = list(csv.DictReader(f))
    assert len(filas) == 1
    assert filas[0]["EMPRESA"] == "INSTITUCION CEVICHERA"  # nombre_motor, no nombre_corto


def test_filtrar_y_escribir_csv_respeta_el_margen_de_15_dias(tmp_path):
    fila_fin_mes_anterior = _fila_contable(EMPRESA="EL TEMPLO", FECHA_EMISION="31/05/2026")
    fila_pagada_inicio_mes_siguiente = _fila_contable(EMPRESA="EL TEMPLO", FECHA_PAGO="03/07/2026")
    fila_dos_meses_antes = _fila_contable(EMPRESA="EL TEMPLO", FECHA_EMISION="15/04/2026")
    destino = tmp_path / "comprobantes.csv"

    n = conciliar.filtrar_y_escribir_csv(
        [fila_fin_mes_anterior, fila_pagada_inicio_mes_siguiente, fila_dos_meses_antes],
        "EL TEMPLO", "EL TEMPLO", "2026-06", destino,
    )

    assert n == 2  # entran las dos del margen, la de dos meses antes no
    with destino.open("r", encoding="utf-8-sig", newline="") as f:
        fechas_emision = [fila["FECHA_EMISION"] for fila in csv.DictReader(f)]
    assert "15/04/2026" not in fechas_emision


def test_filtrar_y_escribir_csv_descarta_fila_sin_fecha_valida_en_ninguna_columna(tmp_path):
    fila_sin_fecha = _fila_contable(EMPRESA="EL TEMPLO")  # FECHA_EMISION y FECHA_PAGO vacías
    destino = tmp_path / "comprobantes.csv"

    n = conciliar.filtrar_y_escribir_csv([fila_sin_fecha], "EL TEMPLO", "EL TEMPLO", "2026-06", destino)

    assert n == 0


# -----------------------------------------------------------------------------
# paga_comprobantes_de: ILLAWARA no tiene cuentas bancarias propias, sus
# compras las paga EL TEMPLO (ver config.yaml y el docstring de
# filtrar_y_escribir_csv/validar_config_conciliacion en conciliar.py).
# -----------------------------------------------------------------------------
def test_filtrar_y_escribir_csv_incluye_filas_de_paga_comprobantes_de_con_empresa_reescrita(tmp_path):
    fila_propia = _fila_contable(EMPRESA="EL TEMPLO", FECHA_EMISION="10/07/2026")
    fila_illawara = _fila_contable(EMPRESA="ILLAWARA", FECHA_EMISION="12/07/2026")
    fila_otra_empresa_no_declarada = _fila_contable(EMPRESA="INSTITUCION", FECHA_EMISION="12/07/2026")
    destino = tmp_path / "comprobantes.csv"

    n = conciliar.filtrar_y_escribir_csv(
        [fila_propia, fila_illawara, fila_otra_empresa_no_declarada],
        "EL TEMPLO", "EL TEMPLO", "2026-07", destino,
        paga_comprobantes_de=["ILLAWARA"],
    )

    assert n == 2  # la propia + la de ILLAWARA; INSTITUCION (no declarada) queda afuera
    with destino.open("r", encoding="utf-8-sig", newline="") as f:
        filas = list(csv.DictReader(f))
    # Las DOS filas incluidas quedan con EMPRESA=nombre_motor (nunca 'ILLAWARA'
    # ni 'EL TEMPLO' a secas): el filtro EMP_KEY del motor descartaría en
    # silencio cualquier valor que no contenga su EMP_KEY (ver comentario
    # arriba de filtrar_y_escribir_csv en conciliar.py).
    assert {f["EMPRESA"] for f in filas} == {"EL TEMPLO"}


def test_filtrar_y_escribir_csv_marca_trazabilidad_en_serie_numero_y_observaciones(tmp_path):
    fila_illawara = _fila_contable(
        EMPRESA="ILLAWARA", FECHA_EMISION="12/07/2026", SERIE_NUMERO="F001-00102426",
    )
    destino = tmp_path / "comprobantes.csv"

    conciliar.filtrar_y_escribir_csv(
        [fila_illawara], "EL TEMPLO", "EL TEMPLO", "2026-07", destino, paga_comprobantes_de=["ILLAWARA"],
    )

    with destino.open("r", encoding="utf-8-sig", newline="") as f:
        fila = next(csv.DictReader(f))
    # SERIE_NUMERO: el motor SÍ la lee (n_comprobante) y es lo único que un
    # contador ve intacto hasta el .xlsx, así que la marca vive ahí también,
    # no solo en OBSERVACIONES (que el motor nunca lee).
    assert fila["SERIE_NUMERO"] == "F001-00102426 [FACT. A ILLAWARA]"
    assert "ILLAWARA" in fila["OBSERVACIONES"]
    assert "EL TEMPLO" in fila["OBSERVACIONES"]


def test_filtrar_y_escribir_csv_no_pisa_observaciones_existentes(tmp_path):
    fila_illawara = _fila_contable(
        EMPRESA="ILLAWARA", FECHA_EMISION="12/07/2026", OBSERVACIONES="Nota original del contador",
    )
    destino = tmp_path / "comprobantes.csv"

    conciliar.filtrar_y_escribir_csv(
        [fila_illawara], "EL TEMPLO", "EL TEMPLO", "2026-07", destino, paga_comprobantes_de=["ILLAWARA"],
    )

    with destino.open("r", encoding="utf-8-sig", newline="") as f:
        fila = next(csv.DictReader(f))
    assert "Nota original del contador" in fila["OBSERVACIONES"]  # no se pisa
    assert "ILLAWARA" in fila["OBSERVACIONES"]  # se concatena la nota de trazabilidad


def test_filtrar_y_escribir_csv_fila_propia_no_lleva_marca_de_trazabilidad(tmp_path):
    """Las filas que YA son de nombre_corto (no llegaron por
    paga_comprobantes_de) no deben llevar la marca [FACT. A ...] ni tocar
    OBSERVACIONES: la marca es solo para distinguir un comprobante ajeno."""
    fila_propia = _fila_contable(
        EMPRESA="EL TEMPLO", FECHA_EMISION="12/07/2026", SERIE_NUMERO="F001-00099999",
    )
    destino = tmp_path / "comprobantes.csv"

    conciliar.filtrar_y_escribir_csv(
        [fila_propia], "EL TEMPLO", "EL TEMPLO", "2026-07", destino, paga_comprobantes_de=["ILLAWARA"],
    )

    with destino.open("r", encoding="utf-8-sig", newline="") as f:
        fila = next(csv.DictReader(f))
    assert fila["SERIE_NUMERO"] == "F001-00099999"
    assert fila["OBSERVACIONES"] == ""


def test_filtrar_y_escribir_csv_sin_paga_comprobantes_de_no_hay_regresion(tmp_path):
    """Sin pasar paga_comprobantes_de (el caso de INSTITUCION, que no declara
    ninguna), el comportamiento es exactamente el de antes: solo pasa la
    propia empresa, sin marca de trazabilidad."""
    fila_propia = _fila_contable(EMPRESA="EL TEMPLO", FECHA_EMISION="10/07/2026", SERIE_NUMERO="F001-1")
    fila_illawara = _fila_contable(EMPRESA="ILLAWARA", FECHA_EMISION="12/07/2026")
    destino = tmp_path / "comprobantes.csv"

    n = conciliar.filtrar_y_escribir_csv([fila_propia, fila_illawara], "EL TEMPLO", "EL TEMPLO", "2026-07", destino)

    assert n == 1
    with destino.open("r", encoding="utf-8-sig", newline="") as f:
        fila = next(csv.DictReader(f))
    assert fila["EMPRESA"] == "EL TEMPLO"
    assert fila["SERIE_NUMERO"] == "F001-1"  # sin marca
    assert fila["OBSERVACIONES"] == ""


def test_filtrar_y_escribir_csv_caso_real_julio_2026_illawara_entra_al_csv_de_el_templo(tmp_path):
    """Caso real verificado jul-2026 (ver config.yaml, comentario junto a
    paga_comprobantes_de): 8 comprobantes facturados a ILLAWARA E.I.R.L.
    (CLIENTE_RUC=20614321734), proveedores ULTRAFRIO/APUDEX/PROGRAS, total
    S/7,024.01, que el banco de EL TEMPLO sí pagó."""
    # Montos individuales de cada comprobante: no vienen dados (el dato
    # confirmado es el total, S/7,024.01, y el reparto 3/3/2 por proveedor,
    # ver el mensaje de tarea); se fabrican acá solo para que el fixture
    # sume ese total exacto, no se presentan como el desglose real.
    montos_ultrafrio = ["900.00", "850.00", "750.00"]  # suma 2500.00
    montos_apudex = ["500.00", "512.00", "500.00"]  # suma 1512.00
    montos_progras = ["1506.00", "1506.01"]  # suma 3012.01
    filas_illawara = [
        _fila_contable(
            EMPRESA="ILLAWARA", FECHA_EMISION="05/07/2026", CLIENTE_RUC="20614321734",
            PROVEEDOR="ULTRAFRIO S.A.C.", TOTAL=monto, SERIE_NUMERO=f"F001-{i}",
        )
        for i, monto in enumerate(montos_ultrafrio, start=1)
    ] + [
        _fila_contable(
            EMPRESA="ILLAWARA", FECHA_EMISION="10/07/2026", CLIENTE_RUC="20614321734",
            PROVEEDOR="APUDEX S.A.C.", TOTAL=monto, SERIE_NUMERO=f"F002-{i}",
        )
        for i, monto in enumerate(montos_apudex, start=1)
    ] + [
        _fila_contable(
            EMPRESA="ILLAWARA", FECHA_EMISION="15/07/2026", CLIENTE_RUC="20614321734",
            PROVEEDOR="PROGRAS S.A.C.", TOTAL=monto, SERIE_NUMERO=f"F003-{i}",
        )
        for i, monto in enumerate(montos_progras, start=1)
    ]
    assert len(filas_illawara) == 8
    total = sum(float(f["TOTAL"]) for f in filas_illawara)
    assert round(total, 2) == 7024.01

    destino = tmp_path / "comprobantes.csv"
    n = conciliar.filtrar_y_escribir_csv(
        filas_illawara, "EL TEMPLO", "EL TEMPLO", "2026-07", destino, paga_comprobantes_de=["ILLAWARA"],
    )

    assert n == 8
    with destino.open("r", encoding="utf-8-sig", newline="") as f:
        filas = list(csv.DictReader(f))
    assert len(filas) == 8
    assert all(f["EMPRESA"] == "EL TEMPLO" for f in filas)  # nombre_motor, nunca 'ILLAWARA'
    assert all("[FACT. A ILLAWARA]" in f["SERIE_NUMERO"] for f in filas)
    assert all("ILLAWARA" in f["OBSERVACIONES"] for f in filas)
    assert round(sum(float(f["TOTAL"]) for f in filas), 2) == 7024.01


# -----------------------------------------------------------------------------
# Herencia (resolver_heredar, mes_anterior)
# -----------------------------------------------------------------------------
def test_mes_anterior_cruza_de_anio():
    assert conciliar.mes_anterior("2026-01") == "2025-12"


def test_mes_anterior_mismo_anio():
    assert conciliar.mes_anterior("2026-07") == "2026-06"


def test_resolver_heredar_descarga_el_unico_xlsx_encontrado(tmp_path):
    almacen = FakeAlmacen()
    carpeta_conciliacion_id = almacen.agregar_carpeta("CONCILIACION")
    carpeta_mes_anterior_id = almacen.asegurar_carpeta("2026-05", carpeta_conciliacion_id)
    almacen.agregar_archivo(
        carpeta_mes_anterior_id, "CONCILIACION EL TEMPLO - Mayo 2026.xlsx", b"contenido del xlsx anterior"
    )

    destino = conciliar.resolver_heredar(almacen, carpeta_conciliacion_id, "EL TEMPLO", "2026-06", tmp_path)

    assert destino is not None
    assert destino.read_bytes() == b"contenido del xlsx anterior"


def test_resolver_heredar_sin_candidatos_devuelve_none_sin_error(tmp_path):
    almacen = FakeAlmacen()
    carpeta_conciliacion_id = almacen.agregar_carpeta("CONCILIACION")
    # no se agrega ningún .xlsx en la carpeta del mes anterior

    destino = conciliar.resolver_heredar(almacen, carpeta_conciliacion_id, "EL TEMPLO", "2026-06", tmp_path)

    assert destino is None


def test_resolver_heredar_con_solo_versiones_elige_la_mas_alta(tmp_path):
    """Caso simple de versionado: solo existen v2 y v3 (sin el archivo sin
    sufijo). Acá el orden alfabético SÍ coincide con la versión más alta."""
    almacen = FakeAlmacen()
    carpeta_conciliacion_id = almacen.agregar_carpeta("CONCILIACION")
    carpeta_mes_anterior_id = almacen.asegurar_carpeta("2026-05", carpeta_conciliacion_id)
    almacen.agregar_archivo(carpeta_mes_anterior_id, "CONCILIACION EL TEMPLO - Mayo 2026 v2.xlsx", b"V2")
    almacen.agregar_archivo(carpeta_mes_anterior_id, "CONCILIACION EL TEMPLO - Mayo 2026 v3.xlsx", b"V3")

    destino = conciliar.resolver_heredar(almacen, carpeta_conciliacion_id, "EL TEMPLO", "2026-06", tmp_path)

    assert destino is not None
    assert destino.read_bytes() == b"V3"


def test_resolver_heredar_con_original_y_versiones_elige_la_version_mas_alta(tmp_path):
    """BUG REAL encontrado (ver reporte final): cuando conviven el archivo
    SIN sufijo (subida original de subir_resultado) y sus versiones ' v2'/'
    v3' (subidas posteriores del mismo mes, típico si la conciliación se
    corrió más de una vez), resolver_heredar debería elegir 'v3' (versión
    más alta), pero candidatos.sort(key=lambda a: a["name"]) ordena por
    ASCII: el espacio antes de 'v2'/'v3' (0x20) es MENOR que el punto antes
    de 'xlsx' del archivo sin sufijo (0x2E), así que 'Mayo 2026 v3.xlsx' <
    'Mayo 2026.xlsx' alfabéticamente y candidatos[-1] termina siendo el
    archivo SIN sufijo (el más viejo), no 'v3'. Este test documenta el
    comportamiento CORRECTO esperado y por eso falla contra la
    implementación actual."""
    almacen = FakeAlmacen()
    carpeta_conciliacion_id = almacen.agregar_carpeta("CONCILIACION")
    carpeta_mes_anterior_id = almacen.asegurar_carpeta("2026-05", carpeta_conciliacion_id)
    almacen.agregar_archivo(carpeta_mes_anterior_id, "CONCILIACION EL TEMPLO - Mayo 2026.xlsx", b"ORIGINAL")
    almacen.agregar_archivo(carpeta_mes_anterior_id, "CONCILIACION EL TEMPLO - Mayo 2026 v2.xlsx", b"V2")
    almacen.agregar_archivo(carpeta_mes_anterior_id, "CONCILIACION EL TEMPLO - Mayo 2026 v3.xlsx", b"V3")

    destino = conciliar.resolver_heredar(almacen, carpeta_conciliacion_id, "EL TEMPLO", "2026-06", tmp_path)

    assert destino is not None
    assert destino.read_bytes() == b"V3"  # se espera la version mas alta; ver docstring del test


# -----------------------------------------------------------------------------
# Subida sin sobrescribir (subir_resultado)
# -----------------------------------------------------------------------------
def test_subir_resultado_si_no_existe_sube_con_el_nombre_deseado(tmp_path):
    almacen = FakeAlmacen()
    carpeta_id = almacen.agregar_carpeta("2026-06")
    ruta_local = tmp_path / "salida.xlsx"
    ruta_local.write_bytes(b"contenido")

    nombre_final, file_id = conciliar.subir_resultado(
        almacen, carpeta_id, "CONCILIACION EL TEMPLO - Junio 2026.xlsx", ruta_local
    )

    assert nombre_final == "CONCILIACION EL TEMPLO - Junio 2026.xlsx"
    assert almacen.archivos[file_id]["name"] == nombre_final


def test_subir_resultado_si_ya_existe_sube_como_v2(tmp_path):
    almacen = FakeAlmacen()
    carpeta_id = almacen.agregar_carpeta("2026-06")
    almacen.agregar_archivo(carpeta_id, "CONCILIACION EL TEMPLO - Junio 2026.xlsx")
    ruta_local = tmp_path / "salida.xlsx"
    ruta_local.write_bytes(b"contenido")

    nombre_final, _ = conciliar.subir_resultado(
        almacen, carpeta_id, "CONCILIACION EL TEMPLO - Junio 2026.xlsx", ruta_local
    )

    assert nombre_final == "CONCILIACION EL TEMPLO - Junio 2026 v2.xlsx"


def test_subir_resultado_si_ya_existe_v2_sube_como_v3(tmp_path):
    almacen = FakeAlmacen()
    carpeta_id = almacen.agregar_carpeta("2026-06")
    almacen.agregar_archivo(carpeta_id, "CONCILIACION EL TEMPLO - Junio 2026.xlsx")
    almacen.agregar_archivo(carpeta_id, "CONCILIACION EL TEMPLO - Junio 2026 v2.xlsx")
    ruta_local = tmp_path / "salida.xlsx"
    ruta_local.write_bytes(b"contenido")

    nombre_final, _ = conciliar.subir_resultado(
        almacen, carpeta_id, "CONCILIACION EL TEMPLO - Junio 2026.xlsx", ruta_local
    )

    assert nombre_final == "CONCILIACION EL TEMPLO - Junio 2026 v3.xlsx"


def test_subir_resultado_nunca_pisa_el_archivo_existente(tmp_path):
    """Garantía de diseño: AlmacenDrive.subir() siempre CREA, nunca
    sobrescribe. Se verifica acá que el contenido del archivo previo con el
    nombre original queda intacto después de subir_resultado()."""
    almacen = FakeAlmacen()
    carpeta_id = almacen.agregar_carpeta("2026-06")
    id_previo = almacen.agregar_archivo(
        carpeta_id, "CONCILIACION EL TEMPLO - Junio 2026.xlsx", b"CONTENIDO_ORIGINAL_NO_TOCAR"
    )
    ruta_local = tmp_path / "salida.xlsx"
    ruta_local.write_bytes(b"CONTENIDO_NUEVO")

    conciliar.subir_resultado(almacen, carpeta_id, "CONCILIACION EL TEMPLO - Junio 2026.xlsx", ruta_local)

    assert almacen.archivos[id_previo]["contenido"] == b"CONTENIDO_ORIGINAL_NO_TOCAR"


# -----------------------------------------------------------------------------
# Funciones auxiliares pequeñas, separadas justamente para poder testearse
# aparte (ver docstring del módulo de conciliar.py): resolver_empresa,
# leer_filas_sheet_contable, descargar_constancias. No están en la lista de
# casos obligatorios pero es barato cubrirlas y refuerzan el resto.
# -----------------------------------------------------------------------------
def test_resolver_empresa_encuentra_por_nombre_corto():
    config = {
        "conciliacion": {
            "empresas": [
                {"nombre_corto": "EL TEMPLO", "nombre_motor": "EL TEMPLO", "cuentas": []},
                {"nombre_corto": "INSTITUCION", "nombre_motor": "INSTITUCION CEVICHERA", "cuentas": []},
            ]
        }
    }

    empresa, motivo = conciliar.resolver_empresa(config, "INSTITUCION")

    assert motivo is None
    assert empresa["nombre_motor"] == "INSTITUCION CEVICHERA"


def test_resolver_empresa_no_encontrada_devuelve_motivo_con_disponibles():
    config = {"conciliacion": {"empresas": [{"nombre_corto": "EL TEMPLO", "nombre_motor": "EL TEMPLO", "cuentas": []}]}}

    empresa, motivo = conciliar.resolver_empresa(config, "NO EXISTE")

    assert empresa is None
    assert "NO EXISTE" in motivo
    assert "EL TEMPLO" in motivo


# -----------------------------------------------------------------------------
# validar_config_conciliacion / quien_paga_comprobantes_de: guardas de
# paga_comprobantes_de (ver docstrings en conciliar.py y el caso real de
# ILLAWARA en config.yaml).
# -----------------------------------------------------------------------------
def _config_conciliacion_con_illawara(illawara_tiene_cuentas: bool = False) -> dict:
    return {
        "conciliacion": {
            "empresas": [
                {
                    "nombre_corto": "EL TEMPLO", "nombre_motor": "EL TEMPLO",
                    "paga_comprobantes_de": ["ILLAWARA"],
                    "cuentas": [{"banco": "interbank", "numero": "4134", "moneda": "PEN", "principal": True}],
                },
                {
                    "nombre_corto": "ILLAWARA", "nombre_motor": "ILLAWARA",
                    "cuentas": [{"banco": "interbank", "numero": "9999", "moneda": "PEN", "principal": True}]
                    if illawara_tiene_cuentas else [],
                },
            ]
        }
    }


def test_validar_config_conciliacion_ok_sin_paga_comprobantes_de():
    config = {"conciliacion": {"empresas": [{"nombre_corto": "EL TEMPLO", "nombre_motor": "EL TEMPLO", "cuentas": []}]}}

    assert conciliar.validar_config_conciliacion(config) is None


def test_validar_config_conciliacion_ok_cuando_empresa_referida_no_tiene_cuentas():
    config = _config_conciliacion_con_illawara(illawara_tiene_cuentas=False)

    assert conciliar.validar_config_conciliacion(config) is None


def test_validar_config_conciliacion_falla_si_empresa_referida_no_existe():
    config = {
        "conciliacion": {
            "empresas": [
                {"nombre_corto": "EL TEMPLO", "nombre_motor": "EL TEMPLO", "paga_comprobantes_de": ["ILLAWARA"], "cuentas": []},
            ]
        }
    }

    motivo = conciliar.validar_config_conciliacion(config)

    assert motivo is not None
    assert "ILLAWARA" in motivo
    assert "EL TEMPLO" in motivo


def test_validar_config_conciliacion_falla_si_empresa_referida_tiene_cuentas_propias():
    """Doble conteo: si ILLAWARA tuviera 'cuentas' propias, sus comprobantes
    entrarían dos veces (en su propia conciliación y en la de EL TEMPLO)."""
    config = _config_conciliacion_con_illawara(illawara_tiene_cuentas=True)

    motivo = conciliar.validar_config_conciliacion(config)

    assert motivo is not None
    assert "ILLAWARA" in motivo
    assert "dos veces" in motivo


def test_quien_paga_comprobantes_de_encuentra_la_empresa_que_declara():
    config = _config_conciliacion_con_illawara()

    assert conciliar.quien_paga_comprobantes_de(config, "ILLAWARA") == "EL TEMPLO"


def test_quien_paga_comprobantes_de_ninguna_declara_devuelve_none():
    config = {"conciliacion": {"empresas": [{"nombre_corto": "EL TEMPLO", "nombre_motor": "EL TEMPLO", "cuentas": []}]}}

    assert conciliar.quien_paga_comprobantes_de(config, "EL TEMPLO") is None


def test_leer_filas_sheet_contable_arma_dicts_desde_la_cabecera():
    servicio = FakeServicioSheets(
        [
            ["FECHA_EMISION", "EMPRESA", "TOTAL"],
            ["15/06/2026", "EL TEMPLO", "118.00"],
        ]
    )

    filas = conciliar.leer_filas_sheet_contable(servicio, "sheet-id", "A1:AF")

    assert filas == [{"FECHA_EMISION": "15/06/2026", "EMPRESA": "EL TEMPLO", "TOTAL": "118.00"}]


def test_estado_pago_infiere_pagada_solo_para_contado():
    """El motor solo cruza filas con ESTADO_PAGO == 'PAGADA'. En el flujo nuevo
    nadie lo marca a mano (el documento no dice si ya se pago), asi que llegaba
    vacio y el motor ignoraba TODOS los comprobantes: julio 2026 se concilio con
    8 filas en el CSV y 0 cruces nuevos. Se infiere desde CONDICION, que si
    viene en el documento, y solo para 'contado': un credito puede cargarse
    semanas despues y ahi el falso positivo por monto+fecha si es un riesgo."""
    assert conciliar._estado_pago_para_el_motor({"CONDICION": "contado"}) == "PAGADA"
    assert conciliar._estado_pago_para_el_motor({"CONDICION": "credito"}) == ""
    assert conciliar._estado_pago_para_el_motor({"CONDICION": ""}) == ""
    # Mayusculas/espacios no deberian cambiar la decision.
    assert conciliar._estado_pago_para_el_motor({"CONDICION": " CONTADO "}) == "PAGADA"
    # Un ESTADO_PAGO ya puesto (corregido a mano en el Sheet) gana siempre.
    assert conciliar._estado_pago_para_el_motor(
        {"ESTADO_PAGO": "PENDIENTE", "CONDICION": "contado"}
    ) == "PENDIENTE"


def test_filtrar_y_escribir_csv_aplica_la_inferencia_de_estado_pago(tmp_path):
    filas = [
        {"EMPRESA": "EL TEMPLO", "FECHA_EMISION": "2026-07-01", "CONDICION": "contado", "TOTAL": 8.4},
        {"EMPRESA": "EL TEMPLO", "FECHA_EMISION": "2026-07-08", "CONDICION": "credito", "TOTAL": 188.8},
    ]
    destino = tmp_path / "comprobantes.csv"

    conciliar.filtrar_y_escribir_csv(filas, "EL TEMPLO", "EL TEMPLO", "2026-07", destino)

    with destino.open(encoding="utf-8-sig", newline="") as f:
        escritas = list(csv.DictReader(f))
    assert [r["ESTADO_PAGO"] for r in escritas] == ["PAGADA", ""]


def test_leer_filas_sheet_contable_pide_numeros_crudos_y_fechas_como_texto():
    """Bug real encontrado el 2026-08-06 al derivar el CSV del Sheet de verdad:
    con el modo de lectura por defecto, un subtotal volvia como "686,44" (coma
    decimal, idioma del Sheet) y el motor lo parsea con
    `float(str(x).replace(',', ''))`, o sea "1507,16" -> 150716.0. El importe
    quedaba inflado x100 y ningun cargo cruzaba. UNFORMATTED_VALUE arregla el
    numero pero devuelve la fecha como serial de Sheets (46214), que el motor
    no sabe leer; la combinacion con FORMATTED_STRING es la unica que devuelve
    las dos cosas bien (verificado contra el Sheet real, no deducido)."""
    servicio = FakeServicioSheets([["TOTAL"], [810]])

    conciliar.leer_filas_sheet_contable(servicio, "sheet-id", "A1:AF")

    assert servicio.opciones_ultima_lectura == {
        "valueRenderOption": "UNFORMATTED_VALUE",
        "dateTimeRenderOption": "FORMATTED_STRING",
    }


def test_leer_filas_sheet_contable_hoja_vacia_devuelve_lista_vacia():
    servicio = FakeServicioSheets([])
    assert conciliar.leer_filas_sheet_contable(servicio, "sheet-id", "A1:AF") == []


def test_descargar_constancias_fusiona_varios_archivos_en_uno(tmp_path):
    almacen = FakeAlmacen()
    carpeta_id = almacen.agregar_carpeta("CONSTANCIAS")
    almacen.agregar_archivo(
        carpeta_id, "cons_4134.json", json.dumps([{"monto": 100.0, "cuenta": "4134"}]).encode("utf-8")
    )
    almacen.agregar_archivo(
        carpeta_id, "cons_4388.json", json.dumps([{"monto": 200.0, "cuenta": "4388"}]).encode("utf-8")
    )
    cuentas = [{"numero": "4134"}, {"numero": "4388"}]

    ruta = conciliar.descargar_constancias(almacen, carpeta_id, cuentas, tmp_path)

    assert ruta is not None
    contenido = json.loads(ruta.read_text(encoding="utf-8"))
    assert len(contenido) == 2
    assert {c["cuenta"] for c in contenido} == {"4134", "4388"}


def test_descargar_constancias_con_versiones_no_duplica(tmp_path):
    """Bug real encontrado el 2026-08-05 al correr la conciliacion de julio
    contra Gmail real: correo_gmail.py sube cons_<cuenta> v2.json como
    SUPERSET acumulativo de cons_<cuenta>.json (no como archivo aparte), asi
    que si descargar_constancias() suma el contenido de ambos, cada
    constancia real termina duplicada. Debe tomar solo la version mas alta
    por cuenta."""
    almacen = FakeAlmacen()
    carpeta_id = almacen.agregar_carpeta("CONSTANCIAS")
    almacen.agregar_archivo(
        carpeta_id, "cons_4134.json",
        json.dumps([{"monto": 100.0, "cuenta": "4134", "numero_solicitud": "1"}]).encode("utf-8"),
    )
    almacen.agregar_archivo(
        carpeta_id, "cons_4134 v2.json",
        json.dumps([
            {"monto": 100.0, "cuenta": "4134", "numero_solicitud": "1"},
            {"monto": 200.0, "cuenta": "4134", "numero_solicitud": "2"},
        ]).encode("utf-8"),
    )
    cuentas = [{"numero": "4134"}]

    ruta = conciliar.descargar_constancias(almacen, carpeta_id, cuentas, tmp_path)

    contenido = json.loads(ruta.read_text(encoding="utf-8"))
    assert len(contenido) == 2  # no 3: la v1 no se suma a la v2
    assert {c["numero_solicitud"] for c in contenido} == {"1", "2"}


def test_descargar_constancias_sin_ninguna_para_la_empresa_devuelve_none(tmp_path):
    almacen = FakeAlmacen()
    carpeta_id = almacen.agregar_carpeta("CONSTANCIAS")
    cuentas = [{"numero": "4134"}]

    ruta = conciliar.descargar_constancias(almacen, carpeta_id, cuentas, tmp_path)

    assert ruta is None  # el motor acepta 'none'


# -----------------------------------------------------------------------------
# Orquestación (main)
# -----------------------------------------------------------------------------
def _config_base() -> dict:
    return {
        "negocio": "SCONCHA",
        "cuenta_google": "administracion.sconcha@gmail.com",
        "drive": {
            "raiz_nombre": "SCONCHA",
            "carpetas": {"raiz": "raiz-id", "buzon": "buzon-id", "procesado": "procesado-id", "revisar": "revisar-id"},
        },
        "empresas": [
            {"nombre_corto": "EL TEMPLO", "razon_social": "EL TEMPLO S.A.C.", "ruc": "20608901494", "locales": ["LINCE"]},
        ],
        "sheets": {"contable": "sheet-contable-id", "detalle": ""},
        "conciliacion": {
            "carpeta": "conciliacion-carpeta-id",
            "empresas": [
                {
                    "nombre_corto": "EL TEMPLO",
                    "nombre_motor": "EL TEMPLO",
                    "caja_chica": {"reposicion_semanal": 250},
                    "cuentas": [{"banco": "interbank", "numero": "4134", "moneda": "PEN", "principal": True}],
                },
            ],
        },
        "correo": {"habilitado": False, "dias_atras": 45, "max_mensajes": 200, "reglas": []},
    }


def _escribir_config(tmp_path, config: dict) -> pathlib.Path:
    ruta_config = tmp_path / "config.yaml"
    ruta_config.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    return ruta_config


def _csv_comprobantes_vacio(tmp_path) -> pathlib.Path:
    """CSV de comprobantes a mano (--comprobantes), para no tener que armar
    también un doble de servicio_sheets en cada test de main(): 0 filas no
    bloquea la corrida, solo lo hace total_eecc == 0."""
    ruta = tmp_path / "comprobantes.csv"
    with ruta.open("w", encoding="utf-8", newline="") as f:
        csv.DictWriter(f, fieldnames=COLUMNAS_CONTABLE).writeheader()
    return ruta


def _almacen_con_un_eecc(mes: str = "2026-06") -> tuple[FakeAlmacen, str, str, str]:
    """FakeAlmacen con la carpeta del mes ya armada (mismos nombres/padres
    que arma main() vía asegurar_carpeta, así que al correr main() los ids
    calzan) y un EECC de la cuenta '4134' ya cargado en EECC."""
    almacen = FakeAlmacen()
    carpeta_mes_id = almacen.asegurar_carpeta(mes, "conciliacion-carpeta-id")
    carpeta_eecc_id = almacen.asegurar_carpeta("EECC", carpeta_mes_id)
    carpeta_constancias_id = almacen.asegurar_carpeta("CONSTANCIAS", carpeta_mes_id)
    almacen.agregar_archivo(carpeta_eecc_id, "EC_4134_062026.pdf", b"contenido eecc de prueba")
    return almacen, carpeta_mes_id, carpeta_eecc_id, carpeta_constancias_id


def _montar_dobles_de_google(monkeypatch, almacen: FakeAlmacen) -> list[list[str]]:
    """Monkeypatchea auth_google (para no intentar OAuth real), conciliar.AlmacenDrive
    (para que main() use el FakeAlmacen en vez de hablar con Drive) y
    conciliar.invocar_motor (para no lanzar build_conciliacion.py como
    subproceso real). Devuelve la lista donde quedan registradas las
    llamadas al motor."""
    monkeypatch.setattr(auth_google, "servicio_drive", lambda: object())
    monkeypatch.setattr(conciliar, "AlmacenDrive", lambda servicio: almacen)

    llamadas_motor: list[list[str]] = []

    def invocar_motor_falso(argumentos: list[str]) -> None:
        llamadas_motor.append(argumentos)
        # el motor real generaría este archivo; main() valida que exista
        # después de invocar_motor(), así que el doble también lo crea.
        salida_xlsx = pathlib.Path(argumentos[2])
        salida_xlsx.parent.mkdir(parents=True, exist_ok=True)
        salida_xlsx.write_bytes(b"contenido xlsx de prueba")

    monkeypatch.setattr(conciliar, "invocar_motor", invocar_motor_falso)
    return llamadas_motor


def _stub_correo_gmail(monkeypatch, falla: bool = False) -> types.ModuleType:
    """Inyecta un módulo doble de correo_gmail en sys.modules, ANTES de que
    conciliar.main() haga su 'import correo_gmail' diferido. Registra en
    .llamadas cada invocación de descargar(), para poder verificar si se
    llamó o no sin depender del correo_gmail real ni de Gmail."""
    modulo = types.ModuleType("correo_gmail")
    modulo.llamadas = []  # type: ignore[attr-defined]

    def descargar_falso(config, almacen, carpetas, servicio=None, dry_run=False):
        modulo.llamadas.append((config, almacen, carpetas, servicio, dry_run))  # type: ignore[attr-defined]
        if falla:
            raise RuntimeError("Gmail no responde (prueba)")
        return {"adjuntos": 0, "constancias": 0, "omitidos": 0, "archivos": [], "errores": []}

    modulo.descargar = descargar_falso  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "correo_gmail", modulo)
    return modulo


def test_main_dry_run_no_sube_a_drive_ni_llama_a_correo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = _config_base()
    config["correo"]["habilitado"] = True  # aunque esté habilitado, dry-run lo omite
    ruta_config = _escribir_config(tmp_path, config)
    ruta_comprobantes = _csv_comprobantes_vacio(tmp_path)
    almacen, *_ = _almacen_con_un_eecc()
    _montar_dobles_de_google(monkeypatch, almacen)
    correo_falso = _stub_correo_gmail(monkeypatch)

    codigo = conciliar.main(
        [
            "--empresa", "EL TEMPLO", "--mes", "2026-06", "--config", str(ruta_config),
            "--comprobantes", str(ruta_comprobantes), "--sin-heredar", "--dry-run",
        ]
    )

    assert codigo == 0
    assert almacen.subidas == []  # no se subió nada a Drive
    assert correo_falso.llamadas == []  # correo_gmail.descargar nunca se llamó


def test_main_correo_deshabilitado_no_llama_a_correo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = _config_base()
    config["correo"]["habilitado"] = False
    ruta_config = _escribir_config(tmp_path, config)
    ruta_comprobantes = _csv_comprobantes_vacio(tmp_path)
    almacen, *_ = _almacen_con_un_eecc()
    _montar_dobles_de_google(monkeypatch, almacen)
    correo_falso = _stub_correo_gmail(monkeypatch)

    codigo = conciliar.main(
        [
            "--empresa", "EL TEMPLO", "--mes", "2026-06", "--config", str(ruta_config),
            "--comprobantes", str(ruta_comprobantes), "--sin-heredar",
        ]
    )

    assert codigo == 0
    assert correo_falso.llamadas == []


def test_main_correo_habilitado_llama_con_firma_y_carpetas_correctas(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = _config_base()
    config["correo"]["habilitado"] = True
    ruta_config = _escribir_config(tmp_path, config)
    ruta_comprobantes = _csv_comprobantes_vacio(tmp_path)
    almacen, _carpeta_mes_id, carpeta_eecc_id, carpeta_constancias_id = _almacen_con_un_eecc()
    _montar_dobles_de_google(monkeypatch, almacen)
    correo_falso = _stub_correo_gmail(monkeypatch)

    codigo = conciliar.main(
        [
            "--empresa", "EL TEMPLO", "--mes", "2026-06", "--config", str(ruta_config),
            "--comprobantes", str(ruta_comprobantes), "--sin-heredar",
        ]
    )

    assert codigo == 0
    assert len(correo_falso.llamadas) == 1
    config_pasado, almacen_pasado, carpetas_pasadas, servicio_pasado, dry_run_pasado = correo_falso.llamadas[0]
    assert almacen_pasado is almacen
    assert servicio_pasado is None
    assert dry_run_pasado is False
    assert carpetas_pasadas == {
        "EECC": carpeta_eecc_id,
        "CONSTANCIAS": carpeta_constancias_id,
        "BUZON": "buzon-id",
    }


def test_main_fallo_de_correo_no_aborta_la_conciliacion(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = _config_base()
    config["correo"]["habilitado"] = True
    ruta_config = _escribir_config(tmp_path, config)
    ruta_comprobantes = _csv_comprobantes_vacio(tmp_path)
    almacen, *_ = _almacen_con_un_eecc()
    _montar_dobles_de_google(monkeypatch, almacen)
    _stub_correo_gmail(monkeypatch, falla=True)

    codigo = conciliar.main(
        [
            "--empresa", "EL TEMPLO", "--mes", "2026-06", "--config", str(ruta_config),
            "--comprobantes", str(ruta_comprobantes), "--sin-heredar",
        ]
    )

    assert codigo == 0  # el fallo del correo no aborta la conciliación
    assert almacen.subidas  # el resto del flujo (subida del xlsx) sí corrió


def test_main_mes_invalido_devuelve_1_sin_tocar_nada(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    codigo = conciliar.main(["--empresa", "EL TEMPLO", "--mes", "2026-6", "--config", "no-existe.yaml"])

    assert codigo == 1
    assert not (tmp_path / "salida").exists()  # ni siquiera llegó a configurar_logging()


def test_main_carpeta_conciliacion_vacia_devuelve_1(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = _config_base()
    config["conciliacion"]["carpeta"] = ""
    ruta_config = _escribir_config(tmp_path, config)

    codigo = conciliar.main(["--empresa", "EL TEMPLO", "--mes", "2026-06", "--config", str(ruta_config)])

    assert codigo == 1
    log = (tmp_path / "salida" / "conciliar.log").read_text(encoding="utf-8")
    assert "init_negocio.py" in log


def test_main_cero_eecc_devuelve_1_y_no_corre_el_motor(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ruta_config = _escribir_config(tmp_path, _config_base())
    almacen = FakeAlmacen()  # la carpeta EECC queda vacía: no se agrega ningún archivo
    llamadas_motor = _montar_dobles_de_google(monkeypatch, almacen)

    codigo = conciliar.main(
        ["--empresa", "EL TEMPLO", "--mes", "2026-06", "--config", str(ruta_config), "--sin-heredar"]
    )

    assert codigo == 1
    assert llamadas_motor == []  # nunca se corrió el motor


# -----------------------------------------------------------------------------
# main(): paga_comprobantes_de de punta a punta (ILLAWARA sin cuentas
# propias, EL TEMPLO paga sus comprobantes).
# -----------------------------------------------------------------------------
def _config_con_illawara(illawara_tiene_cuentas: bool = False) -> dict:
    """_config_base() + ILLAWARA, con EL TEMPLO declarando
    paga_comprobantes_de: [ILLAWARA] (mismo shape que config.yaml real)."""
    config = _config_base()
    config["empresas"].append(
        {"nombre_corto": "ILLAWARA", "razon_social": "ILLAWARA E.I.R.L.", "ruc": "20614321734", "locales": []}
    )
    config["conciliacion"]["empresas"][0]["paga_comprobantes_de"] = ["ILLAWARA"]
    config["conciliacion"]["empresas"].append(
        {
            "nombre_corto": "ILLAWARA", "nombre_motor": "ILLAWARA",
            "cuentas": [{"banco": "interbank", "numero": "9999", "moneda": "PEN", "principal": True}]
            if illawara_tiene_cuentas else [],
        }
    )
    return config


def test_main_bloquea_conciliar_directamente_una_empresa_sin_cuentas_propias(tmp_path, monkeypatch):
    """Intentar --empresa ILLAWARA (sin cuentas propias) no debe llegar a
    tocar Drive: main() corta antes, con un mensaje que dice quién sí la
    concilia (EL TEMPLO, vía paga_comprobantes_de)."""
    monkeypatch.chdir(tmp_path)
    config = _config_con_illawara()
    ruta_config = _escribir_config(tmp_path, config)
    almacen = FakeAlmacen()
    llamadas_motor = _montar_dobles_de_google(monkeypatch, almacen)

    codigo = conciliar.main(["--empresa", "ILLAWARA", "--mes", "2026-07", "--config", str(ruta_config)])

    assert codigo == 1
    assert llamadas_motor == []
    assert almacen.subidas == []
    log = (tmp_path / "salida" / "conciliar.log").read_text(encoding="utf-8")
    assert "ILLAWARA" in log
    assert "EL TEMPLO" in log  # el mensaje dice quién sí la concilia


def test_main_falla_si_paga_comprobantes_de_referencia_empresa_con_cuentas_propias(tmp_path, monkeypatch):
    """validar_config_conciliacion() corta ANTES de tocar Drive/Sheets si la
    config tiene el error de doble conteo (empresa referida con cuentas
    propias)."""
    monkeypatch.chdir(tmp_path)
    config = _config_con_illawara(illawara_tiene_cuentas=True)
    ruta_config = _escribir_config(tmp_path, config)
    almacen = FakeAlmacen()
    llamadas_motor = _montar_dobles_de_google(monkeypatch, almacen)

    codigo = conciliar.main(["--empresa", "EL TEMPLO", "--mes", "2026-07", "--config", str(ruta_config)])

    assert codigo == 1
    assert llamadas_motor == []
    assert almacen.subidas == []
    log = (tmp_path / "salida" / "conciliar.log").read_text(encoding="utf-8")
    assert "dos veces" in log


def test_main_el_templo_concilia_incluyendo_comprobantes_de_illawara(tmp_path, monkeypatch):
    """De punta a punta: --empresa EL TEMPLO deriva el CSV incluyendo también
    las filas de ILLAWARA (vía paga_comprobantes_de), con EMPRESA reescrita
    a nombre_motor de EL TEMPLO."""
    monkeypatch.chdir(tmp_path)
    config = _config_con_illawara()
    ruta_config = _escribir_config(tmp_path, config)
    almacen, *_ = _almacen_con_un_eecc(mes="2026-07")
    _montar_dobles_de_google(monkeypatch, almacen)
    fila_el_templo = ["10/07/2026", "EL TEMPLO"] + [""] * (len(COLUMNAS_CONTABLE) - 2)
    fila_illawara = ["12/07/2026", "ILLAWARA"] + [""] * (len(COLUMNAS_CONTABLE) - 2)
    servicio_sheets = FakeServicioSheets([COLUMNAS_CONTABLE, fila_el_templo, fila_illawara])
    monkeypatch.setattr(auth_google, "servicio_sheets", lambda: servicio_sheets)

    codigo = conciliar.main(
        ["--empresa", "EL TEMPLO", "--mes", "2026-07", "--config", str(ruta_config), "--sin-heredar"]
    )

    assert codigo == 0
    ruta_csv = tmp_path / "salida" / "conciliacion" / "EL TEMPLO" / "2026-07" / "comprobantes.csv"
    with ruta_csv.open("r", encoding="utf-8-sig", newline="") as f:
        filas = list(csv.DictReader(f))
    assert len(filas) == 2
    assert all(f["EMPRESA"] == "EL TEMPLO" for f in filas)  # nombre_motor, ninguna quedó como 'ILLAWARA'
    assert any("[FACT. A ILLAWARA]" in f["SERIE_NUMERO"] for f in filas)
