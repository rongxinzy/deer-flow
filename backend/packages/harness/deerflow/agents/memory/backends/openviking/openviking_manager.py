"""OpenViking memory backend built on the maintained LangChain adapters."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import threading
import time
import weakref
from contextlib import AbstractContextManager
from math import isfinite
from pathlib import Path
from typing import Any, ClassVar, Literal
from urllib.parse import unquote

from pydantic import PrivateAttr

from deerflow.agents.memory.manager import MemoryManager, MemoryManagerError, MemoryReadError

from .config import OpenVikingConfig
from .session import (
    _advanced_cursor,
    _canonical_peer_id,
    _captureable_messages,
    _matching_prefix_count,
    _memory_target_uris,
    _message_signature,
    _requester_peer_id,
    _session_id,
    _string_list,
)

logger = logging.getLogger(__name__)


class RequesterScopeError(MemoryManagerError):
    """Requester-scoped memory cannot identify the requester.

    Distinct from the credential-sharing refusal: this one may be swallowed by
    the configured read/write failure policy (fail closed to "no memory"),
    while a credential mismatch must always surface.
    """


class OpenVikingMemoryManager(MemoryManager):
    """Single-user OpenViking backend using the official integration package.

    DeerFlow continues to choose when capture and recall occur. The official
    adapter owns SDK transport, message conversion, batching, commit retries,
    and retrieval behavior.
    """

    supports_search: ClassVar[bool] = True
    supports_requester_scope: ClassVar[bool] = True

    _config: OpenVikingConfig = PrivateAttr()
    _client: Any = PrivateAttr()
    _recorder: Any = PrivateAttr()
    _retriever: Any = PrivateAttr()
    _use_actor_peer: Any = PrivateAttr()
    _call_openviking: Any = PrivateAttr()
    _partial_write_error: type[Exception] = PrivateAttr()
    _should_keep_hidden_message: Any = PrivateAttr(default=None)
    _session_locks: weakref.WeakValueDictionary[str, threading.RLock] = PrivateAttr(default_factory=weakref.WeakValueDictionary)
    _session_locks_guard: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _lifecycle: threading.Condition = PrivateAttr(default_factory=threading.Condition)
    _active_operations: int = PrivateAttr(default=0)
    _closed: bool = PrivateAttr(default=False)
    _close_requested: bool = PrivateAttr(default=False)
    _resources_closed: bool = PrivateAttr(default=False)
    _resource_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def model_post_init(self, __context: Any) -> None:
        self._config = OpenVikingConfig.from_backend_config(self.backend_config)
        integration = _load_official_integration()
        commit_policy = integration["OpenVikingCommitPolicy"](mode="always")
        self._recorder = integration["OpenVikingSessionRecorder"](
            url=self._config.base_url,
            api_key=self._config.api_key,
            timeout=self._config.timeout_seconds,
            # An explicit mapping disables the SDK's ovcli.conf header fallback.
            extra_headers={},
            commit_policy=commit_policy,
        )
        # The recorder owns one recovery-aware SDK client. Retrieval borrows the
        # same handle, so this backend has one connection pool and one owner.
        self._client = self._recorder.client
        self._retriever = integration["OpenVikingRetriever"](
            client=self._client,
            search_mode="find",
            limit=self._config.search_top_k,
            score_threshold=self._config.score_threshold,
            context_types=("memory",),
            content_mode=self._config.content_mode,
            max_content_chars=self._config.max_injection_chars,
        )
        self._use_actor_peer = integration["use_actor_peer"]
        self._call_openviking = integration["call_openviking"]
        self._partial_write_error = integration["OpenVikingPartialWriteError"]

    @classmethod
    def from_config(
        cls,
        backend_config: dict[str, Any] | None = None,
        *,
        mode: Literal["middleware", "tool"] = "middleware",
        **host_hooks: Any,
    ) -> OpenVikingMemoryManager:
        if mode != "middleware":
            raise ValueError("The OpenViking automatic-memory backend supports memory.mode='middleware' only; use OpenViking MCP for explicit model tools")
        instance = cls(backend_config=backend_config or {}, mode=mode)
        hidden_filter = host_hooks.get("should_keep_hidden_message")
        instance._should_keep_hidden_message = hidden_filter if callable(hidden_filter) else None
        return instance

    def add(
        self,
        thread_id: str,
        messages: list[Any],
        *,
        agent_name: str | None = None,
        user_id: str | None = None,
        requester_id: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        del trace_id
        self._write_conversation(
            thread_id,
            messages,
            agent_name=agent_name,
            user_id=user_id,
            requester_id=requester_id,
        )
        self._audit_access(
            "capture",
            user_id=user_id,
            agent_name=agent_name,
            requester_id=requester_id,
            hits=len(messages),
            thread_id=thread_id,
        )

    @classmethod
    def read_failures_are_fatal_for_config(
        cls,
        backend_config: dict[str, Any] | None,
    ) -> bool:
        return OpenVikingConfig.from_backend_config(backend_config).read_failure_policy == "raise"

    def add_nowait(
        self,
        thread_id: str,
        messages: list[Any],
        *,
        agent_name: str | None = None,
        user_id: str | None = None,
        requester_id: str | None = None,
    ) -> None:
        # Preserve DeerFlow's existing pre-compaction behavior. This backend
        # commits every accepted capture, so add_nowait needs no separate mode.
        self._write_conversation(
            thread_id,
            messages,
            agent_name=agent_name,
            user_id=user_id,
            requester_id=requester_id,
        )

    async def aadd(
        self,
        thread_id: str,
        messages: list[Any],
        *,
        agent_name: str | None = None,
        user_id: str | None = None,
        requester_id: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        await asyncio.to_thread(
            self.add,
            thread_id,
            messages,
            agent_name=agent_name,
            user_id=user_id,
            requester_id=requester_id,
            trace_id=trace_id,
        )

    def get_context(
        self,
        user_id: str | None,
        *,
        agent_name: str | None = None,
        thread_id: str | None = None,
        query: str | None = None,
        requester_id: str | None = None,
    ) -> str:
        if not self._begin_operation():
            return ""
        try:
            try:
                peer_id = self._resolve_read_scope(user_id, agent_name, requester_id)
            except RequesterScopeError as exc:
                if self._config.read_failure_policy == "raise":
                    raise
                logger.warning(
                    "OpenViking memory scope unresolved; continuing without injected memory: %s",
                    exc,
                )
                return ""
            retriever = copy.copy(self._retriever)
            retriever.target_uri = self._memory_target_uris_for(peer_id)
            if thread_id:
                retriever.search_mode = "search"
                retriever.session_id = _session_id(
                    self._config.owner_user_id,
                    peer_id,
                    thread_id,
                )
            try:
                with self._actor_peer_scope(peer_id):
                    documents = retriever.invoke(self._config.injection_query)
            except Exception as exc:
                if self._config.read_failure_policy == "raise":
                    raise MemoryReadError("OpenViking context retrieval failed") from exc
                logger.warning(
                    "OpenViking context retrieval failed; continuing without injected memory",
                    exc_info=True,
                )
                return ""
            documents = self._drop_deleted_documents(documents, peer_id)
            self._audit_access(
                "injection",
                user_id=user_id,
                agent_name=agent_name,
                requester_id=requester_id,
                hits=len(documents),
                thread_id=thread_id,
            )
            return _format_documents(
                documents,
                max_chars=self._config.max_injection_chars,
            )
        finally:
            self._end_operation()

    async def aget_context(
        self,
        user_id: str | None,
        *,
        agent_name: str | None = None,
        thread_id: str | None = None,
        query: str | None = None,
        requester_id: str | None = None,
    ) -> str:
        return await asyncio.to_thread(
            self.get_context,
            user_id,
            agent_name=agent_name,
            thread_id=thread_id,
            query=query,
            requester_id=requester_id,
        )

    def search(
        self,
        query: str,
        top_k: int = 5,
        *,
        user_id: str | None = None,
        agent_name: str | None = None,
        category: str | None = None,
        requester_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if not query.strip() or not self._begin_operation():
            return []
        try:
            try:
                peer_id = self._resolve_read_scope(user_id, agent_name, requester_id)
            except RequesterScopeError as exc:
                if self._config.read_failure_policy == "raise":
                    raise
                logger.warning(
                    "OpenViking memory scope unresolved; returning no results: %s",
                    exc,
                )
                return []
            retriever = copy.copy(self._retriever)
            retriever.target_uri = self._memory_target_uris_for(peer_id)
            retriever.search_mode = "find"
            retriever.session_id = None
            retriever.limit = max(1, min(int(top_k), 100))
            if category:
                retriever.filter = {
                    "op": "must",
                    "field": "category",
                    "conds": [category],
                }
            try:
                with self._actor_peer_scope(peer_id):
                    documents = retriever.invoke(query.strip())
            except Exception as exc:
                if self._config.read_failure_policy == "raise":
                    raise MemoryReadError("OpenViking memory search failed") from exc
                logger.warning(
                    "OpenViking memory search failed; returning no results",
                    exc_info=True,
                )
                return []
            documents = self._drop_deleted_documents(documents, peer_id)
            facts = [_document_to_fact(document) for document in documents]
            self._audit_access(
                "search",
                user_id=user_id,
                agent_name=agent_name,
                requester_id=requester_id,
                hits=len(facts),
            )
            return facts
        finally:
            self._end_operation()

    async def asearch(
        self,
        query: str,
        top_k: int = 5,
        *,
        user_id: str | None = None,
        agent_name: str | None = None,
        category: str | None = None,
        requester_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self.search,
            query,
            top_k,
            user_id=user_id,
            agent_name=agent_name,
            category=category,
            requester_id=requester_id,
        )

    # ------------------------------------------------------------ governance
    def get_memory(
        self,
        *,
        user_id: str | None = None,
        agent_name: str | None = None,
        requester_id: str | None = None,
    ) -> dict[str, Any]:
        """List and read the actor's own memory entries.

        An explicit ``agent_name`` scopes to that agent's bucket. Without one —
        which is how the gateway's management endpoints call in — it covers the
        actor's whole memory: the user-level root plus every peer bucket under
        the credential-bound user. Other employees are separate credentials and
        are never included. Requester-scoped deployments take the peer from
        ``requester_id``, never from ``user_id``.
        """

        if not self._begin_operation():
            return {"facts": []}
        try:
            facts: list[dict[str, Any]] = []
            with self._actor_peer_scope(self._governance_peer(user_id, agent_name, requester_id)):
                for uri in self._scope_uris(user_id, agent_name, requester_id):
                    facts.append({"id": uri, "content": self._read_fact(uri), "source": uri})
            self._audit_access(
                "list",
                user_id=user_id,
                agent_name=agent_name,
                requester_id=requester_id,
                hits=len(facts),
            )
            return {"facts": facts}
        finally:
            self._end_operation()

    def delete_fact(
        self,
        fact_id: str,
        *,
        agent_name: str | None = None,
        user_id: str | None = None,
        requester_id: str | None = None,
    ) -> dict[str, Any]:
        """Remove one memory entry by the OpenViking URI ``search`` returned.

        Returns the remaining memory document, so a caller that adopts the
        response body does not mistake a successful delete for an empty store.
        """

        if not fact_id:
            raise ValueError("OpenViking delete_fact requires a fact id (its OpenViking URI)")
        if not self._begin_operation():
            return {"facts": []}
        try:
            with self._actor_peer_scope(self._governance_peer(user_id, agent_name, requester_id)):
                self._remove_uri(
                    self._assert_own_uri(fact_id, user_id, agent_name, requester_id),
                    user_id=user_id,
                    agent_name=agent_name,
                    requester_id=requester_id,
                )
            self._audit_access("delete", user_id=user_id, agent_name=agent_name, requester_id=requester_id, hits=1)
            return self.get_memory(user_id=user_id, agent_name=agent_name, requester_id=requester_id)
        finally:
            self._end_operation()

    def create_fact(
        self,
        content: str,
        category: str = "context",
        confidence: float = 0.5,
        *,
        agent_name: str | None = None,
        user_id: str | None = None,
        requester_id: str | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        """Write one manually authored fact; return ``(memory document, fact id)``.

        The fact becomes an ordinary OpenViking memory file under the actor's own
        memory root -- the same place recall and the governance reads look, so no
        scope is widened to make it visible. The file name is derived from the
        normalized content rather than from a random id or from raw text: the
        same fact therefore always maps to the same URI, and the write is issued
        with ``mode="create"`` so a concurrent creator of the same fact loses the
        race at the server instead of overwriting or duplicating the entry. That
        rejection is reported as ``ValueError("Duplicate fact")`` -- the
        duplicate contract the gateway maps to 409 and ``memory_add`` reports as
        an error -- but only once the file at that URI is confirmed to hold the
        same normalized content; any other failure is re-raised so a real error
        is never disguised as a duplicate.

        ``category`` and ``confidence`` are accepted because the gateway and the
        ``memory_add`` tool always send them, and both are validated, but an
        OpenViking memory file carries content only: the store derives an entry's
        category from its directory, and recall reports that derived category
        rather than DeerFlow's label. Nothing is written for them, which is also
        how ``update_fact`` preserves them.

        ``wait=True`` makes the write synchronous: OpenViking indexes new content
        in the background, and without it a recall issued right after a create
        can still miss the entry.

        The returned fact id is the URI OpenViking stored the entry at (the
        concrete ``viking://user/...`` form, which is what ``search`` and
        ``get_memory`` report), not the ``viking://~`` alias this backend
        requested. ``fact_id`` is ``None``, with an empty document, when the
        backend is already shut down and the write was dropped -- the same
        "not stored" signal a capacity-evicted fact produces.
        """

        normalized = str(content or "").strip()
        if not normalized:
            raise ValueError("content")
        _validate_confidence(confidence)
        # Deliberately not persisted; see the docstring.
        del category
        if not self._begin_operation():
            logger.warning("OpenViking fact creation ignored after backend shutdown")
            return {"facts": []}, None
        try:
            with self._actor_peer_scope(self._governance_peer(user_id, agent_name, requester_id)):
                requested = self._fact_uri(normalized, user_id, agent_name, requester_id)
                stored = self._write_fact(requested, normalized, create=True)
            self._audit_access("create", user_id=user_id, agent_name=agent_name, requester_id=requester_id, hits=1)
            return self.get_memory(user_id=user_id, agent_name=agent_name, requester_id=requester_id), stored
        finally:
            self._end_operation()

    def update_fact(
        self,
        fact_id: str,
        content: str | None = None,
        category: str | None = None,
        confidence: float | None = None,
        *,
        agent_name: str | None = None,
        user_id: str | None = None,
        requester_id: str | None = None,
    ) -> dict[str, Any]:
        """Replace the content of one existing fact; return the memory document.

        ``fact_id`` is the URI ``search``/``get_memory`` returned, and the same
        scope checks as ``delete_fact`` run before anything is written. A fact id
        this actor does not currently list raises ``KeyError`` -- the gateway's
        404 -- while an unreachable backend keeps raising its transport error, so
        "absent" and "unreadable" never collapse into one answer.

        An OpenViking memory file holds content only, so ``category`` and
        ``confidence`` are validated and preserved by omission.

        Changed content moves: the file name is the digest of the fact's
        normalized content (see ``_fact_file_name``), so a rewritten body
        belongs at the URI that name derives from, not at the old one. The move
        keeps the old URI's directory -- the bucket the fact was written to --
        so it neither widens nor narrows which agents recall it. The new file is
        created first and the old URI is removed only once that write has been
        confirmed -- deleting first would lose the fact if the write failed, and
        replacing in place would leave a name holding content it does not name.
        That stale name is not merely cosmetic: a later ``create_fact`` of the
        old body would race for it, lose to ``mode="create"``, and report the
        failed write instead of restoring the fact. The returned document
        therefore reports the fact under its new id.

        If the new URI already holds the same normalized content the write
        loses the create race and raises ``ValueError("Duplicate fact")`` -- the
        duplicate contract ``create_fact`` follows -- and the old file is left
        untouched, because the update was refused rather than applied. A fact id
        whose file already carries the name the new content hashes to (a store
        written before this move, or a file edited outside DeerFlow) is the one
        case where the name is right and the body is stale, so its body is
        replaced in place.

        Rewriting a fact with the content it already holds is a no-op rather
        than a rewrite: the entry keeps its URI and no reindex is spent on
        unchanged bytes.
        """

        if not fact_id:
            raise ValueError("OpenViking update_fact requires a fact id (its OpenViking URI)")
        normalized = content.strip() if content is not None else None
        if normalized is not None and not normalized:
            raise ValueError("content")
        if confidence is not None:
            _validate_confidence(confidence)
        # Deliberately not persisted; see the docstring.
        del category
        if not self._begin_operation():
            return {"facts": []}
        try:
            with self._actor_peer_scope(self._governance_peer(user_id, agent_name, requester_id)):
                uri = self._assert_own_uri(fact_id, user_id, agent_name, requester_id)
                if not self._lists_uri(uri, user_id, agent_name, requester_id):
                    raise KeyError(fact_id)
                if normalized is not None and not self._holds_content(uri, normalized):
                    # Same directory, new name: only the digest changed, and the
                    # directory is the bucket the fact was written to. Deriving
                    # the URI from the scope instead would relocate a fact read
                    # through a whole-actor call out of its agent's bucket (or
                    # into it), changing which agents recall it.
                    target = f"{uri.rsplit('/', 1)[0]}/{_fact_file_name(normalized)}"
                    if self._credential_alias(target) == self._credential_alias(uri):
                        self._write_fact(target, normalized, create=False)
                    else:
                        # Confirmed before the old URI goes: a create that
                        # fails leaves the fact where it was, and one that is
                        # rejected as a duplicate keeps that file too.
                        self._write_fact(target, normalized, create=True)
                        self._remove_uri(uri, user_id=user_id, agent_name=agent_name, requester_id=requester_id)
            self._audit_access("update", user_id=user_id, agent_name=agent_name, requester_id=requester_id, hits=1)
            return self.get_memory(user_id=user_id, agent_name=agent_name, requester_id=requester_id)
        finally:
            self._end_operation()

    def clear_memory(
        self,
        *,
        user_id: str | None = None,
        agent_name: str | None = None,
        requester_id: str | None = None,
    ) -> dict[str, Any]:
        """Remove the actor's own memory under every scope root it may read.

        An explicit ``agent_name`` clears only that agent's bucket. Without one
        it clears the user-level root and every peer bucket of the
        credential-bound user, which is what recall reads by default. Other
        employees are separate credentials and are never touched.
        """

        if not self._begin_operation():
            return {"facts": []}
        try:
            with self._actor_peer_scope(self._governance_peer(user_id, agent_name, requester_id)):
                roots = self._scope_roots(user_id, agent_name, requester_id)
                for root in roots:
                    self._remove_uri(root, user_id=user_id, agent_name=agent_name, requester_id=requester_id, recursive=True)
            self._audit_access("clear", user_id=user_id, agent_name=agent_name, requester_id=requester_id, hits=len(roots))
            return {"facts": []}
        finally:
            self._end_operation()

    def _scope_roots(self, user_id: str | None, agent_name: str | None, requester_id: str | None = None) -> list[str]:
        """Memory roots this call may read or clear.

        The wide instance listing still runs the owner check. Omitting
        ``agent_name`` must not let another DeerFlow user read the credential
        owner's directory; ``single_tenant_instance`` keeps its existing
        mapping inside ``_resolve_scope``.
        """

        peer_id = self._resolve_scope(user_id, agent_name, requester_id)
        if agent_name is not None or self._config.memory_scope == "requester":
            return [f"viking://~/peers/{peer_id}/memories"]
        # Enumerate peer directories, but never treat the peers container as a
        # memory root: it can also hold resources and curated shared memory.
        roots = ["viking://~/memories"]
        peers = self._call_openviking(self._client, "ls", uri="viking://~/peers", simple=True)
        for uri in _extract_uris(peers):
            prefix = "viking://~/peers/"
            alias = self._credential_alias(uri)
            peer = alias.removeprefix(prefix).rstrip("/")
            if not alias.startswith(prefix) or not peer or "/" in peer or "\\" in peer or peer in {".", ".."} or unquote(peer) != peer:
                raise MemoryManagerError("OpenViking returned an invalid peer directory")
            if peer.startswith("df-shared-"):
                continue
            root = f"{prefix}{peer}/memories"
            if root not in roots:
                roots.append(root)
        return roots

    def _scope_uris(self, user_id: str | None, agent_name: str | None, requester_id: str | None = None) -> list[str]:
        """Every memory file URI under the actor's scope roots.

        Enumerations of a memory root also report the directories inside it --
        extraction writes its entries as ``memories/<type>/...`` -- and a
        directory is not a fact: reading one back as a file fails, which used to
        break every governance read as soon as the store held a single
        extraction directory. Directories are therefore skipped, and only files
        are validated and returned.

        Enumeration failures propagate: an unreachable OpenViking must surface
        as an error, not as an empty memory.

        A directory is recognised by the trailing slash OpenViking's listings
        give every one of them, which is the whole signal available here: asking
        whether a slash-less result is a file would cost a probe request on every
        governance read, and ``glob`` -- the listing this reads -- reports
        directories with the slash (``memories/<type>/``). A directory some
        lister returned without one would be treated as a file, fail ``read``,
        and surface as a read error for the whole document rather than as a
        silently skipped entry.
        """

        uris: list[str] = []
        seen: set[str] = set()
        roots = self._scope_roots(user_id, agent_name, requester_id)
        for root in roots:
            for pattern in ("*", "**/*"):
                result = self._call_openviking(self._client, "glob", pattern=pattern, uri=root)
                for uri in _extract_uris(result):
                    alias = self._credential_alias(uri)
                    if alias.endswith("/"):
                        continue
                    self._validate_memory_uri(alias, [root])
                    if uri not in seen:
                        seen.add(uri)
                        uris.append(uri)
        return uris

    def _read_fact(self, uri: str) -> str:
        """Read one memory file's text exactly as ``read`` returns it."""

        content = self._call_openviking(self._client, "read", uri=uri)
        if isinstance(content, bytes | bytearray):
            content = content.decode("utf-8", errors="replace")
        return str(content)

    def _lists_uri(self, uri: str, user_id: str | None, agent_name: str | None, requester_id: str | None = None) -> bool:
        """Whether *uri* is one of the memory files this actor lists right now.

        The existence check for a write is a listing, not a read: a failed read
        means "absent or unreachable" and the two must not collapse into the
        404 a ``KeyError`` produces.
        """

        alias = self._credential_alias(uri)
        return any(alias == self._credential_alias(candidate) for candidate in self._scope_uris(user_id, agent_name, requester_id))

    def _holds_content(self, uri: str, content: str) -> bool:
        """Whether *uri* already holds the same normalized content."""

        try:
            existing = self._read_fact(uri)
        except Exception:
            return False
        return _fact_content_key(existing) == _fact_content_key(content)

    def _fact_uri(
        self,
        content: str,
        user_id: str | None,
        agent_name: str | None,
        requester_id: str | None = None,
    ) -> str:
        """The memory file URI a manually created fact is written to.

        A whole-actor write (no ``agent_name``, instance scope) goes into the
        user-level ``memories`` root, which every agent scope already recalls; an
        agent-scoped or requester-scoped write goes into that actor peer's own
        ``memories`` directory. Both are roots ``_scope_roots`` lists, so the new
        fact is visible to ``get_memory`` and to recall without widening any
        scope. Resolving the scope first also refuses a foreign DeerFlow user
        before OpenViking is contacted.

        No category subdirectory is created: OpenViking's category vocabulary
        does not cover DeerFlow's free-form labels, and its recall filter matches
        the category it derives from an entry rather than a directory name chosen
        here, so a label directory would only add a directory that every
        governance read then has to step over.
        """

        peer_id = self._resolve_scope(user_id, agent_name, requester_id)
        root = f"viking://~/peers/{peer_id}/memories" if agent_name is not None or self._config.memory_scope == "requester" else "viking://~/memories"
        return f"{root}/{_fact_file_name(content)}"

    def _write_fact(self, uri: str, content: str, *, create: bool) -> str:
        """Write one fact file; return the URI OpenViking stored it at.

        ``mode="create"`` is what makes a concurrent create safe: two callers
        holding the same fact race for the same URI and OpenViking rejects the
        loser instead of letting it overwrite. ``mode="replace"`` is for a body
        rewrite that keeps the file's name -- an ``update_fact`` that moved the
        content would have used create at the new name, so replace only has to
        cover a file that already carries the name the new content hashes to.
        ``wait=True`` is required in both cases, because OpenViking indexes
        new content in the background and a recall issued right after the write
        would otherwise miss it.

        The write response, not the requested URI, is authoritative: the server
        expands the ``viking://~`` alias to the concrete ``viking://user/...``
        form and rewrites characters it does not store (a space in a path arrives
        as ``_``), so only the response names the entry a later ``search`` or
        ``delete_fact`` will see.
        """

        try:
            result = self._call_openviking(
                self._client,
                "write",
                uri=uri,
                content=content,
                mode="create" if create else "replace",
                wait=True,
            )
        except Exception as exc:
            if create and self._holds_content(uri, content):
                raise ValueError("Duplicate fact") from exc
            raise
        stored = result.get("uri") if isinstance(result, dict) else None
        return stored if isinstance(stored, str) and stored else uri

    def _assert_own_uri(self, uri: str, user_id: str | None, agent_name: str | None, requester_id: str | None = None) -> str:
        roots = self._scope_roots(user_id, agent_name, requester_id)
        self._validate_memory_uri(self._credential_alias(uri), roots)
        # OpenViking listings/search return concrete user URIs. Only accept a
        # concrete id that this credential actually enumerated in this scope;
        # otherwise a foreign user's identically named fact could alias ours.
        if uri.startswith("viking://user/") and uri not in self._scope_uris(user_id, agent_name, requester_id):
            raise MemoryManagerError("refusing to modify a memory URI outside this actor's scope")
        return uri

    @staticmethod
    def _credential_alias(uri: str) -> str:
        if uri.startswith("viking://user/"):
            user_and_path = uri[len("viking://user/") :].split("/", 1)
            if len(user_and_path) == 2 and user_and_path[0]:
                return "viking://~/" + user_and_path[1]
        return uri

    def _governance_peer(self, user_id: str | None, agent_name: str | None, requester_id: str | None = None) -> str | None:
        peer = self._resolve_scope(user_id, agent_name, requester_id)
        return peer if agent_name is not None or self._config.memory_scope == "requester" else None

    @staticmethod
    def _validate_memory_uri(uri: str, roots: list[str]) -> str:
        if "\\" in uri or ".." in uri.split("/") or "." in uri.split("/") or unquote(uri) != uri or "?" in uri or "#" in uri:
            raise MemoryManagerError(f"refusing to modify a suspicious memory URI: {uri}")
        if uri.endswith("/"):
            # A memory root and every extraction-created category directory are
            # directories; none of them is a fact, and `rm`/`write` on one either
            # fails server-side or would take the whole subtree with it.
            raise MemoryManagerError(f"refusing to modify a memory directory: {uri}")
        if not any(uri == root or uri.startswith(root + "/") for root in roots):
            raise MemoryManagerError(f"refusing to modify a memory URI outside this actor's scope: {uri}")
        return uri

    def _remove_uri(
        self,
        uri: str,
        *,
        user_id: str | None,
        agent_name: str | None,
        requester_id: str | None = None,
        recursive: bool = False,
    ) -> None:
        # Governance actions are explicit calls: unlike passive capture, a
        # failure surfaces instead of being dropped by the write policy.
        peer_id = self._governance_peer(user_id, agent_name, requester_id)
        with self._actor_peer_scope(peer_id):
            self._call_openviking(self._client, "rm", uri=uri, recursive=recursive)

    def warm(self) -> bool | None:
        if not self._begin_operation():
            return False
        try:
            try:
                health = getattr(self._client, "health", None)
                healthy = bool(health()) if callable(health) else True
            except Exception:
                if self._config.startup_policy == "fail_fast":
                    raise
                logger.warning(
                    "OpenViking startup validation failed; memory will run in degraded mode",
                    exc_info=True,
                )
                return False
            if not healthy and self._config.startup_policy == "fail_fast":
                raise MemoryManagerError("OpenViking health check returned an unhealthy response")
            if not healthy:
                logger.warning("OpenViking health check returned unhealthy; memory will run in degraded mode")
            return healthy
        finally:
            self._end_operation()

    def shutdown_flush(self, timeout: float) -> bool:
        """Stop new work, drain accepted calls, and close owned resources."""

        deadline = time.monotonic() + max(0.0, timeout)
        with self._lifecycle:
            self._closed = True
            self._close_requested = True
            while self._active_operations:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._lifecycle.wait(remaining)
        self._close_resources()
        return True

    def close(self) -> None:
        """Request idempotent closure after any active operation finishes."""

        with self._lifecycle:
            self._closed = True
            self._close_requested = True
            can_close = self._active_operations == 0
        if can_close:
            self._close_resources()

    def _write_conversation(
        self,
        thread_id: str,
        messages: list[Any],
        *,
        agent_name: str | None,
        user_id: str | None,
        requester_id: str | None = None,
    ) -> None:
        if not self._begin_operation():
            logger.warning("OpenViking write ignored after backend shutdown")
            return
        try:
            if not thread_id:
                raise ValueError("OpenViking memory write requires thread_id")
            try:
                peer_id = self._resolve_scope(user_id, agent_name, requester_id)
            except RequesterScopeError as exc:
                if self._config.write_failure_policy == "raise":
                    raise
                logger.warning(
                    "OpenViking memory write skipped; requester scope unresolved: %s",
                    exc,
                )
                return
            session_id = _session_id(
                self._config.owner_user_id,
                peer_id,
                thread_id,
            )
            with self._session_lock(session_id):
                self._capture_locked(
                    session_id,
                    peer_id,
                    _captureable_messages(
                        messages,
                        self._should_keep_hidden_message,
                    ),
                )
        finally:
            self._end_operation()

    def _capture_locked(
        self,
        session_id: str,
        peer_id: str,
        messages: list[Any],
    ) -> None:
        state = self._load_cursor(session_id)
        signatures = [_message_signature(message) for message in messages]

        if state.get("commit_pending"):
            try:
                with self._actor_peer_scope(peer_id):
                    self._recorder.flush(session_id)
            except Exception as exc:
                self._handle_write_error(
                    exc,
                    "OpenViking pending commit retry failed; preserving capture cursor",
                    session_id,
                )
                return
            state = {**state, "commit_pending": False}
            self._save_cursor(session_id, state)

        start = _matching_prefix_count(state, signatures)
        append_only = start is not None
        if append_only:
            pending = messages[start:]
            pending_signatures = signatures[start:]
        else:
            submitted = set(_string_list(state.get("submitted_signatures")))
            pending_pairs = [(message, signature) for message, signature in zip(messages, signatures, strict=True) if signature not in submitted]
            pending = [message for message, _ in pending_pairs]
            pending_signatures = [signature for _, signature in pending_pairs]

        if not pending:
            self._save_cursor(
                session_id,
                _advanced_cursor(
                    state,
                    signatures,
                    [],
                    max_seen=self._config.max_seen_message_ids,
                    commit_pending=False,
                ),
            )
            return

        try:
            with self._actor_peer_scope(peer_id):
                self._recorder.record(
                    session_id,
                    pending,
                    peer_id=peer_id,
                )
        except self._partial_write_error as exc:
            consumed = max(
                0,
                min(
                    len(pending_signatures),
                    int(getattr(exc, "input_messages_consumed", 0)),
                ),
            )
            confirmed = pending_signatures[:consumed]
            commit_pending = bool(getattr(exc, "commit_pending", False))
            if confirmed or commit_pending:
                confirmed_prefix = signatures[: int(start or 0) + consumed] if append_only else None
                self._save_cursor(
                    session_id,
                    _advanced_cursor(
                        state,
                        confirmed_prefix,
                        confirmed,
                        max_seen=self._config.max_seen_message_ids,
                        commit_pending=commit_pending,
                    ),
                )
            self._handle_write_error(
                exc,
                "OpenViking partially recorded a conversation; confirmed progress was preserved",
                session_id,
            )
            return
        except Exception as exc:
            self._handle_write_error(
                exc,
                "OpenViking conversation recording failed; capture cursor was not advanced",
                session_id,
            )
            return

        self._save_cursor(
            session_id,
            _advanced_cursor(
                state,
                signatures,
                pending_signatures,
                max_seen=self._config.max_seen_message_ids,
                commit_pending=False,
            ),
        )

    def _resolve_scope(
        self,
        user_id: str | None,
        agent_name: str | None,
        requester_id: str | None = None,
    ) -> str:
        if self._config.memory_scope == "requester":
            # Requester-scoped memory: the *trusted requester* owns the peer
            # namespace inside the credential-bound employee, so several
            # humans on one instance never share one memory space. The
            # requester is published by governance (never taken from user_id,
            # which stays the storage owner). An unresolved requester fails
            # closed instead of falling back to a shared bucket.
            requester = str(requester_id or "").strip()
            if not requester or requester == "default":
                raise RequesterScopeError("OpenViking requester-scoped memory requires an authenticated requester; refusing to fall back to a shared memory space.")
            return _requester_peer_id(requester)
        resolved_user = str(user_id or "default")
        if resolved_user != self._config.owner_user_id:
            if self._config.single_tenant_instance:
                # Single-tenant instance: one credential, one memory tenant.
                # The runtime user varies per request (internal-auth owner
                # header carries the human requester), but the memory belongs
                # to the EMPLOYEE, not to the requester — every run of this
                # instance maps onto the configured owner. Cross-employee
                # isolation comes from the per-instance credential.
                logger.debug(
                    "single_tenant_instance: mapping runtime user %r onto OpenViking owner %r",
                    resolved_user,
                    self._config.owner_user_id,
                )
                return _canonical_peer_id(agent_name, self._config.default_peer_id)
            raise MemoryManagerError(f"OpenViking USER API key is bound to DeerFlow owner_user_id {self._config.owner_user_id!r}, but this request belongs to {resolved_user!r}. Refusing to share one credential across users.")
        return _canonical_peer_id(agent_name, self._config.default_peer_id)

    def _resolve_read_scope(
        self,
        user_id: str | None,
        agent_name: str | None,
        requester_id: str | None = None,
    ) -> str:
        try:
            return self._resolve_scope(user_id, agent_name, requester_id)
        except MemoryManagerError as exc:
            if self._config.read_failure_policy == "raise":
                raise MemoryReadError(str(exc)) from exc
            raise

    def _memory_target_uris_for(self, peer_id: str) -> list[str]:
        """Memory roots to read for the active scope mode."""

        return _memory_target_uris(
            peer_id,
            shared_peer_id=self._config.shared_peer_id,
            requester_scoped=self._config.memory_scope == "requester",
        )

    def _drop_deleted_documents(self, documents: list[Any], peer_id: str) -> list[Any]:
        """Drop recalled entries whose backing file is already gone.

        Retrieval reads the *index*, which can still hold an entry whose file was
        deleted — a delete that raced a pending index task, or an orphan that a
        rebuild keeps (``reindex`` rebuilds from the records it can see and
        reports ``deleted_records=0``). Such an entry still recalls the deleted
        content, which is exactly what "deleted memory must not be recalled"
        forbids.

        The filesystem is the authority for existence, so entries with a URI
        inside this scope are checked against their directory listing. Missing
        entries are dropped; a failed listing never permits unchecked text through. In
        fail-open mode the affected entries are omitted for this read only;
        strict read policy raises instead. Neither path deletes stored memory.
        Entries without a recognized in-scope URI retain legacy behavior.
        """

        if not documents or not self._config.drop_deleted_entries:
            return documents
        roots = self._memory_target_uris_for(peer_id)
        targets: list[tuple[int, str, str]] = []
        for index, document in enumerate(documents):
            uri = self._credential_alias(_document_uri(document))
            if "/" not in uri or not any(uri == root or uri.startswith(root + "/") for root in roots):
                continue
            parent, _, name = uri.rpartition("/")
            if parent and name:
                targets.append((index, parent, name))
        if not targets:
            return documents

        listed: dict[str, set[str] | None] = {}
        with self._actor_peer_scope(peer_id):
            for _index, parent, _name in targets:
                if parent not in listed:
                    listed[parent] = self._list_child_names(parent)

        keep = [True] * len(documents)
        for index, parent, name in targets:
            names = listed[parent]
            if names is None or name not in names:
                keep[index] = False
        dropped = keep.count(False)
        if not dropped:
            return documents
        logger.info(
            "Omitted %d recalled OpenViking entries without a verified backing file; %d kept",
            dropped,
            len(documents) - dropped,
        )
        return [document for index, document in enumerate(documents) if keep[index]]

    def _list_child_names(self, parent: str) -> set[str] | None:
        """Names directly under *parent*, or ``None`` when it cannot be read."""

        try:
            result = self._call_openviking(self._client, "ls", uri=parent, simple=True)
        except Exception as exc:
            if self._config.read_failure_policy == "raise":
                raise MemoryReadError("OpenViking memory existence verification failed") from exc
            logger.warning(
                "Could not list OpenViking memory directory %s; omitting unverified recalled entries",
                parent,
                exc_info=True,
            )
            return None
        return {self._credential_alias(uri).rstrip("/").rsplit("/", 1)[-1] for uri in _extract_uris(result)}

    def _actor_peer_scope(
        self,
        peer_id: str | None,
    ) -> AbstractContextManager[None]:
        return self._use_actor_peer(peer_id)

    def _audit_access(
        self,
        mode: str,
        *,
        user_id: str | None,
        agent_name: str | None,
        requester_id: str | None,
        hits: int,
        thread_id: str | None = None,
    ) -> None:
        """Leave a trace of one memory access.

        This is the audit slice of the memory governance contract: *who*
        touched memory, under *which scope*, and *how much* came back.

        Entry URIs are deliberately absent: the platform's log redaction
        collapses URL paths to ``/<redacted>`` (`deerflow.logging_config`), so
        they never reach the trail, and memory content is never logged at all —
        it can hold personal data, and a trail is not a second copy of the
        store. A queryable audit store could keep entry ids later; the log layer
        only carries what actually survives here.
        """

        parts = [
            "mode=" + mode,
            "scope=" + self._config.memory_scope,
            "user=" + (user_id or "-"),
            "agent=" + (agent_name or "-"),
            "requester=" + (requester_id or "-"),
            "hits=" + str(hits),
        ]
        if thread_id:
            parts.append("thread=" + thread_id)
        logger.info("memory access %s", " ".join(parts))

    def _session_lock(self, session_id: str) -> threading.RLock:
        with self._session_locks_guard:
            return self._session_locks.setdefault(
                session_id,
                threading.RLock(),
            )

    def _begin_operation(self) -> bool:
        with self._lifecycle:
            if self._closed:
                return False
            self._active_operations += 1
            return True

    def _end_operation(self) -> None:
        should_close = False
        with self._lifecycle:
            self._active_operations -= 1
            if self._active_operations == 0:
                self._lifecycle.notify_all()
                should_close = self._close_requested
        if should_close:
            try:
                self._close_resources()
            except Exception:
                logger.exception("Failed to close OpenViking memory resources")

    def _close_resources(self) -> None:
        with self._resource_lock:
            if self._resources_closed:
                return
            self._recorder.close()
            self._resources_closed = True

    def _state_path(self, session_id: str) -> Path:
        root = Path(self._config.storage_path or ".") / "openviking" / "sessions"
        return root / f"{session_id}.json"

    def _load_cursor(self, session_id: str) -> dict[str, Any]:
        path = self._state_path(session_id)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise MemoryManagerError(f"OpenViking capture cursor is unreadable; refusing unsafe replay (session={session_id})") from exc
        if not isinstance(value, dict):
            raise MemoryManagerError(f"OpenViking capture cursor is invalid; refusing unsafe replay (session={session_id})")
        return value

    def _save_cursor(self, session_id: str, state: dict[str, Any]) -> None:
        path = self._state_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            temp_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temp_path, path)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                logger.debug(
                    "Failed to remove OpenViking cursor temp file: %s",
                    temp_path,
                    exc_info=True,
                )

    def _handle_write_error(
        self,
        exc: Exception,
        message: str,
        session_id: str,
    ) -> None:
        detail = f"{message} (session={session_id})"
        if self._config.write_failure_policy == "raise":
            raise MemoryManagerError(detail) from exc
        logger.error(detail, exc_info=True)


