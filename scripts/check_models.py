"""Check every model in MODEL_REGISTRY against the live Anthropic API.

Model ids cannot be validated offline: ChatAnthropic accepts any string at
construction, so a wrong id surfaces only as a 404 on the first real request —
past the test suite, past CI, and past the app's "Apply Settings" step.

    uv run python scripts/check_models.py

Reads ANTHROPIC_API_KEY from the environment or from a .env file.
Exits non-zero if any registered model is missing.
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

from mcp_agent.models import MODEL_REGISTRY


def main() -> int:
    load_dotenv()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set. Put it in .env or export it.")
        return 2

    try:
        import anthropic
    except ImportError:
        print("The anthropic package is missing. Run: uv sync")
        return 2

    try:
        live = {m.id for m in anthropic.Anthropic().models.list(limit=1000).data}
    except Exception as exc:
        print(f"Could not reach the Anthropic API: {exc}")
        return 2

    missing = [m for m in MODEL_REGISTRY if m not in live]

    print("Registered models:")
    for model_id in MODEL_REGISTRY:
        mark = "ok  " if model_id in live else "MISS"
        print(f"  [{mark}] {model_id}")

    if missing:
        print(f"\n{len(missing)} model id(s) not available on this account.")
        print("Available ids:")
        for model_id in sorted(live):
            print(f"  {model_id}")
        print("\nFix: edit MODEL_REGISTRY in src/mcp_agent/models.py")
        return 1

    print("\nAll registered model ids exist. The registry is verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
