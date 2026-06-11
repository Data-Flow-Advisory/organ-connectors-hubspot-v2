#!/usr/bin/env python3
"""
HubSpot Connector Organ — pure decision logic extracted from discovery-engine.

Source: lib/dataflow_core/connectors/hubspot.py (HubSpotConnector). That class
is a thin wrapper around the HubSpot CRM v3 REST API. Everything in it that is
NOT network I/O is pure and lives here:

  1. REQUEST SHAPING — for a CRM action (list / search / write_back / connect),
     resolve the object type, default property set, HTTP method, path, query
     params or JSON body, and the header template. (The actual `requests` call,
     timeout, and raise_for_status stay in the host — they are side effects.)

  2. RESPONSE NORMALIZATION — turn a HubSpot API JSON body
     ({"results": [...], "paging": {...}}) into a tabular
     {columns, rows, row_count, truncated} shape, exactly as the connector's
     `_list_objects` / `search` methods do.

An organ is a PURE DECIDER. It reads {state, context} JSON and returns
{output, rationale, self_metric} — no side effects, stdlib-only, deterministic,
fail-safe on bad input (never raises; returns a conservative default instead).

The organ never sees the access token. `context.has_access_token` (a bool) is
the only auth signal it consumes, and only to adjust confidence. The emitted
`headers` carry a template placeholder, never a secret.

Contract:
  INPUT:  {
    "state": {
      "operation": "build_request" | "normalize_response",  // default build_request

      // --- build_request ---
      "action": "list" | "search" | "write_back" | "connect",  // default list
      "entity_type": "contacts|deals|companies|contact|deal|company|...",  // aliased
      "limit": 100,                 // list: clamped to [1,100]; search: fixed 20
      "properties": ["email", ...], // optional override of the default set
      "query": "acme",              // search only — free-text query string
      "entity_id": "123",           // write_back only — record id to patch
      "fields": {"phone": "..."},   // write_back only — properties to set

      // --- normalize_response ---
      "body": {"results": [...], "paging": {"next": {...}}}
    },
    "context": {
      "has_access_token": true       // confidence signal only; never the token
    }
  }

  OUTPUT (build_request): {
    "output": {
      "method": "GET",
      "base_url": "https://api.hubapi.com",
      "path": "/crm/v3/objects/contacts",
      "url": "https://api.hubapi.com/crm/v3/objects/contacts",
      "params": {"limit": 100, "properties": "company,email,firstname,lastname,phone"},
      "json_body": null,
      "headers": {"Authorization": "Bearer ${HUBSPOT_ACCESS_TOKEN}",
                  "Content-Type": "application/json"},
      "object_type": "contacts",
      "properties": ["company", "email", "firstname", "lastname", "phone"],
      "requires_auth": true
    },
    "rationale": "...",
    "self_metric": {"confidence": 0.0-1.0, ...}
  }

  OUTPUT (normalize_response): {
    "output": {
      "columns": ["id", "email", ...],
      "rows": [["123", "a@b.com", ...]],
      "row_count": 1,
      "truncated": false
    },
    "rationale": "...",
    "self_metric": {"confidence": 0.0-1.0, ...}
  }
"""

import json
import os
import sys
from typing import Any, Optional


BASE_URL = "https://api.hubapi.com"

# HubSpot CRM v3 object names, plus the synonyms an upstream persona might use.
OBJECT_ALIASES = {
    "contact": "contacts",
    "contacts": "contacts",
    "person": "contacts",
    "people": "contacts",
    "deal": "deals",
    "deals": "deals",
    "opportunity": "deals",
    "opportunities": "deals",
    "company": "companies",
    "companies": "companies",
    "account": "companies",
    "accounts": "companies",
}

# Default property sets, mirroring HubSpotConnector.get_contacts/get_deals/
# get_companies in the source connector.
DEFAULT_PROPERTIES = {
    "contacts": ["firstname", "lastname", "email", "phone", "company"],
    "deals": ["dealname", "amount", "dealstage", "closedate"],
    "companies": ["name", "domain", "industry"],
}

# Source hardcodes these.
LIST_MAX_LIMIT = 100
SEARCH_LIMIT = 20

HEADER_TEMPLATE = {
    "Authorization": "Bearer ${HUBSPOT_ACCESS_TOKEN}",
    "Content-Type": "application/json",
}

VALID_ACTIONS = {"list", "search", "write_back", "connect"}


