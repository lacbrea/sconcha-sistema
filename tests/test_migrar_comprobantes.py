"""Tests de migrar_comprobantes.py.

Sigue el patrón de tests/test_almacen_drive.py: un doble en memoria del
Resource de drive v3 (FakeServicioDrive, copiado tal cual de ese archivo, sin
red ni credenciales), y tmp_path de pytest para simular el árbol de origen de
OneDrive.

Correr con:
    C:\\Python312\\python.exe -m pytest tests/test_migrar_comprobantes.py -q
"""
from __future__ import annotations

import logging
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from almacen_drive import AlmacenDrive, MIME_CARPETA  # noqa: E402
import migrar_comprobantes as mc  # noqa: E402


# -----------------------------------------------------------------------------
# Doble de prueba del Resource de drive v3 (idéntico al de
# tests/test_almacen_drive.py: mismo contrato de AlmacenDrive, misma
# interpretación de la query de Drive).
# -----------------------------------------------------------------------------
class FakeServicioDrive:
    def __init__(self):
        self._archivos: dict[str, dict] = {}
        self._contador = 0
        self._pendiente = None

    def agregar(self, name, parents, mimeType="application/pdf", trashed=False, content=b""):
        self._contador += 1
        file_id = f"id-{self._contador}"
        self._archivos[file_id] = {
            "id": file_id,
            "name": name,
            "parents": list(parents),
            "mimeType": mimeType,
            "trashed": trashed,
            "content": content,
            "webViewLink": f"https://drive.google.com/file/d/{file_id}/view",
        }
        return file_id

    def files(self):
        return self

    def list(self, q, fields=None, pageSize=None, pageToken=None):  # noqa: N803
        self._pendiente = ("list", q)
        return self

    def get(self, fileId, fields=None):  # noqa: N803
        self._pendiente = ("get", fileId)
        return self

    def get_media(self, fileId):  # noqa: N803
        self._pendiente = ("get_media", fileId)
        return self

    def create(self, body, fields=None, media_body=None):
        self._pendiente = ("create", body, media_body)
        return self

    def update(self, fileId, addParents=None, removeParents=None, body=None, fields=None):  # noqa: N803
        self._pendiente = ("update", fileId, addParents, removeParents, body)
        return self

    def execute(self):
        accion = self._pendiente[0]

        if accion == "list":
            _, q = self._pendiente
            return {"files": self._filtrar(q)}

        if accion == "get":
            _, file_id = self._pendiente
            archivo = self._archivos[file_id]
            return {"id": file_id, "parents": archivo["parents"], "webViewLink": archivo["webViewLink"]}

        if accion == "get_media":
            _, file_id = self._pendiente
            return self._archivos[file_id]["content"]

        if accion == "create":
            _, body, media_body = self._pendiente
            self._contador += 1
            file_id = f"id-{self._contador}"
            contenido = b""
            mimetype = body.get("mimeType", "text/plain")
            if media_body is not None:
                contenido = media_body.getbytes(0, media_body.size())
                mimetype = body.get("mimeType") or media_body.mimetype()
            self._archivos[file_id] = {
                "id": file_id,
                "name": body.get("name", ""),
                "parents": list(body.get("parents") or []),
                "mimeType": mimetype,
                "trashed": False,
                "content": contenido,
                "webViewLink": f"https://drive.google.com/file/d/{file_id}/view",
            }
            return {"id": file_id}

        if accion == "update":
            _, file_id, add_parents, remove_parents, body = self._pendiente
            archivo = self._archivos[file_id]
            if remove_parents:
                quitar = set(remove_parents.split(","))
                archivo["parents"] = [p for p in archivo["parents"] if p not in quitar]
            if add_parents:
                for p in add_parents.split(","):
                    if p and p not in archivo["parents"]:
                        archivo["parents"].append(p)
            if body and body.get("name"):
                archivo["name"] = body["name"]
            return {"id": file_id, "parents": archivo["parents"], "name": archivo["name"]}

        raise AssertionError(f"acción no esperada: {accion}")

    def _filtrar(self, q: str) -> list[dict]:
        m_nombre = re.search(r"name = '((?:[^'\\]|\\.)*)'", q)
        m_padre = re.search(r"'([^']*)' in parents", q)
        pide_carpeta = f"mimeType = '{MIME_CARPETA}'" in q

        resultado = []
        for archivo in self._archivos.values():
            if m_nombre and archivo["name"] != m_nombre.group(1).replace("\\'", "'"):
                continue
            if m_padre and m_padre.group(1) not in archivo["parents"]:
                continue
            if pide_carpeta and archivo["mimeType"] != MIME_CARPETA:
                continue
            resultado.append(
                {
                    "id": archivo["id"],
                    "name": archivo["name"],
                    "mimeType": archivo["mimeType"],
                    "size": str(len(archivo["content"])),
                    "trashed": archivo["trashed"],
                }
            )
        return resultado


