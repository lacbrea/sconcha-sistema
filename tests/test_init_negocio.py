"""Tests de init_negocio.py. Corren sin red y sin credenciales:
--dry-run nunca toca disco ni la API de Google, y los casos que sí ejercitan
la API usan un doble de prueba local del Resource de sheets v4
(googleapiclient), igual que tests/test_registro_sheets.py.
"""
from __future__ import annotations

import pathlib
import sys

import pytest
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import init_negocio  # noqa: E402
import registro_sheets  # noqa: E402


# ---------------------------------------------------------------------------
# Doble mínimo de AlmacenDrive para probar preparar_carpetas() (la única
# función de init_negocio.py que la usa, y solo llama a asegurar_carpeta()).
# El doble del Resource de drive v3 (googleapiclient) se prueba aparte,
# contra AlmacenDrive real, en tests/test_almacen_drive.py.
# ---------------------------------------------------------------------------
class _FakeAlmacenCarpetas:
    def __init__(self):
        self.llamadas: list[tuple[str, str | None]] = []
        self._contador = 0
        self._existentes: dict[tuple[str, str | None], str] = {}

    def asegurar_carpeta(self, nombre, padre_id=None):
        clave = (nombre, padre_id)
        if clave in self._existentes:
            return self._existentes[clave]
        self._contador += 1
        nuevo_id = f"carpeta-{self._contador}"
        self._existentes[clave] = nuevo_id
        self.llamadas.append(clave)
        return nuevo_id


# ---------------------------------------------------------------------------
# Doble de prueba del Resource de sheets v4 (googleapiclient), sin red.
# Cubre los métodos que usa init_negocio.py: spreadsheets().get() (verificar
# accesibilidad), spreadsheets().create() y spreadsheets().values().update()
# (crear con cabecera) y spreadsheets().batchUpdate() (formato, no crítico).
# ---------------------------------------------------------------------------
class FakeServicioSheets:
    def __init__(self, ids_accesibles: set[str] | None = None):
        self.ids_accesibles = set(ids_accesibles or ())
        self.creados: list[dict] = []
        self.valores_escritos: dict[str, list[list]] = {}
        self.batch_update_llamado = False
        self._contador = 0
        self._pendiente: tuple | None = None

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, spreadsheetId):  # noqa: N803 - firma igual a la API real
        self._pendiente = ("get", spreadsheetId)
        return self

    def create(self, body, fields):
        self._contador += 1
        nuevo_id = f"nuevo-{self._contador}"
        self.creados.append({"id": nuevo_id, "titulo": body["properties"]["title"]})
        self.ids_accesibles.add(nuevo_id)
        self._pendiente = ("create", nuevo_id)
        return self

    def update(self, spreadsheetId, range, valueInputOption, body):  # noqa: N803
        self.valores_escritos[spreadsheetId] = body["values"]
        self._pendiente = ("update",)
        return self

    def batchUpdate(self, spreadsheetId, body):  # noqa: N803
        self.batch_update_llamado = True
        self._pendiente = ("batchUpdate",)
        return self

    def execute(self):
        accion = self._pendiente[0]
        if accion == "get":
            _, sid = self._pendiente
            if sid not in self.ids_accesibles:
                raise RuntimeError(f"404: spreadsheet '{sid}' no encontrado")
            return {"spreadsheetId": sid}
        if accion == "create":
            _, sid = self._pendiente
            return {"spreadsheetId": sid}
        if accion in ("update", "batchUpdate"):
            return {}
        raise AssertionError(f"acción no esperada: {accion}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _config_minima(
    negocio: str = "LA CALETA",
    con_buzon_tipos: bool = False,
    con_buzon_empresas: bool = False,
    nombres_empresas: list[str] | None = None,
) -> dict:
    carpetas = {"raiz": "", "buzon": "", "procesado": "", "revisar": ""}
    if con_buzon_tipos:
        carpetas["buzon_tipos"] = {"facturas": "", "notas_venta": "", "liquidaciones": "", "otros": ""}
    config = {
        "negocio": negocio,
        "cuenta_google": "administracion.lacaleta@gmail.com",
        "drive": {
            "raiz_nombre": negocio,
            "carpetas": carpetas,
        },
        "sheets": {"contable": "", "detalle": ""},
    }
    if con_buzon_empresas:
        nombres = nombres_empresas if nombres_empresas is not None else ["EL TEMPLO", "INSTITUCION"]
        carpetas["buzon_empresas"] = {
            nombre: {"facturas": "", "notas_venta": "", "liquidaciones": "", "otros": ""} for nombre in nombres
        }
        # RUC ficticio, distinto por empresa: solo hace falta que exista y
        # sea único, resolver_empresa()/nombre_corto no lo validan aquí.
        config["empresas"] = [{"nombre_corto": nombre, "ruc": str(10000000000 + i)} for i, nombre in enumerate(nombres)]
    return config


def _escribir_config_yaml(ruta: pathlib.Path, config: dict) -> None:
    ruta.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# El test que importa: las cabeceras que init_negocio.py usaría para crear
# cada Sheet tienen que ser idénticas en contenido Y orden a las que
# registro_sheets.py usa para escribir las filas. Si alguna vez vuelven a
# divergir (por ejemplo porque alguien reintroduce una copia literal en vez
# del import), este test falla.
# ---------------------------------------------------------------------------
def test_encabezados_contable_identicos_a_registro_sheets():
    assert init_negocio.ENCABEZADOS_CONTABLE == registro_sheets.COLUMNAS_CONTABLE


def test_encabezados_detalle_identicos_a_registro_sheets():
    assert init_negocio.ENCABEZADOS_DETALLE == registro_sheets.COLUMNAS_DETALLE


def test_encabezados_contable_termina_en_archivo():
    """Regresión explícita del bug real: la copia divergente terminaba en
    ADVERTENCIAS (31 columnas) y le faltaba ARCHIVO (columna 32)."""
    assert init_negocio.ENCABEZADOS_CONTABLE[-1] == "ARCHIVO"
    assert len(init_negocio.ENCABEZADOS_CONTABLE) == 32


