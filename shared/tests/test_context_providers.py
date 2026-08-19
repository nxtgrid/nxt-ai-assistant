"""Context provider seam: resolution context, protocol, registry."""

import pytest

from shared.prompts.providers import ResolutionContext
from shared.prompts.types import RequestScope


def test_resolution_context_defaults_are_empty_and_unprivileged():
    ctx = ResolutionContext(scope=RequestScope())
    assert ctx.organization_ids == ()
    assert ctx.role_ids == ()
    assert ctx.is_staff is False
    assert ctx.user_email is None


def test_resolution_context_is_hashable():
    """Providers cache on it; a mutable context would be a correctness bug."""
    ctx = ResolutionContext(scope=RequestScope(organization_id="7"), organization_ids=("7",))
    assert hash(ctx) == hash(
        ResolutionContext(scope=RequestScope(organization_id="7"), organization_ids=("7",))
    )


def test_resolution_context_rejects_a_list_of_orgs():
    """Lists are unhashable and would silently defeat provider caching."""
    with pytest.raises(TypeError):
        hash(ResolutionContext(scope=RequestScope(), organization_ids=["7"]))


def test_from_user_context_maps_permission_fields():
    class _UC:
        user_email = "tech@example.com"
        organization_ids = ["7", "9"]
        roles = ["ops"]
        is_staff = True

    ctx = ResolutionContext.from_user_context(_UC(), grid="Alpha")
    assert ctx.user_email == "tech@example.com"
    assert ctx.organization_ids == ("7", "9")
    assert ctx.role_ids == ("ops",)
    assert ctx.is_staff is True
    assert ctx.scope.grid == "Alpha"
    assert ctx.scope.organization_id == "7"


def test_from_user_context_handles_none():
    ctx = ResolutionContext.from_user_context(None)
    assert ctx.organization_ids == ()
    assert ctx.is_staff is False
    assert ctx.scope.organization_id is None
