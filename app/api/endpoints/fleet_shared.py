"""Helpers shared by two or more fleet endpoint modules.

Kept apart from the domain modules so neither has to import the other:
``_validation_summary`` serves the ingest and credential routes, and the
account-tag hints serve the credential routes and ``/fleet/config``.
"""

import logging

from pydantic import ValidationError
from sqlmodel import Session, col, select

from app.models.db import ProviderConfig
from app.services.account_identity import (
    FINGERPRINTED_ORIGIN_PROVIDERS,
    credential_fingerprint,
    jwt_stable_subject,
    keyed_credential_origin,
)
from app.services.credential_tags import CredentialTagRepo, ParkedMapping, live_sidecar_ids

logger = logging.getLogger(__name__)


def _validation_summary(exc: Exception) -> str:
    """Field paths and error types only, never the offending values.

    A Pydantic error's default text echoes the rejected input, and on the ingest
    routes that input can be a credential. The text goes into both the 400
    response and the log, and the sidecar logs the response body it gets back.
    """
    if isinstance(exc, ValidationError):
        problems = [
            f"{'.'.join(str(part) for part in err['loc']) or '<body>'}: {err['type']}"
            for err in exc.errors(include_input=False, include_url=False, include_context=False)
        ]
        return "; ".join(problems[:10]) or "validation failed"
    return type(exc).__name__


def _fingerprinted_credential_hints(
    session: Session, providers: list[str]
) -> dict[str, dict[str, str]]:
    """``{provider_id: {provider:<pid>#<fingerprint>: account_id}}`` for
    providers whose credential is a bare key (#347; widened from opencode
    to its OpenCode-file siblings in #349).

    The sidecar suffixes such a credential's origin with a fingerprint of
    the value it found on disk so two hosts (or one host after a key
    rotation) can never share an origin. When the same key has been pasted
    into ``provider_configs``, the server can recognise it here and answer
    with the account that paste belongs to — without ever seeing the
    sidecar's filesystem layout, and without the sidecar ever seeing a key
    it didn't already hold. Only the 12-hex fingerprint crosses the wire.

    The hint key is built from the *provider* descriptor rather than the
    file descriptor precisely because the server does not know the
    sidecar's path: it can only ever construct ``provider:<pid>#<fp>``.

    Unlike the auto-hint this ships unconditionally — a fingerprint is not
    a guess. A sidecar that lacks the key cannot produce a matching
    fingerprint, so there is nothing to scope: the match is the scoping.
    """
    keyed = FINGERPRINTED_ORIGIN_PROVIDERS.intersection(providers)
    if not keyed:
        return {}

    rows = list(
        session.exec(
            select(ProviderConfig)
            .where(
                col(ProviderConfig.provider_id).in_(sorted(keyed)),
                ProviderConfig.enabled == True,  # noqa: E712 — SQLModel needs the ==
            )
            .order_by(col(ProviderConfig.provider_id), col(ProviderConfig.account_id))
        ).all()
    )
    candidates: dict[str, dict[str, set[str]]] = {}
    for row in rows:
        try:
            api_key = row.api_key
        except Exception:  # pragma: no cover — undecryptable stored key
            logger.debug("fingerprint hint: cannot decrypt %s key", row.provider_id, exc_info=True)
            continue
        # xAI's pasted bearer is a JWT that rotates; the sidecar keys xAI
        # origins on its stable claim (#523), so the hint must too.
        subject = jwt_stable_subject(api_key) if row.provider_id == "xai" else None
        fingerprint = credential_fingerprint(subject or api_key)
        if not fingerprint:
            continue
        hint_key = keyed_credential_origin(
            CredentialTagRepo.provider_origin(row.provider_id), fingerprint
        )
        candidates.setdefault(row.provider_id, {}).setdefault(hint_key, set()).add(row.account_id)
    # The same key stored under multiple accounts is ambiguous; keep it for
    # manual assignment rather than making ordered rows an accidental policy.
    out = {
        provider_id: {origin: next(iter(ids)) for origin, ids in origins.items() if len(ids) == 1}
        for provider_id, origins in candidates.items()
    }
    if not out:
        return {}
    return out


