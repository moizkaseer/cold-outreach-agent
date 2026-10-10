"""Outbound email. Deliberately NOT under app/tools or app/agents: no agent can reach
this code (a test enforces it). Only the API calls it, after a human approved the email."""

from app.sending.provider import EmailSender, OutboundEmail, ResendSender, SendError

__all__ = ["EmailSender", "OutboundEmail", "ResendSender", "SendError"]
