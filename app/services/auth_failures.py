"""In-memory registry of accounts whose credential a provider recently rejected.

Opaque credentials (API keys, session cookies) carry no expiry, so Token Health
would otherwise report them ``valid`` forever. A collector that gets a 401/403
(``auth_failed``) marks the account here; Token Health then surfaces it as
``invalid`` until a collection succeeds or the credential is replaced/removed.

The flag is per ``(provider, account)``, not per credential family — a
successful strategy using a different credential clears it.
"""

import threading

from app.services.account_identity import canonical_account_id

_lock = threading.Lock()
_flagged: dict[str, set[str]] = {}


def mark(provider: str, account_id: str | None) -> None:
    """Record that *provider* rejected the credential used for *account_id*."""
    with _lock:
        _flagged.setdefault(provider, set()).add(canonical_account_id(account_id or "default"))


def clear(provider: str, account_id: str | None = None) -> None:
    """Drop the flag for one account, or for every account of *provider*."""
    with _lock:
        if account_id is None:
            _flagged.pop(provider, None)
            return
        accounts = _flagged.get(provider)
        if not accounts:
            return
        accounts.discard(canonical_account_id(account_id))
        if not accounts:
            _flagged.pop(provider, None)


def flagged_accounts(provider: str) -> set[str]:
    """Canonical account ids currently flagged for *provider* (a copy)."""
    with _lock:
        return set(_flagged.get(provider, ()))


def reset() -> None:
    """Clear everything (tests)."""
    with _lock:
        _flagged.clear()
