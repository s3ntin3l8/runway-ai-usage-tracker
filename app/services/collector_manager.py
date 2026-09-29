"""
Manages collection of AI provider quotas with smart differential fetching.

This module orchestrates all collectors and wraps them with SmartCollector
for intelligent caching to reduce API calls while maintaining fresh data.
Now supports multi-account dynamic spawning based on discovered tokens.
"""

import asyncio
import logging
import time
from typing import Any

import httpx

from app.core.utils import scrub_log
from app.services.collectors.anthropic import AnthropicCollector
from app.services.collectors.antigravity import AntigravityCollector
from app.services.collectors.chatgpt import ChatGPTCollector
from app.services.collectors.deepseek import DeepSeekCollector
from app.services.collectors.gemini import GeminiCollector
from app.services.collectors.github import GitHubCollector
from app.services.collectors.kimi_api import KimiApiCollector
from app.services.collectors.kimi_coding import KimiCodingCollector
from app.services.collectors.kimi_k2 import KimiK2Collector
from app.services.collectors.minimax import MiniMaxCollector
from app.services.collectors.ollama import OllamaCollector
from app.services.collectors.opencode import OpenCodeCollector
from app.services.collectors.openrouter import OpenRouterCollector
from app.services.collectors.xai import XaiCollector
from app.services.collectors.zai import ZaiCollector
from app.services.smart_collector import SmartCollector
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)


