"""MCP server for Colombian public procurement: the SECOP II contracts dataset on datos.gov.co.

The model never writes a query string. It fills in a typed query (columns, aggregates,
filters) and this server builds the SoQL from allowlisted identifiers and escaped literals,
so the columns with personal data cannot be reached and nothing the model types is
interpreted as query syntax.
"""

import functools
import json
import math
import os
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel

DATASET = "https://www.datos.gov.co/resource/jbjy-vk9h.json"  # "SECOP II - Contratos Electrónicos"
AUDIT_LOG = Path(os.environ.get("SECOP_AUDIT_LOG", Path(__file__).parent / "audit.jsonl"))
# One entity per NIT with its other spellings, biggest first. A snapshot built by secop-api: finding an
# entity by name upstream is a LIKE over six million rows, which is the query that times out.
ENTITIES = Path(__file__).with_name("entities.json")
MAX_ROWS = 100
TIMEOUT_S = 60

# The allowlist. The dataset has 95 columns; the ones left out include bank account numbers,
# home addresses and ID numbers of legal representatives, supervisors and payment officers.
COLUMNS = {
    "nombre_entidad": ("text", "Entidad estatal que publica el contrato (mayúsculas, tildes inconsistentes)"),
    "nit_entidad": ("number", "NIT de la entidad, sin dígito de verificación. Identificador estable de la entidad"),
    "departamento": ("text", "Departamento de la entidad"),
    "ciudad": ("text", "Ciudad de la entidad"),
    "orden": ("text", "Orden de la entidad: Nacional, Territorial, Corporación Autónoma"),
    "sector": ("text", "Sector de la entidad"),
    "id_contrato": ("text", "Identificador del contrato en la plataforma, p. ej. CO1.PCCNTR.1234567"),
    "referencia_del_contrato": ("text", "Número del contrato asignado por la entidad"),
    "estado_contrato": ("text", "En ejecución, Cerrado, Modificado, terminado, Borrador, Aprobado, Cancelado, "
                                "enviado Proveedor, cedido, En aprobación, Suspendido, Prorrogado"),
    "tipo_de_contrato": ("text", "Prestación de servicios, Obra, Suministros, Compraventa, Interventoría..."),
    "modalidad_de_contratacion": ("text", "Contratación directa, Mínima cuantía, Licitación pública, "
                                          "Selección Abreviada de Menor Cuantía, Contratación régimen especial..."),
    "objeto_del_contrato": ("text", "Objeto del contrato, texto libre"),
    "codigo_de_categoria_principal": ("text", "Código UNSPSC de la categoría principal"),
    "fecha_de_firma": ("date", "Fecha de firma. Puede venir vacía en contratos sin firmar"),
    "fecha_de_inicio_del_contrato": ("date", "Fecha de inicio"),
    "fecha_de_fin_del_contrato": ("date", "Fecha de terminación"),
    "anio_firma": ("number", "Año de la fecha de firma (columna derivada)"),
    "valor_del_contrato": ("number", "Valor total en pesos colombianos (COP). Contiene valores atípicos"),
    "valor_pagado": ("number", "Valor pagado a la fecha en COP"),
    "dias_adicionados": ("number", "Días adicionados al contrato"),
    "proveedor_adjudicado": ("text", "Nombre del proveedor o contratista"),
    "documento_proveedor": ("text", "NIT o documento del proveedor. Identificador estable del proveedor"),
    "tipodocproveedor": ("text", "Tipo de documento del proveedor: NIT, Cédula de Ciudadanía..."),
    "es_pyme": ("text", "Si el proveedor es Pyme: Si / No"),
    "origen_de_los_recursos": ("text", "Origen presupuestal de los recursos"),
    "urlproceso": ("text", "URL del proceso en SECOP II, para verificar el contrato en la fuente"),
}
EXPRESSIONS = {"anio_firma": "date_extract_y(fecha_de_firma)"}
MONEY = ("valor_del_contrato", "valor_pagado")
NOTES = [
    "Solo cubre SECOP II. Los contratos publicados en SECOP I o en la Tienda Virtual del Estado no están aquí, "
    "así que los totales son un piso, no el total de la contratación pública.",
    "Para identificar una entidad usa find_entity: es inmediato y devuelve su NIT. No la busques con 'contains' "
    "sobre nombre_entidad, que recorre millones de filas y suele agotar el tiempo. Filtra después por nit_entidad.",
    "Un NIT puede cubrir varias dependencias (other_names en find_entity; por ejemplo, una alcaldía, su concejo "
    "y su personería). Para una sola de ellas, filtra además por nombre_entidad exacto.",
    "Los nombres de proveedores varían. Para identificar uno, acota primero por nit_entidad y año, busca con "
    "'contains' agrupando por nombre y documento_proveedor, y luego filtra por ese documento.",
    "Para saber cuánto se contrató, excluye los estados Borrador y Cancelado.",
    "valor_del_contrato tiene errores de digitación de varios órdenes de magnitud. Siempre que pidas un sum, "
    "pide también max y count del mismo grupo, y avisa si un solo contrato explica casi todo el total.",
    "Hay contratos sin fecha de firma: en ellos anio_firma viene vacío.",
    "Cada monto trae un campo gemelo terminado en _texto, ya escrito en palabras (por ejemplo '491.372 millones "
    "de pesos'). Cita ese texto tal cual y no conviertas unidades: en español un billón es un millón de millones.",
]
STOPWORDS = {"de", "del", "la", "las", "el", "los", "y", "e"}
# How people name an entity versus how it is registered: "Alcaldía de Medellín" is a DISTRITO in the dataset.
ENTITY_KINDS = {"alcaldia", "gobernacion", "municipio", "municipal", "departamento", "distrito", "distrital"}

