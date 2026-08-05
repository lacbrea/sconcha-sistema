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
    def __init__(self, valores: list[list[str]]):
        self._valores = valores

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, spreadsheetId, range):  # noqa: N803, A002
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


def test_leer_filas_sheet_contable_arma_dicts_desde_la_cabecera():
    servicio = FakeServicioSheets(
        [
            ["FECHA_EMISION", "EMPRESA", "TOTAL"],
            ["15/06/2026", "EL TEMPLO", "118.00"],
        ]
    )

    filas = conciliar.leer_filas_sheet_contable(servicio, "sheet-id", "A1:AF")

    assert filas == [{"FECHA_EMISION": "15/06/2026", "EMPRESA": "EL TEMPLO", "TOTAL": "118.00"}]


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
