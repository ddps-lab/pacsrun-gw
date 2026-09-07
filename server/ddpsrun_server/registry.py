"""List the container images this lab has already built, so nobody has to type one from memory.

WHY THIS EXISTS. The New job screen's Image field was a free-text box with an ECR URL in its
placeholder, and that URL is 70 characters of account id, region and repository path. A typo in
any of them is not caught by anything here: the request is valid, the job is created, and the
answer arrives as an ImagePullBackOff on a machine that has already been rented. So the one thing
the screen could not do -- offer the images that exist -- was also the thing that would have made
the field safe.

END-TO-END FLOW of one `GET /v1/images`:

  1. The route (main.py, DDPSRUN-IMAGES-ROUTE) calls `list_images()`.
  2. `DescribeRepositories` returns every repository in THIS account and region. One page.
  3. For each repository, `DescribeImages` returns its images; the tagged ones are sorted newest
     first and the newest few are kept (TAGS_PER_REPO).
  4. Each repository becomes one row: its pullable address, its tags, and when the newest one was
     pushed. The screen puts the addresses in a datalist behind the Image box, so typing filters
     them and the box still accepts anything -- a public image like
     `runpod/pytorch:...` is not in ECR and must stay typeable.

WHAT IT DOES NOT DO, and why each is deliberate.

  IT DOES NOT FILTER BY WHO OWNS WHAT. This is a shared lab account: 19 repositories on
  2026-09-08, and most of them belong to other people's projects (edgeagent-*,
  criu-kubevirt-test/*, theseus/*). Guessing which ones a caller "should" see would mean
  inventing an ownership model out of repository name prefixes, and getting it wrong in the
  hiding direction is worse than the noise -- a researcher who cannot find their own image goes
  back to typing it. Every caller here is a lab member holding a token this deployment issued.

  IT DOES NOT LIST AMIs. There is no equivalent for the machine image: PACSRUN_AWS_AMI holds an
  SSM public-parameter PATH, not an id, and that path resolves to a different ami-* in every
  region on purpose (awsdriverpod.go, awsAMIEnv). There is nothing to enumerate, and a job does
  not choose one anyway -- the driver resolves it per region at rent time.

  IT DOES NOT PAGINATE. `DescribeRepositories` returns up to 1000 per page and this account has
  19. If a lab ever passes 1000 repositories, `truncated` says so rather than the screen quietly
  showing a prefix of the truth.

WHAT IT COSTS. Nothing per call: ECR's price list bills stored bytes ($0.10/GB-month) and data
transferred out, and has no per-request line for DescribeRepositories or DescribeImages. So the
cost of this route is the Lambda time it spends -- 1 + N API round trips for N repositories, and
that N is the reason the tag lookups stop at the repositories the first call returned rather than
walking anything further. (Read off the ECR pricing page, not measured here.)

WHY boto3 IS IMPORTED INSIDE A FUNCTION. The Lambda runtime provides it, so this package does not
depend on it -- the same choice artifacts.py and lambda_handler.py make. The tests replace
`ecr_client` with a fake and never import boto3 at all.

Grep anchor: DDPSRUN-IMAGES
"""

from __future__ import annotations

from dataclasses import dataclass, field

# How many tags one repository contributes. A repository accumulates a tag per build -- the
# operator's has one per commit -- and the useful ones are the newest. Showing all of them would
# make the Image datalist thousands of entries long for one repository.
TAGS_PER_REPO = 8

# One DescribeRepositories page. See "IT DOES NOT PAGINATE" above.
MAX_REPOSITORIES = 1000


@dataclass
class ImageView:
    """One repository, as the screen shows it.

    Attributes:
        repository: the repository name, e.g. "pacsrun/operator".
        registry: the host part, e.g. "<account>.dkr.ecr.us-west-2.amazonaws.com".
        tags: the newest tags, newest first. Empty for a repository that holds only untagged
            images, which is a real state (a build that was overwritten) and is shown rather
            than hidden, because an empty repository in the list is the answer to "why can I
            not find my image".
        pushed_at: ISO-8601 of the newest image's push, or "" when the repository is empty.
            The screen sorts on it: what somebody built this morning is what they mean.
    """

    repository: str
    registry: str
    tags: list[str] = field(default_factory=list)
    pushed_at: str = ""

    def addresses(self) -> list[str]:
        """The pullable strings for this repository, one per tag.

        This is what the Image box needs: `registry/repository:tag`, ready to paste into a
        PacsJob. A repository with no tags contributes nothing, because there is nothing anybody
        could pull.
        """
        return [f"{self.registry}/{self.repository}:{tag}" for tag in self.tags]


