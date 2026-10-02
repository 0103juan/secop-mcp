# secop-mcp

[![CI](https://github.com/0103juan/secop-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/0103juan/secop-mcp/actions/workflows/ci.yml)

An MCP server that lets an LLM answer questions about Colombian public procurement from the official open data: the **SECOP II electronic contracts** dataset on [datos.gov.co](https://www.datos.gov.co/Gastos-Gubernamentales/SECOP-II-Contratos-Electr-nicos/jbjy-vk9h), more than six million contracts, updated daily.

```
"¿Quiénes fueron los mayores contratistas de la Alcaldía de Medellín en 2024?"
        │
   Claude (MCP host) ──MCP/stdio──▶ server.py ──HTTPS──▶ datos.gov.co (Socrata API)
        │                              │
        │                              └──▶ audit.jsonl
        ▼
   describe_dataset → query_contracts (find the entity's NIT) → query_contracts (total per supplier)
```

Anyone can install it: there is no database to set up and no key to request.

## The design decision: the model never writes a query

The dataset is queried with SoQL, a SQL-like language passed in the URL. The obvious tool is "here is a `where` string, run it". This server does not offer that. `query_contracts` takes a **typed query**:

```json
{
  "columns": ["proveedor_adjudicado", "documento_proveedor"],
  "aggregates": [{"function": "sum", "column": "valor_del_contrato"},
                 {"function": "max", "column": "valor_del_contrato"},
                 {"function": "count"}],
  "filters": [{"column": "nit_entidad", "op": "=", "value": 890905211},
              {"column": "anio_firma", "op": "=", "value": 2024},
              {"column": "estado_contrato", "op": "!=", "value": "Cancelado"}],
  "limit": 5
}
```

and the server builds the SoQL from it. Three things follow:

- **Column allowlist by construction.** Column names are an enum in the tool's JSON Schema, so a request for anything else is rejected before any HTTP call. Nothing has to be parsed or filtered afterwards.
- **Values are always literals.** Text is quoted and escaped, numbers must parse as finite numbers, dates must be `YYYY-MM-DD`. A value such as `x' OR 1=1 --` ends up inside a string.
- **Privacy by default.** The published dataset has 95 columns, and some hold personal data: bank account numbers, home addresses and ID numbers of legal representatives, supervisors and payment officers. Being public does not make it appropriate to hand to a model. The allowlist holds 25 of the published columns plus one derived column (the signing year), covering the entity, the supplier, the contract, its dates and its amounts. The other 70, the personal ones among them, cannot be reached.

## The data is the hard part

`describe_dataset` returns the columns and, more importantly, the traps. These are the ones I hit while building it:

- **Typos of several orders of magnitude.** The largest contract signed since 2024 is recorded as 6.4 × 10¹⁵ pesos, more than the entire national budget. One such row ruins any `SUM`. The server tells the model to request `max` and `count` alongside every `sum` and to say so when one contract explains the total.
- **Names are not identifiers.** A search for "Medellín" returns the sports institute, the district, a library and a hospital. The model is told to resolve a name to a NIT first and filter by NIT afterwards.
- **Drafts and cancellations are in the data.** "How much was contracted" has to exclude them.
- **SECOP II is not all of public procurement.** SECOP I and the state's online store are separate datasets, so every total is a floor.

## An example, checked against the API

Top suppliers of the Distrito de Medellín (NIT 890905211) for contracts signed in 2024, excluding drafts and cancellations, as returned on 1 October 2026:

| Supplier | Total (COP) | Largest single contract | Contracts |
|---|---|---|---|
| Bancolombia | 491,372,098,658 | 491,372,098,658 | 1 |
| ESE Metrosalud | 342,731,922,079 | 45,000,000,000 | 28 |
| Institución Universitaria ITM | 283,678,557,488 | 17,305,351,117 | 63 |

The first row is why `max` and `count` travel with `sum`: the top "supplier" is a single contract, which a careful answer should point out rather than rank next to 63 separate ones. The dataset changes daily, so these figures will drift.

## Run it

```bash
uv sync
uv run pytest        # 17 tests; 16 run offline, 1 calls datos.gov.co and is skipped without network
```

Connect it to a client:

```bash
claude mcp add secop -- uv run --directory /absolute/path/to/secop-mcp python server.py
```

Or ask through the included host (needs `ANTHROPIC_API_KEY`):

```bash
uv run python chat.py "¿Cuánto contrató la Gobernación de Antioquia por licitación pública en 2023?"
```

An optional `SOCRATA_APP_TOKEN` environment variable raises the anonymous rate limit.

## What the tests prove

- The SoQL generated for a grouped total is exactly the expected one.
- Hostile values stay inside string literals; ill-typed values (a non-number for a NIT, `inf`, a malformed date) are rejected.
- Personal-data columns and made-up column names cannot be selected, filtered or sorted on, both when calling the function directly and through the MCP protocol, where the schema rejects them before any request is made.
- The row limit is capped at 100, rows come back with real numbers instead of strings, and every call is written to the audit log.
- Against the live API, the generated SoQL is accepted and returns the expected shape.

## Limits, stated plainly

- Filters are combined with `AND` only. There is no `OR`, no nesting and no joins, because there is one dataset.
- Text search is a case-insensitive substring match. It does not fold accents, and the source is inconsistent about them.
- The server reports the data; it cannot repair it. Outliers, duplicates and late updates in SECOP II flow straight through.
- Response time depends on datos.gov.co: usually under a second for filtered queries, 10 seconds or more for scans over all six million rows, and occasionally a timeout.
- `chat.py` has not been run against the live model yet. The server has: over stdio, against the real API.

## Layout

```
server.py       the MCP server: the column allowlist, the query builder, two tools, the audit log
chat.py         Claude as MCP host, answering in Spanish
test_server.py  query-builder, privacy and protocol tests, plus one live test
```