def test_encabezados_detalle_longitud_correcta():
    assert len(init_negocio.ENCABEZADOS_DETALLE) == 15


# ---------------------------------------------------------------------------
# --dry-run: no toca disco ni llama a la API de Google.
# ---------------------------------------------------------------------------
def test_dry_run_no_crea_carpetas(tmp_path, capsys):
    config = _config_minima()
    ruta_config = tmp_path / "config.yaml"
    _escribir_config_yaml(ruta_config, config)

    codigo = init_negocio.main(["--config", str(ruta_config), "--dry-run"])

    assert codigo == 0
    salida = capsys.readouterr().out
    assert "DRY-RUN" in salida


def test_dry_run_no_importa_ni_llama_auth_google(tmp_path, monkeypatch):
    """auth_google (y por lo tanto la API de Google, tanto Sheets como
    Drive) no debe ni siquiera importarse en modo --dry-run: si el código
    intentara autenticar, este doble saboteado lo delataría."""
    llamadas: list[str] = []

    class _AuthGoogleSaboteado:
        class ErrorAutenticacion(RuntimeError):
            pass

        @staticmethod
        def servicio_sheets():
            llamadas.append("servicio_sheets")
            raise AssertionError("--dry-run no debe llamar a auth_google.servicio_sheets()")

        @staticmethod
        def servicio_drive():
            llamadas.append("servicio_drive")
            raise AssertionError("--dry-run no debe llamar a auth_google.servicio_drive()")

    monkeypatch.setitem(sys.modules, "auth_google", _AuthGoogleSaboteado)

    config = _config_minima()
    ruta_config = tmp_path / "config.yaml"
    _escribir_config_yaml(ruta_config, config)

    codigo = init_negocio.main(["--config", str(ruta_config), "--dry-run"])

    assert codigo == 0
    assert llamadas == []


def test_dry_run_no_escribe_config_yaml(tmp_path):
    config = _config_minima()
    ruta_config = tmp_path / "config.yaml"
    _escribir_config_yaml(ruta_config, config)
    texto_original = ruta_config.read_text(encoding="utf-8")

    init_negocio.main(["--config", str(ruta_config), "--dry-run"])

    assert ruta_config.read_text(encoding="utf-8") == texto_original


# ---------------------------------------------------------------------------
# Idempotencia de carpetas: si ya existen, no falla ni las duplica.
# ---------------------------------------------------------------------------
def test_preparar_carpetas_es_idempotente():
    config = _config_minima()
    almacen = _FakeAlmacenCarpetas()

    ids_1 = init_negocio.preparar_carpetas(config, almacen, dry_run=False)
    assert set(ids_1) == {"raiz", "buzon", "procesado", "revisar"}
    assert all(ids_1.values())  # ningún id vacío
    assert len(almacen.llamadas) == 4  # una asegurar_carpeta() por carpeta

    # Segunda corrida: no debe fallar ni duplicar nada (mismos ids, no crece
    # la lista de llamadas efectivas -asegurar_carpeta() es idempotente-).
    ids_2 = init_negocio.preparar_carpetas(config, almacen, dry_run=False)
    assert ids_2 == ids_1
    assert len(almacen.llamadas) == 4


def test_preparar_carpetas_arma_la_jerarquia_correcta():
    config = _config_minima(negocio="EL FARO")
    almacen = _FakeAlmacenCarpetas()

    ids = init_negocio.preparar_carpetas(config, almacen, dry_run=False)

    # buzon/procesado/revisar cuelgan de la raíz, que a su vez cuelga de
    # 'root' (padre_id=None en asegurar_carpeta significa "Mi unidad").
    assert (init_negocio.NOMBRE_BUZON, ids["raiz"]) in almacen.llamadas
    assert (init_negocio.NOMBRE_PROCESADO, ids["raiz"]) in almacen.llamadas
    assert (init_negocio.NOMBRE_REVISAR, ids["raiz"]) in almacen.llamadas
    assert ("EL FARO", None) in almacen.llamadas


def test_preparar_carpetas_dry_run_no_toca_el_almacen():
    config = _config_minima()

    resultado = init_negocio.preparar_carpetas(config, None, dry_run=True)

    assert resultado == {}


# ---------------------------------------------------------------------------
# Carpeta CONCILIACION: opcional, solo se crea si config.yaml trae la
# sección 'conciliacion'. Ver docstring de NOMBRE_CONCILIACION en
# init_negocio.py para el porqué.
# ---------------------------------------------------------------------------
def test_preparar_carpetas_crea_conciliacion_si_la_seccion_existe():
    config = _config_minima()
    config["conciliacion"] = {"carpeta": "", "empresas": []}
    almacen = _FakeAlmacenCarpetas()

    ids = init_negocio.preparar_carpetas(config, almacen, dry_run=False)

    assert set(ids) == {"raiz", "buzon", "procesado", "revisar", "conciliacion"}
    assert ids["conciliacion"]
    assert (init_negocio.NOMBRE_CONCILIACION, ids["raiz"]) in almacen.llamadas


def test_preparar_carpetas_no_crea_conciliacion_si_no_hay_seccion():
    config = _config_minima()
    assert "conciliacion" not in config
    almacen = _FakeAlmacenCarpetas()

    ids = init_negocio.preparar_carpetas(config, almacen, dry_run=False)

    assert set(ids) == {"raiz", "buzon", "procesado", "revisar"}
    assert not any(nombre == init_negocio.NOMBRE_CONCILIACION for nombre, _ in almacen.llamadas)


def test_preparar_carpetas_dry_run_informa_conciliacion_si_hay_seccion(capsys):
    config = _config_minima()
    config["conciliacion"] = {"carpeta": "", "empresas": []}

    init_negocio.preparar_carpetas(config, None, dry_run=True)

    salida = capsys.readouterr().out
    assert init_negocio.NOMBRE_CONCILIACION in salida
    assert "no se crearía la carpeta CONCILIACION" not in salida