def _load_official_integration() -> dict[str, Any]:
    try:
        from langchain_openviking import (
            OpenVikingCommitPolicy,
            OpenVikingPartialWriteError,
            OpenVikingRetriever,
            OpenVikingSessionRecorder,
            has_request_actor_peer_support,
        )
        from langchain_openviking.actor_peer import use_actor_peer
        from langchain_openviking.client import call_openviking
    except ImportError as exc:
        raise ImportError("The OpenViking memory backend requires langchain-openviking==0.1.0. Install DeerFlow backend dependencies and retry.") from exc
    if not has_request_actor_peer_support():
        raise ImportError("The installed OpenViking SDK lacks request-scoped actor-peer support. Install openviking-sdk>=0.1.6,<0.2 and retry.")
    return {
        "OpenVikingCommitPolicy": OpenVikingCommitPolicy,
        "OpenVikingPartialWriteError": OpenVikingPartialWriteError,
        "OpenVikingRetriever": OpenVikingRetriever,
        "OpenVikingSessionRecorder": OpenVikingSessionRecorder,
        "use_actor_peer": use_actor_peer,
        "call_openviking": call_openviking,
    }


def _format_documents(documents: list[Any], *, max_chars: int) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for document in documents:
        content = " ".join(str(getattr(document, "page_content", "") or "").split())
        key = content.casefold()
        if not content or key in seen:
            continue
        seen.add(key)
        metadata = getattr(document, "metadata", {}) or {}
        category = metadata.get("openviking_category") or "memory"
        line = f"- [{category}] {content}"
        candidate = "\n".join([*lines, line])
        if len(candidate) > max_chars:
            remaining = max_chars - len("\n".join(lines)) - (1 if lines else 0)
            if remaining > 16:
                lines.append(f"{line[: max(0, remaining - 1)]}…")
            break
        lines.append(line)
    return "\n".join(lines)


