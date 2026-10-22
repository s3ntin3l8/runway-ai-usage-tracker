"""Policy evidence must tolerate ordering without dropping duplicate ACL entries."""

import copy

from tests.keychain_policy_fixture import canonicalize, compare


def entry(tags=(24, 28), applications=None, description="synthetic-private-description"):
    return {
        "tags": tags,
        "selector": (0, 0),
        "applications": applications,
        "description": description,
    }


def test_ordering_is_not_a_policy_change_and_repr_contains_no_metadata():
    before = canonicalize([entry(applications=[b"private-app-two", b"private-app-one"])])
    after = canonicalize(
        [entry(tags=(28, 24), applications=[b"private-app-one", b"private-app-two"])]
    )
    assert compare(before, after) == {
        "access_policy_preserved": True,
        "integrity_changed": False,
        "changed_policy_authorizations": [],
    }
    assert "private" not in repr(before)


def test_integrity_difference_is_reported_separately():
    before = canonicalize([entry(), entry(tags=(65539,), description="synthetic-one")])
    after = canonicalize([entry(), entry(tags=(65539,), description="synthetic-two")])
    result = compare(before, after)
    assert result["access_policy_preserved"]
    assert result["integrity_changed"]


def test_duplicates_are_preserved_and_changes_name_only_authorization_tags():
    entries = [entry(applications=[b"synthetic-one"]), entry(applications=[b"synthetic-two"])]
    before = canonicalize(entries)
    changed = copy.deepcopy(entries)
    changed[0]["applications"] = [b"synthetic-three"]
    result = compare(before, canonicalize(changed))
    assert not result["access_policy_preserved"]
    assert result["changed_policy_authorizations"] == [[24, 28]]
    assert "synthetic" not in repr(result)


def test_null_and_empty_trusted_application_lists_have_distinct_policy():
    assert not compare(
        canonicalize([entry(applications=None)]), canonicalize([entry(applications=[])])
    )["access_policy_preserved"]


def test_mixed_integrity_and_access_entry_is_not_excluded():
    before = canonicalize([entry(tags=(24, 65539), description="one")])
    after = canonicalize([entry(tags=(24, 65539), description="two")])
    assert not compare(before, after)["access_policy_preserved"]
