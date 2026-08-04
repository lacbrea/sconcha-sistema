"""Tests de extractores/modelo.py: construcción del prompt y manejo de la respuesta.

No llaman a la API de Anthropic ni requieren ANTHROPIC_API_KEY: se ejercitan
directamente `_construir_prompt` y `_procesar_respuesta` con objetos de
respuesta simulados (SimpleNamespace), y `extraer()` solo hasta el punto
donde fallaría por falta de credenciales, sin llegar a la red.
"""
import json
import pathlib
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from extractores import modelo


def _respuesta(stop_reason: str, texto: str):
    bloque_texto = SimpleNamespace(type="text", text=texto)
    return SimpleNamespace(stop_reason=stop_reason, content=[bloque_texto])


def _linea_de_razones_sociales(prompt: str) -> str:
    for linea in prompt.splitlines():
        if "razones sociales propias" in linea:
            return linea
    return ""


# --- (a) sin config, el prompt no menciona ningún RUC -----------------------------

def test_prompt_sin_config_no_menciona_ningun_ruc():
    prompt = modelo._construir_prompt(None)
    assert "razones sociales propias" not in prompt
    # Ningún RUC de SCONCHA (ni ningún otro) debe quedar cocido en el prompt.
    assert "20608901494" not in prompt
    assert "20612506036" not in prompt
    assert "20614321734" not in prompt


def test_prompt_con_lista_de_empresas_vacia_tampoco_menciona_razones_sociales():
    prompt = modelo._construir_prompt([])
    assert "razones sociales propias" not in prompt


def test_modulo_ya_no_hardcodea_rucs_propios():
    # No debe quedar ninguna constante de módulo tipo _RUCS_PROPIOS.
    assert not hasattr(modelo, "_RUCS_PROPIOS")
    assert not hasattr(modelo, "MODELO_CLAUDE")


# --- (b) con empresas, el prompt las incluye y es idéntico entre llamadas ---------

def test_prompt_con_dos_empresas_incluye_ambas():
    empresas = [
        {"razon_social": "EL TEMPLO S.A.C.", "ruc": "20608901494"},
        {"razon_social": "INSTITUCION CEVICHERA S.A.C.", "ruc": "20612506036"},
    ]
    prompt = modelo._construir_prompt(empresas)

    linea = _linea_de_razones_sociales(prompt)
    assert "EL TEMPLO S.A.C." in linea
    assert "20608901494" in linea
    assert "INSTITUCION CEVICHERA S.A.C." in linea
    assert "20612506036" in linea


def test_prompt_es_identico_byte_a_byte_entre_llamadas_consecutivas():
    empresas = [
        {"razon_social": "EL TEMPLO S.A.C.", "ruc": "20608901494"},
        {"razon_social": "INSTITUCION CEVICHERA S.A.C.", "ruc": "20612506036"},
    ]
    prompt_1 = modelo._construir_prompt(empresas)
    prompt_2 = modelo._construir_prompt(empresas)

    assert prompt_1 == prompt_2
    assert prompt_1.encode("utf-8") == prompt_2.encode("utf-8")


def test_prompt_no_depende_del_orden_de_entrada_de_las_empresas():
    # Si config.yaml trae las empresas en otro orden entre corridas, el prompt
    # no debe cambiar de bytes -- o se invalida el cache_control del bloque
    # de system y se paga el prompt completo en cada documento.
    empresas_orden_a = [
        {"razon_social": "EL TEMPLO S.A.C.", "ruc": "20608901494"},
        {"razon_social": "INSTITUCION CEVICHERA S.A.C.", "ruc": "20612506036"},
        {"razon_social": "ILLAWARA E.I.R.L.", "ruc": "20614321734"},
    ]
    empresas_orden_b = list(reversed(empresas_orden_a))

    assert modelo._construir_prompt(empresas_orden_a) == modelo._construir_prompt(empresas_orden_b)


def test_extraer_acepta_llamarse_sin_tercer_parametro(monkeypatch):
    # extraer(ruta, tipo) sigue siendo válido -- config es opcional.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(modelo.ClaveApiFaltanteError):
        modelo.extraer(pathlib.Path("no_existe.pdf"), "pdf")


def test_extraer_toma_modelo_y_esfuerzo_de_config(monkeypatch):
    # No debe requerir ANTHROPIC_API_KEY para fallar por falta de archivo/API key
    # antes de siquiera intentar construir la llamada -- solo confirmamos que
    # config es aceptado sin romper la firma y sin tocar la red.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config = {"modelo": "claude-otro-modelo", "esfuerzo": "medium", "empresas": []}
    with pytest.raises(modelo.ClaveApiFaltanteError):
        modelo.extraer(pathlib.Path("no_existe.pdf"), "pdf", config)


# --- (c) JSON truncado sale como ErrorModeloClaude, no JSONDecodeError -----------

def test_json_truncado_por_max_tokens_se_relanza_como_error_modelo_claude():
    respuesta = _respuesta("max_tokens", '{"proveedor_ruc": "2060890')  # JSON incompleto

    with pytest.raises(modelo.ErrorModeloClaude):
        modelo._procesar_respuesta(respuesta, "pdf")


def test_json_truncado_no_deja_escapar_jsondecodeerror():
    respuesta = _respuesta("max_tokens", '{"proveedor_ruc": "2060890')

    try:
        modelo._procesar_respuesta(respuesta, "pdf")
        pytest.fail("Se esperaba ErrorModeloClaude")
    except json.JSONDecodeError:
        pytest.fail("_procesar_respuesta dejó escapar un JSONDecodeError crudo")
    except modelo.ErrorModeloClaude as exc:
        assert "max_tokens" in str(exc) or "truncó" in str(exc)


def test_json_invalido_sin_truncar_tambien_se_relanza_como_error_modelo_claude():
    respuesta = _respuesta("end_turn", "esto no es json")

    with pytest.raises(modelo.ErrorModeloClaude):
        modelo._procesar_respuesta(respuesta, "pdf")


def test_json_valido_con_max_tokens_agrega_advertencia_de_truncamiento():
    datos = {
        "proveedor_ruc": "20111111111", "proveedor_razon_social": "X",
        "cliente_ruc": None, "cliente_razon_social": None,
        "tipo_documento": "factura", "serie_numero": "F001-1",
        "fecha_emision": "2026-01-01", "fecha_vencimiento": None, "condicion": None,
        "moneda": "PEN", "tipo_cambio": None,
        "subtotal": 100.0, "igv": 18.0, "icbper": None, "descuento_global": None,
        "total": 118.0, "detraccion_pct": None, "detraccion_monto": None,
        "detraccion_codigo": None, "retencion": None, "documento_referencia": None,
        "confianza": 0.9, "items": [],
    }
    respuesta = _respuesta("max_tokens", json.dumps(datos))

    comp = modelo._procesar_respuesta(respuesta, "pdf")

    assert comp.total == 118.0
    assert any("truncó" in a for a in comp.advertencias)


def test_respuesta_refusal_lanza_respuesta_rechazada():
    respuesta = _respuesta("refusal", "")
    with pytest.raises(modelo.RespuestaRechazadaError):
        modelo._procesar_respuesta(respuesta, "pdf")
