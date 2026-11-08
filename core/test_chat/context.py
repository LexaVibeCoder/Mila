"""Turn-scoped sandbox descriptor passed from the test runner to the executor."""

from dataclasses import dataclass


@dataclass
class SandboxContext:
    """Identity of one Observatory test session.

    The executor uses it to write the session diary, switch the session state
    and resolve the only chat that exists in the sandbox. No global DB row
    (presence/chats/chat_aliases) is involved.
    """

    session_id: str
    chat_id: str
    alias: str
    user_name: str
    user_id: str
    state: str = "dialog"