def _logger_silencioso() -> logging.Logger:
    """Logger de prueba que no imprime nada (evita ensuciar la salida de
    pytest); migrar_comprobantes usa el logger de módulo directamente, así
    que basta con bajarle el nivel a CRITICAL antes de cada test."""
    logging.getLogger("migrar_comprobantes").setLevel(logging.CRITICAL)
    return logging.getLogger("migrar_comprobantes")


# -----------------------------------------------------------------------------
# normalizar_nombre_mes() / resolver_anio_mes(): las 17 carpetas de mes
# reales de la tabla del encargo.
# -----------------------------------------------------------------------------
CARPETAS_MES_REALES = [
    # (carpeta_anio, carpeta_mes, anio_mes_esperado)
    ("2025", "ABRIL 2025", "2025-04"),
    ("2025", "AGOSTO 2025", "2025-08"),
    ("2025", "DIC 2025", "2025-12"),
    ("2025", "JULIO 2025", "2025-07"),
    ("2025", "JUNIO 2025", "2025-06"),
    ("2025", "MARZO 2025", "2025-03"),
    ("2025", "MAYO 2025", "2025-05"),
    ("2025", "NOV 2025", "2025-11"),
    ("2025", "OCT 2025", "2025-10"),
    ("2025", "SETIEMBRE 2025", "2025-09"),
    ("2026", "ABRIL", "2026-04"),
    ("2026", "ENERO", "2026-01"),
    ("2026", "FEBRERO", "2026-02"),
    ("2026", "JULIO", "2026-07"),
    ("2026", "JUNIO", "2026-06"),
    ("2026", "MARZO", "2026-03"),
    ("2026", "MAYO", "2026-05"),
]


def test_las_17_carpetas_de_mes_reales_resuelven_al_anio_mes_correcto():
    for carpeta_anio, carpeta_mes, esperado in CARPETAS_MES_REALES:
        assert mc.resolver_anio_mes(carpeta_anio, carpeta_mes) == esperado, carpeta_mes


def test_nombre_de_mes_desconocido_aborta():
    import pytest

    with pytest.raises(ValueError):
        mc.resolver_anio_mes("2025", "MESINVENTADO 2025")


# -----------------------------------------------------------------------------
# clasificar_subcarpeta()
# -----------------------------------------------------------------------------
def test_subcarpeta_que_no_empieza_con_boleta_ni_factura_aborta():
    import pytest

    with pytest.raises(ValueError):
        mc.clasificar_subcarpeta("RECIBOS VARIOS")


def test_factura_singular_se_clasifica_como_facturas():
    assert mc.clasificar_subcarpeta("FACTURA") == "FACTURAS"


def test_facturas_julio_se_clasifica_como_facturas():
    assert mc.clasificar_subcarpeta("FACTURAS JULIO") == "FACTURAS"


def test_boletas_octubre_se_clasifica_como_boletas():
    """Caso trampa: "BOLETAS OCTUBRE" vive dentro de la carpeta padre "OCT
    2025" (nombre abreviado, distinto al de la subcarpeta)."""
    assert mc.clasificar_subcarpeta("BOLETAS OCTUBRE") == "BOLETAS"


