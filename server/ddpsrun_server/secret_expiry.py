"""DDPSRUN-SECRET-EXPIRY: is a registered credential still good.

WHY THIS IS A FILE AND NOT TWO LINES INLINE. On 2026-09-08 a judge credential
expired at 14:27Z and the only way to find that out was to submit a job and watch
it fail at the Bedrock call -- hours in, on a rented GPU. The credentials this lab
uses for that are temporary by design (`GetFederationToken` gives 36 hours), so
"has this one run out" is a question the tool has to be able to answer before the
money is spent, in two places: `GET /v1/secrets` says which names are past their
date, and `validate` refuses a job that asks for one. Two callers, one parser.

WHAT IT DELIBERATELY DOES NOT DO. It does not check the credential -- nothing here
calls AWS. It compares a date the user typed against the clock. A value revoked
early still looks good, and a date that was wrong when it was stored is wrong
here; both are the user's to get right. What this removes is the case where the
date was known, written down, and nobody looked.

Grep anchor: DDPSRUN-SECRET-EXPIRY
"""
from __future__ import annotations

import datetime


def parse(text: str | None) -> datetime.datetime | None:
    """An ISO-8601 timestamp as an aware datetime, or None when it is not one.

    Args:
        text: what the caller sent, e.g. "2026-09-10T02:27:00Z".

    Returns:
        A timezone-aware datetime, or None for anything unparseable -- including
        None itself, so a name with no date reads as "does not expire".

    WHY `Z` NEEDS HANDLING AT ALL. `datetime.fromisoformat` learned to accept it
    in Python 3.11 and this server runs 3.12, so it would work -- but the CLI
    supports 3.9, where it raises. One conversion here keeps both on the same
    parser instead of the two disagreeing about which timestamps are legal.

    A DATE WITH NO TIMEZONE IS READ AS UTC. Rejecting it would be defensible and
    unhelpful: `2026-09-10` is what a person types, and treating it as UTC is
    both the least surprising reading and the conservative one for an expiry, in
    the timezones this lab works in.
    """
    if not text:
        return None
    candidate = text.strip()
    if candidate.endswith(("Z", "z")):
        candidate = candidate[:-1] + "+00:00"
    try:
        when = datetime.datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return when


def is_past(text: str | None, now: datetime.datetime | None = None) -> bool:
    """Has that date gone by.

    Args:
        text: the stored expiry.
        now: for tests. Defaults to the real clock, in UTC.

    Returns:
        True only when the date parsed AND is in the past. An unparseable or
        absent date is NOT past: refusing a job over a string we could not read
        would block work over our own inability to read it, and the 400 on the
        way in is where a bad date belongs.
    """
    when = parse(text)
    if when is None:
        return False
    return when <= (now or datetime.datetime.now(datetime.timezone.utc))