def test_preparar_carpetas_dry_run_informa_que_no_hay_conciliacion_sin_seccion(capsys):
    config = _config_minima()
    assert "conciliacion" not in config

    init_negocio.preparar_carpetas(config, None, dry_run=True)

    salida = capsys.readouterr().out
    assert "no se crearía la carpeta CONCILIACION" in salida


# ---------------------------------------------------------------------------
# _actualizar_carpeta_conciliacion_en_config: mismo mecanismo
# (_reemplazar_clave) aplicado a la clave 'carpeta' dentro de la sección
# 'conciliacion', preservando el resto del archivo.
# ---------------------------------------------------------------------------
_CONFIG_CON_CONCILIACION = """\
negocio: LA CALETA
cuenta_google: administracion.lacaleta@gmail.com
drive:
  raiz_nombre: LA CALETA
  carpetas:
    raiz: "id-raiz"
    buzon: "id-buzon"
    procesado: "id-procesado"
    revisar: "id-revisar"
conciliacion:
  carpeta: ""       # id de Drive de LA CALETA/CONCILIACION - lo llena init_negocio.py
  empresas:
    - nombre_corto: LA CALETA CENTRO
      nombre_motor: LA CALETA CENTRO
sheets:
  contable: ""
  detalle: ""
"""


def test_actualizar_carpeta_conciliacion_en_config_preserva_comentarios_y_resto_del_archivo(tmp_path):
    ruta_config = tmp_path / "config_temporal.yaml"
    ruta_config.write_text(_CONFIG_CON_CONCILIACION, encoding="utf-8")

    init_negocio._actualizar_carpeta_conciliacion_en_config(ruta_config, "id-conciliacion-789")

    texto_final = ruta_config.read_text(encoding="utf-8")

    assert 'carpeta: "id-conciliacion-789"' in texto_final
    # El comentario junto a 'carpeta:' se preserva.
    assert "# id de Drive de LA CALETA/CONCILIACION - lo llena init_negocio.py" in texto_final
    # El resto del archivo no cambia.
    for linea_esperada in [
        "negocio: LA CALETA",
        '    raiz: "id-raiz"',
        "  empresas:",
        "    - nombre_corto: LA CALETA CENTRO",
        "      nombre_motor: LA CALETA CENTRO",
    ]:
        assert linea_esperada in texto_final

    data = yaml.safe_load(texto_final)
    assert data["conciliacion"]["carpeta"] == "id-conciliacion-789"
    assert data["negocio"] == "LA CALETA"
    assert data["drive"]["carpetas"]["raiz"] == "id-raiz"


# ---------------------------------------------------------------------------
# main() end-to-end: cableado completo de CONCILIACION, incluida la
# idempotencia (no reescribe el config si el id ya está puesto).
# ---------------------------------------------------------------------------
def _parchar_auth_google(monkeypatch, servicio_sheets, almacen_falso):
    monkeypatch.setattr(init_negocio, "AlmacenDrive", lambda servicio_drive: almacen_falso)

    class _AuthGoogleFalso:
        class ErrorAutenticacion(RuntimeError):
            pass

        @staticmethod
        def servicio_drive():
            return object()  # no se usa directo: AlmacenDrive está parcheado arriba

        @staticmethod
        def servicio_sheets():
            return servicio_sheets

    monkeypatch.setitem(sys.modules, "auth_google", _AuthGoogleFalso)


def test_main_crea_conciliacion_y_escribe_su_id_si_la_seccion_existe(tmp_path, monkeypatch):
    config = _config_minima()
    config["conciliacion"] = {"carpeta": "", "empresas": []}
    ruta_config = tmp_path / "config.yaml"
    _escribir_config_yaml(ruta_config, config)

    servicio = FakeServicioSheets()
    almacen_falso = _FakeAlmacenCarpetas()
    _parchar_auth_google(monkeypatch, servicio, almacen_falso)

    codigo = init_negocio.main(["--config", str(ruta_config)])

    assert codigo == 0
    assert len(almacen_falso.llamadas) == 5  # raiz + buzon + procesado + revisar + conciliacion

    config_final = yaml.safe_load(ruta_config.read_text(encoding="utf-8"))
    assert config_final["conciliacion"]["carpeta"]

    # Segunda corrida: idempotente. No debe reescribir el config (el id ya
    # está puesto), ni volver a llamar a asegurar_carpeta() de más.
    texto_tras_primera = ruta_config.read_text(encoding="utf-8")
    codigo_2 = init_negocio.main(["--config", str(ruta_config)])
    assert codigo_2 == 0
    assert ruta_config.read_text(encoding="utf-8") == texto_tras_primera
    assert len(almacen_falso.llamadas) == 5


def test_main_no_crea_conciliacion_ni_escribe_clave_si_no_hay_seccion(tmp_path, monkeypatch):
    config = _config_minima()
    assert "conciliacion" not in config
    ruta_config = tmp_path / "config.yaml"
    _escribir_config_yaml(ruta_config, config)

    servicio = FakeServicioSheets()
    almacen_falso = _FakeAlmacenCarpetas()
    _parchar_auth_google(monkeypatch, servicio, almacen_falso)

    codigo = init_negocio.main(["--config", str(ruta_config)])

    assert codigo == 0
    assert len(almacen_falso.llamadas) == 4  # raiz + buzon + procesado + revisar, sin conciliacion
    assert not any(nombre == init_negocio.NOMBRE_CONCILIACION for nombre, _ in almacen_falso.llamadas)

    config_final = yaml.safe_load(ruta_config.read_text(encoding="utf-8"))
    assert "conciliacion" not in config_final