# -----------------------------------------------------------------------------
# recorrer_arbol_origen(): árbol simulado con tmp_path.
# -----------------------------------------------------------------------------
def _crear_arbol(tmp_path: pathlib.Path) -> pathlib.Path:
    """Arma un árbol mínimo bajo tmp_path que ejercita: nombre de mes con
    sufijo de año, nombre de mes sin sufijo, FACTURA singular, subcarpeta
    con nombre de mes completo dentro de una carpeta padre abreviada."""
    raiz = tmp_path / "EL TEMPLO"

    base_2025 = raiz / "2025" / "ABRIL 2025"
    (base_2025 / "BOLETAS ABRIL").mkdir(parents=True)
    (base_2025 / "BOLETAS ABRIL" / "b1.pdf").write_bytes(b"boleta 1")
    (base_2025 / "FACTURA ABRIL").mkdir(parents=True)
    (base_2025 / "FACTURA ABRIL" / "f1.pdf").write_bytes(b"factura 1")

    base_oct = raiz / "2025" / "OCT 2025"
    (base_oct / "BOLETAS OCTUBRE").mkdir(parents=True)
    (base_oct / "BOLETAS OCTUBRE" / "b2.pdf").write_bytes(b"boleta 2")
    (base_oct / "FACTURAS OCTUBRE").mkdir(parents=True)
    (base_oct / "FACTURAS OCTUBRE" / "f2.pdf").write_bytes(b"factura 2")

    base_2026 = raiz / "2026" / "ENERO"
    (base_2026 / "BOLETAS").mkdir(parents=True)
    (base_2026 / "BOLETAS" / "b3.pdf").write_bytes(b"boleta 3")
    (base_2026 / "FACTURAS").mkdir(parents=True)
    (base_2026 / "FACTURAS" / "f3.pdf").write_bytes(b"factura 3")

    # Artefacto de OneDrive/Office: se ignora en silencio, no debe aparecer
    # ni abortar la migración.
    (base_2026 / "BOLETAS" / "desktop.ini").write_bytes(b"")

    return raiz


def test_recorrer_arbol_origen_encuentra_todos_los_archivos_y_los_clasifica(tmp_path):
    raiz = _crear_arbol(tmp_path)
    archivos = mc.recorrer_arbol_origen(raiz)

    nombres = {a.ruta.name for a in archivos}
    assert nombres == {"b1.pdf", "f1.pdf", "b2.pdf", "f2.pdf", "b3.pdf", "f3.pdf"}

    por_nombre = {a.ruta.name: a for a in archivos}
    assert por_nombre["b1.pdf"].anio_mes == "2025-04"
    assert por_nombre["b1.pdf"].categoria == "BOLETAS"
    assert por_nombre["f1.pdf"].categoria == "FACTURAS"
    assert por_nombre["b2.pdf"].anio_mes == "2025-10"
    assert por_nombre["b2.pdf"].categoria == "BOLETAS"
    assert por_nombre["f3.pdf"].anio_mes == "2026-01"


def test_recorrer_arbol_origen_aborta_con_subcarpeta_invalida(tmp_path):
    import pytest

    raiz = tmp_path / "EL TEMPLO"
    otros = raiz / "2026" / "ENERO" / "RECIBOS"
    otros.mkdir(parents=True)
    (otros / "x.pdf").write_bytes(b"x")

    with pytest.raises(ValueError):
        mc.recorrer_arbol_origen(raiz)


def test_recorrer_arbol_origen_aborta_con_mes_invalido(tmp_path):
    import pytest

    raiz = tmp_path / "EL TEMPLO"
    carpeta = raiz / "2026" / "MESRARO" / "BOLETAS"
    carpeta.mkdir(parents=True)
    (carpeta / "x.pdf").write_bytes(b"x")

    with pytest.raises(ValueError):
        mc.recorrer_arbol_origen(raiz)


# -----------------------------------------------------------------------------
# ruta_destino(): "EL TEMPLO" -> "EL_TEMPLO" con guion bajo.
# -----------------------------------------------------------------------------
def test_ruta_destino_usa_el_templo_con_guion_bajo(tmp_path):
    archivo = mc.ArchivoOrigen(
        ruta=tmp_path / "algo.pdf",
        carpeta_anio="2025",
        carpeta_mes="OCT 2025",
        subcarpeta="BOLETAS OCTUBRE",
        anio_mes="2025-10",
        categoria="BOLETAS",
    )
    assert mc.ruta_destino(archivo) == "01_PROCESADO/2025-10/EL_TEMPLO/BOLETAS/algo.pdf"


# -----------------------------------------------------------------------------
# ejecutar_migracion(): idempotencia, dry-run, errores parciales.
# -----------------------------------------------------------------------------
def _archivo_origen(tmp_path, nombre, anio_mes="2025-10", categoria="BOLETAS") -> "mc.ArchivoOrigen":
    ruta = tmp_path / nombre
    ruta.write_bytes(f"contenido de {nombre}".encode())
    return mc.ArchivoOrigen(
        ruta=ruta,
        carpeta_anio="2025",
        carpeta_mes="OCT 2025",
        subcarpeta="BOLETAS OCTUBRE",
        anio_mes=anio_mes,
        categoria=categoria,
    )


