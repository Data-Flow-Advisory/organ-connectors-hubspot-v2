# organ-connectors-hubspot

Pure decision logic extracted from discovery-engine's HubSpot CRM connector
(`lib/dataflow_core/connectors/hubspot.py` — `HubSpotConnector`).

An **organ** is a pure decider: it reads `{state, context}` JSON and returns
`{output, rationale, self_metric}` JSON. No network, no side effects,
stdlib-only, deterministic, and fail-safe on bad input (it never raises — it
returns a conservative default).

> **v2 note.** This is the corrected build. A prior attempt
> (`organ-connectors-hubspot`, single commit) invented a "ranking" behaviour
> that does **not** exist anywhere in the source connector, and its CI was red.
> This organ extracts the connector's *actual* pure logic: request shaping and
> response normalization.

## What it decides

The source `HubSpotConnector` is a thin wrapper over the HubSpot CRM v3 REST
API. Everything in it that is **not** an HTTP call is pure and lives here:

1. **Request shaping** (`operation: "build_request"`) — for a CRM `action`
   (`list` / `search` / `write_back` / `connect`), resolve the object type and
   default property set, then produce the HTTP method, path, URL, query params
   or JSON body, and a header *template*. The host performs the actual
   `requests` call (timeout, `raise_for_status`) — those are side effects and
   stay out of the organ.

2. **Response normalization** (`operation: "normalize_response"`) — turn a
   HubSpot list/search response body (`{"results": [...], "paging": {...}}`)
   into a tabular `{columns, rows, row_count, truncated}` shape, exactly as
   `_list_objects` / `search` do downstream of the HTTP fetch.

The organ **never sees the access token.** `context.has_access_token` (a bool)
is the only auth signal it consumes, and only to adjust confidence. The emitted
`headers` carry the template placeholder `Bearer ${HUBSPOT_ACCESS_TOKEN}`,
never a secret.

## I/O contract

### build_request

```json
{
  "state": {
    "operation": "build_request",
    "action": "list",
    "entity_type": "contacts",
    "limit": 100,
    "properties": ["email", "lifecyclestage"]
  },
  "context": { "has_access_token": true }
}
```

→

```json
{
  "output": {
    "method": "GET",
    "base_url": "https://api.hubapi.com",
    "path": "/crm/v3/objects/contacts",
    "url": "https://api.hubapi.com/crm/v3/objects/contacts",
    "params": { "limit": 100, "properties": "email,lifecyclestage" },
    "json_body": null,
    "headers": {
      "Authorization": "Bearer ${HUBSPOT_ACCESS_TOKEN}",
      "Content-Type": "application/json"
    },
    "object_type": "contacts",
    "properties": ["email", "lifecyclestage"],
    "requires_auth": true
  },
  "rationale": "...",
  "self_metric": { "confidence": 0.95, "action": "list", "object_type": "contacts", "...": "..." }
}
```

`action` values:

| action       | method | path                                   | body / params |
|--------------|--------|----------------------------------------|---------------|
| `list`       | GET    | `/crm/v3/objects/{object}`             | params `{limit (≤100), properties}` |
| `search`     | POST   | `/crm/v3/objects/{object}/search`      | json `{filterGroups: [], query, limit: 20}` |
| `write_back` | PATCH  | `/crm/v3/objects/{object}/{id}`        | json `{properties: {...}}` |
| `connect`    | GET    | `/crm/v3/objects/contacts`             | params `{limit: 1}` (token probe) |

`entity_type` is aliased: `contact/contacts/person → contacts`,
`deal/deals/opportunity → deals`, `company/companies/account → companies`.
Default property sets mirror the source: contacts
`firstname,lastname,email,phone,company`; deals `dealname,amount,dealstage,closedate`;
companies `name,domain,industry`. Unknown object types pass through verbatim
with lowered confidence and no default properties.

### normalize_response

```json
{
  "state": {
    "operation": "normalize_response",
    "body": {
      "results": [{ "id": "101", "properties": { "email": "a@b.com" } }],
      "paging": { "next": { "after": "101" } }
    }
  }
}
```

→

```json
{
  "output": { "columns": ["id", "email"], "rows": [["101", "a@b.com"]], "row_count": 1, "truncated": true },
  "rationale": "...",
  "self_metric": { "confidence": 1.0, "row_count": 1, "column_count": 2, "truncated": true }
}
```

Columns are `["id"] + sorted(first_result.properties.keys())`; later rows
missing a key get `None` in that column (faithful to the source). `truncated`
is true when the response carries `paging.next`.

## Run it

```bash
# from a sample file
ORGAN_INPUT=samples/list_contacts.json python3 organ.py

# from stdin
echo '{"state":{"action":"search","entity_type":"deals","query":"acme"}}' | python3 organ.py

# tests (no pytest required)
python3 test_organ.py
```

## Files

| File | Purpose |
|------|---------|
| `organ.py` | the pure decider (`decide` / `run_organ` / `main`) |
| `test_organ.py` | unit + subprocess-contract tests |
| `samples/*.json` | committed shadow-run inputs |
| `.github/workflows/conformance.yml` | CI: tests, shadow-run, contract, determinism, stdlib-only |
