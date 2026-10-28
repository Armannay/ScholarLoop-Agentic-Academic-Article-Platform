"""The served Runs/Article surface, machine-checked against the frozen spec.

``docs/openapi/openapi.yaml`` is frozen (tag ``contracts-v1.1``): "implement the
endpoints exactly as in the spec" is only true if something keeps proving it. This
module reads the YAML, reads ``/openapi.json`` from the running app, and compares the
two operation by operation:

* every runs/article operation in the spec is served, with the same methods;
* parameters: same names, locations, requiredness, types, bounds and defaults;
* status codes: exactly the spec's set (plus FastAPI's automatic ``422``, which no
  annotation can suppress);
* error responses: the spec's ``ErrorEnvelope`` (and ``ValidationErrorEnvelope`` for
  422) rather than FastAPI's own ``HTTPValidationError``;
* payloads: the same component per status, the same field set, and never a field the
  app requires that the spec does not.

Three deviations are **asserted by name**, so they stay deliberate and cannot grow a
fourth silently:

1. ``run_id``/``section_id`` are opaque strings to the router instead of the spec's
   ``Ulid``/``SectionId`` pattern, so a malformed id answers the ``404`` the spec
   declares for those operations (like ``/api/projects/{project_id}``) instead of a
   ``422`` it does not;
2. ``project_id`` accepts the spec's slug **and** a project ULID - a strict superset,
   requested by Step 11 (``spec-change-requests.md`` #3) and needed for the console to
   address a stored project;
3. ``422`` is present on operations where the app validates a parameter and the spec
   does not list it; FastAPI adds it whenever a parameter is constrained.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from app.api.routers.runs import PROJECT_ID_PATTERN

pytestmark = [pytest.mark.api]

HTTP_METHODS = ("get", "post", "put", "patch", "delete")

#: The frozen spec's ``ProjectId`` pattern. The router accepts this *plus* a ULID.
SPEC_PROJECT_ID_PATTERN = "^[a-z0-9][a-z0-9-]{2,63}$"

#: Path params that the spec types (``Ulid``/``SectionId``) and the router takes as a
#: string. See deviation 1 in the module docstring.
OPAQUE_ID_PARAMS = {("run_id", "path"), ("section_id", "path")}

#: Deviations the spec cannot express through FastAPI (docstring 1-3).
ACCEPTED_EXTRA_STATUS_CODES = {"422"}

SCALAR_KEYS = (
    "type",
    "format",
    "pattern",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "default",
    "enum",
    "const",
)

#: Every payload schema the surface puts on the wire, in the spec's spelling.
WIRE_MODELS = (
    "RunView",
    "RunSummary",
    "RunList",
    "RunCreate",
    "UserDecision",
    "DigestView",
    "LedgerView",
    "QualityView",
    "QualityPoint",
    "ArticleView",
    "ArticleSection",
    "RunUsageView",
    "UsageBreakdown",
    "RunEventList",
    "RunEvent",
    "SectionHistoryView",
    "VersionInfo",
    "SectionDiffView",
    "TextDiff",
    "SnapshotList",
    "SnapshotSummary",
    "RestoreRequest",
    "RestoreView",
    "ErrorEnvelope",
    "ValidationErrorEnvelope",
)


# --------------------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def spec(repo_root: Path) -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load(
        (repo_root / "docs/openapi/openapi.yaml").read_text(encoding="utf-8")
    )
    return document


@pytest.fixture
def served(client: TestClient) -> dict[str, Any]:
    document: dict[str, Any] = client.get("/openapi.json").json()
    return document


# --------------------------------------------------------------------------- helpers
def _is_surface(path: str) -> bool:
    """True for the operations this step owns: ``/api/runs/…`` and a project's runs."""
    return path.startswith("/api/runs") or path.endswith("/runs")


