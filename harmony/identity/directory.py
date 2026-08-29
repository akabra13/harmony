"""The user directory: who exists, who they report to, who covers for them.

In this build the directory is backed by seed data. In a real deployment it is
Entra ID or Workday, and the interface is what would not change — which is the
point of stating it as a protocol rather than reaching into a table.

Two organisational relationships matter to the harness and are therefore part of
the interface rather than left in a payload somewhere:

``manager_id``
    Where an approval escalates when it exceeds someone's authority.

``backup_approver_id``
    Where an approval routes when the approver is unavailable. The brief's rule —
    unanswered at end of day, approver out tomorrow — needs a designated human, not
    a search.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from harmony.identity.models import Principal


@runtime_checkable
class UserDirectory(Protocol):
    """Lookup of people and their organisational relationships."""

    def get(self, user_id: str) -> Principal: ...

    def try_get(self, user_id: str) -> Principal | None: ...

    def all(self) -> list[Principal]: ...


def manager_chain(directory: UserDirectory, user_id: str, *, limit: int = 10) -> list[str]:
    """Ids from ``user_id`` up to the top of the reporting line.

    Bounded, and it refuses to revisit anyone: a directory with a reporting cycle is
    a data problem that should surface as a short chain rather than as a hang inside
    an approval decision.
    """
    chain: list[str] = []
    seen: set[str] = set()
    current: str | None = user_id
    while current and current not in seen and len(chain) < limit:
        chain.append(current)
        seen.add(current)
        person = directory.try_get(current)
        current = person.manager_id if person else None
    return chain


def most_senior(directory: UserDirectory, user_ids: list[str]) -> str:
    """Pick the most senior of several candidate approvers.

    Used when more than one gate rule demands approval. Seniority is decided by the
    reporting line: if one candidate appears in another's management chain, they are
    the more senior. Where the candidates are unrelated, the one with the longest
    chain to the top wins — an arbitrary but stable tie-break, and the audit records
    every rule's demand so the choice is inspectable either way.
    """
    if not user_ids:
        raise ValueError("no candidate approvers")
    unique = list(dict.fromkeys(user_ids))
    if len(unique) == 1:
        return unique[0]

    chains = {uid: manager_chain(directory, uid) for uid in unique}
    for candidate in unique:
        others = [uid for uid in unique if uid != candidate]
        if all(candidate in chains[other] for other in others):
            return candidate
    return max(unique, key=lambda uid: (len(chains[uid]), uid))
