"""Service identities and human principals (SECURITY_MODEL.md sections 5 and 6.1).

- `operator` (host CLI through `docker compose exec`) always acts as the
  principal `host-cli:operator`.
- `hermes-plugin` must forward the human principal in `X-HO-Principal`.
  The plugin only does so from human-only surfaces (slash commands,
  Dashboard), never from LLM-callable tools (AD-10).
"""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass

from .errors import BadRequest, Unauthorized

PRINCIPAL_HEADER = "X-HO-Principal"
_PRINCIPAL = re.compile(r"^[a-z][a-z0-9_-]{0,31}:[^\s]{1,200}$")
# Channels the plugin may assert. host-cli is reserved for the operator identity.
_PLUGIN_CHANNELS_FORBIDDEN = {"host-cli"}


@dataclass(frozen=True)
class Identity:
    name: str


@dataclass(frozen=True)
class Principal:
    channel: str
    subject: str

    @property
    def value(self) -> str:
        return f"{self.channel}:{self.subject}"

    def as_json(self) -> dict[str, str]:
        return {"channel": self.channel, "subject": self.subject}

    @classmethod
    def parse(cls, value: str) -> "Principal":
        if not _PRINCIPAL.match(value):
            raise BadRequest("principal must look like channel:subject")
        channel, subject = value.split(":", 1)
        return cls(channel, subject)


class Authenticator:
    def __init__(self, tokens: dict[str, str]) -> None:
        self._tokens = dict(tokens)

    def identify(self, authorization: str | None) -> Identity:
        if not authorization or not authorization.startswith("Bearer "):
            raise Unauthorized("missing bearer token")
        presented = authorization.removeprefix("Bearer ").strip().encode()
        match: str | None = None
        for token, identity in self._tokens.items():
            # Compare against every token so timing does not reveal which matched.
            if hmac.compare_digest(presented, token.encode()):
                match = identity
        if match is None:
            raise Unauthorized("invalid token")
        return Identity(match)

    @staticmethod
    def principal(identity: Identity, header: str | None) -> Principal:
        if identity.name == "operator":
            return Principal("host-cli", "operator")
        if not header:
            raise Unauthorized(f"{PRINCIPAL_HEADER} is required for this action")
        principal = Principal.parse(header)
        if principal.channel in _PLUGIN_CHANNELS_FORBIDDEN:
            raise Unauthorized(f"channel {principal.channel} cannot be asserted by {identity.name}")
        return principal
