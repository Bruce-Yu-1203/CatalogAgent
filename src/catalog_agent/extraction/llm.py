"""Model client boundary. The runtime talks to models only through `ModelClient`.

`LangChainModelClient` wraps a LangChain chat model (provider/model from the environment). Tests use
scripted fakes that implement the same protocol, so no test calls a paid API.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field

from catalog_agent.config import Secrets


class ToolCall(BaseModel):
    id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class ModelReply(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


class ModelUnavailable(RuntimeError):
    pass


class ModelClient(Protocol):
    model_id: str

    async def invoke(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply: ...


class LangChainModelClient:
    def __init__(
        self,
        provider: str,
        model: str,
        api_key: str,
        max_output_tokens: int,
        timeout_s: float = 60,
        effort: str = "low",
    ):
        if provider.lower() != "anthropic":
            raise ModelUnavailable(f"provider {provider!r} is not installed in this build (anthropic only)")
        from langchain_anthropic import ChatAnthropic

        self.model_id = f"{provider}:{model}"
        # Current Claude models think adaptively and thinking counts toward max_tokens, so the
        # output cap must leave room for it; low effort keeps this copy-and-cite task cheap.
        self._llm = ChatAnthropic(
            model=model,
            api_key=api_key,
            max_tokens=max_output_tokens,
            timeout=timeout_s,
            max_retries=1,
            output_config={"effort": effort},
        )

    async def invoke(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

        lc: list[Any] = []
        for m in messages:
            role = m["role"]
            if role == "system":
                lc.append(SystemMessage(m["content"]))
            elif role == "user":
                lc.append(HumanMessage(m["content"]))
            elif role == "assistant":
                lc.append(AIMessage(content=m.get("content") or "", tool_calls=m.get("tool_calls", [])))
            elif role == "tool":
                lc.append(ToolMessage(content=m["content"], tool_call_id=m["tool_call_id"]))
        resp = await self._llm.bind_tools(tools).ainvoke(lc)
        usage = getattr(resp, "usage_metadata", None) or {}
        text = resp.content if isinstance(resp.content, str) else ""
        return ModelReply(
            text=text,
            tool_calls=[ToolCall(id=tc["id"], name=tc["name"], args=tc.get("args") or {}) for tc in resp.tool_calls],
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
        )


def model_from_env(max_output_tokens: int) -> ModelClient:
    s = Secrets()
    if not (s.llm_provider and s.llm_model and s.llm_api_key):
        raise ModelUnavailable("LLM_PROVIDER, LLM_MODEL and LLM_API_KEY are not all set")
    return LangChainModelClient(s.llm_provider, s.llm_model, s.llm_api_key, max_output_tokens)