def _document_uri(document: Any) -> str:
    """The OpenViking URI a retrieved document points at, if any."""

    metadata = getattr(document, "metadata", {}) or {}
    uri = metadata.get("openviking_uri") or metadata.get("source")
    return uri if isinstance(uri, str) else ""


def _document_to_fact(document: Any) -> dict[str, Any]:
    metadata = getattr(document, "metadata", {}) or {}
    uri = _document_uri(document)
    score = metadata.get("openviking_score")
    return {
        "id": uri,
        "content": str(getattr(document, "page_content", "") or ""),
        "category": metadata.get("openviking_category") or "memory",
        "confidence": score,
        "source": uri,
        "score": score,
    }


def _fact_content_key(content: str) -> str:
    """The normalization every layer already compares fact content with.

    ``memory_add``'s fast-path duplicate check and the DeerMem backend both use
    ``str.strip().casefold()`` (``_memory_content_key`` / ``_fact_content_key``).
    Deriving a fact's URI from the same key is what keeps the duplicate check a
    caller performs before writing and the identity the store ends up with from
    disagreeing.
    """

    return content.strip().casefold()


def _fact_file_name(content: str) -> str:
    """A stable, path-safe OpenViking file name for one fact's content.

    Content-derived rather than random: the same fact maps to the same URI for
    every writer and process, which is what turns a concurrent duplicate into a
    rejected create instead of a second entry. The digest also keeps raw text out
    of the URI -- OpenViking rewrites characters such as a space in a written
    path (``fact-f 空格.md`` is stored as ``fact-f_空格.md`` and the requested URI
    then no longer reads back), so a name built from content could never be
    trusted as the fact id. ``.md`` is required: OpenViking refuses a new file
    without one of its known text extensions.
    """

    digest = hashlib.sha256(_fact_content_key(content).encode("utf-8")).hexdigest()[:32]
    return f"df-fact-{digest}.md"


def _validate_confidence(confidence: Any) -> float:
    """Reject a confidence outside the range the fact contract allows.

    Mirrors the DeerMem backend's validation -- including rejecting a ``bool``
    that would otherwise pass as ``0``/``1`` -- but reports a ValueError instead
    of raising TypeError out of ``isfinite`` for a non-numeric value.
    """

    if isinstance(confidence, bool) or not isinstance(confidence, int | float) or not isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence")
    return float(confidence)


def _extract_uris(value: Any) -> list[str]:
    """Collect ``viking://`` URIs from a glob/ls result of any nesting."""

    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.startswith("viking://") else []
    if isinstance(value, dict):
        if isinstance(value.get("uri"), str):
            return [value["uri"]]
        uris: list[str] = []
        for key in ("matches", "result", "files", "items"):
            uris.extend(_extract_uris(value.get(key)))
        return uris
    if isinstance(value, list):
        nested: list[str] = []
        for item in value:
            nested.extend(_extract_uris(item))
        return nested
    return []


__all__ = [
    "OpenVikingMemoryManager",
    "_canonical_peer_id",
    "_session_id",
]
