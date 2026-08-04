"""Catalogo de insumos y motor de emparejamiento de descripciones de factura.

Puerto a Python de la logica de matching de
C:\\Users\\luisa\\OneDrive\\SCONCHA\\AUTO\\sconcha-app\\src\\lib\\matching.ts
(funcion fuzzyMatchInsumo), mas la salvaguarda contra falsos positivos por
token generico que ya existe en el motor de conciliacion de este mismo
proyecto: C:\\Users\\luisa\\OneDrive\\SCONCHA\\AUTO\\CONCILIACION\\skill\\scripts\\build_conciliacion.py
(ver GENERIC_TOKENS y name_match, alrededor de la linea 140). Ese motor sufrio
en produccion un cruce falso entre un cargo de MAPFRE y "GRUPO GIOBRE PERU
S.A.C." solo por compartir el token "PERU"; aqui aplicamos la misma idea:
un token compartido solo cuenta como evidencia si tiene 4+ letras y no esta
en la lista de tokens genericos.
"""
from __future__ import annotations

import csv
import pathlib
import re
import unicodedata
from dataclasses import dataclass

# Umbral por debajo del cual NO se propone emparejamiento. Un item mal
# emparejado contamina el inventario en silencio (se registra un consumo del
# insumo equivocado); uno sin emparejar solo pide revision manual. Ante la
# duda, no emparejar.
UMBRAL_MATCH = 0.55

# Base: el mismo listado de build_conciliacion.py GENERIC_TOKENS (palabras que
# aparecen en razones sociales de medio Peru y no distinguen un proveedor de
# otro). Se reusa tal cual por si una descripcion de factura arrastra texto
# del proveedor, y se extiende con las unidades de medida / envase que
# Restaurant.pe antepone a casi todos los nombres "crudos" de insumo
# ('ACEITUNA KILOS', 'ACEITE DE OLIVA LITRO', 'CAJA DE LAPICERO UNIDAD'):
# esas unidades son el equivalente, en el dominio de insumos, del token
# 'PERU' del bug original (MAPFRE cruzo con GRUPO GIOBRE PERU S.A.C. solo por
# 'PERU'): aparecen en decenas de aliases sin aportar identidad de producto.
GENERIC_TOKENS = {
    "PERU", "PERUANA", "PERUANO", "PERUANAS", "PERUANOS", "GRUPO",
    "INVERSIONES", "SERVICIOS", "SERVICIO", "GENERALES", "GENERAL",
    "CORPORACION", "DISTRIBUIDORA", "DISTRIBUCIONES", "COMERCIAL",
    "COMERCIALIZADORA", "NEGOCIOS", "EMPRESA", "EMPRESAS", "COMPANIA",
    "INDUSTRIA", "INDUSTRIAS", "INDUSTRIAL", "IMPORTACIONES",
    "EXPORTACIONES", "SOLUCIONES", "REPRESENTACIONES", "INTERNACIONAL",
    "NACIONAL", "MULTISERVICIOS", "CONSULTORES", "ASOCIADOS", "HERMANOS",
    "SOCIEDAD", "ANONIMA", "CERRADA", "LIMITADA", "LIMA",
    # Unidades de medida y palabras de envase/cantidad que aparecen sueltas
    # en los nombres "crudos" de Restaurant.pe y no aportan identidad de
    # insumo por si solas.
    "UNIDAD", "UNIDADES", "UND", "KILO", "KILOS", "KG", "GRAMO", "GRAMOS",
    "LITRO", "LITROS", "ML", "BOTELLA", "PAQUETE", "DOCENA", "SACO", "CAJA",
}

# Codigo de barras u otro prefijo puramente numerico al inicio de la
# descripcion (ej. exports de algunos POS que anteponen el codigo interno).
RE_CODIGO_BARRAS_INICIAL = re.compile(r"^\s*\d{6,}\s+")

# Prefijo "( INSUMO )" (con variantes de espaciado) que antepone
# Restaurant.pe a los nombres de insumo en sus exports.
RE_PREFIJO_INSUMO = re.compile(r"^\s*\(\s*INSUMO\s*\)\s*")


