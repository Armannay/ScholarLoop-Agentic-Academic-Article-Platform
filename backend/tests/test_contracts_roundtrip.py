"""Contract round-trip and boundary tests.

Two promises are checked here:

1. **Round trip.** ``model -> json -> model`` is lossless for every contract, so a
   payload that crosses the API, the ledger or a provider boundary comes back identical.
2. **Completeness.** Every entry in the contract registry has a sample payload, so
   adding a contract cannot silently escape the round-trip check.
"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, ValidationError

from app import CONTRACT_VERSION
from app.contracts.common import SCHEMA_VERSION, VersionedContract
from app.contracts.registry import (
    CONTRACT_NAMES,
    CONTRACTS,
    JSON_SCHEMA_DIALECT,
    get_contract,
    json_schema_for,
)
from tests.factories import sample_payloads

pytestmark = pytest.mark.contracts

PAYLOADS = sample_payloads()


def test_every_contract_has_a_sample_payload() -> None:
    """A new contract must ship with a fixture, or this fails."""
    assert set(PAYLOADS) == set(CONTRACT_NAMES), (
        "sample_payloads() and CONTRACTS are out of sync: "
        f"missing={sorted(set(CONTRACT_NAMES) - set(PAYLOADS))}, "
        f"extra={sorted(set(PAYLOADS) - set(CONTRACT_NAMES))}"
    )


def test_contract_version_matches_schema_version() -> None:
    """One version number for the whole wire format."""
    assert CONTRACT_VERSION == SCHEMA_VERSION == 1


@pytest.mark.parametrize("name", CONTRACT_NAMES)
def test_round_trip_is_lossless(name: str) -> None:
    """model -> JSON -> model must be identical for every contract."""
    model = get_contract(name)
    instance = model.model_validate(PAYLOADS[name])

    via_json = model.model_validate_json(instance.model_dump_json())
    assert via_json == instance

    via_dict = model.model_validate(instance.model_dump(mode="json"))
    assert via_dict == instance

    assert json.loads(instance.model_dump_json()) is not None


@pytest.mark.parametrize("name", CONTRACT_NAMES)
def test_unknown_fields_are_rejected(name: str) -> None:
    """extra='forbid': a typo must fail at the boundary, not silently change behaviour."""
    model = get_contract(name)
    with pytest.raises(ValidationError) as excinfo:
        model.model_validate({**PAYLOADS[name], "clearly_not_a_field": 1})
    assert any(error["type"] == "extra_forbidden" for error in excinfo.value.errors())


@pytest.mark.parametrize("name", CONTRACT_NAMES)
def test_schema_version_is_pinned(name: str) -> None:
    """Document contracts carry schema_version=1 and reject any other value."""
    model = get_contract(name)
    if not issubclass(model, VersionedContract):
        pytest.skip(f"{name} is not a document-level contract")
    validated = model.model_validate(PAYLOADS[name])
    assert validated.schema_version == 1
    with pytest.raises(ValidationError):
        model.model_validate({**PAYLOADS[name], "schema_version": 2})


@pytest.mark.parametrize("name", CONTRACT_NAMES)
def test_json_schema_is_exportable_and_strict(name: str) -> None:
    """Exported schemas are self-describing and forbid extra properties."""
    schema = json_schema_for(name)
    assert schema["$id"].endswith(f"/v1/{name}.json")
    assert schema["title"] == name
    assert schema["type"] == "object"
    assert schema.get("additionalProperties") is False


@pytest.mark.parametrize("name", CONTRACT_NAMES)
def test_model_dump_is_json_serialisable(name: str) -> None:
    """Nothing in a contract needs a custom encoder - the API can return it directly."""
    instance = get_contract(name).model_validate(PAYLOADS[name])
    assert isinstance(instance, BaseModel)
    json.dumps(instance.model_dump(mode="json"), default=_no_default)


def _no_default(value: object) -> None:
    """Fail loudly if json.dumps had to fall back to default()."""
    raise AssertionError(f"not JSON serialisable: {value!r}")


def test_get_contract_rejects_unknown_name() -> None:
    """Unknown names raise KeyError that lists the valid ones."""
    with pytest.raises(KeyError) as excinfo:
        get_contract("ContractThatDoesNotExist")
    assert "Brief" in str(excinfo.value)


def test_registry_names_are_stable() -> None:
    """The registry exposes all 23 names; renames are a contract change."""
    assert len(CONTRACTS) == 23
    assert CONTRACT_NAMES[:3] == ("Brief", "Task", "Plan")


@pytest.mark.parametrize("name", CONTRACT_NAMES)
def test_exported_schema_declares_its_dialect(name: str) -> None:
    """A consumer must not have to guess the draft from the shape of the document."""
    assert json_schema_for(name)["$schema"] == JSON_SCHEMA_DIALECT


def test_package_exports_every_registered_contract() -> None:
    """``app.contracts`` is the import surface; a registry entry missing from it is a trap."""
    import app.contracts as package

    missing = [name for name in CONTRACT_NAMES if not hasattr(package, name)]
    assert missing == [], f"app.contracts does not export: {missing}"
    assert set(CONTRACT_NAMES) <= set(package.__all__)
    # The rule constants the orchestrator needs are exported alongside the models.
    assert {"REQUIRED_ROLES", "STOP_ACTIONS", "GATE_NAMES"} <= set(package.__all__)
