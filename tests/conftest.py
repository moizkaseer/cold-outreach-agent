import logfire
import pytest


@pytest.fixture(autouse=True, scope="session")
def _logfire_local_only() -> None:
    # Spans are created in-process but never sent anywhere during tests.
    logfire.configure(send_to_logfire=False, console=False)