def test_dry_run_no_llama_a_subir_ni_una_vez(tmp_path):
    """Defensa en profundidad: aunque se le pase un AlmacenDrive real (con
    subir() instrumentado para reventar si se llama), dry_run=True nunca lo
    toca."""
    _logger_silencioso()
    servicio = FakeServicioDrive()
    almacen = AlmacenDrive(servicio)

    llamadas_subir = []
    almacen.subir = lambda *a, **k: llamadas_subir.append((a, k)) or (_ for _ in ()).throw(AssertionError("no debería llamarse"))

    archivos = [_archivo_origen(tmp_path, "a.pdf"), _archivo_origen(tmp_path, "b.pdf")]
    resultado = mc.ejecutar_migracion(archivos, almacen, "procesado-id", dry_run=True)

    assert llamadas_subir == []
    assert resultado.conteos.get("se_subiria") == 2
    assert resultado.total_encontrados == 2


def test_dry_run_funciona_con_almacen_none():
    _logger_silencioso()
    resultado = mc.ejecutar_migracion([], None, "procesado-id", dry_run=True)
    assert resultado.total_encontrados == 0


def test_primera_corrida_sube_todo_y_crea_carpetas_por_mes_y_categoria(tmp_path):
    _logger_silencioso()
    servicio = FakeServicioDrive()
    almacen = AlmacenDrive(servicio)

    archivos = [
        _archivo_origen(tmp_path, "b1.pdf", anio_mes="2025-10", categoria="BOLETAS"),
        _archivo_origen(tmp_path, "f1.pdf", anio_mes="2025-10", categoria="FACTURAS"),
    ]
    resultado = mc.ejecutar_migracion(archivos, almacen, "procesado-id", dry_run=False)

    assert resultado.conteos.get("subido") == 2
    assert resultado.conteos.get("ya_existia", 0) == 0
    assert resultado.conteos.get("error", 0) == 0

    # Verifica que la jerarquía de carpetas quedó bien armada en Drive.
    nombres_archivos = {a["name"] for a in servicio._archivos.values() if a["mimeType"] != MIME_CARPETA}
    assert nombres_archivos == {"b1.pdf", "f1.pdf"}

    carpetas = {a["name"]: a for a in servicio._archivos.values() if a["mimeType"] == MIME_CARPETA}
    assert "2025-10" in carpetas
    assert "EL_TEMPLO" in carpetas
    assert {"BOLETAS", "FACTURAS"} <= set(carpetas.keys())


def test_segunda_corrida_no_sube_nada_todo_ya_existia(tmp_path):
    _logger_silencioso()
    servicio = FakeServicioDrive()
    almacen = AlmacenDrive(servicio)

    archivos = [
        _archivo_origen(tmp_path, "b1.pdf", anio_mes="2025-10", categoria="BOLETAS"),
        _archivo_origen(tmp_path, "f1.pdf", anio_mes="2025-10", categoria="FACTURAS"),
    ]

    primera = mc.ejecutar_migracion(archivos, almacen, "procesado-id", dry_run=False)
    assert primera.conteos.get("subido") == 2

    segunda = mc.ejecutar_migracion(archivos, almacen, "procesado-id", dry_run=False)
    assert segunda.conteos.get("subido", 0) == 0
    assert segunda.conteos.get("ya_existia") == 2

    # No se duplicó ningún archivo en Drive.
    nombres_archivos = [a["name"] for a in servicio._archivos.values() if a["mimeType"] != MIME_CARPETA]
    assert sorted(nombres_archivos) == ["b1.pdf", "f1.pdf"]


def test_un_error_de_subida_no_impide_procesar_los_siguientes(tmp_path):
    _logger_silencioso()
    servicio = FakeServicioDrive()
    almacen = AlmacenDrive(servicio)

    subir_original = almacen.subir

    def subir_con_fallo(carpeta_id, nombre, origen, mimetype="application/octet-stream"):
        if nombre == "falla.pdf":
            raise RuntimeError("error simulado de red")
        return subir_original(carpeta_id, nombre, origen, mimetype=mimetype)

    almacen.subir = subir_con_fallo

    archivos = [
        _archivo_origen(tmp_path, "falla.pdf"),
        _archivo_origen(tmp_path, "ok1.pdf"),
        _archivo_origen(tmp_path, "ok2.pdf"),
    ]
    resultado = mc.ejecutar_migracion(archivos, almacen, "procesado-id", dry_run=False)

    assert resultado.conteos.get("error") == 1
    assert resultado.conteos.get("subido") == 2

    nombres_archivos = {a["name"] for a in servicio._archivos.values() if a["mimeType"] != MIME_CARPETA}
    assert nombres_archivos == {"ok1.pdf", "ok2.pdf"}


