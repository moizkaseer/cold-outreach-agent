"""Tracing via Logfire (OpenTelemetry under the hood, so traces aren't vendor-locked).

What is traced: one span per pipeline run and step, per agent run, per LLM call and per
tool call, carrying METADATA only — agent, model, turn, stop reason, token counts,
estimated cost, tool name, error flag. Prompts and fetched page text are deliberately
NOT sent: they can be large and contain scraped third-party content. (This is also why
we don't use logfire.instrument_anthropic, which records full messages.)

With no LOGFIRE_TOKEN configured, nothing leaves the process.
"""

from __future__ import annotations

import logfire
from pydantic import SecretStr


def setup_tracing(service_name: str, token: SecretStr | None) -> None:
    logfire.configure(
        service_name=service_name,
        token=token.get_secret_value() if token else None,
        send_to_logfire="if-token-present",
        console=False,
    )
