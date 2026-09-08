"""List a job's result files in S3, and mint the links that download them.

END-TO-END FLOW of one `GET /v1/jobs/{id}/artifacts`:

  1. The route (main.py, DDPSRUN-ARTIFACTS-ROUTE) fetches the PacsJob and reads
     `spec.resultPath` — an address the SERVER built at submit time
     (`models.result_path_for`), never one the caller typed. That is the whole
     scoping story: a caller can only ever reach the prefix their own job was
     given, because the prefix comes off the job object, not off the request.

     ★ AND WHICH JOBS ARE "THEIR OWN" IS A SEPARATE QUESTION, answered elsewhere.
     `is_ours()` below checks only the bucket and the deployment-wide prefix --
     no namespace and no owner -- so the sentence above holds only as far as the
     route's own fetch does. Until 2026-09-08 that fetch was scoped to a
     NAMESPACE alone, and with two people in one namespace either could list and
     download the other's result files. The route now runs its fetch through
     `main.require_owner` (DDPSRUN-OWNER-GATE); this module is unchanged and was
     never the place to fix it.
  2. `split_result_path()` turns "s3://bucket/pacsrun/ns/job/" into
     (bucket, "pacsrun/ns/job/"), and the route refuses anything outside this
     deployment's own bucket and prefix (`is_ours()`), so a PacsJob written by
     hand with kubectl cannot point this server at somebody else's bucket.
  3. `list_artifacts()` asks S3 once with ListObjectsV2 (one page, up to 1000
     keys) and, for each file, calls GeneratePresignedUrl. A presigned URL is
     an ordinary GET URL carrying this server's signature; S3 honours it for
     exactly one key until the signature expires (EXPIRES_SECONDS). Minting it
     is local arithmetic — no network call — and S3 checks the SIGNER's
     permission when the URL is USED, which is why the Lambda role needs
     s3:GetObject (terraform/lambda, DDPSRUN-ARTIFACTS-READ) even though the
     server itself never downloads anything.
  4. The browser follows the URL and downloads straight from S3. The bytes do
     not pass through Lambda, and they must not: a Lambda response is capped at
     about 6 MB and one adapter file measured 528,550,256 bytes (s39).

WHY boto3 IS IMPORTED INSIDE A FUNCTION, NOT AT THE TOP. The Lambda runtime
provides boto3, so this package deliberately does not depend on it — the same
choice `lambda_handler.py` makes for the tokens secret. The tests never import
it either: they replace `s3_client` with a fake.

Grep anchor: DDPSRUN-ARTIFACTS
"""

from __future__ import annotations

from dataclasses import dataclass, field

# How long a minted download link keeps working. Long enough to click every
# file on the screen one by one; short enough that a link pasted into a chat
# is stale within the meeting it was pasted into.
EXPIRES_SECONDS = 600

# One ListObjectsV2 page. A job's result folder holds a handful of files (the
# measured runs produced 5), so paginating further would only ever serve a
# runaway job that wrote thousands — and for that, `truncated` says so.
MAX_KEYS = 1000


class ForeignResultPath(Exception):
    """The job's resultPath points outside this deployment's result bucket.

    Raised instead of listing, because listing it would make this server read
    an arbitrary bucket named by whoever wrote the PacsJob. The route turns
    this into a note on an otherwise empty answer, not an error: the job is
    real, we simply refuse to follow its pointer.
    """


@dataclass(frozen=True)
class ArtifactFile:
    """One result file, ready to show and to download."""

    name: str            # key relative to the job's prefix, e.g. "run.sh"
    size_bytes: int
    last_modified: str   # RFC 3339, as S3 reported it
    url: str             # presigned GET, good for EXPIRES_SECONDS


@dataclass(frozen=True)
class Listing:
    """What `list_artifacts` found under one job's prefix."""

    files: list[ArtifactFile] = field(default_factory=list)
    truncated: bool = False


def split_result_path(result_path: str) -> tuple[str, str] | None:
    """Read "s3://bucket/key/prefix/" into (bucket, key prefix).

    Args:
        result_path: the job's `spec.resultPath`, or anything else.

    Returns:
        (bucket, prefix) with the prefix ending in "/", or None when the value
        is not an s3:// address at all — a job with no resultPath simply has
        nothing to list, which is a normal answer, not an error.
    """
    if not result_path or not result_path.startswith("s3://"):
        return None
    rest = result_path[len("s3://"):]
    bucket, _, prefix = rest.partition("/")
    if not bucket or not prefix:
        return None
    return bucket, prefix if prefix.endswith("/") else prefix + "/"


def is_ours(bucket: str, prefix: str, result_bucket: str, result_prefix: str) -> bool:
    """Is this address inside the one bucket and prefix this server serves?

    The same fence PACSrun's own controller puts around resultPath
    (PACSRUN-RESULT-TENANCY), checked again here because a kubectl-applied
    PacsJob reaches this route without ever passing that controller check on
    the way in — and because the IAM policy (DDPSRUN-ARTIFACTS-READ) is scoped
    the same way, so anything outside would only fail later and less clearly.
    """
    return bucket == result_bucket and prefix.startswith(result_prefix)


def s3_client():
    """The boto3 S3 client, created on first use.

    A module-level function rather than an inline import so the tests can
    replace it (`monkeypatch.setattr(artifacts, "s3_client", ...)`) without
    having boto3 installed at all.
    """
    import boto3  # provided by the Lambda runtime, so it is not in our package

    return boto3.client("s3")


def list_artifacts(
    bucket: str,
    prefix: str,
    result_bucket: str,
    result_prefix: str,
) -> Listing:
    """Every file under one job's prefix, each with its download link.

    Args:
        bucket, prefix: from `split_result_path`.
        result_bucket, result_prefix: this deployment's own, from Settings —
            the fence `is_ours` checks.

    Returns:
        A `Listing`, possibly empty: a Running job that has not uploaded yet
        and a Failed job that never got that far both simply have no files.

    Raises:
        ForeignResultPath: the address is outside our bucket or prefix.
        botocore.exceptions.ClientError: S3 refused the list — most likely the
            IAM policy is missing; the route reports it as 502 with S3's own
            words, because "empty" would be a lie.
    """
    if not is_ours(bucket, prefix, result_bucket, result_prefix):
        raise ForeignResultPath(
            f"this job's resultPath points at s3://{bucket}/{prefix}, which is "
            f"outside this server's result bucket, so it was not listed"
        )

    client = s3_client()
    answer = client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=MAX_KEYS)

    files: list[ArtifactFile] = []
    for entry in answer.get("Contents", []):
        key = entry["Key"]
        name = key[len(prefix):]
        # The prefix itself can appear as a zero-byte "folder" key when
        # something created it explicitly. It is not a file; skip it.
        if not name:
            continue
        files.append(
            ArtifactFile(
                name=name,
                size_bytes=int(entry.get("Size", 0)),
                last_modified=(
                    entry["LastModified"].isoformat()
                    if hasattr(entry.get("LastModified"), "isoformat")
                    else str(entry.get("LastModified", ""))
                ),
                url=client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": bucket, "Key": key},
                    ExpiresIn=EXPIRES_SECONDS,
                ),
            )
        )
    return Listing(files=files, truncated=bool(answer.get("IsTruncated", False)))
