#!/usr/bin/env python3
"""Conformance check for the organ's connection-standard port declaration.

Asserts the connection-standard contract for ports.json:

  1. ports.json parses and has the required {inputs, outputs} shape, with each
     input declaring a boolean `required`.
  2. Every `type` referenced by a port exists in the types.json vocabulary.
  3a. decide() actually READS every declared input name from `state`.
  3b. decide() WRITES exactly the declared output names — collectively. This
      organ dispatches on `state.operation` (build_request vs normalize_response),
      so different operations emit different output key sets. The contract is
      therefore: across a set of representative states (the empty/fail-safe
      state plus every committed sample), (i) no operation may write an output
      key that is NOT declared, and (ii) every declared output key MUST be
      written by at least one operation (full coverage, no dead ports).

Pure / stdlib-only — runnable on every supported Python with no extra deps.
Exits non-zero (with ::error:: annotations) on any violation so CI goes red.
"""
import json
import re
import sys
from pathlib import Path

from organ import decide

PORTS_PATH = Path("ports.json")
TYPES_PATH = Path("types.json")
ORGAN_PATH = Path("organ.py")


def _fail(msg: str) -> None:
    print(f"::error::{msg}")
    sys.exit(1)


def _load_json(path: Path, label: str) -> object:
    if not path.exists():
        _fail(f"{label} missing: {path}")
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        _fail(f"{label} does not parse as JSON: {e}")


def _vocabulary(types_doc: object) -> set:
    if not isinstance(types_doc, dict):
        _fail("types.json must be a JSON object mapping type-name -> description")
    # Keys starting with "_" are metadata/comments, not vocabulary entries.
    return {k for k in types_doc if not k.startswith("_")}


def _state_reads_from_organ() -> set:
    """Top-level keys decide() reads from `state`, parsed from organ.py source.

    Matches `state.get("X")` and `state["X"]` subscript forms.
    """
    source = ORGAN_PATH.read_text()
    reads: set = set()
    for m in re.finditer(r"state[^\n]*?\.get\(\s*[\"']([A-Za-z_][\w]*)[\"']", source):
        reads.add(m.group(1))
    for m in re.finditer(r"state[^\n]*?\[\s*[\"']([A-Za-z_][\w]*)[\"']\s*\]", source):
        reads.add(m.group(1))
    return reads


def _representative_states() -> list:
    """State payloads to exercise decide() for the output-name check.

    The empty state covers the fail-safe path; each committed sample covers a
    real operation. Together they must exercise every declared output.
    """
    states = [{}]  # empty / fail-safe path
    for sample in sorted(Path("samples").glob("*.json")):
        try:
            payload = json.loads(sample.read_text())
        except json.JSONDecodeError:
            continue
        states.append(payload.get("state", payload) if isinstance(payload, dict) else {})
    return states


def main() -> None:
    if not ORGAN_PATH.exists():
        _fail("no organ.py — cannot validate ports")

    ports = _load_json(PORTS_PATH, "ports.json")
    types_doc = _load_json(TYPES_PATH, "types.json")
    vocab = _vocabulary(types_doc)

    # ---- 1. shape ----------------------------------------------------------
    if not isinstance(ports, dict):
        _fail("ports.json must be a JSON object")
    for key in ("inputs", "outputs"):
        if not isinstance(ports.get(key), list):
            _fail(f"ports.json.{key} must be a list")

    input_names: list = []
    for i, port in enumerate(ports["inputs"]):
        if not isinstance(port, dict) or "name" not in port or "type" not in port:
            _fail(f"inputs[{i}] must be an object with 'name' and 'type'")
        if "required" not in port or not isinstance(port["required"], bool):
            _fail(f"inputs[{i}] ({port.get('name')!r}) must declare a boolean 'required'")
        input_names.append(port["name"])

    output_names: list = []
    for i, port in enumerate(ports["outputs"]):
        if not isinstance(port, dict) or "name" not in port or "type" not in port:
            _fail(f"outputs[{i}] must be an object with 'name' and 'type'")
        output_names.append(port["name"])

    # ---- 2. every declared type is in the vocabulary -----------------------
    for port in ports["inputs"] + ports["outputs"]:
        if port["type"] not in vocab:
            _fail(
                f"port {port['name']!r} declares type {port['type']!r} which is "
                f"not in the types.json vocabulary {sorted(vocab)}"
            )

    # ---- 3a. decide() reads every declared input from `state` --------------
    reads = _state_reads_from_organ()
    missing_reads = [n for n in input_names if n not in reads]
    if missing_reads:
        _fail(
            f"ports.json declares input(s) {missing_reads} that decide() does not "
            f"read from state (state reads found: {sorted(reads)})"
        )

    # ---- 3b. declared outputs == union of produced output keys -------------
    # No operation may emit an undeclared output; every declared output must be
    # produced by at least one representative operation.
    declared_out = set(output_names)
    produced_union: set = set()
    for state in _representative_states():
        result = decide(state, {})
        produced = set(result.get("output", {}))
        undeclared = produced - declared_out
        if undeclared:
            _fail(
                "decide() produced output key(s) not declared in ports.json:\n"
                f"  undeclared : {sorted(undeclared)}\n"
                f"  declared   : {sorted(declared_out)}\n"
                f"  state      : {json.dumps(state)[:200]}"
            )
        produced_union |= produced

    never_produced = declared_out - produced_union
    if never_produced:
        _fail(
            "ports.json declares output(s) that no representative operation "
            "produces (dead ports):\n"
            f"  never produced : {sorted(never_produced)}\n"
            f"  produced union : {sorted(produced_union)}"
        )

    print("OK ports.json:")
    print(f"  inputs        : {input_names}")
    print(f"  outputs       : {output_names}")
    print(f"  vocab         : {sorted(vocab)}")
    print(f"  produced union: {sorted(produced_union)}")
    print("All port-contract checks passed.")


if __name__ == "__main__":
    main()
