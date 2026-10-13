"""A configured extension middleware must honor its ordering anchor.

Governance publishes the trusted requester; memory's first recall runs inside
``DynamicContextMiddleware.before_agent``. Configured extensions
(``extensions.middlewares``) are attached *before* ``ClarificationMiddleware``
joins the chain, so anchoring must work at that point and must not depend on
the clarification middleware being present.
"""

from __future__ import annotations

from langchain.agents.middleware import AgentMiddleware

from deerflow.agents.lead_agent.agent import _attach_configured_middlewares


class _DynamicContext(AgentMiddleware):
    pass


class _Governance(AgentMiddleware):
    pass


class _Unanchored(AgentMiddleware):
    pass


def test_anchored_extension_lands_before_its_anchor_without_clarification() -> None:
    """The real production order: ClarificationMiddleware is not on the chain yet."""
    _Governance._prev_anchor = _DynamicContext  # type: ignore[attr-defined]
    try:
        chain: list[AgentMiddleware] = [_DynamicContext()]
        _attach_configured_middlewares(chain, [_Governance()])
    finally:
        del _Governance._prev_anchor  # type: ignore[attr-defined]

    order = [type(mw) for mw in chain]
    assert order.index(_Governance) < order.index(_DynamicContext)


def test_unanchored_extensions_keep_tail_placement() -> None:
    chain: list[AgentMiddleware] = [_DynamicContext()]

    _attach_configured_middlewares(chain, [_Unanchored()])

    assert [type(mw) for mw in chain] == [_DynamicContext, _Unanchored]