def normalizar(texto: str) -> str:
    """Mayusculas, sin tildes, sin puntuacion, espacios colapsados.

    Tambien quita el prefijo '( INSUMO )' y un codigo de barras numerico
    inicial, para que 'ACEITE DE OLIVA' y '( INSUMO ) ACEITE DE OLIVA LITRO'
    normalicen a algo comparable.
    """
    if texto is None:
        return ""
    t = texto.strip()
    t = RE_PREFIJO_INSUMO.sub("", t)
    t = RE_CODIGO_BARRAS_INICIAL.sub("", t)
    t = t.upper()
    # Quita tildes/diacriticos (equivalente a normalize('NFD') + replace en TS).
    t = unicodedata.normalize("NFD", t)
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    # Sin puntuacion: todo lo que no sea letra/numero/espacio pasa a espacio.
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _tokens_significativos(texto_normalizado: str) -> set[str]:
    """Tokens de 4+ letras que no son genericos: la unica evidencia valida
    de que dos descripciones hablan del mismo insumo cuando no hay match
    exacto ni por substring."""
    return {
        w
        for w in texto_normalizado.split(" ")
        if w and len(w) >= 4 and w not in GENERIC_TOKENS and not w.isdigit()
    }


@dataclass
class _EntradaCatalogo:
    insumo: str
    categoria: str
    unidad_base: str
    alias_normalizado: str


class Catalogo:
    """Carga insumos.csv y resuelve descripciones de factura a insumos del catalogo."""

    def __init__(self, csv_path: pathlib.Path):
        self.csv_path = pathlib.Path(csv_path)
        self._por_alias_exacto: dict[str, tuple[str, str]] = {}
        self._entradas: list[_EntradaCatalogo] = []
        self._cargar()

    def _cargar(self) -> None:
        with self.csv_path.open("r", encoding="utf-8", newline="") as f:
            lector = csv.DictReader(f)
            for fila in lector:
                insumo = (fila.get("insumo") or "").strip()
                categoria = (fila.get("categoria") or "").strip()
                unidad_base = (fila.get("unidad_base") or "").strip()
                alias = (fila.get("alias") or "").strip()
                if not insumo or not alias:
                    continue
                alias_norm = normalizar(alias)
                # Alias exacto: gana el primero que se cargue (el CSV no trae
                # colisiones reales entre insumos distintos; si las hubiera,
                # se respeta el orden del archivo).
                self._por_alias_exacto.setdefault(alias_norm, (insumo, categoria))
                self._entradas.append(
                    _EntradaCatalogo(
                        insumo=insumo,
                        categoria=categoria,
                        unidad_base=unidad_base,
                        alias_normalizado=alias_norm,
                    )
                )

    def emparejar(self, descripcion: str) -> tuple[str | None, str | None, float]:
        """Devuelve (insumo, categoria, score 0..1).

        Primero busca coincidencia exacta por alias (score 1.0). Si no hay,
        aplica coincidencia difusa por palabras en comun, replicando
        fuzzyMatchInsumo (matching.ts) pero exigiendo ademas al menos un
        token significativo (4+ letras, no generico) como evidencia; de lo
        contrario dos descripciones podrian cruzar solo por compartir
        palabras genericas como 'UNIDAD' o 'PERU'.

        Por debajo de UMBRAL_MATCH devuelve (None, None, score): el item
        queda marcado como no emparejado en vez de mal emparejado.
        """
        desc_norm = normalizar(descripcion)
        if not desc_norm:
            return (None, None, 0.0)

        exacto = self._por_alias_exacto.get(desc_norm)
        if exacto is not None:
            return (exacto[0], exacto[1], 1.0)

        desc_words = set(desc_norm.split(" "))
        desc_tokens_sig = _tokens_significativos(desc_norm)

        mejor: tuple[str, str, float] | None = None
        for entrada in self._entradas:
            alias_norm = entrada.alias_normalizado
            if not alias_norm:
                continue
            alias_words = alias_norm.split(" ")

            if desc_norm == alias_norm:
                score = 1.0
            elif alias_norm in desc_words:
                score = 0.95  # el alias completo aparece como una palabra
            elif desc_norm.startswith(alias_norm + " "):
                score = 0.92
            elif alias_norm in desc_norm:
                score = 0.85  # como substring
            else:
                comunes = sum(1 for w in alias_words if w in desc_words)
                score = (comunes / len(alias_words)) * 0.7 if alias_words else 0.0

            if score <= 0:
                continue

            # Salvaguarda anti-falso-positivo: si el score no vino de una
            # coincidencia exacta/substring (>= 0.85), exige al menos un
            # token significativo compartido. Evita que dos descripciones
            # crucen solo por palabras genericas ('UNIDAD', 'KILOS', 'PERU').
            if score < 0.85:
                alias_tokens_sig = _tokens_significativos(alias_norm)
                if not (desc_tokens_sig & alias_tokens_sig):
                    continue

            if mejor is None or score > mejor[2]:
                mejor = (entrada.insumo, entrada.categoria, score)

        if mejor is None or mejor[2] < UMBRAL_MATCH:
            return (None, None, mejor[2] if mejor else 0.0)

        return mejor
