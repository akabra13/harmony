"""Northfield's user directory, backed by the seeded ``users`` table.

Implements :class:`harmony.identity.directory.UserDirectory`. In a real deployment
this class is replaced by one that reads Entra ID and nothing above it changes,
which is the whole reason the harness talks to an interface here rather than to a
table.
"""

from __future__ import annotations

from harmony.identity.models import ApprovalLimits, Principal, PrincipalKind, ScopeSet
from harmony.kernel.errors import NotRegistered
from harmony.kernel.store import Store, load_json


class NorthfieldDirectory:
    """People, from the ERP's user table."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def get(self, user_id: str) -> Principal:
        principal = self.try_get(user_id)
        if principal is None:
            raise NotRegistered(f"no user '{user_id}' in the directory", user_id=user_id)
        return principal

    def try_get(self, user_id: str) -> Principal | None:
        row = self._store.query_one("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return self._to_principal(row) if row else None

    def all(self) -> list[Principal]:
        return [
            self._to_principal(r)
            for r in self._store.query("SELECT * FROM users ORDER BY user_id")
        ]

    def by_email(self, email: str) -> Principal | None:
        row = self._store.query_one("SELECT * FROM users WHERE email = ?", (email,))
        return self._to_principal(row) if row else None

    @staticmethod
    def _to_principal(row) -> Principal:
        return Principal(
            id=row["user_id"],
            kind=PrincipalKind.USER,
            name=row["name"],
            email=row["email"],
            role=row["role"],
            manager_id=row["manager_id"],
            backup_approver_id=row["backup_approver_id"],
            scopes=ScopeSet(load_json(row["scopes"], [])),
            approval_limits=ApprovalLimits(**load_json(row["approval_limits"], {})),
        )


def directory_factory(store: Store) -> NorthfieldDirectory:
    return NorthfieldDirectory(store)