# -----------------------------------------------------------------------------
# --excluir: archivo_esta_excluido() y su integración en ejecutar_migracion().
# -----------------------------------------------------------------------------
def test_archivo_esta_excluido_por_los_tres_niveles(tmp_path):
    archivo = _archivo_origen(tmp_path, "f.pdf", anio_mes="2026-07", categoria="FACTURAS")
    object.__setattr__(archivo, "carpeta_anio", "2026")
    object.__setattr__(archivo, "carpeta_mes", "JULIO")
    object.__setattr__(archivo, "subcarpeta", "FACTURAS")

    assert mc.archivo_esta_excluido(archivo, ["2026/JULIO/FACTURAS"]) is True
    assert mc.archivo_esta_excluido(archivo, ["2026/JULIO"]) is True  # patron mas corto excluye el mes completo
    assert mc.archivo_esta_excluido(archivo, ["2026/JULIO/BOLETAS"]) is False
    assert mc.archivo_esta_excluido(archivo, ["2025/JULIO/FACTURAS"]) is False
    assert mc.archivo_esta_excluido(archivo, None) is False
    assert mc.archivo_esta_excluido(archivo, []) is False


def test_archivo_esta_excluido_tolera_barras_y_mayusculas(tmp_path):
    archivo = _archivo_origen(tmp_path, "f.pdf")
    object.__setattr__(archivo, "carpeta_anio", "2026")
    object.__setattr__(archivo, "carpeta_mes", "JULIO")
    object.__setattr__(archivo, "subcarpeta", "FACTURAS")

    assert mc.archivo_esta_excluido(archivo, ["2026\\JULIO\\FACTURAS"]) is True
    assert mc.archivo_esta_excluido(archivo, ["2026/julio/facturas"]) is True
    assert mc.archivo_esta_excluido(archivo, ["  2026/JULIO/FACTURAS  "]) is True
    assert mc.archivo_esta_excluido(archivo, ["/2026/JULIO/FACTURAS/"]) is True


def test_archivo_esta_excluido_acepta_varios_patrones_repetidos(tmp_path):
    archivo_julio = _archivo_origen(tmp_path, "f.pdf")
    object.__setattr__(archivo_julio, "carpeta_anio", "2026")
    object.__setattr__(archivo_julio, "carpeta_mes", "JULIO")
    object.__setattr__(archivo_julio, "subcarpeta", "FACTURAS")

    archivo_otro = _archivo_origen(tmp_path, "g.pdf")
    object.__setattr__(archivo_otro, "carpeta_anio", "2025")
    object.__setattr__(archivo_otro, "carpeta_mes", "OCT 2025")
    object.__setattr__(archivo_otro, "subcarpeta", "BOLETAS OCTUBRE")

    patrones = ["2026/JULIO/FACTURAS", "2025/OCT 2025/BOLETAS OCTUBRE"]
    assert mc.archivo_esta_excluido(archivo_julio, patrones) is True
    assert mc.archivo_esta_excluido(archivo_otro, patrones) is True