def test_dry_run_conciliacion_no_llama_api_ni_escribe_config(tmp_path, capsys):
    config = _config_minima()
    config["conciliacion"] = {"carpeta": "", "empresas": []}
    ruta_config = tmp_path / "config.yaml"
    _escribir_config_yaml(ruta_config, config)
    texto_original = ruta_config.read_text(encoding="utf-8")

    codigo = init_negocio.main(["--config", str(ruta_config), "--dry-run"])

    assert codigo == 0
    assert ruta_config.read_text(encoding="utf-8") == texto_original
    salida = capsys.readouterr().out
    assert init_negocio.NOMBRE_CONCILIACION in salida


# ---------------------------------------------------------------------------
# Subcarpetas de 00_BUZON por tipo (drive.carpetas.buzon_tipos): igual que
# CONCILIACION, opcionales — solo se crean si config.yaml trae la sección.
# ---------------------------------------------------------------------------
def test_preparar_carpetas_crea_buzon_tipos_si_la_seccion_existe():
    config = _config_minima(con_buzon_tipos=True)
    almacen = _FakeAlmacenCarpetas()

    ids = init_negocio.preparar_carpetas(config, almacen, dry_run=False)

    assert set(ids) == {"raiz", "buzon", "procesado", "revisar", "buzon_tipos"}
    assert set(ids["buzon_tipos"]) == {"facturas", "notas_venta", "liquidaciones", "otros"}
    assert all(ids["buzon_tipos"].values())  # ningún id vacío

    # Las 4 subcarpetas cuelgan de 00_BUZON (id_buzon), no de la raíz.
    for nombre in init_negocio.BUZON_TIPOS.values():
        assert (nombre, ids["buzon"]) in almacen.llamadas


def test_preparar_carpetas_no_crea_buzon_tipos_si_no_hay_seccion():
    config = _config_minima()
    assert "buzon_tipos" not in config["drive"]["carpetas"]
    almacen = _FakeAlmacenCarpetas()

    ids = init_negocio.preparar_carpetas(config, almacen, dry_run=False)

    assert set(ids) == {"raiz", "buzon", "procesado", "revisar"}
    assert "buzon_tipos" not in ids
    nombres_creados = {nombre for nombre, _ in almacen.llamadas}
    assert not nombres_creados & set(init_negocio.BUZON_TIPOS.values())


def test_preparar_carpetas_buzon_tipos_es_idempotente():
    config = _config_minima(con_buzon_tipos=True)
    almacen = _FakeAlmacenCarpetas()

    ids_1 = init_negocio.preparar_carpetas(config, almacen, dry_run=False)
    llamadas_1 = len(almacen.llamadas)
    assert llamadas_1 == 8  # raiz + buzon + procesado + revisar + 4 tipos

    ids_2 = init_negocio.preparar_carpetas(config, almacen, dry_run=False)
    assert ids_2 == ids_1
    assert len(almacen.llamadas) == llamadas_1  # no crece: nada se duplica


def test_preparar_carpetas_dry_run_informa_buzon_tipos_si_hay_seccion(capsys):
    config = _config_minima(con_buzon_tipos=True)

    resultado = init_negocio.preparar_carpetas(config, None, dry_run=True)

    assert resultado == {}
    salida = capsys.readouterr().out
    for nombre in init_negocio.BUZON_TIPOS.values():
        assert nombre in salida
    assert "no se crearían las" not in salida


def test_preparar_carpetas_dry_run_informa_que_no_hay_buzon_tipos_sin_seccion(capsys):
    config = _config_minima()
    assert "buzon_tipos" not in config["drive"]["carpetas"]

    init_negocio.preparar_carpetas(config, None, dry_run=True)

    salida = capsys.readouterr().out
    assert "no se crearían las" in salida


# ---------------------------------------------------------------------------
# _reemplazar_clave_anidada / _actualizar_buzon_tipos_en_config: acotan el
# reemplazo al bloque de 'buzon_tipos:' para no chocar con una clave del
# mismo nombre en otra sección del archivo (ver docstring en init_negocio.py).
# ---------------------------------------------------------------------------
_CONFIG_CON_BUZON_TIPOS = """\
negocio: LA CALETA
cuenta_google: administracion.lacaleta@gmail.com
drive:
  raiz_nombre: LA CALETA
  carpetas:
    raiz: "id-raiz"
    buzon: "id-buzon"
    procesado: "id-procesado"
    revisar: "id-revisar"
    buzon_tipos:
      facturas: ""       # lo rellena init_negocio.py
      notas_venta: ""
      liquidaciones: ""
      otros: ""
sheets:
  contable: ""
  detalle: ""
# Sección ajena, deliberadamente con una clave "otros" que NO debe tocarse:
# es la trampa que _reemplazar_clave_anidada tiene que esquivar.
seccion_ajena:
  otros: "no me toques"
  facturas: "tampoco a mi"
"""


def test_actualizar_buzon_tipos_en_config_preserva_comentarios_y_resto_del_archivo(tmp_path):
    ruta_config = tmp_path / "config_temporal.yaml"
    ruta_config.write_text(_CONFIG_CON_BUZON_TIPOS, encoding="utf-8")

    init_negocio._actualizar_buzon_tipos_en_config(
        ruta_config,
        {
            "facturas": "id-facturas",
            "notas_venta": "id-notas-venta",
            "liquidaciones": "id-liquidaciones",
            "otros": "id-otros",
        },
    )

    texto_final = ruta_config.read_text(encoding="utf-8")

    assert 'facturas: "id-facturas"' in texto_final
    assert 'notas_venta: "id-notas-venta"' in texto_final
    assert 'liquidaciones: "id-liquidaciones"' in texto_final
    assert 'otros: "id-otros"' in texto_final
    # El comentario junto a 'facturas:' se preserva.
    assert "# lo rellena init_negocio.py" in texto_final
    # El resto del archivo no cambia.
    for linea_esperada in [
        "negocio: LA CALETA",
        '    raiz: "id-raiz"',
        "sheets:",
    ]:
        assert linea_esperada in texto_final

    data = yaml.safe_load(texto_final)
    assert data["drive"]["carpetas"]["buzon_tipos"] == {
        "facturas": "id-facturas",
        "notas_venta": "id-notas-venta",
        "liquidaciones": "id-liquidaciones",
        "otros": "id-otros",
    }
    # La clave 'otros'/'facturas' de la sección ajena NUNCA debió tocarse:
    # esta es la regresión explícita de la ambigüedad de nombres genéricos.
    assert data["seccion_ajena"]["otros"] == "no me toques"
    assert data["seccion_ajena"]["facturas"] == "tampoco a mi"


