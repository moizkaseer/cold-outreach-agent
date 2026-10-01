from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """Base for every agent input/output.

    extra="forbid": if an LLM invents a field, validation fails (and we retry) instead of
    silently dropping it — a hallucinated field is a signal, not noise.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