def test_ejecutar_migracion_cuenta_excluidos_aparte_y_no_los_sube(tmp_path):
    """El caso real: 1.800 = 1.746 a migrar + 54 excluidos (ya subidos al
    buzon por --a-buzon). Los excluidos no deben tocar 'almacen' ni una vez,
    ni siquiera en corrida real (dry_run=False)."""
    _logger_silencioso()
    servicio = FakeServicioDrive()
    almacen = AlmacenDrive(servicio)

    llamadas_subir = []
    subir_original = almacen.subir
    almacen.subir = lambda *a, **k: (llamadas_subir.append((a, k)), subir_original(*a, **k))[1]

    archivos = []
    for i in range(3):
        a = _archivo_origen(tmp_path, f"factura_julio_{i}.pdf", anio_mes="2026-07", categoria="FACTURAS")
        object.__setattr__(a, "carpeta_anio", "2026")
        object.__setattr__(a, "carpeta_mes", "JULIO")
        object.__setattr__(a, "subcarpeta", "FACTURAS")
        archivos.append(a)
    for i in range(2):
        archivos.append(_archivo_origen(tmp_path, f"boleta_{i}.pdf", anio_mes="2025-10", categoria="BOLETAS"))

    resultado = mc.ejecutar_migracion(
        archivos, almacen, "procesado-id", dry_run=False, patrones_exclusion=["2026/JULIO/FACTURAS"]
    )

    assert resultado.total_encontrados == 5
    assert resultado.conteos.get("excluido") == 3
    assert resultado.conteos.get("subido") == 2
    # subir() solo se llamo para las 2 boletas no excluidas, nunca para las
    # 3 facturas de julio (el subarbol excluido).
    nombres_llamados = {llamada[0][1] for llamada in llamadas_subir}
    assert nombres_llamados == {"boleta_0.pdf", "boleta_1.pdf"}
    nombres_subidos = {a["name"] for a in servicio._archivos.values() if a["mimeType"] != MIME_CARPETA}
    assert nombres_subidos == {"boleta_0.pdf", "boleta_1.pdf"}


def test_ejecutar_migracion_excluidos_tambien_en_dry_run(tmp_path):
    _logger_silencioso()
    archivo = _archivo_origen(tmp_path, "f.pdf", anio_mes="2026-07", categoria="FACTURAS")
    object.__setattr__(archivo, "carpeta_anio", "2026")
    object.__setattr__(archivo, "carpeta_mes", "JULIO")
    object.__setattr__(archivo, "subcarpeta", "FACTURAS")

    resultado = mc.ejecutar_migracion(
        [archivo], None, "procesado-id", dry_run=True, patrones_exclusion=["2026/JULIO/FACTURAS"]
    )
    assert resultado.conteos.get("excluido") == 1
    assert resultado.conteos.get("se_subiria", 0) == 0


# -----------------------------------------------------------------------------
# recorrer_arbol_plano(): modo --a-buzon (subida plana, sin interpretar
# año/mes/subcarpeta).
# -----------------------------------------------------------------------------
def test_recorrer_arbol_plano_encuentra_archivos_sin_interpretar_carpetas(tmp_path):
    """Trampa central de --a-buzon: el nombre de la carpeta hoja no es un mes
    valido ("FACTURAS"), y aun asi debe funcionar (no debe intentar resolver
    meses, a diferencia de recorrer_arbol_origen)."""
    raiz = tmp_path / "2026" / "JULIO" / "FACTURAS"
    raiz.mkdir(parents=True)
    (raiz / "f1.pdf").write_bytes(b"factura 1")
    (raiz / "f2.pdf").write_bytes(b"factura 2")
    sub = raiz / "sub"
    sub.mkdir()
    (sub / "f3.pdf").write_bytes(b"factura 3")
    (raiz / "desktop.ini").write_bytes(b"")
    (raiz / "~$temporal.pdf").write_bytes(b"")

    archivos = mc.recorrer_arbol_plano(raiz)

    nombres = {a.name for a in archivos}
    assert nombres == {"f1.pdf", "f2.pdf", "f3.pdf"}


def test_recorrer_arbol_plano_aborta_si_no_existe_el_origen(tmp_path):
    import pytest

    with pytest.raises(FileNotFoundError):
        mc.recorrer_arbol_plano(tmp_path / "no-existe")


# -----------------------------------------------------------------------------
# resolver_carpeta_buzon(): lee drive.carpetas.buzon_tipos de config.yaml.
# -----------------------------------------------------------------------------
def _config_buzon() -> dict:
    return {
        "drive": {
            "carpetas": {
                "buzon_tipos": {
                    "facturas": "1MWILm4v8zRRRuK-Uosil0CFX1GJNPA4p",
                    "notas_venta": "1jQpnMDplGHAxsK-W4GFkybl9k8V80Kzz",
                    "liquidaciones": "1ANEvgNrZdLa9hM7wXFlnkWgOp43bTzIs",
                    "otros": "1rfRT-J-R1X_X5-pz-fQjs9AycBq7UJ3z",
                }
            }
        }
    }