def test_main_crea_buzon_tipos_y_escribe_sus_ids_si_la_seccion_existe(tmp_path, monkeypatch):
    config = _config_minima(con_buzon_tipos=True)
    ruta_config = tmp_path / "config.yaml"
    _escribir_config_yaml(ruta_config, config)

    servicio = FakeServicioSheets()
    almacen_falso = _FakeAlmacenCarpetas()
    _parchar_auth_google(monkeypatch, servicio, almacen_falso)

    codigo = init_negocio.main(["--config", str(ruta_config)])

    assert codigo == 0
    assert len(almacen_falso.llamadas) == 8  # raiz + buzon + procesado + revisar + 4 tipos

    config_final = yaml.safe_load(ruta_config.read_text(encoding="utf-8"))
    ids_finales = config_final["drive"]["carpetas"]["buzon_tipos"]
    assert all(ids_finales.values())

    # Segunda corrida: idempotente, no reescribe el config ni duplica carpetas.
    texto_tras_primera = ruta_config.read_text(encoding="utf-8")
    codigo_2 = init_negocio.main(["--config", str(ruta_config)])
    assert codigo_2 == 0
    assert ruta_config.read_text(encoding="utf-8") == texto_tras_primera
    assert len(almacen_falso.llamadas) == 8


def test_main_no_crea_buzon_tipos_ni_escribe_ids_si_no_hay_seccion(tmp_path, monkeypatch):
    config = _config_minima()
    assert "buzon_tipos" not in config["drive"]["carpetas"]
    ruta_config = tmp_path / "config.yaml"
    _escribir_config_yaml(ruta_config, config)

    servicio = FakeServicioSheets()
    almacen_falso = _FakeAlmacenCarpetas()
    _parchar_auth_google(monkeypatch, servicio, almacen_falso)

    codigo = init_negocio.main(["--config", str(ruta_config)])

    assert codigo == 0
    assert len(almacen_falso.llamadas) == 4  # sin las 4 de buzon_tipos
    nombres_creados = {nombre for nombre, _ in almacen_falso.llamadas}
    assert not nombres_creados & set(init_negocio.BUZON_TIPOS.values())

    config_final = yaml.safe_load(ruta_config.read_text(encoding="utf-8"))
    assert "buzon_tipos" not in config_final["drive"]["carpetas"]


def test_dry_run_buzon_tipos_no_llama_api_ni_escribe_config(tmp_path, capsys):
    config = _config_minima(con_buzon_tipos=True)
    ruta_config = tmp_path / "config.yaml"
    _escribir_config_yaml(ruta_config, config)
    texto_original = ruta_config.read_text(encoding="utf-8")

    codigo = init_negocio.main(["--config", str(ruta_config), "--dry-run"])

    assert codigo == 0
    assert ruta_config.read_text(encoding="utf-8") == texto_original
    salida = capsys.readouterr().out
    for nombre in init_negocio.BUZON_TIPOS.values():
        assert nombre in salida


# ---------------------------------------------------------------------------
# _nombre_carpeta_empresa(): mismo criterio que
# procesar.nombre_empresa_carpeta() (duplicado deliberado, ver docstring en
# init_negocio.py) — un nombre con espacio sale con guion bajo.
# ---------------------------------------------------------------------------
def test_nombre_carpeta_empresa_reemplaza_espacios_por_guion_bajo():
    assert init_negocio._nombre_carpeta_empresa("EL TEMPLO") == "EL_TEMPLO"
    assert init_negocio._nombre_carpeta_empresa("INSTITUCION") == "INSTITUCION"


# ---------------------------------------------------------------------------
# Subcarpetas de 00_BUZON por empresa (drive.carpetas.buzon_empresas):
# alternativa anidada a buzon_tipos para negocios con varias empresas. Las
# dos secciones son mutuamente excluyentes; ver _validar_buzon_empresas().
# ---------------------------------------------------------------------------
def test_preparar_carpetas_crea_buzon_empresas_si_la_seccion_existe():
    config = _config_minima(con_buzon_empresas=True, nombres_empresas=["EL TEMPLO", "INSTITUCION"])
    almacen = _FakeAlmacenCarpetas()

    ids = init_negocio.preparar_carpetas(config, almacen, dry_run=False)

    assert set(ids) == {"raiz", "buzon", "procesado", "revisar", "buzon_empresas"}
    assert set(ids["buzon_empresas"]) == {"EL TEMPLO", "INSTITUCION"}
    for nombre_corto in ("EL TEMPLO", "INSTITUCION"):
        assert set(ids["buzon_empresas"][nombre_corto]) == {"facturas", "notas_venta", "liquidaciones", "otros"}
        assert all(ids["buzon_empresas"][nombre_corto].values())  # ningún id vacío

    # Las carpetas de empresa cuelgan de 00_BUZON (id_buzon), no de la raíz,
    # y usan el nombre de carpeta (guion bajo), no el nombre_corto tal cual.
    assert ("EL_TEMPLO", ids["buzon"]) in almacen.llamadas
    assert ("INSTITUCION", ids["buzon"]) in almacen.llamadas
    id_el_templo = ids["buzon_empresas"]["EL TEMPLO"]["facturas"]
    # Y las 4 subcarpetas de tipo cuelgan de la carpeta de la empresa, no de
    # 00_BUZON directamente ni de la de la otra empresa.
    id_carpeta_el_templo = [
        nid for (nombre, padre), nid in almacen._existentes.items() if nombre == "EL_TEMPLO"
    ][0]
    assert (init_negocio.BUZON_TIPOS["facturas"], id_carpeta_el_templo) in almacen.llamadas


