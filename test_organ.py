#!/usr/bin/env python3
"""Tests for the HubSpot Connector Organ.

Self-contained: runnable as `python3 test_organ.py` (no pytest needed) and
also discoverable by pytest. Exercises decide() directly plus the stdin/stdout
subprocess contract.
"""

import json
import subprocess
import sys

from organ import decide, run_organ


# ---- build_request: list ----------------------------------------------------

def test_list_contacts_defaults():
    out = decide({"action": "list", "entity_type": "contacts"})
    o = out["output"]
    assert o["method"] == "GET"
    assert o["path"] == "/crm/v3/objects/contacts"
    assert o["url"] == "https://api.hubapi.com/crm/v3/objects/contacts"
    assert o["object_type"] == "contacts"
    # default property set (sorted)
    assert o["params"]["properties"] == "company,email,firstname,lastname,phone"
    assert o["params"]["limit"] == 100
    assert o["json_body"] is None
    assert out["self_metric"]["confidence"] > 0.7


def test_list_is_default_action_and_object():
    # No action, no entity_type -> list contacts.
    out = decide({})
    assert out["output"]["method"] == "GET"
    assert out["output"]["object_type"] == "contacts"
    assert out["self_metric"]["action"] == "list"


def test_list_deals_default_properties():
    out = decide({"action": "list", "entity_type": "deals"})
    o = out["output"]
    assert o["object_type"] == "deals"
    assert o["params"]["properties"] == "amount,closedate,dealname,dealstage"


def test_list_companies_default_properties():
    out = decide({"action": "list", "entity_type": "companies"})
    assert out["output"]["params"]["properties"] == "domain,industry,name"


def test_entity_alias_resolution():
    # singular + synonym forms resolve to plural object names
    assert decide({"entity_type": "deal"})["output"]["object_type"] == "deals"
    assert decide({"entity_type": "opportunity"})["output"]["object_type"] == "deals"
    assert decide({"entity_type": "company"})["output"]["object_type"] == "companies"
    assert decide({"entity_type": "account"})["output"]["object_type"] == "companies"
    assert decide({"entity_type": "person"})["output"]["object_type"] == "contacts"


def test_limit_clamp_high():
    out = decide({"action": "list", "entity_type": "contacts", "limit": 5000})
    assert out["output"]["params"]["limit"] == 100


def test_limit_clamp_low_and_bad():
    assert decide({"action": "list", "limit": 0})["output"]["params"]["limit"] == 1
    assert decide({"action": "list", "limit": "oops"})["output"]["params"]["limit"] == 100


def test_properties_override():
    out = decide({"action": "list", "entity_type": "contacts",
                  "properties": ["email", "lifecyclestage"]})
    assert out["output"]["params"]["properties"] == "email,lifecyclestage"
    assert out["output"]["properties"] == ["email", "lifecyclestage"]


def test_unrecognised_entity_lowers_confidence():
    out = decide({"action": "list", "entity_type": "widgets"})
    assert out["output"]["object_type"] == "widgets"
    assert out["self_metric"]["entity_recognised"] is False
    # no default property set for an unknown object -> no properties param
    assert "properties" not in out["output"]["params"]


# ---- build_request: search --------------------------------------------------

def test_search_default_object_is_contacts():
    out = decide({"action": "search", "query": "acme"})
    o = out["output"]
    assert o["method"] == "POST"
    assert o["path"] == "/crm/v3/objects/contacts/search"
    assert o["params"] is None
    assert o["json_body"] == {"filterGroups": [], "query": "acme", "limit": 20}


def test_search_explicit_entity():
    out = decide({"action": "search", "entity_type": "deals", "query": "Q3"})
    assert out["output"]["path"] == "/crm/v3/objects/deals/search"
    assert out["output"]["json_body"]["query"] == "Q3"


def test_search_missing_query_is_empty_string():
    out = decide({"action": "search", "entity_type": "contacts"})
    assert out["output"]["json_body"]["query"] == ""


# ---- build_request: write_back ----------------------------------------------

def test_write_back_shape():
    out = decide({
        "action": "write_back",
        "entity_type": "contacts",
        "entity_id": "501",
        "fields": {"phone": "+44 1", "company": "DFA"},
    })
    o = out["output"]
    assert o["method"] == "PATCH"
    assert o["path"] == "/crm/v3/objects/contacts/501"
    assert o["json_body"] == {"properties": {"phone": "+44 1", "company": "DFA"}}
    assert o["properties"] == ["company", "phone"]


