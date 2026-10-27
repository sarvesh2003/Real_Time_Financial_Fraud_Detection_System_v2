"""Deterministic stand-ins for the model and the MCP tools.

These exercise control flow only. Nothing here measures model quality.
"""

import json

from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool

FINAL_PROMPT_MARKER = "Respond with ONLY"


def tool_call(name, args=None):
    return ("tool", name, args or {})


CONCLUDE = ("conclude",)


class ScriptedModel:
    """Returns scripted actions to `investigate` and a scripted final report.

    An action is tool_call(...), CONCLUDE, an AIMessage, or an exception to
    raise. `conclusion` is a dict (sent as JSON), a raw string, or an exception.
    """

    def __init__(self, actions=(), conclusion=None):
        self.actions = list(actions)
        self.conclusion = conclusion if conclusion is not None else {
            "root_cause_id": "none", "summary": "No supported issue.", "confidence": "inconclusive",
            "reasoning": "Baseline looked healthy.", "cited_evidence_indices": [0, 1]}
        self.calls = 0

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        self.calls += 1
        if FINAL_PROMPT_MARKER in str(messages[-1].content):
            if isinstance(self.conclusion, BaseException):
                raise self.conclusion
            content = self.conclusion if isinstance(self.conclusion, str) else json.dumps(self.conclusion)
            return AIMessage(content=content, response_metadata={"finish_reason": "stop"})
        step = self.actions.pop(0) if self.actions else CONCLUDE
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, AIMessage):
            return step
        if step[0] == "conclude":
            return AIMessage(content="", tool_calls=[
                {"name": "ConcludeInvestigation", "args": {"reason": "enough evidence"}, "id": "c"}
            ], response_metadata={"finish_reason": "tool_calls"})
        return AIMessage(content="", tool_calls=[{"name": step[1], "args": step[2], "id": "t"}],
                         response_metadata={"finish_reason": "tool_calls"})


class FakeTools:
    """The five tool names with canned replies; counts real invocations."""

    def __init__(self, logs="go-enricher-1 | enricher stopped: CLUSTERDOWN", fail=()):
        self.invocations = []
        self.replies = {
            "get_container_state": "redis2: state=exited, status=\"Exited (0)\"\ngo-server: state=running",
            "get_metrics": 'job="redis" -> latest 1000:0',
            "query_logs": logs,
            "list_metric_names": "fraud_server_transactions_total",
            "read_config": "maxmemory = 0",
        }
        self.fail = set(fail)

    def build(self, names=None):
        tools = []
        for name in names or self.replies:
            tools.append(StructuredTool.from_function(
                coroutine=self._coroutine(name), name=name, description=f"fake {name}"))
        return tools

    def _coroutine(self, name):
        async def call(**kwargs):
            self.invocations.append((name, kwargs))
            if name in self.fail:
                raise RuntimeError(f"{name} backend unavailable")
            return self.replies[name]

        # StructuredTool infers the argument schema from the signature.
        signatures = {
            "get_container_state": "async def f():\n    return await call()",
            "get_metrics": "async def f(promql: str, window: str):\n    return await call(promql=promql, window=window)",
            "query_logs": "async def f(service: str, since: str):\n    return await call(service=service, since=since)",
            "list_metric_names": "async def f(contains: str = ''):\n    return await call(contains=contains)",
            "read_config": "async def f(service: str):\n    return await call(service=service)",
        }
        namespace = {"call": call}
        exec(signatures[name], namespace)
        return namespace["f"]


def initial_state(trigger="Assess current health"):
    return {"trigger": trigger, "evidence_log": [], "calls_used": 0,
            "pending_action": None, "conclusion": None, "trace": []}


def report(indices, confidence="confirmed", root="redis-node-stopped"):
    return {"root_cause_id": root, "summary": "redis2 is stopped.", "confidence": confidence,
            "reasoning": "Container state and logs agree.", "cited_evidence_indices": indices}
