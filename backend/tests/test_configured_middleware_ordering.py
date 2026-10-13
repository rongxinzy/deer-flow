"""A configured extension middleware must honor its ordering anchor.

Governance publishes the trusted requester; memory's first recall runs inside
``DynamicContextMiddleware.before_agent``. If configured extensions only ever
landed on the tail, that recall would run before the publish and see an
unresolved requester — a silent "no memory" failure in requester scope.
"""

from __future__ import annotations

from langchain.agents.middleware import AgentMiddleware

from deerflow.agents.lead_agent.agent import _attach_configured_middlewares
from deerflow.agents.middlewares.clarification_middleware import ClarificationMiddleware


class _DynamicContext(AgentMiddleware):
    pass


class _Governance(AgentMiddleware):
    pass


class _Unanchored(AgentMiddleware):
    pass


def test_anchored_configured_middleware_lands_before_its_anchor() -> None:
    _Governance._prev_anchor = _DynamicContext  # type: ignore[attr-defined]
    try:
        chain: list[AgentMiddleware] = [_DynamicContext(), ClarificationMiddleware()]
        _attach_configured_middlewares(chain, [_Governance()])
    finally:
        del _Governance._prev_anchor  # type: ignore[attr-defined]

    order = [type(mw) for mw in chain]
    assert order.index(_Governance) < order.index(_DynamicContext)


def test_unanchored_configured_middleware_keeps_tail_placement() -> None:
    chain: list[AgentMiddleware] = [_DynamicContext()]

    _attach_configured_middlewares(chain, [_Unanchored()])

    assert [type(mw) for mw in chain] == [_DynamicContext, _Unanchored]