def test_preparar_carpetas_no_crea_nada_sin_buzon_tipos_ni_buzon_empresas():
    config = _config_minima()
    assert "buzon_tipos" not in config["drive"]["carpetas"]
    assert "buzon_empresas" not in config["drive"]["carpetas"]
    almacen = _FakeAlmacenCarpetas()

    ids = init_negocio.preparar_carpetas(config, almacen, dry_run=False)

    assert set(ids) == {"raiz", "buzon", "procesado", "revisar"}
    nombres_creados = {nombre for nombre, _ in almacen.llamadas}
    assert not nombres_creados & set(init_negocio.BUZON_TIPOS.values())
    assert "EL_TEMPLO" not in nombres_creados


def test_preparar_carpetas_buzon_empresas_es_idempotente():
    config = _config_minima(con_buzon_empresas=True, nombres_empresas=["EL TEMPLO", "INSTITUCION", "ILLAWARA"])
    almacen = _FakeAlmacenCarpetas()

    ids_1 = init_negocio.preparar_carpetas(config, almacen, dry_run=False)
    llamadas_1 = len(almacen.llamadas)
    assert llamadas_1 == 4 + 3 * (1 + 4)  # raiz+buzon+procesado+revisar + 3 empresas * (carpeta + 4 tipos)

    ids_2 = init_negocio.preparar_carpetas(config, almacen, dry_run=False)
    assert ids_2 == ids_1
    assert len(almacen.llamadas) == llamadas_1  # no crece: nada se duplica


def test_preparar_carpetas_aborta_si_hay_buzon_tipos_y_buzon_empresas_a_la_vez():
    config = _config_minima(con_buzon_tipos=True, con_buzon_empresas=True, nombres_empresas=["EL TEMPLO"])
    almacen = _FakeAlmacenCarpetas()

    with pytest.raises(SystemExit) as excinfo:
        init_negocio.preparar_carpetas(config, almacen, dry_run=False)
    assert "buzon_tipos" in str(excinfo.value)
    assert "buzon_empresas" in str(excinfo.value)


def test_preparar_carpetas_aborta_si_buzon_empresas_nombra_empresa_no_configurada():
    config = _config_minima(con_buzon_empresas=True, nombres_empresas=["EL TEMPLO"])
    # Nombra una empresa que NO está en config["empresas"].
    config["drive"]["carpetas"]["buzon_empresas"]["NO_EXISTE"] = {
        "facturas": "", "notas_venta": "", "liquidaciones": "", "otros": "",
    }
    almacen = _FakeAlmacenCarpetas()

    with pytest.raises(SystemExit) as excinfo:
        init_negocio.preparar_carpetas(config, almacen, dry_run=False)
    assert "NO_EXISTE" in str(excinfo.value)


def test_preparar_carpetas_dry_run_informa_arbol_completo_de_buzon_empresas(capsys):
    config = _config_minima(con_buzon_empresas=True, nombres_empresas=["EL TEMPLO", "INSTITUCION", "ILLAWARA"])

    resultado = init_negocio.preparar_carpetas(config, None, dry_run=True)

    assert resultado == {}
    salida = capsys.readouterr().out
    # Las 12 rutas completas (3 empresas x 4 tipos) aparecen en la salida.
    for nombre_corto in ("EL TEMPLO", "INSTITUCION", "ILLAWARA"):
        nombre_carpeta = init_negocio._nombre_carpeta_empresa(nombre_corto)
        assert f"00_BUZON/{nombre_carpeta}" in salida
        for nombre_tipo in init_negocio.BUZON_TIPOS.values():
            assert f"00_BUZON/{nombre_carpeta}/{nombre_tipo}" in salida


def test_dry_run_no_crea_buzon_empresas_ni_llama_api(tmp_path, capsys):
    config = _config_minima(con_buzon_empresas=True, nombres_empresas=["EL TEMPLO", "INSTITUCION"])
    ruta_config = tmp_path / "config.yaml"
    _escribir_config_yaml(ruta_config, config)
    texto_original = ruta_config.read_text(encoding="utf-8")

    codigo = init_negocio.main(["--config", str(ruta_config), "--dry-run"])

    assert codigo == 0
    assert ruta_config.read_text(encoding="utf-8") == texto_original
    salida = capsys.readouterr().out
    assert "EL_TEMPLO" in salida
    assert "INSTITUCION" in salida


def test_main_crea_buzon_empresas_y_escribe_sus_ids_en_el_sitio_correcto(tmp_path, monkeypatch):
    config = _config_minima(con_buzon_empresas=True, nombres_empresas=["EL TEMPLO", "INSTITUCION"])
    ruta_config = tmp_path / "config.yaml"
    _escribir_config_yaml(ruta_config, config)

    servicio = FakeServicioSheets()
    almacen_falso = _FakeAlmacenCarpetas()
    _parchar_auth_google(monkeypatch, servicio, almacen_falso)

    codigo = init_negocio.main(["--config", str(ruta_config)])

    assert codigo == 0
    assert len(almacen_falso.llamadas) == 4 + 2 * (1 + 4)  # raiz+buzon+procesado+revisar + 2*(carpeta+4 tipos)

    config_final = yaml.safe_load(ruta_config.read_text(encoding="utf-8"))
    ids_finales = config_final["drive"]["carpetas"]["buzon_empresas"]
    assert set(ids_finales) == {"EL TEMPLO", "INSTITUCION"}
    for nombre_corto in ("EL TEMPLO", "INSTITUCION"):
        assert all(ids_finales[nombre_corto].values())
    # Los ids de una empresa no se mezclaron con los de la otra.
    assert len({tuple(sorted(v.values())) for v in ids_finales.values()}) == 2

    # Segunda corrida: idempotente, no reescribe el config ni duplica carpetas.
    texto_tras_primera = ruta_config.read_text(encoding="utf-8")
    codigo_2 = init_negocio.main(["--config", str(ruta_config)])
    assert codigo_2 == 0
    assert ruta_config.read_text(encoding="utf-8") == texto_tras_primera
    assert len(almacen_falso.llamadas) == 4 + 2 * (1 + 4)


