import asyncio
import json
import urllib.error
import urllib.request

import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

import server
from server import Aggregate, Filter, build_query

PERSONAL = ["n_mero_de_cuenta", "nombre_del_banco", "domicilio_representante_legal",
            "identificaci_n_representante_legal", "n_mero_de_documento_supervisor"]


@pytest.fixture(autouse=True)
def audit(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AUDIT_LOG", tmp_path / "audit.jsonl")


@pytest.fixture
def fake_api(monkeypatch):
    """Replace the HTTP call; record the SoQL the server would have sent."""
    sent = []
    monkeypatch.setattr(server, "_fetch", lambda params: sent.append(params) or [
        {"proveedor_adjudicado": "ACME", "sum_valor_del_contrato": "1500000.50", "count": "3",
         "urlproceso": {"url": "https://example.test/x"}}])
    return sent


def test_grouped_total_builds_the_expected_soql():
    params = build_query(
        ["proveedor_adjudicado", "documento_proveedor"],
        [Aggregate(function="sum", column="valor_del_contrato"), Aggregate(function="count")],
        [Filter(column="nit_entidad", op="=", value="890905211"),
         Filter(column="anio_firma", op="=", value=2024),
         Filter(column="estado_contrato", op="!=", value="Cancelado")],
        order_by=None, descending=True, limit=5)
    assert params == {
        "$select": "proveedor_adjudicado, documento_proveedor, "
                   "sum(valor_del_contrato) as sum_valor_del_contrato, count(*) as count",
        "$where": "nit_entidad = 890905211 AND date_extract_y(fecha_de_firma) = 2024 "
                  "AND estado_contrato != 'Cancelado'",
        "$group": "proveedor_adjudicado, documento_proveedor",
        "$order": "sum_valor_del_contrato DESC",
        "$limit": "5",
    }


def test_values_are_literals_never_syntax():
    hostile = "x' OR 1=1 --"
    where = build_query(["id_contrato"], [], [Filter(column="proveedor_adjudicado", op="=", value=hostile),
                                              Filter(column="objeto_del_contrato", op="contains", value="vías' --")],
                        None, True, 10)["$where"]
    assert where == "proveedor_adjudicado = 'x'' OR 1=1 --' AND upper(objeto_del_contrato) like '%VÍAS'' --%'"


@pytest.mark.parametrize("filter_", [
    Filter(column="nit_entidad", op="=", value="1 OR 1=1"),             # not a number
    Filter(column="valor_del_contrato", op=">", value="inf"),           # not finite
    Filter(column="fecha_de_firma", op=">=", value="2024-01-01' OR"),   # not a date
    Filter(column="valor_del_contrato", op="contains", value="9"),      # contains on a number
])
def test_ill_typed_values_are_rejected(filter_):
    with pytest.raises(ToolError):
        build_query(["id_contrato"], [], [filter_], None, True, 10)


@pytest.mark.parametrize("column", PERSONAL + ["nombre_entidad; drop", "count(*)"])
def test_columns_outside_the_allowlist_are_unreachable(column):
    with pytest.raises(ToolError, match="no permitida"):
        build_query([column], [], [], None, True, 10)
    with pytest.raises(ToolError, match="order_by"):
        build_query(["id_contrato"], [], [], column, True, 10)


def test_limit_is_capped_and_empty_queries_are_refused():
    assert build_query(["id_contrato"], [], [], None, True, 10_000)["$limit"] == str(server.MAX_ROWS)
    with pytest.raises(ToolError):
        build_query([], [], [], None, True, 10)
    with pytest.raises(ToolError, match="numéricas"):
        build_query([], [Aggregate(function="sum", column="proveedor_adjudicado")], [], None, True, 10)


def test_rows_come_back_typed_and_every_call_is_audited(fake_api):
    result = server.query_contracts(
        columns=["proveedor_adjudicado", "urlproceso"],
        aggregates=[Aggregate(function="sum", column="valor_del_contrato"), Aggregate(function="count")])
    assert result["rows"] == [{"proveedor_adjudicado": "ACME", "sum_valor_del_contrato": 1500000.5,
                               "sum_valor_del_contrato_texto": "1.500.000 pesos", "count": 3,
                               "urlproceso": "https://example.test/x"}]
    with pytest.raises(ToolError):
        server.query_contracts(columns=["n_mero_de_cuenta"])
    entries = [json.loads(line) for line in server.AUDIT_LOG.read_text(encoding="utf-8").splitlines()]
    assert [e["status"] for e in entries] == ["ok", "error"] and entries[0]["soql"] == fake_api[0]


def test_over_the_mcp_protocol_the_schema_rejects_personal_columns(fake_api):
    async def scenario():
        async with Client(server.mcp) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            ok = await client.call_tool("query_contracts", {
                "columns": ["proveedor_adjudicado"], "aggregates": [{"function": "count"}],
                "filters": [{"column": "departamento", "op": "contains", "value": "antioquia"}]})
            denied = await client.call_tool("query_contracts", {"columns": ["n_mero_de_cuenta"]})
            return tools, ok, denied

    tools, ok, denied = asyncio.run(scenario())
    assert set(tools) == {"describe_dataset", "find_entity", "query_contracts"}
    assert not ok.is_error and fake_api[0]["$where"] == "upper(departamento) like '%ANTIOQUIA%'"
    assert denied.is_error and len(fake_api) == 1  # rejected before any request was made
    schema = json.dumps(tools["query_contracts"].input_schema)
    assert "nit_entidad" in schema and not any(column in schema for column in PERSONAL)


@pytest.mark.parametrize("value, text", [
    (491_372_098_658, "491.372 millones de pesos"),   # the amount the model once called "491 billones"
    (4_310_000_000_000, "4,31 billones de pesos"),    # a Spanish billón is 10^12
    (45_000_000_000, "45.000 millones de pesos"),
    (9_999_999, "9.999.999 pesos"),
])
def test_amounts_are_written_out_by_code(value, text):
    assert server.pesos(value) == text


def test_counts_are_not_mistaken_for_money():
    row = server._typed({"count_valor_del_contrato": "12", "max_valor_del_contrato": "20000000"},
                        {"count_valor_del_contrato", "max_valor_del_contrato"})
    assert row == {"count_valor_del_contrato": 12, "max_valor_del_contrato": 20000000,
                   "max_valor_del_contrato_texto": "20 millones de pesos"}


def test_an_entity_is_found_by_name_without_touching_the_dataset(fake_api):
    found = server.find_entity("medellín")  # accents and case do not matter
    district = next(e for e in found["entities"] if e["nit"] == 890905211)
    assert found["match"] == "all_words" and fake_api == []
    assert "CONCEJO MUNICIPAL DE MEDELLIN" in district["other_names"]  # one NIT, several bodies

    exact = server.find_entity("Alcaldía de Popayán")
    assert exact["match"] == "all_words" and exact["entities"][0]["nit"] == 891580006


def test_a_name_people_use_falls_back_to_the_place():
    # SECOP II registers Medellín's city government as a DISTRITO, so no entity has the word "alcaldía".
    found = server.find_entity("Alcaldía de Medellín")
    assert found["match"] == "place_only" and 890905211 in [e["nit"] for e in found["entities"]]
    assert server.find_entity("xyzzy") == {"entities": [], "match": "none"}
    with pytest.raises(ToolError):
        server.find_entity("de la")


def _online() -> bool:
    try:
        urllib.request.urlopen(server.DATASET + "?$limit=1", timeout=15)
        return True
    except (urllib.error.URLError, TimeoutError):
        return False


@pytest.mark.skipif(not _online(), reason="datos.gov.co not reachable")
def test_live_api_accepts_the_generated_soql():
    result = server.query_contracts(
        columns=["anio_firma"],
        aggregates=[Aggregate(function="count"), Aggregate(function="sum", column="valor_del_contrato"),
                    Aggregate(function="max", column="valor_del_contrato")],
        filters=[Filter(column="nit_entidad", op="=", value=890905211),  # Distrito de Medellín
                 Filter(column="fecha_de_firma", op=">=", value="2023-01-01"),
                 Filter(column="fecha_de_firma", op="<", value="2024-01-01"),
                 Filter(column="nombre_entidad", op="contains", value="medell")])
    assert result["rows"][0]["anio_firma"] == 2023 and result["rows"][0]["count"] > 1000
    assert isinstance(result["rows"][0]["sum_valor_del_contrato"], (int, float))
