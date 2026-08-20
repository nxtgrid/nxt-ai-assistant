"""Drive ACL evaluation: which permission entries grant which callers."""

import pytest

from shared.utils.drive_permissions import ROLE_RANK, _permission_grants


def _grants(perm, email="tech@example.com", required="reader", need_write=False):
    return _permission_grants(perm, email, ROLE_RANK[required], need_write)


def test_exact_email_match_grants_read():
    assert _grants({"type": "user", "emailAddress": "Tech@Example.com", "role": "reader"})


def test_email_match_is_case_insensitive():
    assert _grants({"type": "user", "emailAddress": "TECH@EXAMPLE.COM", "role": "reader"})


def test_a_different_email_does_not_grant():
    assert not _grants({"type": "user", "emailAddress": "other@example.com", "role": "reader"})


def test_reader_role_does_not_satisfy_a_write_requirement():
    perm = {"type": "user", "emailAddress": "tech@example.com", "role": "reader"}
    assert not _grants(perm, required="writer", need_write=True)


def test_anyone_grants_read_but_never_write():
    perm = {"type": "anyone", "role": "writer"}
    assert _grants(perm)
    assert not _grants(perm, required="writer", need_write=True)


def test_domain_permission_grants_a_matching_domain():
    """'Shared with everyone at the company' is the common case for an ops doc."""
    assert _grants({"type": "domain", "domain": "Example.com", "role": "reader"})


def test_domain_permission_does_not_grant_another_domain():
    perm = {"type": "domain", "domain": "example.com", "role": "reader"}
    assert not _grants(perm, email="outsider@elsewhere.com")


def test_domain_permission_does_not_grant_a_lookalike_suffix():
    """notexample.com must not match example.com."""
    perm = {"type": "domain", "domain": "example.com", "role": "reader"}
    assert not _grants(perm, email="tech@notexample.com")


def test_group_permission_never_grants():
    """Expanding group membership needs the Admin SDK, which is not wired up."""
    perm = {"type": "group", "emailAddress": "ops@example.com", "role": "writer"}
    assert not _grants(perm)


class _FakeExecutable:
    def __init__(self, result=None, error=None):
        self._result, self._error = result, error

    def execute(self):
        if self._error:
            raise self._error
        return self._result


class _FakeService:
    """Minimal stand-in for the Drive v3 client used by user_can_access."""

    def __init__(self, list_result=None, list_error=None, get_result=None):
        self._list_result, self._list_error = list_result, list_error
        self._get_result = get_result

    def permissions(self):
        return self

    def files(self):
        return self

    def list(self, **_kwargs):
        return _FakeExecutable(self._list_result, self._list_error)

    def get(self, **_kwargs):
        return _FakeExecutable(self._get_result)


@pytest.fixture
def patched_drive(monkeypatch):
    """Swap out credentials + client construction; hand back a setter."""
    import shared.utils.drive_permissions as dp

    monkeypatch.setattr(dp, "get_drive_credentials", lambda: object())
    holder = {}
    monkeypatch.setattr(dp, "build", lambda *a, **kw: holder["service"])
    return holder


@pytest.mark.asyncio
async def test_no_email_fails_closed(patched_drive):
    from shared.utils.drive_permissions import user_can_access

    patched_drive["service"] = _FakeService()
    assert await user_can_access("file-1", None) is False


@pytest.mark.asyncio
async def test_strict_withholds_the_service_account_reachability_grant(patched_drive):
    """The whole point: a link-shared doc must not be readable by everyone."""
    from shared.utils.drive_permissions import user_can_access

    patched_drive["service"] = _FakeService(
        list_error=RuntimeError("403"),
        get_result={"id": "file-1", "permissions": []},
    )
    assert await user_can_access("file-1", "tech@example.com", strict=True) is False


@pytest.mark.asyncio
async def test_non_strict_keeps_the_reachability_grant(patched_drive):
    """Existing callers (e.g. /learn's fetch_document) must not change behaviour."""
    from shared.utils.drive_permissions import user_can_access

    patched_drive["service"] = _FakeService(
        list_error=RuntimeError("403"),
        get_result={"id": "file-1", "permissions": []},
    )
    assert await user_can_access("file-1", "tech@example.com") is True


@pytest.mark.asyncio
async def test_strict_still_grants_on_an_explicit_share(patched_drive):
    from shared.utils.drive_permissions import user_can_access

    patched_drive["service"] = _FakeService(
        list_result={
            "permissions": [
                {"type": "user", "emailAddress": "tech@example.com", "role": "reader"}
            ]
        }
    )
    assert await user_can_access("file-1", "tech@example.com", strict=True) is True


@pytest.mark.asyncio
async def test_strict_still_grants_on_a_domain_share(patched_drive):
    from shared.utils.drive_permissions import user_can_access

    patched_drive["service"] = _FakeService(
        list_result={"permissions": [{"type": "domain", "domain": "example.com", "role": "reader"}]}
    )
    assert await user_can_access("file-1", "tech@example.com", strict=True) is True


@pytest.mark.asyncio
async def test_an_api_explosion_fails_closed(patched_drive):
    from shared.utils.drive_permissions import user_can_access

    def _boom(*_a, **_kw):
        raise RuntimeError("network down")

    import shared.utils.drive_permissions as dp

    patched_drive["service"] = _FakeService()
    dp.get_drive_credentials = _boom
    assert await user_can_access("file-1", "tech@example.com", strict=True) is False