def test_main_aborta_si_buzon_empresas_nombra_empresa_no_configurada(tmp_path, monkeypatch):
    config = _config_minima(con_buzon_empresas=True, nombres_empresas=["EL TEMPLO"])
    config["drive"]["carpetas"]["buzon_empresas"]["NO_EXISTE"] = {
        "facturas": "", "notas_venta": "", "liquidaciones": "", "otros": "",
    }
    ruta_config = tmp_path / "config.yaml"
    _escribir_config_yaml(ruta_config, config)

    servicio = FakeServicioSheets()
    almacen_falso = _FakeAlmacenCarpetas()
    _parchar_auth_google(monkeypatch, servicio, almacen_falso)

    with pytest.raises(SystemExit):
        init_negocio.main(["--config", str(ruta_config)])
    assert not almacen_falso.llamadas  # aborta antes de crear ninguna carpeta


def test_main_aborta_si_hay_buzon_tipos_y_buzon_empresas_a_la_vez(tmp_path, monkeypatch):
    config = _config_minima(con_buzon_tipos=True, con_buzon_empresas=True, nombres_empresas=["EL TEMPLO"])
    ruta_config = tmp_path / "config.yaml"
    _escribir_config_yaml(ruta_config, config)

    servicio = FakeServicioSheets()
    almacen_falso = _FakeAlmacenCarpetas()
    _parchar_auth_google(monkeypatch, servicio, almacen_falso)

    with pytest.raises(SystemExit):
        init_negocio.main(["--config", str(ruta_config)])
    assert not almacen_falso.llamadas


# ---------------------------------------------------------------------------
# preparar_sheet(): reutilización, creación e ID inválido, con el doble de
# prueba del servicio de Sheets (sin red).
# ---------------------------------------------------------------------------
def test_preparar_sheet_reutiliza_id_existente_si_es_accesible():
    servicio = FakeServicioSheets(ids_accesibles={"id-viejo"})

    resultado = init_negocio.preparar_sheet(
        servicio, "LA CALETA", "contable", init_negocio.ENCABEZADOS_CONTABLE, "id-viejo", False
    )

    assert resultado == "id-viejo"
    assert servicio.creados == []  # no crea uno nuevo si el existente sirve


def test_preparar_sheet_crea_uno_nuevo_si_no_hay_id():
    servicio = FakeServicioSheets()

    resultado = init_negocio.preparar_sheet(
        servicio, "LA CALETA", "contable", init_negocio.ENCABEZADOS_CONTABLE, "", False
    )

    assert resultado.startswith("nuevo-")
    assert len(servicio.creados) == 1
    assert servicio.creados[0]["titulo"] == "LA CALETA - contable"
    assert servicio.valores_escritos[resultado] == [init_negocio.ENCABEZADOS_CONTABLE]


def test_preparar_sheet_titulo_usa_solo_el_negocio_configurado():
    """Regresión de un defecto de replicabilidad real (encontrado corriendo
    la prueba del Paso 4 de esta fase con un negocio ficticio distinto de
    SCONCHA): el título anteponía "SCONCHA" sin importar el negocio
    configurado (ej. "SCONCHA EL FARO - contable"), confuso para el dueño de
    otro negocio. El título debe salir únicamente de `negocio`, sin ningún
    prefijo fijo."""
    servicio = FakeServicioSheets()

    init_negocio.preparar_sheet(servicio, "EL FARO", "detalle", init_negocio.ENCABEZADOS_DETALLE, "", False)

    assert servicio.creados[0]["titulo"] == "EL FARO - detalle"
    assert "SCONCHA" not in servicio.creados[0]["titulo"]


def test_preparar_sheet_titulo_para_sconcha_mismo_no_cambia():
    servicio = FakeServicioSheets()

    init_negocio.preparar_sheet(servicio, "SCONCHA", "detalle", init_negocio.ENCABEZADOS_DETALLE, "", False)

    assert servicio.creados[0]["titulo"] == "SCONCHA - detalle"


def test_preparar_sheet_sale_con_mensaje_claro_si_id_no_es_accesible():
    servicio = FakeServicioSheets(ids_accesibles=set())  # "id-borrado" no está

    with pytest.raises(SystemExit) as exc_info:
        init_negocio.preparar_sheet(
            servicio, "LA CALETA", "contable", init_negocio.ENCABEZADOS_CONTABLE, "id-borrado", False
        )

    mensaje = str(exc_info.value)
    assert "id-borrado" in mensaje
    assert "config.yaml" in mensaje


def test_preparar_sheet_dry_run_no_toca_el_servicio():
    servicio = FakeServicioSheets()

    resultado = init_negocio.preparar_sheet(
        servicio, "LA CALETA", "contable", init_negocio.ENCABEZADOS_CONTABLE, "", True
    )

    assert resultado == ""
    assert servicio.creados == []


# ---------------------------------------------------------------------------
# _actualizar_ids_en_config: reescribe solo contable/detalle, preserva todo
# lo demás (comentarios incluidos). Siempre sobre un archivo temporal, nunca
# sobre el config.yaml real del proyecto.
# ---------------------------------------------------------------------------
_CONFIG_CON_COMENTARIOS = """\
negocio: LA CALETA
cuenta_google: administracion.lacaleta@gmail.com
drive:
  raiz: "G:/Mi unidad/LA CALETA"     # ajustar a la letra que asigne Drive
  buzon: "00_BUZON"
  procesado: "01_PROCESADO"
  revisar: "02_REVISAR"
empresas:
  - nombre_corto: LA CALETA CENTRO
    razon_social: LA CALETA CENTRO S.A.C.
    ruc: "20123456789"
    locales: [CENTRO]
sheets:
  contable: ""      # lo llena init_negocio.py
  detalle: ""
"""


