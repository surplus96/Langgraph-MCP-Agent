"""A chat model that answers from a script.

`GenericFakeChatModel` cannot do this: it has no `bind_tools`, and it streams
by splitting message content, so an AIMessage carrying tool calls and no text
produces no generations at all. Both were hit before this existed.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult


class ScriptedModel(BaseChatModel):
    """Returns `replies` in order, one per model call, repeating the last."""

    replies: list[AIMessage]
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self

    def _generate(
        self,
        messages: list,
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._next())])

    def _stream(
        self,
        messages: list,
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        """Stream the scripted reply as a real model would.

        Load-bearing, not decoration. Without it LangGraph's `messages` mode
        yields whole `AIMessage` objects, `StreamAccumulator` sees no
        `AIMessageChunk` and reports empty text, and a test written against
        that would be pinning the fake instead of the code.
        """
        reply = self._next()
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content=reply.content,
                tool_call_chunks=[
                    {
                        "name": call["name"],
                        "args": json.dumps(call["args"]),
                        "id": call["id"],
                        "index": position,
                        "type": "tool_call_chunk",
                    }
                    for position, call in enumerate(reply.tool_calls or [])
                ],
            )
        )

    def _next(self) -> AIMessage:
        index = min(self.calls, len(self.replies) - 1)
        self.calls += 1
        return self.replies[index]