class CollectorManager:
    """
    Manages collection of all AI provider quotas with support for multiple accounts.

    Dynamically spawns SmartCollector instances for:
    1. Default/Static accounts (configured via ENV)
    2. Dynamic accounts (ingested from sidecars via token_cache)
    """

    def __init__(self):
        """Initialize collector registry."""
        self._sync_lock = asyncio.Lock()
        # Registry of available collector classes and their default settings
        self.collector_registry = {
            "anthropic": (AnthropicCollector, "Claude (Anthropic)", 900),
            "antigravity": (AntigravityCollector, "Antigravity", 900),
            "gemini": (GeminiCollector, "Gemini", 900),
            "github": (GitHubCollector, "GitHub Copilot", 900),
            "chatgpt": (ChatGPTCollector, "ChatGPT", 900),
            "opencode": (OpenCodeCollector, "OpenCode", 900),
            "zai": (ZaiCollector, "zAI", 900),
            "kimi_api": (KimiApiCollector, "Kimi API", 900),
            "kimi_coding": (KimiCodingCollector, "Kimi Coding", 900),
            "kimi_k2": (KimiK2Collector, "Kimi K2", 900),
            "openrouter": (OpenRouterCollector, "OpenRouter", 900),
            "deepseek": (DeepSeekCollector, "DeepSeek", 900),
            "minimax": (MiniMaxCollector, "MiniMax", 900),
            "ollama": (OllamaCollector, "Ollama Cloud", 900),
            "xai": (XaiCollector, "xAI (Grok)", 900),
        }

        # Active collectors keyed by "provider_id:account_id"
        self.smart_collectors: dict[str, SmartCollector] = {}
        self._client = None
        self._last_sync_time: float = 0.0
        self._credential_source_preferences: dict[tuple[str, str], dict[str, tuple[bool, int]]] = {}
        self._collect_lock = asyncio.Lock()
        self._collect_future: asyncio.Future | None = None
        self.last_collection_outcomes: list[dict[str, Any]] = []
        # Concurrency limit: max 10 collectors running at once
        self._semaphore = asyncio.Semaphore(10)

        logger.info(
            f"CollectorManager initialized with {len(self.collector_registry)} registered providers"
        )

    async def _sync_collectors(  # noqa: PLR0915 — known-debt: smart-collector lifecycle, refactor tracked separately
        self, *, force: bool = False
    ):
        """Synchronize active SmartCollectors with discovered accounts.

        Throttled to run at most once every 60 seconds to avoid redundant
        TokenCache lookups on every /api/limits request. Config mutations
        pass ``force=True`` so an enable/disable takes effect immediately
        instead of waiting out the throttle.
        """
        if not force and time.time() - self._last_sync_time < 60.0:
            return
        async with self._sync_lock:
            # Re-check inside lock to avoid double-sync when multiple requests
            # are waiting on the lock simultaneously.
            if not force and time.time() - self._last_sync_time < 60.0:
                return
            # Load DB provider configs: provider_id -> account_id -> ProviderConfig
            # We use a nested map to handle multi-account overrides correctly.
            db_configs: dict[str, dict[str, Any]] = {}
            global_poll_interval: int | None = None
            source_preferences: dict[tuple[str, str], dict[str, tuple[bool, int]]] = {}
            try:
                from sqlmodel import Session
                from sqlmodel import select as sqlselect

                from app.core.db import engine
                from app.models.db import CredentialSource, ProviderConfig, SystemConfig

                with Session(engine) as _s:
                    for r in _s.exec(sqlselect(ProviderConfig)).all():
                        if r.provider_id not in db_configs:
                            db_configs[r.provider_id] = {}
                        # Keep a detached value snapshot. The session commits
                        # and closes below; retaining ORM instances here makes
                        # later attribute reads fail when expire_on_commit is
                        # enabled (and can trigger implicit DB access).
                        from types import SimpleNamespace

                        db_configs[r.provider_id][r.account_id] = SimpleNamespace(
                            enabled=r.enabled,
                            poll_interval_seconds=r.poll_interval_seconds,
                            account_label=r.account_label,
                            strategies=r.strategies,
                        )

                        # Sync manual tokens to cache to survive reloads/restarts
                        if r.enabled and (r.api_key or r.session_cookie):
                            await self._sync_manual_config_to_cache(r, _s)

                    for row in _s.exec(sqlselect(CredentialSource)).all():
                        account_sources = source_preferences.setdefault(
                            (row.provider_id, row.account_id), {}
                        )
                        account_sources[row.source_id] = (row.enabled, row.priority)

                    sys_cfg = _s.exec(sqlselect(SystemConfig)).first()
                    if sys_cfg and sys_cfg.default_poll_interval_seconds:
                        global_poll_interval = sys_cfg.default_poll_interval_seconds

                    _s.commit()

            except Exception as e:
                logger.debug(f"Could not load provider configs from DB: {e}")
            self._credential_source_preferences = source_preferences
            await self.reconcile_token_cache_from_durable_tags()

            # 1. Ensure Default/Static collectors are present
            for p_id, (cls, name, ttl) in self.collector_registry.items():
                # For default collector, we look for account_id="default"
                provider_acc_configs = db_configs.get(p_id, {})
                db_cfg = provider_acc_configs.get("default")

                if db_cfg is not None and not db_cfg.enabled:
                    # Remove existing collector if it was previously running
                    self.smart_collectors.pop(f"{p_id}:default", None)
                    continue
                if db_cfg is None and provider_acc_configs:
                    # Config rows exist but none is the "default" sentinel —
                    # collection is account-keyed (wizard-created emails).
                    # Spawning a blanket default here would shadow step 2's
                    # per-account `enabled` checks and keep collecting after
                    # the user disables the only account.
                    self.smart_collectors.pop(f"{p_id}:default", None)
                    continue
                effective_ttl = (
                    db_cfg.poll_interval_seconds
                    if db_cfg and db_cfg.poll_interval_seconds
                    else global_poll_interval or ttl
                )
                key = f"{p_id}:default"
                db_label = db_cfg.account_label if db_cfg else None
                if key not in self.smart_collectors:
                    logger.info(f"Spawning default collector for {p_id}")
                    collector_instance = cls(account_label=db_label)
                    if not collector_instance.CREDENTIALS_KEYED_BY_ACCOUNT_ID:
                        # Display identity may be resolved during collection,
                        # but credentials and auth-failure tracking remain on
                        # the default source slot.
                        collector_instance.credential_account_id = "default"
                    # Apply user strategy ordering/toggles if configured
                    if db_cfg and db_cfg.strategies:
                        collector_instance.apply_strategy_config(db_cfg.strategies)
                    self.smart_collectors[key] = SmartCollector(
                        collector=collector_instance,
                        collector_name=name,
                        ttl=effective_ttl,
                    )
                else:
                    sc = self.smart_collectors[key]
                    if effective_ttl != sc.ttl:
                        sc.ttl = effective_ttl
                    # Propagate updated account_label from ProviderConfig
                    if sc.collector.account_label != db_label:
                        sc.collector.account_label = db_label
                    # Propagate updated strategy config
                    new_strategies = db_cfg.strategies if db_cfg else None
                    if sc.collector._user_strategies != new_strategies:
                        sc.collector.apply_strategy_config(new_strategies)

            # 2. Discover active dynamic collectors from TokenCache
            active_accounts = await token_cache.get_all_active_accounts()
            active_keys = set()

            # Identity-pending sources are intentionally absent from the
            # aggregate account cache so they cannot render as "default".
            # Run a dedicated verifier regardless of whether a normal default
            # collector exists: that collector may already have a resolved
            # display identity and therefore read another credential slot.
            for p_id, (cls, name, ttl) in self.collector_registry.items():
                pending_sources = await token_cache.get_source_candidates(p_id, "default")
                has_pending_source = any(
                    source.get("source_type") == "sidecar"
                    and source.get("credential_origin")
                    and source.get("identity_pending") is True
                    for source in pending_sources
                )
                key = f"{p_id}:default:identity-pending"
                if not has_pending_source:
                    self.smart_collectors.pop(key, None)
                    continue
                active_keys.add(key)
                if key in self.smart_collectors:
                    continue
                full_name = f"{name} (identity pending)"
                collector_instance = cls(account_id="default")
                collector_instance.credential_account_id = "default"
                self.smart_collectors[key] = SmartCollector(
                    collector=collector_instance,
                    collector_name=full_name,
                    ttl=ttl,
                )
                logger.info("Spawning identity-verification collector for %s", p_id)

            for p_id, acc_id, acc_name in active_accounts:
                if p_id in self.collector_registry:
                    default_key = f"{p_id}:default"
                    default_collector = self.smart_collectors.get(default_key)
                    # Skip only the cache entry whose slot the default collector
                    # actually reads. Its display identity can differ from that
                    # credential slot when LatestUsage restores a durable id.
                    default_credential_account_id = None
                    if default_collector is not None:
                        default_credential_account_id = (
                            getattr(
                                default_collector.collector,
                                "credential_account_id",
                                None,
                            )
                            or default_collector.collector.account_id
                            or "default"
                        )
                    if default_collector is not None and acc_id == default_credential_account_id:
                        continue
                    cls, name, ttl = self.collector_registry[p_id]

                    # For dynamic account, check for specific override OR fallback to default provider override
                    provider_acc_configs = db_configs.get(p_id, {})
                    if (
                        acc_id == "default"
                        and provider_acc_configs
                        and "default" not in provider_acc_configs
                    ):
                        # Config rows exist but none is the default sentinel —
                        # collection is account-keyed. The sidecar can still
                        # stamp a literal "default" into the token cache
                        # (`_gemini_account_email` / `_ag_account_email`
                        # fallbacks); spawning {pid}:default here would keep
                        # collecting after the user disabled the only account.
                        self.smart_collectors.pop(f"{p_id}:default", None)
                        continue
                    db_cfg = provider_acc_configs.get(acc_id) or provider_acc_configs.get("default")

                    if db_cfg is not None and not db_cfg.enabled:
                        # Remove existing dynamic collector and its stale cards
                        self.smart_collectors.pop(f"{p_id}:{acc_id}", None)
                        continue
                    effective_ttl = (
                        db_cfg.poll_interval_seconds
                        if db_cfg and db_cfg.poll_interval_seconds
                        else global_poll_interval or ttl
                    )

                    # Prioritize DB override label, then acc_name from cache
                    db_label = db_cfg.account_label if db_cfg else None
                    final_label = db_label or acc_name

                    key = f"{p_id}:{acc_id}"
                    active_keys.add(key)
                    if key not in self.smart_collectors:
                        full_name = f"{name} ({final_label or acc_id[:6]})"
                        logger.info(f"Spawning dynamic collector for {p_id} account {acc_id}")
                        collector_instance = cls(account_id=acc_id, account_label=final_label)
                        # Apply user strategy ordering/toggles if configured
                        if db_cfg and db_cfg.strategies:
                            collector_instance.apply_strategy_config(db_cfg.strategies)
                        self.smart_collectors[key] = SmartCollector(
                            collector=collector_instance,
                            collector_name=full_name,
                            ttl=effective_ttl,
                        )
                    else:
                        sc = self.smart_collectors[key]
                        if effective_ttl != sc.ttl:
                            sc.ttl = effective_ttl
                        if sc.collector.account_label != final_label:
                            sc.collector.account_label = final_label
                        # Propagate updated strategy config
                        new_strategies = db_cfg.strategies if db_cfg else None
                        if sc.collector._user_strategies != new_strategies:
                            sc.collector.apply_strategy_config(new_strategies)

            # 3. Prune collectors whose accounts disappeared from the token cache
            stale_keys = [
                key
                for key in self.smart_collectors
                if not key.endswith(":default") and key not in active_keys
            ]
            for key in stale_keys:
                logger.info(f"Removing stale collector for {key}")
                self.smart_collectors.pop(key, None)
            self._last_sync_time = time.time()

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create a persistent httpx client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=30.0, event_hooks={"response": [token_cache.observe_response]}
            )
        return self._client

    async def close(self):
        """Close the internal HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _sync_manual_config_to_cache(self, r, session=None):
        """Helper to push a single ProviderConfig into the token cache."""
        from app.core.utils import IdentityExtractor

        all_tokens = {}

        # Handle API Key (OAuth Token)
        if r.api_key:
            # Strip Bearer prefix if present
            token_val = r.api_key
            if token_val.lower().startswith("bearer "):
                token_val = token_val[7:].strip()

            if r.provider_id == "xai":
                # A manually saved xAI token is the access bearer only.
                all_tokens["xai_access"] = token_val
            else:
                all_tokens["oauth_token"] = token_val
                # These collectors read the credential from the api_key slot.
                if r.provider_id in (
                    "opencode",
                    "ollama",
                    "minimax",
                    "kimi_coding",
                    "deepseek",
                    "openrouter",
                    "zai",
                    "kimi_api",
                    "kimi_k2",
                ):
                    all_tokens["api_key"] = token_val
            if r.provider_id == "chatgpt":
                acc_id = IdentityExtractor.get_openai_account_id_from_jwt(token_val)
                if acc_id:
                    all_tokens["account_id"] = acc_id

        # Handle Session Cookie
        if r.session_cookie:
            # Map generic session_cookie to all common provider-specific keys
            all_tokens.update(
                {
                    "session_cookie": r.session_cookie,
                    "cookie_session": r.session_cookie,
                    "cookie_sessionKey": r.session_cookie,
                    "cookie___Secure-next-auth.session-token": r.session_cookie,
                }
            )
            # OpenCode's console handshake uses both auth and console_session.
            # Scope the extra slot to the provider that reads it.
            if r.provider_id == "opencode":
                all_tokens["console_session"] = r.session_cookie

        # Handle oai-sc service-credential cookie (ChatGPT only)
        if r.provider_id == "chatgpt" and r.oai_sc_cookie:
            all_tokens["cookie_oai-sc"] = r.oai_sc_cookie

        if all_tokens:
            source_id = f"config:{r.provider_id}:{r.account_id or 'default'}"
            enabled = True
            priority = 0
            if session is not None:
                from app.services.credential_sources import touch_source

                source = touch_source(
                    session,
                    provider_id=r.provider_id,
                    account_id=r.account_id or "default",
                    source_id=source_id,
                    source_type="config",
                    source_label="Manual configuration",
                )
                enabled = source.enabled
                priority = source.priority
            await token_cache.store(
                r.provider_id,
                all_tokens,
                account_id=r.account_id or "default",
                source="config",
                source_id=source_id,
                source_metadata={
                    "source_type": "config",
                    "source_label": "Manual configuration",
                    "enabled": enabled,
                    "priority": priority,
                },
            )

    async def collect_all(self) -> list[dict[str, Any]]:
        """
        Collect all limits across all active accounts.

        Implements a single-flight pattern: if a collection cycle is already
        in progress, concurrent callers wait for it and share the same result
        instead of triggering redundant parallel collections.

        The lock is held only for the brief check/register of the Future so
        that followers can immediately join the in-flight work rather than
        blocking behind it.
        """
        async with self._collect_lock:
            if self._collect_future is not None and not self._collect_future.done():
                # A collection is already running — grab the future and join it
                future = self._collect_future
                is_leader = False
            else:
                # Register a new future before releasing the lock so any
                # concurrent callers that arrive now will find it and wait
                future = asyncio.get_running_loop().create_future()
                self._collect_future = future
                is_leader = True
        # Lock released; only the leader drives the collection

        if is_leader:
            try:
                result = await self._do_collect()
                if not future.done():
                    future.set_result(result)
            except asyncio.CancelledError:
                if not future.done():
                    future.cancel()
                raise
            except BaseException as e:
                if not future.done():
                    future.set_exception(e)
                raise

        return await future

    async def _do_collect(self) -> list[dict[str, Any]]:
        """Execute one collection cycle across all active collectors."""
        # Ensure we have collectors for all current accounts
        await self._sync_collectors()

        client = await self._get_client()

        active_keys = list(self.smart_collectors.keys())
        # Wrap each in a Task so we can harvest partial results on global timeout.
        # Each task also has its own 25s per-collector timeout in
        # _collect_with_semaphore, so the 45s here is a safety net for the
        # combined latency of the queue under semaphore contention.
        tasks = [
            asyncio.create_task(self._collect_with_semaphore(key, client)) for key in active_keys
        ]
        done, pending = await asyncio.wait(tasks, timeout=45.0)
        if pending:
            logger.error(
                "Global collector timeout reached after 45s: "
                f"{len(pending)} task(s) still running, harvesting {len(done)} completed."
            )
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

        results: list[Any] = []
        for t in tasks:
            if t.cancelled():
                results.append(asyncio.CancelledError())
            elif (exc := t.exception()) is not None:
                results.append(exc)
            else:
                results.append(t.result())

        flattened = []
        outcomes: list[dict[str, Any]] = []
        for i, res in enumerate(results):
            key = active_keys[i]
            smart = self.smart_collectors.get(key)
            provider_id = getattr(getattr(smart, "collector", None), "PROVIDER_ID", None)
            account_id = getattr(getattr(smart, "collector", None), "account_id", None) or "default"
            failed = isinstance(res, (Exception, asyncio.CancelledError))
            state = "failed" if failed else (smart.last_collection_state if smart else "failed")
            outcomes.append(
                {
                    "provider_id": provider_id,
                    "account_id": account_id,
                    "source_id": f"server:{provider_id}",
                    "state": state,
                }
            )
            if failed:
                logger.error(f"Unexpected error from collector {active_keys[i]}: {res}")
                continue
            if isinstance(res, list):
                flattened.extend(res)

        logger.info(
            f"Collected {len(flattened)} total cards from {len(active_keys)} active accounts"
        )
        self.last_collection_outcomes = outcomes
        return flattened

    async def _collect_with_semaphore(
        self, key: str, client: httpx.AsyncClient
    ) -> list[dict[str, Any]]:
        """Run a single collector with semaphore/timeout protection."""
        health_updates: dict[str, str] = {}
        async with self._semaphore:
            collector = self.smart_collectors[key].collector
            provider_id = getattr(collector, "PROVIDER_ID", None)
            account_id = (
                getattr(collector, "credential_account_id", None)
                or getattr(collector, "account_id", None)
                or "default"
            )
            result = await self._collect_with_source_failover(key, client, health_updates)
        # Release the shared semaphore before doing synchronous DB I/O.
        if isinstance(provider_id, str) and isinstance(account_id, str):
            self._record_source_health(provider_id, account_id, health_updates)
        return result

    async def _collect_with_source_failover(
        self,
        key: str,
        client: httpx.AsyncClient,
        health_updates: dict[str, str],
    ) -> list[dict[str, Any]]:
        smart = self.smart_collectors[key]
        collector = smart.collector
        provider_id = getattr(collector, "PROVIDER_ID", None)
        account_id = (
            getattr(collector, "credential_account_id", None)
            or getattr(collector, "account_id", None)
            or "default"
        )
        default_account_label = getattr(collector, "account_label", None)
        identity_verification = key.endswith(":identity-pending")
        candidates = (
            await token_cache.get_source_candidates(provider_id, account_id)
            if isinstance(provider_id, str) and isinstance(account_id, str)
            else []
        )
        if not candidates or not isinstance(provider_id, str):
            if identity_verification:
                return []
            return await asyncio.wait_for(smart.collect(client), timeout=25.0)

        # A regular default collector must not race the verifier for pending
        # sources. The dedicated collector sees only pending sidecar bundles.
        candidates = [
            candidate
            for candidate in candidates
            if bool(candidate.get("identity_pending")) == identity_verification
        ]
        if not candidates:
            if identity_verification:
                return []
            return await asyncio.wait_for(smart.collect(client), timeout=25.0)

        preferences = self._credential_source_preferences.get((provider_id, account_id), {})
        candidates = [
            candidate
            for candidate in candidates
            if (
                preferences[candidate["source_id"]][0]
                if candidate["source_id"] in preferences
                else candidate.get("enabled", True)
            )
        ]
        candidates.sort(
            key=lambda candidate: (
                preferences[candidate["source_id"]][1]
                if candidate["source_id"] in preferences
                else candidate.get("priority", 0),
                candidate["source_id"],
            )
        )
        if not candidates:
            return []

        successful_result: list[dict[str, Any]] | None = None
        deadline = asyncio.get_running_loop().time() + 25.0
        await smart.reset()
        for index, candidate in enumerate(candidates):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            if index:
                await smart.reset()
            if account_id == "default":
                # Collectors can mutate account_id after an API response.
                # Reset before each source attempt so a verified identity from
                # source A can never be attributed to source B on a later poll.
                collector.account_id = "default"
                collector.account_label = default_account_label
            if hasattr(collector, "credential_account_id"):
                collector.credential_account_id = account_id
            async with token_cache.using_source(
                provider_id, account_id, candidate["source_id"]
            ) as attempt:
                try:
                    result = await asyncio.wait_for(smart.collect(client), timeout=remaining)
                except Exception:
                    logger.exception(
                        "Credential source collection failed for %s/%s (%s)",
                        scrub_log(provider_id),
                        scrub_log(account_id),
                        scrub_log(candidate["source_id"]),
                    )
                    health_updates[candidate["source_id"]] = "unavailable"
                    continue
            if attempt["auth_failed"]:
                health_updates[candidate["source_id"]] = "auth_failed"
                continue
            if any(card.get("error_type") in {"api_error", "parse_error"} for card in result):
                health_updates[candidate["source_id"]] = "unavailable"
                continue
            health_updates[candidate["source_id"]] = "healthy"
            # A provider may learn a stable identity only after calling its
            # upstream API (Antigravity userinfo is one example). Bind that
            # identity to the exact source used for this successful attempt.
            # Never infer it from another account's historical usage.
            resolved_id = getattr(collector, "account_id", None)
            if (
                account_id == "default"
                and candidate.get("source_type") == "sidecar"
                and candidate.get("credential_origin")
                and isinstance(resolved_id, str)
                and resolved_id.strip()
                and resolved_id.strip().lower() != "default"
            ):
                await self._promote_source_identity(
                    provider_id,
                    account_id,
                    candidate["source_id"],
                    resolved_id,
                )
            elif (
                account_id == "default"
                and candidate.get("source_type") == "sidecar"
                and candidate.get("credential_origin")
            ):
                # Let an unresolved source call its API so it can prove its
                # identity, but never publish an unidentified quota card into
                # the shared default account history.
                preview: list[dict[str, Any]] = [
                    {
                        key: card[key]
                        for key in (
                            "service_name",
                            "remaining",
                            "unit",
                            "unit_type",
                            "pct_used",
                            "window_type",
                            "reset",
                            "reset_at",
                        )
                        if key in card
                    }
                    for card in result
                    if not card.get("error_type")
                ]
                self._persist_identity_pending_preview(provider_id, candidate, preview)
                successful_result = []
                break
            successful_result = result
            break
        return successful_result if successful_result is not None else []

    async def _promote_source_identity(
        self, provider_id: str, old_account_id: str, source_id: str, account_id: str
    ) -> None:
        """Move one default source to the stable identity it proved itself."""
        from datetime import UTC, datetime

        from sqlmodel import Session, select

        from app.core.db import engine
        from app.models.db import CredentialSource
        from app.services.account_identity import canonical_account_id
        from app.services.credential_tags import CredentialTagRepo, PendingCredentialTagRepo

        target = canonical_account_id(account_id)
        if not target or target == "default":
            return
        with Session(engine) as session:
            source = session.exec(
                select(CredentialSource).where(
                    CredentialSource.provider_id == provider_id,
                    CredentialSource.account_id == old_account_id,
                    CredentialSource.source_id == source_id,
                )
            ).first()
            if source is None or not source.credential_origin:
                return
            existing_target = session.exec(
                select(CredentialSource).where(
                    CredentialSource.provider_id == provider_id,
                    CredentialSource.account_id == target,
                    CredentialSource.source_id == source_id,
                )
            ).first()
            source_sidecar_id = source.sidecar_id
            source_credential_origin = source.credential_origin
            if existing_target is None:
                source.account_id = target
                session.add(source)
            else:
                existing_target.source_type = source.source_type
                existing_target.source_label = source.source_label
                existing_target.credential_origin = source.credential_origin
                existing_target.sidecar_id = source.sidecar_id
                seen_values = [
                    value
                    for value in (source.last_seen, existing_target.last_seen)
                    if value is not None
                ]
                existing_target.last_seen = (
                    max(
                        value.replace(tzinfo=UTC) if value.tzinfo is None else value
                        for value in seen_values
                    )
                    if seen_values
                    else datetime.now(UTC)
                )
                session.add(existing_target)
                session.delete(source)
            if source_sidecar_id:
                CredentialTagRepo.set_tag(
                    session,
                    provider_id=provider_id,
                    credential_origin=source_credential_origin,
                    account_id=target,
                    sidecar_id=source_sidecar_id,
                    set_by="identity_verification",
                )
                PendingCredentialTagRepo.delete(
                    session,
                    sidecar_id=source_sidecar_id,
                    provider_id=provider_id,
                    credential_origin=source_credential_origin,
                )
            session.commit()
        moved = await self.reconcile_token_cache_from_durable_tags(
            provider_id=provider_id, source_id=source_id
        )
        if not moved:
            await token_cache.move_source(provider_id, old_account_id, target, source_id)

    def _persist_identity_pending_preview(
        self,
        provider_id: str,
        candidate: dict[str, Any],
        preview: list[dict[str, Any]],
    ) -> None:
        """Persist safe quota fields against the pending credential row."""
        sidecar_id = candidate.get("sidecar_id")
        credential_origin = candidate.get("credential_origin")
        if not isinstance(sidecar_id, str) or not isinstance(credential_origin, str):
            return

        from sqlmodel import Session

        from app.core.db import engine
        from app.services.credential_tags import PendingCredentialTagRepo

        with Session(engine) as session:
            PendingCredentialTagRepo.set_quota_preview(
                session,
                sidecar_id=sidecar_id,
                provider_id=provider_id,
                credential_origin=credential_origin,
                preview=preview,
            )
            session.commit()

    async def reconcile_token_cache_from_durable_tags(
        self,
        *,
        provider_id: str | None = None,
        source_id: str | None = None,
    ) -> int:
        """Move cached sidecar credentials to accounts named by durable tags.

        Tags commit before the in-memory cache can be updated. Rechecking the
        durable mapping during startup/sync and after promotions closes that
        gap without relying on a later sidecar heartbeat.
        """
        from sqlmodel import Session, select

        from app.core.db import engine
        from app.models.db import CredentialSource
        from app.services.credential_tags import CredentialTagRepo

        provider_ids = [provider_id] if provider_id else list(self.collector_registry)
        moves: list[tuple[str, str, str, str, bool, int]] = []
        for pid in provider_ids:
            candidates = await token_cache.get_source_candidates(pid, "default")
            with Session(engine) as session:
                for candidate in candidates:
                    candidate_id = candidate.get("source_id")
                    origin = candidate.get("credential_origin")
                    sidecar_id = candidate.get("sidecar_id")
                    if (
                        candidate.get("source_type") != "sidecar"
                        or not isinstance(candidate_id, str)
                        or (source_id is not None and candidate_id != source_id)
                        or not isinstance(origin, str)
                        or not isinstance(sidecar_id, str)
                    ):
                        continue
                    target = CredentialTagRepo.get_account_id(
                        session,
                        provider_id=pid,
                        credential_origin=origin,
                        sidecar_id=sidecar_id,
                    )
                    if not target or target == "default":
                        continue
                    source = session.exec(
                        select(CredentialSource).where(
                            CredentialSource.provider_id == pid,
                            CredentialSource.account_id == target,
                            CredentialSource.source_id == candidate_id,
                        )
                    ).first()
                    enabled = source.enabled if source else candidate.get("enabled", True)
                    priority = source.priority if source else candidate.get("priority", 0)
                    moves.append(
                        (pid, candidate_id, target, "default", bool(enabled), int(priority))
                    )

        for pid, candidate_id, target, old_account_id, enabled, priority in moves:
            await token_cache.move_source(pid, old_account_id, target, candidate_id)
            old_preferences = self._credential_source_preferences.get((pid, old_account_id))
            if old_preferences is not None:
                old_preferences.pop(candidate_id, None)
            self._credential_source_preferences.setdefault((pid, target), {})[candidate_id] = (
                enabled,
                priority,
            )
        return len(moves)

    @staticmethod
    def _record_source_health(provider_id: str, account_id: str, updates: dict[str, str]) -> None:
        if not updates:
            return

        from sqlmodel import Session, col
        from sqlmodel import select as sqlselect

        from app.core.db import engine
        from app.models.db import CredentialSource

        with Session(engine) as session:
            rows = session.exec(
                sqlselect(CredentialSource).where(
                    CredentialSource.provider_id == provider_id,
                    CredentialSource.account_id == account_id,
                    col(CredentialSource.source_id).in_(updates),
                )
            ).all()
            for row in rows:
                health = updates[row.source_id]
                row.health = health
                row.health_detail = {
                    "auth_failed": "Authentication failed",
                    "unavailable": "Collection failed",
                }.get(health)
                session.add(row)
            session.commit()

    def get_collector_stats(self) -> dict[str, Any]:
        """Get flattened statistics for all active collectors."""
        return {"collectors": [sc.get_stats() for sc in self.smart_collectors.values()]}

    async def collect_one(
        self, provider_id: str, account_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Reset and immediately re-collect a single provider."""
        target_prefix = f"{provider_id}:"
        results: list[dict[str, Any]] = []
        client = await self._get_client()

        for key, sc in self.smart_collectors.items():
            if key.startswith(target_prefix):
                if (
                    account_id is None
                    or key == f"{provider_id}:{account_id}"
                    or (
                        account_id == "default" and key == f"{provider_id}:default:identity-pending"
                    )
                ):
                    await sc.reset()
                    try:
                        res = await self._collect_with_semaphore(key, client)
                        if isinstance(res, list):
                            results.extend(res)
                    except Exception as e:
                        logger.error(f"Error collecting {key}: {e}")

        return results

    def _create_collector(self, provider_id: str) -> Any:
        """Instantiate a one-off collector for *provider_id* (not added to smart_collectors).

        Used by the debug endpoint to run a collector that is not currently active.
        Returns None if the provider is not registered.
        """
        entry = self.collector_registry.get(provider_id)
        if entry is None:
            return None
        cls, _name, _ttl = entry
        return cls()

    def set_credential_source_preferences(
        self, provider_id: str, account_id: str, preferences: dict[str, tuple[bool, int]]
    ) -> None:
        """Replace the in-memory source preferences for one account."""
        self._credential_source_preferences[(provider_id, account_id)] = preferences

    def clear_credential_source_preferences(self, provider_id: str, account_id: str) -> None:
        """Drop the in-memory source preferences for one account."""
        self._credential_source_preferences.pop((provider_id, account_id), None)

    async def reset_collector(self, provider_id: str, account_id: str | None = None):
        """Reset internal state for specific collector(s)."""
        target_prefix = f"{provider_id}:"

        for key, sc in self.smart_collectors.items():
            if key.startswith(target_prefix):
                if account_id is None or key == f"{provider_id}:{account_id}":
                    await sc.reset()

    def get_supported_strategies(self, provider_id: str) -> list[dict]:
        """
        Return the list of supported strategies for a given provider.
        Each entry: {"id": str, "label": str}
        Returns [] if the provider has no declared STRATEGIES.
        """
        entry = self.collector_registry.get(provider_id)
        if entry is None:
            return []
        cls, _name, _ttl = entry
        strategies = getattr(cls, "STRATEGIES", {})
        result = []
        for s_id, entry in strategies.items():
            if len(entry) >= 2:
                label = entry[0]
                result.append({"id": s_id, "label": label})
        return result


# Global instance
collector_manager = CollectorManager()
manager = collector_manager