@dataclass
class Catalogue:
    """Everything `GET /v1/images` answers."""

    images: list[ImageView] = field(default_factory=list)
    truncated: bool = False


def ecr_client(region: str = ""):
    """The boto3 ECR client, created on first use.

    A module-level function rather than an inline import so the tests can replace it
    (`monkeypatch.setattr(registry, "ecr_client", ...)`) without having boto3 installed.

    Args:
        region: which region's registry to ask. Empty uses the Lambda's own, which is the
            deployment's region and the only one this gateway has ever pointed at.
    """
    import boto3  # provided by the Lambda runtime, so it is not in our package

    return boto3.client("ecr", region_name=region) if region else boto3.client("ecr")


def _registry_host(repository_uri: str, repository_name: str) -> str:
    """The host part of a repository URI.

    ECR reports `repositoryUri` as `<account>.dkr.ecr.<region>.amazonaws.com/<name>`, and the
    name can itself contain slashes ("pacsrun/operator"). So the host is the URI with the name
    and its separating slash removed from the end, which is exact -- splitting on the first
    slash would keep "pacsrun/" in the host for every namespaced repository.

    Args:
        repository_uri: what ECR reported.
        repository_name: the repository's own name.

    Returns:
        The host, or the whole URI when it does not end in the name (which would mean ECR
        changed the shape of the field, and a wrong guess is worse than an odd-looking row).
    """
    suffix = "/" + repository_name
    if repository_uri.endswith(suffix):
        return repository_uri[: -len(suffix)]
    return repository_uri


def list_images(region: str = "") -> Catalogue:
    """Every repository in this account and region, with its newest tags.

    Args:
        region: which region's registry to ask, or empty for the Lambda's own.

    Returns:
        A `Catalogue`, newest push first. A repository whose DescribeImages call fails is
        included with no tags rather than dropped: the caller asked what exists, and "this one
        exists and I could not read its tags" is a truer answer than silence. Failing the whole
        route because one repository of nineteen misbehaved would be the worse trade.
    """
    client = ecr_client(region)

    described = client.describe_repositories(maxResults=MAX_REPOSITORIES)
    repositories = described.get("repositories", [])
    truncated = bool(described.get("nextToken"))

    rows: list[ImageView] = []
    for repo in repositories:
        name = repo.get("repositoryName", "")
        if not name:
            continue
        row = ImageView(
            repository=name,
            registry=_registry_host(repo.get("repositoryUri", ""), name),
        )
        try:
            images = client.describe_images(repositoryName=name).get("imageDetails", [])
        except Exception:  # noqa: BLE001 -- see the docstring's return note
            images = []

        # imagePushedAt is a datetime from boto3 and a string from a fake, and both sort
        # correctly against their own kind. str() is only for the field that goes out.
        tagged = [i for i in images if i.get("imageTags")]
        tagged.sort(key=lambda i: str(i.get("imagePushedAt", "")), reverse=True)
        for image in tagged:
            for tag in image.get("imageTags", []):
                if tag not in row.tags:
                    row.tags.append(tag)
            if len(row.tags) >= TAGS_PER_REPO:
                break
        row.tags = row.tags[:TAGS_PER_REPO]
        if tagged:
            row.pushed_at = str(tagged[0].get("imagePushedAt", ""))
        rows.append(row)

    # Newest first, and a repository with no push date last rather than first: an empty
    # repository is the least likely thing anybody is looking for.
    rows.sort(key=lambda r: (r.pushed_at != "", r.pushed_at), reverse=True)
    return Catalogue(images=rows, truncated=truncated)