def test_resolver_carpeta_buzon_devuelve_el_id_configurado():
    config = _config_buzon()
    assert mc.resolver_carpeta_buzon(config, "facturas") == "1MWILm4v8zRRRuK-Uosil0CFX1GJNPA4p"
    assert mc.resolver_carpeta_buzon(config, "otros") == "1rfRT-J-R1X_X5-pz-fQjs9AycBq7UJ3z"


def test_resolver_carpeta_buzon_tipo_desconocido_aborta():
    import pytest

    with pytest.raises(ValueError):
        mc.resolver_carpeta_buzon(_config_buzon(), "inexistente")


def test_resolver_carpeta_buzon_config_sin_seccion_aborta():
    import pytest

    with pytest.raises(ValueError):
        mc.resolver_carpeta_buzon({}, "facturas")


# -----------------------------------------------------------------------------
# ejecutar_migracion_a_buzon(): subida plana, idempotencia por
# buscar_por_nombre(), dry-run, errores parciales.
# -----------------------------------------------------------------------------
def _archivos_planos(tmp_path, cantidad: int) -> list[pathlib.Path]:
    rutas = []
    for i in range(cantidad):
        ruta = tmp_path / f"factura_{i}.pdf"
        ruta.write_bytes(f"contenido {i}".encode())
        rutas.append(ruta)
    return rutas


def test_a_buzon_dry_run_no_llama_a_subir(tmp_path):
    _logger_silencioso()
    servicio = FakeServicioDrive()
    almacen = AlmacenDrive(servicio)
    almacen.subir = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no deberia llamarse"))

    archivos = _archivos_planos(tmp_path, 3)
    resultado = mc.ejecutar_migracion_a_buzon(archivos, almacen, "buzon-facturas-id", dry_run=True)

    assert resultado.conteos.get("se_subiria") == 3
    assert resultado.total_encontrados == 3


def test_a_buzon_primera_corrida_sube_plano_sin_subcarpetas(tmp_path):
    _logger_silencioso()
    servicio = FakeServicioDrive()
    almacen = AlmacenDrive(servicio)

    archivos = _archivos_planos(tmp_path, 54)
    resultado = mc.ejecutar_migracion_a_buzon(archivos, almacen, "buzon-facturas-id", dry_run=False)

    assert resultado.conteos.get("subido") == 54
    assert resultado.conteos.get("ya_existia", 0) == 0

    # Todo quedo directo en la carpeta del buzon: ninguna carpeta nueva.
    carpetas = [a for a in servicio._archivos.values() if a["mimeType"] == MIME_CARPETA]
    assert carpetas == []
    archivos_subidos = [a for a in servicio._archivos.values() if a["mimeType"] != MIME_CARPETA]
    assert len(archivos_subidos) == 54
    assert all(a["parents"] == ["buzon-facturas-id"] for a in archivos_subidos)


def test_a_buzon_segunda_corrida_da_todo_ya_existia_y_cero_subidos(tmp_path):
    _logger_silencioso()
    servicio = FakeServicioDrive()
    almacen = AlmacenDrive(servicio)

    archivos = _archivos_planos(tmp_path, 54)
    primera = mc.ejecutar_migracion_a_buzon(archivos, almacen, "buzon-facturas-id", dry_run=False)
    assert primera.conteos.get("subido") == 54

    segunda = mc.ejecutar_migracion_a_buzon(archivos, almacen, "buzon-facturas-id", dry_run=False)
    assert segunda.conteos.get("subido", 0) == 0
    assert segunda.conteos.get("ya_existia") == 54

    archivos_en_drive = [a for a in servicio._archivos.values() if a["mimeType"] != MIME_CARPETA]
    assert len(archivos_en_drive) == 54  # no se duplico nada


def test_a_buzon_un_error_de_subida_no_impide_procesar_los_siguientes(tmp_path):
    _logger_silencioso()
    servicio = FakeServicioDrive()
    almacen = AlmacenDrive(servicio)

    subir_original = almacen.subir

    def subir_con_fallo(carpeta_id, nombre, origen, mimetype="application/octet-stream"):
        if nombre == "factura_1.pdf":
            raise RuntimeError("error simulado de red")
        return subir_original(carpeta_id, nombre, origen, mimetype=mimetype)

    almacen.subir = subir_con_fallo

    archivos = _archivos_planos(tmp_path, 3)
    resultado = mc.ejecutar_migracion_a_buzon(archivos, almacen, "buzon-facturas-id", dry_run=False)

    assert resultado.conteos.get("error") == 1
    assert resultado.conteos.get("subido") == 2
