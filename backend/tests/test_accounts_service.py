"""AccountStore services: CRUD, ownership, key rotation, delete protection, bindings.

The HTTP layer is a thin shell over these rules, so they are pinned here first:
write-only keys, per-user ownership (cross-user reads are 404), delete protection
for the sole account of an enabled role, and the planner's provider rule.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.accounts.models import AccountRow, AuditLogRow
from app.accounts.schemas import AccountCreate, AccountPatch, BindingPut, BudgetPatch
from app.accounts.service import AccountService, BindingService, UserService, validate_api_key
from app.contracts.enums import AccountProvider, AccountStatus, BindingRole, BindingStrategy
from app.core.errors import ConflictError, ContractViolation, NotFoundError
from tests.test_accounts_crypto import api_key

pytestmark = pytest.mark.accounts


def make_account(
    service: AccountService,
    owner_id: str,
    *,
    provider: AccountProvider = AccountProvider.ANTHROPIC,
    label: str = "work",
    key: str | None = None,
    base_url: str | None = None,
):
    return service.create(
        owner_id,
        AccountCreate(
            provider=provider,
            label=label,
            api_key=key or api_key(),
            base_url=base_url,
            budget=BudgetPatch(tokens=100_000, usd=25.0),
        ),
    )


# --------------------------------------------------------------------------- keys
def test_validate_api_key_accepts_console_keys_rejects_subscription_tokens() -> None:
    assert validate_api_key(AccountProvider.ANTHROPIC, api_key()) == api_key()
    with pytest.raises(ContractViolation, match="Pro/Max"):
        validate_api_key(AccountProvider.ANTHROPIC, "sk-ant-oat01-" + "A1b2" * 8)
    with pytest.raises(ContractViolation, match="Console API key"):
        validate_api_key(AccountProvider.ANTHROPIC, "totally-other-shape-key")
    # OpenAI-compatible services have no shared key shape (OpenRouter, local, ...).
    assert validate_api_key(AccountProvider.OPENAI_COMPATIBLE, "sk-or-v1-whatever-12345678")


def test_create_stores_ciphertext_and_never_the_key(
    account_service: AccountService, user_row, encryptor
) -> None:
    key = api_key("Ff1e2d3c4b5a6978")
    view = make_account(account_service, user_row.id, key=key)
    assert view.key_mask == "****6978"
    assert view.key_fingerprint != key
    row = account_service.get_row(user_row.id, view.id)
    assert key not in row.key_ciphertext
    assert encryptor.decrypt(row.key_ciphertext, aad=row.id) == key
    assert account_service.decrypt_key(row) == key


# --------------------------------------------------------------------------- ownership
def test_cross_user_access_is_404(
    account_service: AccountService, user_service: UserService, session, user_row
) -> None:
    view = make_account(account_service, user_row.id)
    other = user_service.register("other@example.org", "another-good-passphrase")
    session.commit()
    with pytest.raises(NotFoundError):
        account_service.get(other.id, view.id)
    with pytest.raises(NotFoundError):
        account_service.patch(other.id, view.id, AccountPatch(label="stolen"))
    with pytest.raises(NotFoundError):
        account_service.delete(other.id, view.id, disable_roles=True)


# --------------------------------------------------------------------------- patch
def test_patch_rotates_key_and_updates_mask_and_fingerprint(
    account_service: AccountService, user_row
) -> None:
    view = make_account(account_service, user_row.id)
    new_key = api_key("New9key8mat7erial")
    rotated = account_service.patch(
        user_row.id,
        view.id,
        AccountPatch(api_key=new_key, label="rotated", status=AccountStatus.ACTIVE),
    )
    assert rotated.key_mask == "****rial"
    assert rotated.key_fingerprint != view.key_fingerprint
    row = account_service.get_row(user_row.id, view.id)
    assert account_service.decrypt_key(row) == new_key


def test_patch_budget_can_be_set_and_cleared(account_service: AccountService, user_row) -> None:
    view = make_account(account_service, user_row.id)
    assert view.budget.tokens == 100_000
    updated = account_service.patch(
        user_row.id, view.id, AccountPatch(budget=BudgetPatch(tokens=50_000, usd=None))
    )
    assert updated.budget.tokens == 50_000
    assert updated.budget.usd is None
    cleared = account_service.patch(user_row.id, view.id, AccountPatch(budget=None))
    assert cleared.budget.tokens is None and cleared.budget.usd is None


def test_patch_base_url_rules_follow_provider(account_service: AccountService, user_row) -> None:
    view = make_account(account_service, user_row.id)  # anthropic
    with pytest.raises(ContractViolation, match="does not use base_url"):
        account_service.patch(user_row.id, view.id, AccountPatch(base_url="https://x.example/v1"))
    openai_view = make_account(
        account_service,
        user_row.id,
        provider=AccountProvider.OPENAI_COMPATIBLE,
        label="router",
        base_url="https://openrouter.example/api/v1",
    )
    with pytest.raises(ContractViolation, match="requires base_url"):
        account_service.patch(user_row.id, openai_view.id, AccountPatch(base_url=None))


# --------------------------------------------------------------------------- delete protection
def _bind(
    bindings: BindingService,
    owner_id: str,
    role: BindingRole,
    account_ids: list[str],
    *,
    strategy: BindingStrategy = BindingStrategy.FAILOVER,
    enabled: bool = True,
) -> None:
    bindings.put(
        owner_id,
        role,
        BindingPut(
            account_ids=account_ids,
            strategy=strategy,
            model="claude-sonnet-4-5",
            max_input_tokens=24_000,
            max_output_tokens=4_000,
            enabled=enabled,
        ),
    )


def test_delete_sole_account_of_enabled_role_is_refused(
    account_service: AccountService, binding_service: BindingService, user_row
) -> None:
    view = make_account(account_service, user_row.id)
    _bind(
        binding_service,
        user_row.id,
        BindingRole.WRITER,
        [view.id],
        strategy=BindingStrategy.DEDICATED,
    )
    with pytest.raises(ConflictError) as excinfo:
        account_service.delete(user_row.id, view.id)
    assert excinfo.value.context["roles"] == ["writer"]
    # The account is still there, untouched.
    assert account_service.get(user_row.id, view.id).id == view.id


def test_delete_with_disable_roles_disables_the_binding(
    account_service: AccountService, binding_service: BindingService, user_row, session
) -> None:
    view = make_account(account_service, user_row.id)
    _bind(
        binding_service,
        user_row.id,
        BindingRole.WRITER,
        [view.id],
        strategy=BindingStrategy.DEDICATED,
    )
    result = account_service.delete(user_row.id, view.id, disable_roles=True)
    assert result.disabled_roles == ["writer"]
    binding = binding_service.get(user_row.id, BindingRole.WRITER)
    assert binding is not None and binding.enabled is False
    session.commit()
    assert session.get(AccountRow, view.id) is None


def test_delete_with_replacement_swaps_the_binding(
    account_service: AccountService, binding_service: BindingService, user_row
) -> None:
    old = make_account(account_service, user_row.id, label="old")
    new = make_account(account_service, user_row.id, label="new", key=api_key("Replacemen7keyx"))
    _bind(
        binding_service,
        user_row.id,
        BindingRole.WRITER,
        [old.id],
        strategy=BindingStrategy.DEDICATED,
    )
    result = account_service.delete(user_row.id, old.id, replacement_account_id=new.id)
    assert result.replaced_in == ["writer"]
    binding = binding_service.get(user_row.id, BindingRole.WRITER)
    assert binding is not None
    assert binding.account_ids == [new.id]
    assert binding.enabled is True


def test_delete_from_multi_account_binding_just_removes_it(
    account_service: AccountService, binding_service: BindingService, user_row
) -> None:
    first = make_account(account_service, user_row.id, label="a")
    second = make_account(account_service, user_row.id, label="b", key=api_key("Second9key8here"))
    _bind(binding_service, user_row.id, BindingRole.REVIEWER, [first.id, second.id])
    result = account_service.delete(user_row.id, first.id)
    assert result.removed_from == ["reviewer"]
    binding = binding_service.get(user_row.id, BindingRole.REVIEWER)
    assert binding is not None
    assert binding.account_ids == [second.id]
    # Two-account failover would be invalid with one account: strategy relaxed.
    assert binding.strategy == BindingStrategy.DEDICATED


def test_delete_does_not_touch_disabled_or_other_role_bindings(
    account_service: AccountService, binding_service: BindingService, user_row
) -> None:
    view = make_account(account_service, user_row.id)
    _bind(
        binding_service,
        user_row.id,
        BindingRole.COMPACTOR,
        [view.id],
        strategy=BindingStrategy.DEDICATED,
        enabled=False,
    )
    result = account_service.delete(user_row.id, view.id)
    assert result.disabled_roles == []
    binding = binding_service.get(user_row.id, BindingRole.COMPACTOR)
    assert binding is not None and binding.enabled is False


# --------------------------------------------------------------------------- bindings
def test_binding_requires_owned_accounts(
    account_service: AccountService,
    binding_service: BindingService,
    user_service: UserService,
    session,
    user_row,
) -> None:
    view = make_account(account_service, user_row.id)
    other = user_service.register("thief@example.org", "another-good-passphrase")
    session.commit()
    with pytest.raises(NotFoundError):
        _bind(binding_service, other.id, BindingRole.WRITER, [view.id])


def test_planner_binding_rejects_non_anthropic_accounts(
    account_service: AccountService, binding_service: BindingService, user_row
) -> None:
    router = make_account(
        account_service,
        user_row.id,
        provider=AccountProvider.OPENAI_COMPATIBLE,
        label="router",
        base_url="https://openrouter.example/api/v1",
    )
    with pytest.raises(ContractViolation, match="planner role must bind anthropic"):
        _bind(
            binding_service,
            user_row.id,
            BindingRole.PLANNER,
            [router.id],
            strategy=BindingStrategy.DEDICATED,
        )
    # Anthropic (Console key) is accepted - this is the Planner's credential.
    claude = make_account(account_service, user_row.id, label="claude-console")
    view = binding_service.put(
        user_row.id,
        BindingRole.PLANNER,
        BindingPut(
            account_ids=[claude.id],
            strategy=BindingStrategy.DEDICATED,
            model="claude-sonnet-4-5",
            max_input_tokens=120_000,
            max_output_tokens=8_000,
        ),
    )
    assert view.role == BindingRole.PLANNER


def test_binding_contract_validation_is_enforced(
    account_service: AccountService, binding_service: BindingService, user_row
) -> None:
    view = make_account(account_service, user_row.id)
    # failover needs at least two accounts - the frozen contract says so.
    with pytest.raises(Exception, match="at least two accounts"):
        _bind(binding_service, user_row.id, BindingRole.WRITER, [view.id])


def test_audit_trail_written_without_secrets(
    account_service: AccountService, user_row, session
) -> None:
    key = api_key("Aud1t2me3now4pls")
    make_account(account_service, user_row.id, key=key, label="audited")
    session.commit()
    rows = session.scalars(select(AuditLogRow)).all()
    actions = [row.action for row in rows]
    assert "account.create" in actions
    for row in rows:
        assert key not in (row.detail or "")
        assert key not in row.actor and key not in row.target
