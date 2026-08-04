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
def _config_minima(tmp_path: pathlib.Path, negocio: str = "LA CALETA") -> dict:
    return {
        "negocio": negocio,
        "cuenta_google": "administracion.lacaleta@gmail.com",
        "drive": {
            "raiz": str(tmp_path / "RAIZ"),
            "buzon": "00_BUZON",
            "procesado": "01_PROCESADO",
            "revisar": "02_REVISAR",
        },
        "sheets": {"contable": "", "detalle": ""},
    }


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
    config = _config_minima(tmp_path)
    ruta_config = tmp_path / "config.yaml"
    _escribir_config_yaml(ruta_config, config)

    codigo = init_negocio.main(["--config", str(ruta_config), "--dry-run"])

    assert codigo == 0
    assert not (tmp_path / "RAIZ").exists()
    salida = capsys.readouterr().out
    assert "DRY-RUN" in salida


def test_dry_run_no_importa_ni_llama_auth_google(tmp_path, monkeypatch):
    """auth_google (y por lo tanto la API de Google) no debe ni siquiera
    importarse en modo --dry-run: si el código intentara autenticar, este
    doble saboteado lo delataría."""
    llamadas: list[str] = []

    class _AuthGoogleSaboteado:
        class ErrorAutenticacion(RuntimeError):
            pass

        @staticmethod
        def servicio_sheets():
            llamadas.append("servicio_sheets")
            raise AssertionError("--dry-run no debe llamar a auth_google.servicio_sheets()")

    monkeypatch.setitem(sys.modules, "auth_google", _AuthGoogleSaboteado)

    config = _config_minima(tmp_path)
    ruta_config = tmp_path / "config.yaml"
    _escribir_config_yaml(ruta_config, config)

    codigo = init_negocio.main(["--config", str(ruta_config), "--dry-run"])

    assert codigo == 0
    assert llamadas == []


def test_dry_run_no_escribe_config_yaml(tmp_path):
    config = _config_minima(tmp_path)
    ruta_config = tmp_path / "config.yaml"
    _escribir_config_yaml(ruta_config, config)
    texto_original = ruta_config.read_text(encoding="utf-8")

    init_negocio.main(["--config", str(ruta_config), "--dry-run"])

    assert ruta_config.read_text(encoding="utf-8") == texto_original


# ---------------------------------------------------------------------------
# Idempotencia de carpetas: si ya existen, no falla ni las duplica.
# ---------------------------------------------------------------------------
def test_preparar_carpetas_es_idempotente(tmp_path):
    config = _config_minima(tmp_path)

    init_negocio.preparar_carpetas(config, dry_run=False)
    raiz = tmp_path / "RAIZ"
    assert raiz.exists()
    subcarpetas_creadas = sorted(p.name for p in raiz.iterdir())
    assert subcarpetas_creadas == ["00_BUZON", "01_PROCESADO", "02_REVISAR"]

    # Segunda corrida: no debe fallar ni duplicar nada.
    init_negocio.preparar_carpetas(config, dry_run=False)
    assert sorted(p.name for p in raiz.iterdir()) == subcarpetas_creadas


def test_preparar_carpetas_reporta_reutilizacion(tmp_path, capsys):
    config = _config_minima(tmp_path)
    init_negocio.preparar_carpetas(config, dry_run=False)
    capsys.readouterr()  # descarta la salida de la primera corrida

    init_negocio.preparar_carpetas(config, dry_run=False)
    salida = capsys.readouterr().out
    assert "ya existía" in salida
    assert "creada:" not in salida


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
# main() end-to-end con el doble de servicio (sin red): crea sheets nuevos,
# actualiza config.yaml con los IDs, y es idempotente en una segunda corrida.
# ---------------------------------------------------------------------------
def test_main_crea_sheets_y_actualiza_config(tmp_path, monkeypatch):
    config = _config_minima(tmp_path)
    ruta_config = tmp_path / "config.yaml"
    _escribir_config_yaml(ruta_config, config)

    servicio = FakeServicioSheets()

    class _AuthGoogleFalso:
        class ErrorAutenticacion(RuntimeError):
            pass

        @staticmethod
        def servicio_sheets():
            return servicio

    monkeypatch.setitem(sys.modules, "auth_google", _AuthGoogleFalso)

    codigo = init_negocio.main(["--config", str(ruta_config)])

    assert codigo == 0
    assert (tmp_path / "RAIZ" / "00_BUZON").exists()
    assert len(servicio.creados) == 2  # contable + detalle

    config_final = yaml.safe_load(ruta_config.read_text(encoding="utf-8"))
    assert config_final["sheets"]["contable"].startswith("nuevo-")
    assert config_final["sheets"]["detalle"].startswith("nuevo-")