def _account_tag_hints_for_providers(
    session: Session,
    providers: list[str],
    *,
    sidecar_id: str | None = None,
    include_parked: bool = True,
) -> dict[str, dict[str, str]]:
    """Return ``{provider_id: {credential_origin: account_id, ...}}`` for the
    requested providers, scoped to the requesting sidecar (#319).

    Powers ``/fleet/config``, ``/fleet/ingest``, and the manifest
    response, so the sidecar can stamp cards it couldn't resolve
    locally.

    Thin wrapper over :meth:`CredentialTagRepo.list_pending_payload` +
    :meth:`CredentialTagRepo.auto_hints_for_single_account_providers`
    — the lookup keys live in the repo so future callers all read
    through the same SQL shape (PR #290 round-2 review Hermes body
    suggestion #6).

    Merge order, most authoritative first:

    1. operator ``credential_tags`` rows — explicit, always win;
    2. :func:`_fingerprinted_credential_hints` — a definitive match
       between the sidecar's discovered key and a key the operator
       pasted into ``provider_configs`` (#347 T1);
    3. the single-account auto-hint — implicit, kept as the last
       resort for a fresh provider like MiniMax whose quota gauge
       lives at the operator's chosen account but whose upstream has
       no per-user identity, so the sidecar's local discovery returns
       nothing useful. The hint unblocks the events on the very next
       cycle so the quota gauge and the sidecar events merge into one
       Fleet entry.

    Requester identity: with ``sidecar_id`` given, both that sidecar's
    scoped tags and deployment-wide (NULL) tags ship (scoped winning).
    Without one (old sidecar binaries that don't send
    ``?sidecar_id=`` on ``/fleet/config``), the caller is treated as
    the deployment's only live sidecar when exactly one exists — so a
    single-host operator's newly created machine-scoped tags still
    reach an un-upgraded binary — and falls back to deployment-wide
    tags only when zero or 2+ sidecars are live.

    ``include_parked=False`` drops the mappings whose target account is
    archived (see :class:`ParkedMapping`). A parked mapping
    resolves the credential but must never reach a sidecar: shipping it
    would make the sidecar stamp the archived account with new usage
    and resurrect a pair the operator retired. Callers that decide
    whether an origin is *resolved* — ``visible_pending_rows`` — keep
    the default, because a parked mapping does resolve the origin and
    the operator must not be nagged about it again.

    The map is **complete per provider**: every origin the server
    resolves for a provider it is asked about is in it. A caller that
    hands it to a sidecar as "the answer for these providers" may
    therefore replace what it holds for them — the manifest response
    does exactly that, and adds an explicit empty bucket for a provider
    with no hints at all.
    """
    effective_sidecar_id = sidecar_id
    if effective_sidecar_id is None:
        live = live_sidecar_ids(session)
        if len(live) == 1:
            effective_sidecar_id = live[0]
    tags = CredentialTagRepo.list_effective_tags(
        session, providers=providers, sidecar_id=effective_sidecar_id
    )
    resolved: dict[str, dict[str, str]] = {}
    for (pid, origin), tag in tags.items():
        resolved.setdefault(pid, {})[origin] = tag.account_id
    # A fingerprint match outranks the auto-hint but never an operator tag:
    # the tag is a deliberate per-origin choice, the fingerprint a derived
    # one. Unlike tags, the fingerprint hint is not scoped to a sidecar —
    # it doesn't need to be: only a sidecar that already holds the key can
    # build a matching fingerprint, so the match is the scoping.
    for pid, by_origin in _fingerprinted_credential_hints(session, providers).items():
        bucket = resolved.setdefault(pid, {})
        for origin, account_id in by_origin.items():
            bucket.setdefault(origin, account_id)
    if not include_parked:
        # A parked mapping resolves the credential but must never reach a
        # sidecar: shipping it would make the sidecar stamp the archived
        # account with new usage and resurrect a pair the operator retired.
        # The check is tag-aware — a redirect tag names an account of its
        # *target* provider, so its parked-ness is looked up there.
        parked = ParkedMapping(session, providers)
        # A redirect tag names an account of its *target* provider — seed those
        # too, so the whole check costs two queries instead of one per
        # provider.
        parked.seed(
            {tag.target_provider_id for tag in tags.values() if tag.target_provider_id is not None}
        )
        for (pid, origin), tag in tags.items():
            if parked.is_parked(tag):
                tagged_origins = resolved.get(pid)
                if tagged_origins is not None:
                    tagged_origins.pop(origin, None)
                    if not tagged_origins:
                        resolved.pop(pid, None)
    return resolved


__all__ = [
    "_account_tag_hints_for_providers",
    "_fingerprinted_credential_hints",
    "_validation_summary",
]
