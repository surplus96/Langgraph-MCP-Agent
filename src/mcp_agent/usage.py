"""Token accounting.

A leaf module on purpose: this is four integers and some arithmetic, and
importing it must not drag in the agent stack. Session state needs it too.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Self

from langchain_core.messages.ai import UsageMetadata

logger = logging.getLogger(__name__)

#: Per-TTL cache-write keys langchain-anthropic emits instead of the generic one.
_TTL_CACHE_WRITE_KEYS = ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")


def _as_int(field: str, value: object) -> int:
    """Coerce one usage field to an int.

    Accounting must never be able to fail a turn, so an unexpected value is
    logged and dropped rather than raised.
    """
    if value is None:
        return 0
    if isinstance(value, bool):  # bool is an int subclass; never a token count
        logger.warning("Ignoring boolean usage field %r", field)
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float | str):
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    logger.warning("Ignoring non-numeric usage field %r=%r", field, value)
    return 0


@dataclass(frozen=True)
class TokenUsage:
    """Token counts for one turn or one session.

    ``input_tokens`` is the true total including cached tokens: Anthropic
    reports cached tokens separately, and langchain-anthropic folds them back
    in. So ``cache_read / input_tokens`` is the share served from cache.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_creation: int = 0

    @classmethod
    def from_metadata(cls, metadata: UsageMetadata | None) -> Self:
        """Read one usage record, tolerating missing and malformed fields."""
        if not metadata:
            return cls()

        # Read the details through a plain dict: the per-TTL keys below are
        # optional extras rather than declared members of the TypedDict.
        raw_details = metadata.get("input_token_details") or {}
        details = {key: _as_int(key, value) for key, value in dict(raw_details).items()}

        # When Anthropic returns a per-TTL breakdown, langchain-anthropic zeroes
        # the generic `cache_creation` key and reports the split instead, so as
        # not to double count. Reading only the generic key therefore loses the
        # entire cache-write figure. The streaming path does not hit this today
        # (MessageDeltaUsage carries no breakdown) but any non-streaming call in
        # the pipeline would, and a 1h or mixed TTL makes it routine.
        cache_creation = details.get("cache_creation") or 0
        if not cache_creation:
            cache_creation = sum(details.get(key, 0) for key in _TTL_CACHE_WRITE_KEYS)

        totals = dict(metadata)
        return cls(
            input_tokens=_as_int("input_tokens", totals.get("input_tokens")),
            output_tokens=_as_int("output_tokens", totals.get("output_tokens")),
            cache_read=details.get("cache_read", 0),
            cache_creation=cache_creation,
        )

    def __add__(self, other: object) -> TokenUsage:
        if other == 0:  # so sum() works, since it starts from the int 0
            return self
        if not isinstance(other, TokenUsage):
            return NotImplemented
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read=self.cache_read + other.cache_read,
            cache_creation=self.cache_creation + other.cache_creation,
        )

    __radd__ = __add__

    @property
    def uncached_input(self) -> int:
        """Input tokens billed at full rate.

        With correct metadata ``input == uncached + read + creation`` holds
        exactly, so a negative result means the counts disagree. Clamping keeps
        the display sane; the log line is what makes the defect findable.
        """
        remainder = self.input_tokens - self.cache_read - self.cache_creation
        if remainder < 0:
            logger.warning(
                "Token counts do not reconcile: input=%d read=%d creation=%d",
                self.input_tokens,
                self.cache_read,
                self.cache_creation,
            )
            return 0
        return remainder

    @property
    def cache_hit_rate(self) -> float:
        """Share of input *tokens* served from cache, 0.0-1.0.

        Not a share of cost: cache reads still bill at roughly a tenth of the
        normal rate, so a 90% hit rate saves rather less than 90% of the input
        bill.
        """
        if not self.input_tokens:
            return 0.0
        return self.cache_read / self.input_tokens
