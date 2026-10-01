from pathlib import Path

from pydantic import Field

from app.schemas.base import StrictModel


class Sender(StrictModel):
    """Who is writing and what they can honestly claim.

    The writer may only state outcomes listed here: they are the sender's real, provable
    results. Nothing about the sender is ever invented by a model.
    """

    name: str = Field(min_length=1)
    company: str = Field(min_length=1)
    offer: str = Field(min_length=1, description="What you sell, in one or two plain sentences.")
    outcomes: list[str] = Field(
        min_length=1,
        description="Real results you can prove, e.g. 'cut onboarding time 40% for Acme'.",
    )
    ask: str = Field(
        default="Open to a 15-minute call next week?",
        description="The low-friction ask the email ends with.",
    )

    @property
    def is_placeholder(self) -> bool:
        """True for the committed example profile, which must never write real emails."""
        return self.name == "Your Name" or any("EXAMPLE ONLY" in o for o in self.outcomes)


def load_sender(path: Path) -> Sender:
    return Sender.model_validate_json(path.read_text(encoding="utf-8"))
