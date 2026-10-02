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
   describe_dataset → find_entity (local directory: the entity's NIT) → query_contracts (total per supplier)
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
- **Names are not identifiers.** A search for "Medellín" returns the sports institute, the district, a library and a hospital, and the city government is not registered as "Alcaldía" at all. `find_entity` resolves a name to a NIT from a local directory of the 5,800 entities, and the model filters by NIT afterwards.
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

## The same question, asked through the model

`uv run python chat.py` with its default question, "¿Cuáles fueron los cinco mayores contratistas de la Alcaldía de Medellín en 2024?", run on 1 October 2026 with `claude-sonnet-5-5`. Five tool calls in about two minutes, from the audit log:

| Call | Result |
|---|---|
| `describe_dataset` | columns and traps |
| name contains "medell", year 2024, grouped by entity | **timeout** after 60 s: a substring match over a year of contracts |
| name contains "alcaldía de medell", year 2024 | 0 rows: SECOP II does not call it "Alcaldía" |
| name and city contain "medell", year 2024 | 15 entities, which is how it found the official name |
| exact entity name, year 2024, drafts and cancellations excluded, `sum` + `max` + `count` by supplier, top 5 | 5 rows |

The five suppliers and amounts in the answer are the ones in the table above, and it did what the server asks for: it listed the filters it used, said the first place is a single contract that should be checked at the source, said that most of the list is contracts between public bodies, and said SECOP II totals are a floor. It also noticed that the Concejo and the Personería share the district's NIT and filtered by the exact entity name instead, which the NIT-first advice in `describe_dataset` does not anticipate.

Two things went wrong, and both are worth more than the success:

- **One unit slip in the prose.** The table says "491.372 mil millones", which is right. A sentence below it calls the same contract "unos 491 billones de pesos", which in Spanish is a thousand times more. The tool returned the right number; the model mislabelled it once while writing. Amounts should be formatted by code, not by the model.
- **Finding the entity cost a timeout and two extra calls.** Name search is the slow path on this dataset: a substring match over a year of contracts.

## What that session changed

Both problems were fixed in the tool, not in the prompt.

- **`find_entity`, a third tool.** The server now ships the entity directory that [secop-api](https://github.com/0103juan/secop-api) builds: one entry per NIT with its other spellings. The search runs in memory, ignores case and accents, and never touches the dataset. "Alcaldía de Medellín" matches nothing word for word, because the dataset calls it a district; the tool then searches by the place alone, says so (`"match": "place_only"`) and returns the district with the council and the ombudsman listed under the same NIT. `describe_dataset` now tells the model not to look for entities with `contains`.
- **Amounts are written out by code.** Every money value in a result comes with a twin field, `sum_valor_del_contrato_texto: "491.372 millones de pesos"`, and the model is told to quote it as it is. The rule that a Spanish *billón* is a million millions lives in one tested function instead of in the model's arithmetic.

The session above is the one before these changes. I have not run the model against the new tools yet, so what they do to the number of calls and to the answer is still to be measured; the tests below check the tools themselves.

## Run it

```bash
uv sync
uv run pytest        # 24 tests; 23 run offline, 1 calls datos.gov.co and is skipped without network
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
- An entity is found by name without any request to the dataset, whatever the accents, and a name people use ("Alcaldía de Medellín") falls back to the place.
- Amounts are written out as a Colombian reader expects, 10¹² is a *billón*, and a count is never formatted as money.
- Against the live API, the generated SoQL is accepted and returns the expected shape.

## Limits, stated plainly

- Filters are combined with `AND` only. There is no `OR`, no nesting and no joins, because there is one dataset.
- Text search is a case-insensitive substring match. It does not fold accents, and the source is inconsistent about them.
- The server reports the data; it cannot repair it. Outliers, duplicates and late updates in SECOP II flow straight through.
- Response time depends on datos.gov.co: usually under a second for filtered queries, 10 seconds or more for scans over all six million rows, and occasionally a timeout.
- The entity directory is a snapshot (`entities.json`, copied from secop-api). An entity that started publishing after it was built is not found by name until the file is refreshed.
- `chat.py` has been run against the live model on one question (above), before `find_entity` and the formatted amounts existed. That is an example, not an evaluation: there is no golden set of questions for this server.

## Layout

```
server.py       the MCP server: the column allowlist, the query builder, three tools, the audit log
entities.json   the entity directory find_entity searches: one entry per NIT with its other spellings
chat.py         Claude as MCP host, answering in Spanish; prints the tool calls, tokens and seconds a session took
test_server.py  query-builder, privacy and protocol tests, plus one live test
```