Column = Literal[*COLUMNS]


class Filter(BaseModel):
    column: Column
    op: Literal["=", "!=", ">", ">=", "<", "<=", "contains"]
    value: str | float


class Aggregate(BaseModel):
    function: Literal["count", "sum", "avg", "min", "max"]
    column: Column | None = None  # not needed for count

    @property
    def alias(self) -> str:
        return self.function if self.column is None else f"{self.function}_{self.column}"


mcp = MCPServer("secop", instructions=(
    "Contratación pública de Colombia: contratos electrónicos de SECOP II (datos.gov.co), más de seis "
    "millones de filas. Llama primero a describe_dataset: explica las columnas y las trampas de los datos. "
    "Para ubicar una entidad por su nombre usa find_entity. "
    "Todo conteo o total debe venir de un agregado de query_contracts; nunca sumes filas por tu cuenta. "
    "Al dar una cifra, di qué filtros usaste. Los valores están en pesos colombianos."))


def _expression(column: str) -> str:
    if column not in COLUMNS:  # the schema already rejects these over MCP; this guards direct callers too
        raise ToolError(f"Columna no permitida: {column!r}")
    return EXPRESSIONS.get(column, column)


def _literal(column: str, value: str | float) -> str:
    kind = COLUMNS[column][0]
    if kind == "number":
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = math.nan
        if not math.isfinite(number):
            raise ToolError(f"{column} es numérica; {value!r} no es un número")
        return str(int(number)) if number.is_integer() else repr(number)
    if kind == "date" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value)):
        raise ToolError(f"{column} es una fecha; usa el formato AAAA-MM-DD, no {value!r}")
    return "'" + str(value).replace("'", "''") + "'"


def build_query(columns: list[str], aggregates: list[Aggregate], filters: list[Filter],
                order_by: str | None, descending: bool, limit: int) -> dict[str, str]:
    """Turn a typed query into Socrata parameters. Every identifier comes from COLUMNS; every value is a literal."""
    if not columns and not aggregates:
        raise ToolError("Pide al menos una columna o un agregado")
    select = [f"{_expression(c)} as {c}" if c in EXPRESSIONS else _expression(c) for c in columns]
    for aggregate in aggregates:
        if aggregate.column is None:
            if aggregate.function != "count":
                raise ToolError(f"{aggregate.function} necesita una columna")
            select.append("count(*) as count")
            continue
        if aggregate.function in ("sum", "avg") and COLUMNS.get(aggregate.column, ("",))[0] != "number":
            raise ToolError(f"{aggregate.function} solo aplica a columnas numéricas, no a {aggregate.column}")
        select.append(f"{aggregate.function}({_expression(aggregate.column)}) as {aggregate.alias}")

    where = []
    for f in filters:
        expression = _expression(f.column)
        if f.op == "contains":
            if COLUMNS[f.column][0] != "text":
                raise ToolError(f"'contains' solo aplica a columnas de texto, no a {f.column}")
            where.append(f"upper({expression}) like " + _literal(f.column, f"%{str(f.value).upper()}%"))
        else:
            where.append(f"{expression} {f.op} {_literal(f.column, f.value)}")

    params = {"$select": ", ".join(select), "$limit": str(max(1, min(limit, MAX_ROWS)))}
    if where:
        params["$where"] = " AND ".join(where)
    if aggregates and columns:
        params["$group"] = ", ".join(columns)
    sortable = [*columns, *(a.alias for a in aggregates)]
    if order_by is None and aggregates and columns:
        order_by = aggregates[0].alias
    if order_by is not None:
        if order_by not in sortable:
            raise ToolError(f"order_by debe ser una de las columnas o agregados pedidos: {sortable}")
        params["$order"] = f"{order_by} {'DESC' if descending else 'ASC'}"
    return params


