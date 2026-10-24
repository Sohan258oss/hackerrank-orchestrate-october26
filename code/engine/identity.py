"""Stage 2: resolve a request email to a user and every identifier they have used.

Matching uses both raw values and SHA-256 hashes: user_identities.email_hash
and the audited email_hash values are sha256(lowercase(email)), so a request
email can be found through its hash even where only hashes were recorded.

`resolve(email, as_of)` reconstructs identifiers as known at `as_of`
(normally the case's audit_at): changes logged after `as_of` are rolled back
and their new values excluded, while every earlier value is kept because old
identifiers can survive in systems that were never cleaned.
"""
from __future__ import annotations

import csv
import hashlib
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ID_FIELDS = ("email", "phone", "email_hash")


def norm_email(value: str) -> str:
    return value.strip().lower()


def email_sha256(value: str) -> str:
    return hashlib.sha256(norm_email(value).encode("utf-8")).hexdigest()


def phone_digits(value: str) -> str:
    return re.sub(r"\D", "", value)


@dataclass
class Identity:
    user_id: str
    request_email: str
    matched_via: list[str]                 # current_email | historical_email | email_hash
    name: str
    email_at: str                          # email in effect at as_of
    phone_at: str
    emails: list[str]                      # every known email, email_at first
    phones: list[str]
    phone_digits: list[str]
    hashes: list[str]
    names: list[str]
    history: list[dict[str, str]] = field(default_factory=list)   # audit rows <= as_of
    ignored_future_changes: int = 0        # audit rows after as_of

    def identifiers(self) -> set[str]:
        """Flat set for matching evidence: lower-cased emails and hashes, phones, user_id."""
        return {self.user_id, *self.emails, *self.hashes, *self.phones, *self.phone_digits}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Resolution:
    status: str                            # resolved | ambiguous | not_found
    request_email: str
    candidates: list[Identity]

    @property
    def identity(self) -> Identity | None:
        return self.candidates[0] if self.status == "resolved" else None


class IdentityIndex:
    def __init__(self, dataset: Path) -> None:
        with (dataset / "user_identities.csv").open(encoding="utf-8", newline="") as fh:
            self.users = {r["user_id"]: r for r in csv.DictReader(fh)}
        self.changes: dict[str, list[dict[str, str]]] = defaultdict(list)
        with (dataset / "user_audit_log.csv").open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                self.changes[row["user_id"]].append(row)
        for rows in self.changes.values():
            rows.sort(key=lambda r: (r["event_time"], r["audit_id"]))

        self.by_email: dict[str, dict[str, str]] = defaultdict(dict)   # email -> {user_id: how}
        self.by_hash: dict[str, set[str]] = defaultdict(set)
        for uid, user in self.users.items():
            self.by_email[norm_email(user["email"])][uid] = "current_email"
            self.by_hash[user["email_hash"].lower()].add(uid)
            self.by_hash[email_sha256(user["email"])].add(uid)
        for uid, rows in self.changes.items():
            for row in rows:
                if row["field_name"] == "email":
                    for value in (row["old_value"], row["new_value"]):
                        self.by_email[norm_email(value)].setdefault(uid, "historical_email")
                        self.by_hash[email_sha256(value)].add(uid)
                elif row["field_name"] == "email_hash":
                    for value in (row["old_value"], row["new_value"]):
                        self.by_hash[value.lower()].add(uid)

        # Any identifier ever held -> owning users; used to attribute evidence to people.
        self.owner_by_value: dict[str, set[str]] = defaultdict(set)
        for uid, user in self.users.items():
            for value in self.all_values(uid):
                self.owner_by_value[value].add(uid)

    def all_values(self, user_id: str) -> set[str]:
        """Every identifier the user has held at any time, normalized for matching."""
        user = self.users[user_id]
        values = {user_id, norm_email(user["email"]), user["email_hash"].lower(), email_sha256(user["email"])}
        phones = {user["phone"]}
        for row in self.changes.get(user_id, []):
            for value in (row["old_value"], row["new_value"]):
                if row["field_name"] == "email":
                    values |= {norm_email(value), email_sha256(value)}
                elif row["field_name"] == "email_hash":
                    values.add(value.lower())
                elif row["field_name"] == "phone":
                    phones.add(value)
        return values | {phone_digits(p) for p in phones if phone_digits(p)}

    def owners(self, values: set[str]) -> set[str]:
        out: set[str] = set()
        for value in values:
            out |= self.owner_by_value.get(value, set())
        return out

    def resolve(self, email: str, as_of: str | None = None) -> Resolution:
        e = norm_email(email)
        matches: dict[str, list[str]] = defaultdict(list)
        for uid, how in self.by_email.get(e, {}).items():
            matches[uid].append(how)
        for uid in self.by_hash.get(email_sha256(e), set()):
            matches[uid].append("email_hash")
        candidates = [self.build(uid, e, sorted(set(hows)), as_of) for uid, hows in sorted(matches.items())]
        status = "not_found" if not candidates else "resolved" if len(candidates) == 1 else "ambiguous"
        return Resolution(status, e, candidates)

    def build(self, user_id: str, request_email: str, matched_via: list[str], as_of: str | None) -> Identity:
        user = self.users[user_id]
        rows = self.changes.get(user_id, [])
        past = [r for r in rows if as_of is None or r["event_time"] <= as_of]
        future = [r for r in rows if as_of is not None and r["event_time"] > as_of]

        # Roll the current snapshot back past any change made after as_of.
        at = {f: user[f] for f in ID_FIELDS}
        for row in sorted(future, key=lambda r: r["event_time"], reverse=True):
            f = row["field_name"]
            if f in at and at[f].strip().lower() == row["new_value"].strip().lower():
                at[f] = row["old_value"]

        emails = [norm_email(at["email"])]
        phones = [at["phone"]]
        hashes = [at["email_hash"].lower()]
        for row in past:
            for value in (row["old_value"], row["new_value"]):
                if row["field_name"] == "email":
                    emails.append(norm_email(value))
                elif row["field_name"] == "phone":
                    phones.append(value)
                elif row["field_name"] == "email_hash":
                    hashes.append(value.lower())
        emails.append(request_email)
        emails = list(dict.fromkeys(emails))
        phones = list(dict.fromkeys(phones))
        hashes = list(dict.fromkeys(hashes + [email_sha256(x) for x in emails]))
        return Identity(
            user_id=user_id, request_email=request_email, matched_via=matched_via,
            name=user["name"], email_at=emails[0], phone_at=at["phone"],
            emails=emails, phones=phones, phone_digits=list(dict.fromkeys(phone_digits(p) for p in phones)),
            hashes=hashes, names=[user["name"]], history=past, ignored_future_changes=len(future),
        )