def test_write_back_missing_id_lowers_confidence():
    out = decide({"action": "write_back", "entity_type": "deals",
                  "fields": {"amount": "10"}})
    assert out["output"]["path"] == "/crm/v3/objects/deals/"
    assert out["self_metric"]["confidence"] < 0.6


def test_write_back_non_dict_fields():
    out = decide({"action": "write_back", "entity_type": "contacts",
                  "entity_id": "1", "fields": "nope"})
    assert out["output"]["json_body"] == {"properties": {}}


# ---- build_request: connect -------------------------------------------------

def test_connect_probe():
    out = decide({"action": "connect"})
    o = out["output"]
    assert o["method"] == "GET"
    assert o["path"] == "/crm/v3/objects/contacts"
    assert o["params"] == {"limit": 1}


def test_invalid_action_falls_back_to_list():
    out = decide({"action": "frobnicate", "entity_type": "contacts"})
    assert out["self_metric"]["action"] == "list"
    assert out["output"]["method"] == "GET"


# ---- auth / headers ---------------------------------------------------------

def test_headers_never_carry_a_real_token():
    out = decide({"action": "list"})
    auth = out["output"]["headers"]["Authorization"]
    assert auth == "Bearer ${HUBSPOT_ACCESS_TOKEN}"
    assert "sk-" not in auth and "pat-" not in auth


def test_no_token_lowers_confidence():
    with_token = decide({"action": "list", "entity_type": "contacts"},
                        {"has_access_token": True})
    without = decide({"action": "list", "entity_type": "contacts"},
                     {"has_access_token": False})
    assert without["self_metric"]["confidence"] < with_token["self_metric"]["confidence"]
    assert without["self_metric"]["has_access_token"] is False


# ---- normalize_response -----------------------------------------------------

def test_normalize_basic():
    body = {
        "results": [
            {"id": "1", "properties": {"email": "a@b.com", "firstname": "Ann"}},
            {"id": "2", "properties": {"email": "c@d.com", "firstname": "Cy"}},
        ],
        "paging": {"next": {"after": "2"}},
    }
    out = decide({"operation": "normalize_response", "body": body})
    o = out["output"]
    assert o["columns"] == ["id", "email", "firstname"]
    assert o["rows"] == [["1", "a@b.com", "Ann"], ["2", "c@d.com", "Cy"]]
    assert o["row_count"] == 2
    assert o["truncated"] is True


def test_normalize_empty():
    out = decide({"operation": "normalize_response", "body": {"results": []}})
    assert out["output"] == {"columns": [], "rows": [], "row_count": 0, "truncated": False}


def test_normalize_no_paging_not_truncated():
    body = {"results": [{"id": "9", "properties": {"name": "X"}}]}
    out = decide({"operation": "normalize_response", "body": body})
    assert out["output"]["truncated"] is False
    assert out["output"]["columns"] == ["id", "name"]


def test_normalize_missing_property_fills_none():
    # column keys come from the FIRST result; later rows missing a key -> None
    body = {"results": [
        {"id": "1", "properties": {"email": "a@b.com", "phone": "1"}},
        {"id": "2", "properties": {"email": "c@d.com"}},
    ]}
    out = decide({"operation": "normalize_response", "body": body})
    assert out["output"]["rows"][1] == ["2", "c@d.com", None]


def test_normalize_garbage_body_fails_safe():
    out = decide({"operation": "normalize_response", "body": "not a dict"})
    assert out["output"]["row_count"] == 0


# ---- robustness / fail-safe -------------------------------------------------

def test_decide_never_raises_on_garbage_state():
    out = decide("totally wrong")
    assert "output" in out and "self_metric" in out


def test_determinism():
    payload = {"state": {"action": "list", "entity_type": "deals", "limit": 50}}
    a = run_organ(payload)
    b = run_organ(payload)
    assert a == b


# ---- stdin/stdout subprocess contract --------------------------------------

def test_subprocess_contract():
    payload = {"state": {"action": "list", "entity_type": "contacts"}}
    result = subprocess.run(
        [sys.executable, "organ.py"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    output = json.loads(result.stdout)
    assert "output" in output
    assert "rationale" in output
    assert "self_metric" in output
    assert 0.0 <= output["self_metric"]["confidence"] <= 1.0


def test_subprocess_invalid_json():
    result = subprocess.run(
        [sys.executable, "organ.py"],
        input="{ not json",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "invalid JSON" in result.stderr


if __name__ == "__main__":
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"✓ {t.__name__}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"✗ {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