def _fetch(params: dict[str, str]) -> list[dict]:
    headers = {"User-Agent": "secop-mcp"}
    if token := os.environ.get("SOCRATA_APP_TOKEN"):  # optional; raises the anonymous rate limit
        headers["X-App-Token"] = token
    request = urllib.request.Request(f"{DATASET}?{urllib.parse.urlencode(params)}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        raise ToolError(f"datos.gov.co rechazó la consulta (HTTP {e.code}): {e.read(300).decode(errors='replace')}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise ToolError(f"datos.gov.co no respondió: {e}. Prueba una consulta más acotada")


def pesos(value: float) -> str:
    """An amount the way a Colombian reader writes it, so the model copies a unit instead of choosing one."""
    def grouped(number: float) -> str:
        return f"{number:,.0f}".replace(",", ".")
    if abs(value) >= 1e12:  # a Spanish "billón" is a million millions
        return f"{value / 1e12:.2f}".replace(".", ",") + " billones de pesos"
    if abs(value) >= 1e7:
        return grouped(value / 1e6) + " millones de pesos"
    return grouped(value) + " pesos"


def _typed(row: dict, numeric: set[str]) -> dict:
    """Socrata returns every value as a string; give numbers back as numbers and flatten URL objects."""
    out = {}
    for key, value in row.items():
        if isinstance(value, dict):
            value = value.get("url")
        elif key in numeric and value is not None:
            number = float(value)
            value = int(number) if number.is_integer() else number
        out[key] = value
        if key in numeric and value is not None and not key.startswith("count") and any(key == c or key.endswith("_" + c) for c in MONEY):
            out[key + "_texto"] = pesos(value)
    return out


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text.lower()) if not unicodedata.combining(c))


@functools.cache
def _directory() -> list[tuple[dict, list[str]]]:
    """Every entity with its spellings folded for search: the name first, then the aliases."""
    return [(entity, [_fold(spelling) for spelling in (entity["name"], *entity["aliases"])])
            for entity in json.loads(ENTITIES.read_text(encoding="utf-8"))]


def _audit(tool: str, **fields) -> None:
    entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "tool": tool, **fields}
    with AUDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


@mcp.tool()
def describe_dataset() -> dict:
    """Explain the SECOP II contracts dataset: every queryable column and the known data pitfalls. Call this first."""
    _audit("describe_dataset", status="ok")
    return {"dataset": "SECOP II - Contratos Electrónicos (datos.gov.co, jbjy-vk9h)",
            "columns": [{"name": name, "type": kind, "description": text} for name, (kind, text) in COLUMNS.items()],
            "notes": NOTES}


@mcp.tool()
def find_entity(name: str, limit: int = 10) -> dict:
    """Find a state entity by name and get its NIT. Immediate: it searches a local directory, not the dataset.

    Case and accents are ignored, and every word must appear in one of the entity's names. Entities
    are registered under their legal name: a city government ("Alcaldía de X") is often MUNICIPIO DE X
    or DISTRITO ... DE X, and a "Gobernación" is often DEPARTAMENTO DE X. So when nothing has every
    word, the search is repeated without the kind of entity and match says "place_only": check the
    names before choosing. The directory is a snapshot; an entity that started publishing recently
    may be missing.
    """
    words = [word for word in _fold(name).split() if word not in STOPWORDS]
    if len("".join(words)) < 3:
        _audit("find_entity", status="error", error="query too short")
        raise ToolError("Escribe al menos tres letras del nombre de la entidad")

    def search(wanted: list[str]) -> list[dict]:
        by_name, by_alias = [], []
        for entity, spellings in _directory():  # sorted by number of contracts, so each list is biggest first
            spelling = next((i for i, text in enumerate(spellings) if all(word in text for word in wanted)), None)
            if spelling is None:
                continue
            aliases = entity["aliases"]
            found = {key: entity[key] for key in ("nit", "name", "department", "level", "contracts")} | {
                "other_names": aliases[:5], "other_names_omitted": max(0, len(aliases) - 5)}
            if spelling:
                by_alias.append(found | {"matched_as": aliases[spelling - 1]})
            else:
                by_name.append(found)
        return (by_name + by_alias)[:max(1, min(limit, 25))]

    entities, match = search(words), "all_words"
    place = [word for word in words if word not in ENTITY_KINDS]
    if not entities and place and place != words:
        entities, match = search(place), "place_only"
    _audit("find_entity", status="ok", query=name, rows=len(entities))
    return {"entities": entities, "match": match if entities else "none"}


@mcp.tool()
def query_contracts(columns: list[Column] = [], aggregates: list[Aggregate] = [], filters: list[Filter] = [],
                    order_by: str | None = None, descending: bool = True, limit: int = 20) -> dict:
    """Query SECOP II contracts. Returns at most 100 rows.

    With aggregates and columns together, rows are grouped by the columns (for example
    columns=[proveedor_adjudicado, documento_proveedor] with sum of valor_del_contrato gives the
    total per supplier). Filters are combined with AND; 'contains' is a case-insensitive
    substring match on a text column. Dates are AAAA-MM-DD. order_by takes a requested column
    or an aggregate alias such as sum_valor_del_contrato or count.
    """
    try:
        params = build_query(columns, aggregates, filters, order_by, descending, limit)
        numeric = {c for c in columns if COLUMNS[c][0] == "number"} | {
            a.alias for a in aggregates if a.function in ("count", "sum", "avg") or COLUMNS[a.column][0] == "number"}
        rows = [_typed(row, numeric) for row in _fetch(params)]
    except ToolError as e:
        _audit("query_contracts", status="error", error=str(e))
        raise
    _audit("query_contracts", status="ok", rows=len(rows), soql=params)
    return {"rows": rows, "row_count": len(rows), "may_have_more": len(rows) == int(params["$limit"]), "soql": params}


if __name__ == "__main__":
    mcp.run()  # stdio transport
