"""job_id, the Kubernetes object name behind it, and the labels that connect them.

END-TO-END FLOW of this file:

  1. `POST /v1/jobs` calls `new_job_id()`, which returns something like
     `job-3f9a1c4e7b02`.
  2. `object_name(job_id)` turns that into the PacsJob's Kubernetes name,
     `ddpsrun-3f9a1c4e7b02`. The mapping is pure arithmetic on the string, so
     the server keeps no database.
  3. `labels(...)` attaches the job_id, the owner, and the user's display name
     to the object, so a human running `kubectl get pacsjobs -l ddpsrun.io/owner=alice`
     can see whose job is whose.
  4. `GET /v1/jobs/{job_id}` runs step 2 again and fetches that exact name in the
     caller's namespace. A job_id belonging to another namespace simply is not
     there, which is why guessing an id gets a caller nothing.

WHY A job_id AT ALL, when the Kubernetes name would do. `docs/03-api.md` puts it
plainly: "job_id 는 우리가 만든다. kubernetes 객체 이름을 그대로 쓰지 않는다."
The name we hand out is part of the public API and has to keep working; the
object name is internal and we may want to change how it is built (add the user,
add a date, shorten it) without breaking a script somebody wrote six months ago.
Today the two are one substring apart, and that is fine — the point is that
`object_name` is the only place that knows it.

WHY RANDOM AND NOT A COUNTER. A counter needs somewhere to keep the count, and
the server is meant to hold no state. 12 hex characters is 48 bits; with a
thousand jobs the chance any two collide is about 1 in 560 million.

Grep anchor: DDPSRUN-JOBID
"""

from __future__ import annotations

import re
import uuid

# Public prefix, seen by users.
JOB_ID_PREFIX = "job-"
# Internal prefix, seen only in `kubectl get pacsjobs`.
OBJECT_NAME_PREFIX = "ddpsrun-"
# How many hex characters carry the randomness.
ID_HEX_LENGTH = 12

JOB_ID_PATTERN = re.compile(rf"^{JOB_ID_PREFIX}[0-9a-f]{{{ID_HEX_LENGTH}}}$")

# Our own label keys. A separate group from `pacsrun.io/*` on purpose: those
# belong to the controller and it lists and indexes by them, so writing into
# that group risks colliding with a meaning the controller already has.
JOB_ID_LABEL = "ddpsrun.io/job-id"
OWNER_LABEL = "ddpsrun.io/owner"
DISPLAY_NAME_LABEL = "ddpsrun.io/name"

# A Kubernetes label value must be at most 63 characters and must start and end
# with an alphanumeric, with dashes, underscores and dots allowed in between.
# https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/
_LABEL_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_LABEL_MAX = 63


class NamingError(ValueError):
    """A job_id the server did not issue. Routes turn this into HTTP 404."""


def new_job_id() -> str:
    """Mint a fresh job_id.

    Returns:
        `job-` followed by 12 lowercase hex characters.
    """
    return JOB_ID_PREFIX + uuid.uuid4().hex[:ID_HEX_LENGTH]


def object_name(job_id: str) -> str:
    """Map a job_id to the Kubernetes object name that holds it.

    Args:
        job_id: an id previously returned by `new_job_id`.

    Returns:
        The PacsJob's `metadata.name`.

    Raises:
        NamingError: the string is not a job_id this server could have issued.
            Checking the shape here means a caller cannot smuggle an arbitrary
            object name (`../`, a name in kube-system, a very long string) into
            a Kubernetes API path.
    """
    if not JOB_ID_PATTERN.match(job_id or ""):
        raise NamingError(f"{job_id!r} is not a job id")
    return OBJECT_NAME_PREFIX + job_id[len(JOB_ID_PREFIX):]


def job_id_from_object_name(name: str) -> str | None:
    """Map back, for turning a listed object into an API response.

    Args:
        name: a PacsJob's `metadata.name`.

    Returns:
        The job_id, or None when the object was not created by this server —
        somebody may have applied a PacsJob with kubectl by hand.
    """
    if not name.startswith(OBJECT_NAME_PREFIX):
        return None
    candidate = JOB_ID_PREFIX + name[len(OBJECT_NAME_PREFIX):]
    return candidate if JOB_ID_PATTERN.match(candidate) else None


def label_value(raw: str) -> str:
    """Force an arbitrary user string into something Kubernetes accepts as a label value.

    Args:
        raw: whatever the user typed, e.g. a job name with spaces or Korean text.

    Returns:
        A safe label value, possibly empty when nothing survived. The caller
        drops empty labels rather than sending an invalid one.

    Example:
        >>> label_value("bank 실험2'")
        'bank---2'
    """
    cleaned = _LABEL_UNSAFE.sub("-", raw)[:_LABEL_MAX]
    # Strip until both ends are alphanumeric; a label may not begin or end with
    # a dash, dot or underscore.
    return cleaned.strip("-._")


def labels(job_id: str, owner: str, display_name: str) -> dict[str, str]:
    """Build the label set every PacsJob this server creates carries.

    Args:
        job_id: the public id.
        owner: `Principal.user`.
        display_name: the `name` field of the submit request.

    Returns:
        A label map with empty values dropped.
    """
    candidate = {
        JOB_ID_LABEL: job_id,
        OWNER_LABEL: label_value(owner),
        DISPLAY_NAME_LABEL: label_value(display_name),
    }
    return {key: value for key, value in candidate.items() if value}