def _resolve_object_type(entity_type: Optional[str]) -> tuple[str, bool]:
    """Resolve an entity_type to a HubSpot object name.

    Returns (object_type, was_recognised). Unknown / blank entity types fall
    back to 'contacts' (the connector's default object), flagged unrecognised.
    """
    if not entity_type or not str(entity_type).strip():
        return "contacts", False
    key = str(entity_type).strip().lower()
    if key in OBJECT_ALIASES:
        return OBJECT_ALIASES[key], True
    # Already a canonical plural we don't alias? Accept it verbatim if it's a
    # known default set; otherwise pass through but flag as unrecognised.
    if key in DEFAULT_PROPERTIES:
        return key, True
    return key, False


def _clamp_list_limit(limit: Any) -> int:
    """Clamp a list limit to [1, 100] (source uses min(limit, 100))."""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return LIST_MAX_LIMIT
    return min(max(n, 1), LIST_MAX_LIMIT)


def _build_request(state: dict, has_token: bool) -> dict:
    """Shape an HTTP request plan for a HubSpot CRM action. Pure."""
    action = str(state.get("action", "list") or "list").strip().lower()
    if action not in VALID_ACTIONS:
        action = "list"

    entity_type = state.get("entity_type")
    object_type, recognised = _resolve_object_type(entity_type)

    # --- connect: a 1-row contacts probe (source HubSpotConnector.connect) ---
    if action == "connect":
        output = {
            "method": "GET",
            "base_url": BASE_URL,
            "path": "/crm/v3/objects/contacts",
            "url": f"{BASE_URL}/crm/v3/objects/contacts",
            "params": {"limit": 1},
            "json_body": None,
            "headers": dict(HEADER_TEMPLATE),
            "object_type": "contacts",
            "properties": [],
            "requires_auth": True,
        }
        rationale = "Connectivity probe: GET 1 contact to validate the token."

    # --- search: POST .../{object}/search with a free-text query ---
    elif action == "search":
        # Source defaults the object to contacts when entity_type is absent.
        if not entity_type or not str(entity_type).strip():
            object_type = "contacts"
        query = state.get("query", "")
        path = f"/crm/v3/objects/{object_type}/search"
        output = {
            "method": "POST",
            "base_url": BASE_URL,
            "path": path,
            "url": f"{BASE_URL}{path}",
            "params": None,
            "json_body": {
                "filterGroups": [],
                "query": "" if query is None else str(query),
                "limit": SEARCH_LIMIT,
            },
            "headers": dict(HEADER_TEMPLATE),
            "object_type": object_type,
            "properties": [],
            "requires_auth": True,
        }
        rationale = (
            f"Free-text search over {object_type} for query "
            f"{state.get('query')!r} (HubSpot search API, limit {SEARCH_LIMIT})."
        )

    # --- write_back: PATCH .../{object}/{id} with a properties bag ---
    elif action == "write_back":
        entity_id = state.get("entity_id")
        fields = state.get("fields") or {}
        if not isinstance(fields, dict):
            fields = {}
        eid = "" if entity_id is None else str(entity_id)
        path = f"/crm/v3/objects/{object_type}/{eid}"
        output = {
            "method": "PATCH",
            "base_url": BASE_URL,
            "path": path,
            "url": f"{BASE_URL}{path}",
            "params": None,
            "json_body": {"properties": fields},
            "headers": dict(HEADER_TEMPLATE),
            "object_type": object_type,
            "properties": sorted(fields.keys()),
            "requires_auth": True,
        }
        rationale = (
            f"Update {object_type} record {eid!r} with "
            f"{len(fields)} property/-ies (PATCH)."
        )

    # --- list (default): GET .../{object} with properties + limit ---
    else:
        limit = _clamp_list_limit(state.get("limit", LIST_MAX_LIMIT))
        override = state.get("properties")
        if override and isinstance(override, list) and len(override) > 0:
            properties = [str(p) for p in override]
            prop_source = "provided"
        else:
            properties = list(DEFAULT_PROPERTIES.get(object_type, []))
            prop_source = "default"
        params: dict[str, Any] = {"limit": limit}
        # Source joins properties as a comma string; sorted for determinism.
        if properties:
            params["properties"] = ",".join(sorted(properties))
        path = f"/crm/v3/objects/{object_type}"
        output = {
            "method": "GET",
            "base_url": BASE_URL,
            "path": path,
            "url": f"{BASE_URL}{path}",
            "params": params,
            "json_body": None,
            "headers": dict(HEADER_TEMPLATE),
            "object_type": object_type,
            "properties": sorted(properties),
            "requires_auth": True,
        }
        rationale = (
            f"List up to {limit} {object_type} with {len(properties)} "
            f"{prop_source} propert{'y' if len(properties) == 1 else 'ies'}."
        )

    # Confidence: baseline + signals.
    confidence = 0.6
    if recognised or action == "connect":
        confidence += 0.2
    else:
        confidence -= 0.15
    if has_token:
        confidence += 0.15
    if action == "write_back" and not state.get("entity_id"):
        confidence -= 0.4  # patch with no id is almost certainly a mistake
    if action == "search" and not state.get("query"):
        confidence -= 0.1
    confidence = min(max(confidence, 0.0), 1.0)

    return {
        "output": output,
        "rationale": rationale,
        "self_metric": {
            "confidence": round(confidence, 2),
            "action": action,
            "object_type": output["object_type"],
            "entity_recognised": recognised,
            "has_access_token": bool(has_token),
        },
    }