def test_actualizar_ids_en_config_preserva_comentarios_y_resto_del_archivo(tmp_path):
    ruta_config = tmp_path / "config_temporal.yaml"
    ruta_config.write_text(_CONFIG_CON_COMENTARIOS, encoding="utf-8")

    init_negocio._actualizar_ids_en_config(ruta_config, "id-contable-123", "id-detalle-456")

    texto_final = ruta_config.read_text(encoding="utf-8")

    # Los IDs quedaron escritos.
    assert 'contable: "id-contable-123"' in texto_final
    assert 'detalle: "id-detalle-456"' in texto_final
    # El comentario junto a "contable:" se preserva.
    assert "# lo llena init_negocio.py" in texto_final
    # El resto del archivo (comentarios, estructura, otras claves) no cambia.
    for linea_esperada in [
        "negocio: LA CALETA",
        'cuenta_google: administracion.lacaleta@gmail.com',
        '  raiz: "G:/Mi unidad/LA CALETA"     # ajustar a la letra que asigne Drive',
        "empresas:",
        "  - nombre_corto: LA CALETA CENTRO",
        '    ruc: "20123456789"',
    ]:
        assert linea_esperada in texto_final

    # El archivo sigue siendo YAML válido con los valores correctos.
    data = yaml.safe_load(texto_final)
    assert data["sheets"]["contable"] == "id-contable-123"
    assert data["sheets"]["detalle"] == "id-detalle-456"
    assert data["negocio"] == "LA CALETA"
    assert data["empresas"][0]["ruc"] == "20123456789"


# ---------------------------------------------------------------------------
# _actualizar_carpetas_en_config: mismo mecanismo (_reemplazar_clave)
# aplicado a las 4 claves de drive.carpetas.
# ---------------------------------------------------------------------------
_CONFIG_CON_CARPETAS = """\
negocio: LA CALETA
cuenta_google: administracion.lacaleta@gmail.com
drive:
  raiz_nombre: LA CALETA        # nombre de la carpeta en "Mi unidad"
  carpetas:
    raiz: ""                    # los rellena init_negocio.py
    buzon: ""
    procesado: ""
    revisar: ""
empresas:
  - nombre_corto: LA CALETA CENTRO
    razon_social: LA CALETA CENTRO S.A.C.
    ruc: "20123456789"
    locales: [CENTRO]
sheets:
  contable: ""
  detalle: ""
"""


def test_actualizar_carpetas_en_config_preserva_comentarios_y_resto_del_archivo(tmp_path):
    ruta_config = tmp_path / "config_temporal.yaml"
    ruta_config.write_text(_CONFIG_CON_CARPETAS, encoding="utf-8")

    init_negocio._actualizar_carpetas_en_config(
        ruta_config,
        {"raiz": "id-raiz", "buzon": "id-buzon", "procesado": "id-procesado", "revisar": "id-revisar"},
    )

    texto_final = ruta_config.read_text(encoding="utf-8")

    assert 'raiz: "id-raiz"' in texto_final
    assert 'buzon: "id-buzon"' in texto_final
    assert 'procesado: "id-procesado"' in texto_final
    assert 'revisar: "id-revisar"' in texto_final
    # El comentario junto a 'raiz_nombre:' y el resto del archivo no cambian.
    assert '# nombre de la carpeta en "Mi unidad"' in texto_final
    assert "negocio: LA CALETA" in texto_final
    assert "  - nombre_corto: LA CALETA CENTRO" in texto_final
    assert '    ruc: "20123456789"' in texto_final

    data = yaml.safe_load(texto_final)
    assert data["drive"]["carpetas"] == {
        "raiz": "id-raiz", "buzon": "id-buzon", "procesado": "id-procesado", "revisar": "id-revisar"
    }
    assert data["drive"]["raiz_nombre"] == "LA CALETA"
    assert data["negocio"] == "LA CALETA"


# ---------------------------------------------------------------------------
# main() end-to-end con dobles de servicio (sin red): crea carpetas y sheets
# nuevos, actualiza config.yaml con todos los IDs.
# ---------------------------------------------------------------------------
def test_main_crea_sheets_y_actualiza_config(tmp_path, monkeypatch):
    config = _config_minima()
    ruta_config = tmp_path / "config.yaml"
    _escribir_config_yaml(ruta_config, config)

    servicio = FakeServicioSheets()
    almacen_falso = _FakeAlmacenCarpetas()
    monkeypatch.setattr(init_negocio, "AlmacenDrive", lambda servicio_drive: almacen_falso)

    class _AuthGoogleFalso:
        class ErrorAutenticacion(RuntimeError):
            pass

        @staticmethod
        def servicio_drive():
            return object()  # no se usa directo: AlmacenDrive está parcheado arriba

        @staticmethod
        def servicio_sheets():
            return servicio

    monkeypatch.setitem(sys.modules, "auth_google", _AuthGoogleFalso)

    codigo = init_negocio.main(["--config", str(ruta_config)])

    assert codigo == 0
    assert len(servicio.creados) == 2  # contable + detalle
    assert len(almacen_falso.llamadas) == 4  # raiz + buzon + procesado + revisar

    config_final = yaml.safe_load(ruta_config.read_text(encoding="utf-8"))
    assert config_final["sheets"]["contable"].startswith("nuevo-")
    assert config_final["sheets"]["detalle"].startswith("nuevo-")
    assert config_final["drive"]["carpetas"]["raiz"]
    assert config_final["drive"]["carpetas"]["buzon"]
    assert config_final["drive"]["carpetas"]["procesado"]
    assert config_final["drive"]["carpetas"]["revisar"]

    # Segunda corrida: idempotente, no crea sheets ni carpetas de nuevo.
    codigo_2 = init_negocio.main(["--config", str(ruta_config)])
    assert codigo_2 == 0
    assert len(servicio.creados) == 2
    assert len(almacen_falso.llamadas) == 4