def _operations(spec: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    found: list[tuple[str, str, dict[str, Any]]] = []
    for path, item in spec["paths"].items():
        if not _is_surface(path):
            continue
        for method, operation in item.items():
            if method in HTTP_METHODS:
                found.append((path, method, operation))
    return found


def _operation_of(spec: dict[str, Any], path: str, method: str) -> dict[str, Any]:
    operation: dict[str, Any] = spec["paths"][path][method]
    return operation


def _resolve(document: dict[str, Any], schema: dict[str, Any]) -> Any:
    """Follow local ``$ref``s into that document's ``components``; external refs stay."""
    seen = 0
    while isinstance(schema, dict) and "/components/" in str(schema.get("$ref", "")) and seen < 8:
        component_section, _, name = str(schema["$ref"]).split("/components/", 1)[1].partition("/")
        schema = document["components"][component_section][name]
        seen += 1
    return schema


def _ref_name(schema: dict[str, Any]) -> str | None:
    ref = schema.get("$ref") if isinstance(schema, dict) else None
    return ref.rsplit("/", 1)[-1] if isinstance(ref, str) else None


def _drop_null(schema: dict[str, Any]) -> dict[str, Any]:
    """An optional scalar that admits ``null`` is the same optional scalar."""
    if "anyOf" in schema:
        branches = [branch for branch in schema["anyOf"] if branch.get("type") != "null"]
        if len(branches) == 1:
            return branches[0]
    declared = schema.get("type")
    if isinstance(declared, list):
        rest = [kind for kind in declared if kind != "null"]
        if len(rest) == 1:
            return {**schema, "type": rest[0]}
    return schema


def _scalar(document: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    resolved = _drop_null(_resolve(document, schema))
    return {key: resolved[key] for key in SCALAR_KEYS if key in resolved}


def _base_type(document: dict[str, Any], schema: dict[str, Any]) -> str | None:
    resolved = _drop_null(_resolve(document, schema))
    if "allOf" in resolved and len(resolved["allOf"]) == 1:
        # The spec wraps RunCreate.brief in `allOf: [Brief]` to hang a description off it.
        return _base_type(document, resolved["allOf"][0])
    declared = resolved.get("type")
    if isinstance(declared, str):
        return declared
    if "anyOf" in resolved:
        kinds = {
            branch.get("type")
            for branch in resolved["anyOf"]
            if isinstance(branch, dict) and branch.get("type") != "null"
        }
        if len(kinds) == 1:
            return str(kinds.pop())
    if "items" in resolved:
        return "array"
    if "properties" in resolved or "additionalProperties" in resolved:
        return "object"
    if "$ref" in resolved:
        return "object"  # an external contract document ($ref ../schemas/X.json)
    return None


def _parameters(document: dict[str, Any], operation: dict[str, Any]) -> dict[tuple[str, str], Any]:
    resolved: dict[tuple[str, str], Any] = {}
    for raw in operation.get("parameters", []):
        param = _resolve(document, raw)
        resolved[(param["name"], param["in"])] = param
    return resolved


def _response(spec: dict[str, Any], operation: dict[str, Any], code: str) -> dict[str, Any]:
    response: dict[str, Any] = _resolve(spec, operation["responses"][code])
    return response


def _response_schema_name(response: dict[str, Any]) -> str | None:
    content = response.get("content", {})
    if "text/event-stream" in content and "application/json" not in content:
        return None  # a string of SSE frames; checked at the wire level instead
    return _ref_name(content.get("application/json", {}).get("schema", {}))


def _request_schema_name(operation: dict[str, Any]) -> str | None:
    body = operation.get("requestBody")
    if body is None:
        return None
    return _ref_name(body["content"]["application/json"]["schema"])


# --------------------------------------------------------------------------- operations
def test_every_spec_operation_in_the_surface_is_served(
    spec: dict[str, Any], served: dict[str, Any]
) -> None:
    """Sixteen operations, and every one of them has a route."""
    operations = _operations(spec)
    assert len(operations) == 16, [f"{method.upper()} {path}" for path, method, _ in operations]

    missing = [
        f"{method.upper()} {path}"
        for path, method, _ in operations
        if method not in served["paths"].get(path, {})
    ]
    assert missing == []


def test_no_extra_operation_hides_in_the_surface(
    spec: dict[str, Any], served: dict[str, Any]
) -> None:
    """Serving more than the spec says is drift too."""
    extra: list[str] = []
    for path, item in served["paths"].items():
        if not _is_surface(path):
            continue
        for method in item:
            if method in HTTP_METHODS and method not in spec["paths"].get(path, {}):
                extra.append(f"{method.upper()} {path}")
    assert extra == []


# --------------------------------------------------------------------------- parameters
def test_parameters_match_the_spec(spec: dict[str, Any], served: dict[str, Any]) -> None:
    mismatches: list[str] = []
    for path, method, operation in _operations(spec):
        where = f"{method.upper()} {path}"
        spec_params = _parameters(spec, operation)
        served_params = _parameters(served, served["paths"][path][method])
        if set(spec_params) != set(served_params):
            mismatches.append(f"{where}: params {sorted(spec_params)} != {sorted(served_params)}")
            continue
        for key, spec_param in spec_params.items():
            served_param = served_params[key]
            if bool(spec_param.get("required", False)) != bool(served_param.get("required", False)):
                mismatches.append(f"{where} {key}: required differs")
            if key == ("project_id", "path"):
                # Deviation 2: the spec's slug pattern plus a project ULID.
                served_pattern = served_param["schema"].get("pattern")
                if served_pattern != PROJECT_ID_PATTERN:
                    mismatches.append(
                        f"{where} {key}: pattern {served_pattern!r} is not the agreed one"
                    )
                elif not re.fullmatch(served_pattern, "peer-review-2026"):
                    mismatches.append(f"{where} {key}: refuses a spec-legal slug")
                elif not re.fullmatch(served_pattern, "01M5Z0X96Y46B0GTT36PSYZBQA"):
                    mismatches.append(f"{where} {key}: refuses a project ULID")
                continue
            if key in OPAQUE_ID_PARAMS:
                # Deviation 1: both sides are strings; the router adds no pattern.
                if _scalar(served, served_param["schema"]) != {"type": "string"}:
                    mismatches.append(
                        f"{where} {key}: expected an opaque string, "
                        f"got {_scalar(served, served_param['schema'])}"
                    )
                elif _scalar(spec, spec_param["schema"]).get("type") != "string":
                    mismatches.append(f"{where} {key}: the spec stopped typing this as a string")
                continue
            expected = _scalar(spec, spec_param["schema"])
            actual = _scalar(served, served_param["schema"])
            if expected != actual:
                mismatches.append(f"{where} {key}: {actual} != {expected}")
    assert mismatches == []


# --------------------------------------------------------------------------- status codes
def test_status_codes_match_the_spec(spec: dict[str, Any], served: dict[str, Any]) -> None:
    problems: list[str] = []
    for path, method, operation in _operations(spec):
        where = f"{method.upper()} {path}"
        expected = {code for code in operation["responses"] if code != "default"}
        actual = {code for code in served["paths"][path][method]["responses"] if code != "default"}
        if not expected <= actual:
            problems.append(f"{where}: missing {sorted(expected - actual)}")
        if not (actual - expected) <= ACCEPTED_EXTRA_STATUS_CODES:
            problems.append(
                f"{where}: undocumented {sorted((actual - expected) - ACCEPTED_EXTRA_STATUS_CODES)}"
            )
    assert problems == []


def test_error_responses_declare_the_spec_envelope(
    spec: dict[str, Any], served: dict[str, Any]
) -> None:
    """401/404/409 are ``ErrorEnvelope``; 422 is ``ValidationErrorEnvelope``."""
    wanted = {
        "401": "ErrorEnvelope",
        "404": "ErrorEnvelope",
        "409": "ErrorEnvelope",
        "422": "ValidationErrorEnvelope",
    }
    problems: list[str] = []
    for path, method, operation in _operations(spec):
        where = f"{method.upper()} {path}"
        served_responses = served["paths"][path][method]["responses"]
        for code, component in wanted.items():
            if code not in operation["responses"]:
                continue
            if code not in served_responses:
                problems.append(f"{where}: {code} is not declared")
                continue
            name = _response_schema_name(served_responses[code])
            if name != component:
                problems.append(f"{where} {code}: {name!r} != {component!r}")
            spec_name = _response_schema_name(_response(spec, operation, code))
            if spec_name != component:
                problems.append(f"{where} {code}: the spec itself moved to {spec_name!r}")
    assert problems == []


# --------------------------------------------------------------------------- payloads
def test_success_and_request_bodies_use_the_spec_components(
    spec: dict[str, Any], served: dict[str, Any]
) -> None:
    problems: list[str] = []
    for path, method, operation in _operations(spec):
        where = f"{method.upper()} {path}"
        served_operation = served["paths"][path][method]
        for code in operation["responses"]:
            if code == "default" or not code.startswith("2"):
                continue
            expected = _response_schema_name(_response(spec, operation, code))
            if expected is None:
                continue  # SSE
            actual = _response_schema_name(served_operation["responses"].get(code, {}))
            if actual != expected:
                problems.append(f"{where} {code}: {actual!r} != {expected!r}")
        expected_body = _request_schema_name(operation)
        actual_body = _request_schema_name(served_operation)
        if expected_body != actual_body:
            problems.append(f"{where} body: {actual_body!r} != {expected_body!r}")
        if expected_body is not None:
            spec_required = bool(operation["requestBody"].get("required", False))
            served_required = bool(served_operation["requestBody"].get("required", False))
            if spec_required != served_required:
                problems.append(f"{where} body: required {served_required} != {spec_required}")
    assert problems == []


def test_wire_models_have_the_spec_fields(spec: dict[str, Any], served: dict[str, Any]) -> None:
    """Same fields, same base types, and no extra required field."""
    spec_models = spec["components"]["schemas"]
    served_models = served["components"]["schemas"]
    problems: list[str] = []

    for name in WIRE_MODELS:
        if name not in served_models:
            problems.append(f"{name}: not a component of the served document")
            continue
        expected = spec_models[name]
        actual = served_models[name]
        expected_props = expected.get("properties", {})
        actual_props = actual.get("properties", {})
        if set(expected_props) != set(actual_props):
            problems.append(
                f"{name}: extra {sorted(set(actual_props) - set(expected_props))}, "
                f"missing {sorted(set(expected_props) - set(actual_props))}"
            )
        expected_required = set(expected.get("required", []))
        actual_required = set(actual.get("required", []))
        if not actual_required <= expected_required:
            problems.append(f"{name}: requires {sorted(actual_required - expected_required)}")
        if not expected_required <= set(actual_props):
            problems.append(f"{name}: the spec requires a field it does not declare")
        if (
            expected.get("additionalProperties") is False
            and actual.get("additionalProperties") is not False
        ):
            problems.append(f"{name}: the served schema allows unknown fields")
        for field in sorted(set(expected_props) & set(actual_props)):
            expected_type = _base_type(spec, expected_props[field])
            actual_type = _base_type(served, actual_props[field])
            if expected_type != actual_type:
                problems.append(f"{name}.{field}: {actual_type!r} != {expected_type!r}")
    assert problems == []


# --------------------------------------------------------------------------- wire behaviour
def test_every_operation_rejects_an_anonymous_caller(
    spec: dict[str, Any], client: TestClient
) -> None:
    """The spec declares 401 on all sixteen operations."""
    problems: list[str] = []
    for path, method, operation in _operations(spec):
        if "401" not in operation["responses"]:
            continue
        url = _concrete_url(path)
        response = client.request(method.upper(), url, json=_placeholder_body(path, method))
        if response.status_code != 401:
            problems.append(f"{method.upper()} {url}: {response.status_code}")
    assert problems == []


def test_project_id_path_accepts_a_slug_and_a_project_ulid(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """Deviation 2, asserted end to end: no 422 for either spelling."""
    slug = client.get("/api/projects/peer-review-2026/runs", headers=auth_headers)
    assert slug.status_code != 422, slug.text
    ulid = client.get("/api/projects/01M5Z0X96Y46B0GTT36PSYZBQA/runs", headers=auth_headers)
    assert ulid.status_code != 422, ulid.text
    unclear = client.get("/api/projects/Bad Project/runs", headers=auth_headers)
    assert unclear.status_code == 422, unclear.text


def _concrete_url(path: str) -> str:
    """Fill the id path parameters with values their routes accept."""
    return (
        path.replace("{project_id}", "peer-review-2026")
        .replace("{section_id}", "introduction")
        .replace("{run_id}", "01M5Z0X96Y46B0GTT36PSYZBQA")
    )


def _placeholder_body(path: str, method: str) -> dict[str, Any] | None:
    """A well-formed-enough body, so a 422 cannot mask the missing token."""
    if method != "post":
        return None
    if path.endswith("/decision"):
        return {"option": "finish"}
    if path.endswith("/restore"):
        return {"snapshot_id": "iter-1"}
    return {"quick_edit": {"section_id": "introduction", "instructions": "noop"}}