def _normalize_response(state: dict) -> dict:
    """Flatten a HubSpot list/search response body into columns + rows. Pure.

    Mirrors HubSpotConnector._list_objects / .search post-processing:
      results = body["results"]; if empty -> empty result.
      prop_keys = sorted(results[0]["properties"].keys())
      columns = ["id"] + prop_keys
      rows = [[id] + [props.get(k) for k in prop_keys] for each result]
      truncated = body["paging"]["next"] is present
    """
    body = state.get("body")
    if not isinstance(body, dict):
        body = {}
    results = body.get("results")
    if not isinstance(results, list):
        results = []

    if not results:
        return {
            "output": {"columns": [], "rows": [], "row_count": 0, "truncated": False},
            "rationale": "Empty result set: no rows to normalize.",
            "self_metric": {"confidence": 1.0, "row_count": 0, "column_count": 0},
        }

    first_props = results[0].get("properties") if isinstance(results[0], dict) else None
    if not isinstance(first_props, dict):
        first_props = {}
    prop_keys = sorted(first_props.keys())
    columns = ["id"] + prop_keys

    rows = []
    for r in results:
        if not isinstance(r, dict):
            continue
        props = r.get("properties")
        if not isinstance(props, dict):
            props = {}
        rows.append([r.get("id")] + [props.get(k) for k in prop_keys])

    paging = body.get("paging")
    truncated = bool(isinstance(paging, dict) and paging.get("next") is not None)

    return {
        "output": {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
        },
        "rationale": (
            f"Normalized {len(rows)} HubSpot record(s) into {len(columns)} "
            f"column(s); truncated={truncated}."
        ),
        "self_metric": {
            "confidence": 1.0,
            "row_count": len(rows),
            "column_count": len(columns),
            "truncated": truncated,
        },
    }


def decide(state: dict, context: Optional[dict] = None) -> dict:
    """Pure decider. Dispatches on state.operation. Never raises."""
    context = context or {}
    try:
        if not isinstance(state, dict):
            state = {}
        operation = str(state.get("operation", "build_request") or "build_request").strip().lower()
        has_token = bool(context.get("has_access_token", True))

        if operation == "normalize_response":
            return _normalize_response(state)
        # default: build_request
        return _build_request(state, has_token)

    except Exception as e:  # fail-safe: never raise to the orchestrator
        return {
            "output": {
                "method": "GET",
                "base_url": BASE_URL,
                "path": "/crm/v3/objects/contacts",
                "url": f"{BASE_URL}/crm/v3/objects/contacts",
                "params": {"limit": 1},
                "json_body": None,
                "headers": dict(HEADER_TEMPLATE),
                "object_type": "contacts",
                "properties": [],
                "requires_auth": True,
            },
            "rationale": f"Fallback to a safe contacts probe due to error: {e}",
            "self_metric": {"confidence": 0.1, "error": str(e)},
        }


def run_organ(payload: dict) -> dict:
    """Convenience wrapper: take a full {state, context} payload, return result."""
    if not isinstance(payload, dict):
        payload = {}
    return decide(payload.get("state", {}), payload.get("context"))


def main() -> int:
    """Entrypoint: read JSON from ORGAN_INPUT file or stdin; write result to stdout."""
    path = os.environ.get("ORGAN_INPUT")
    raw = open(path).read() if path else sys.stdin.read()

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"invalid JSON: {e}"}), file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": f"failed to read input: {e}"}), file=sys.stderr)
        return 1

    result = run_organ(payload if isinstance(payload, dict) else {})
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
