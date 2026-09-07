"""The container images this lab has built, and the four things this route deliberately will not do.

WHAT IT GUARDS. The Image field on the New job screen was a free-text box whose placeholder was a
70-character ECR URL. A typo in the account id, the region or the repository path is caught by
nothing here: the request is valid, the PacsJob is created, the machine is rented, and the answer
arrives as an ImagePullBackOff. So the one thing the field could not do -- offer the addresses
that exist -- was also the thing that would have made it safe.

boto3 IS NEVER IMPORTED. `registry.ecr_client` is replaced with a fake, the same way
artifacts.py's `s3_client` is, so this file runs with no AWS account, no credential and no network.
"""

import pytest

from ddpsrun_server import registry as r

ACCOUNT_HOST = "example.dkr.ecr.us-west-2.amazonaws.com"


class FakeECR:
    """ECR, answering from a dict. `describe_images` may be told to fail for one repository."""

    def __init__(self, repositories, images, explode=(), next_token=None):
        self.repositories = repositories
        self.images = images
        self.explode = set(explode)
        self.next_token = next_token
        self.asked = []

    def describe_repositories(self, **_kwargs):
        answer = {"repositories": self.repositories}
        if self.next_token:
            answer["nextToken"] = self.next_token
        return answer

    def describe_images(self, repositoryName, **_kwargs):
        self.asked.append(repositoryName)
        if repositoryName in self.explode:
            raise RuntimeError("AccessDeniedException: not authorized to perform ecr:DescribeImages")
        return {"imageDetails": self.images.get(repositoryName, [])}


def repo(name):
    return {"repositoryName": name, "repositoryUri": f"{ACCOUNT_HOST}/{name}"}


def image(tags, pushed):
    return {"imageTags": tags, "imagePushedAt": pushed}


def with_fake(monkeypatch, fake):
    monkeypatch.setattr(r, "ecr_client", lambda region="": fake)
    return r.list_images()


def test_a_repository_becomes_pullable_addresses(monkeypatch):
    """The one thing the Image box needs: strings ready to paste, not parts to assemble."""
    fake = FakeECR(
        repositories=[repo("pacsrun/operator")],
        images={"pacsrun/operator": [image(["fd7c9b1c84e1"], "2026-09-06T11:00:00Z")]},
    )
    answer = with_fake(monkeypatch, fake)
    assert [i.repository for i in answer.images] == ["pacsrun/operator"]
    assert answer.images[0].addresses() == [
        f"{ACCOUNT_HOST}/pacsrun/operator:fd7c9b1c84e1"
    ]


def test_the_host_survives_a_namespaced_repository(monkeypatch):
    """A repository name can hold slashes, and splitting the URI on the first one gets it wrong.

    ECR reports `repositoryUri` as `<host>/<name>` and the name of every repository this lab
    builds under a project prefix -- pacsrun/operator, criu-kubevirt-test/criu-agent -- contains
    a slash of its own. A host taken as "everything before the first slash" would be right, but
    a host taken by splitting the REST off would leave "pacsrun/" glued to it. This pins the
    subtraction: the host is the URI with the name and its separator removed from the END.
    """
    fake = FakeECR(
        repositories=[repo("criu-kubevirt-test/criu-agent")],
        images={"criu-kubevirt-test/criu-agent": [image(["v3"], "2026-09-01T00:00:00Z")]},
    )
    answer = with_fake(monkeypatch, fake)
    assert answer.images[0].registry == ACCOUNT_HOST
    assert answer.images[0].addresses() == [
        f"{ACCOUNT_HOST}/criu-kubevirt-test/criu-agent:v3"
    ]


def test_a_uri_that_does_not_end_in_the_name_is_left_whole(monkeypatch):
    """If ECR ever changes the field's shape, an odd-looking row beats a wrong guess."""
    fake = FakeECR(
        repositories=[{"repositoryName": "thing", "repositoryUri": "surprising-shape"}],
        images={"thing": [image(["t"], "2026-09-01T00:00:00Z")]},
    )
    answer = with_fake(monkeypatch, fake)
    assert answer.images[0].registry == "surprising-shape"


def test_the_newest_tags_come_first_and_are_capped(monkeypatch):
    """A repository gains a tag per build -- the operator's has one per commit.

    Showing all of them would make the datalist thousands of entries long for one repository,
    and the useful ones are the newest. Both halves are asserted: the order, and the cap.
    """
    many = [image([f"build-{n:03d}"], f"2026-09-{(n % 28) + 1:02d}T00:00:00Z") for n in range(40)]
    fake = FakeECR(repositories=[repo("busy")], images={"busy": many})
    answer = with_fake(monkeypatch, fake)
    tags = answer.images[0].tags
    assert len(tags) == r.TAGS_PER_REPO
    newest = sorted(many, key=lambda i: i["imagePushedAt"], reverse=True)[0]["imageTags"][0]
    assert tags[0] == newest


def test_an_untagged_repository_is_shown_with_no_addresses(monkeypatch):
    """An empty row is the answer to "why can I not find my image", so it is not hidden.

    A repository holding only untagged images is a real state -- a build whose tag was moved to
    a newer push. It contributes no address, because there is nothing anybody could pull, and it
    still appears so the reader can see that the repository exists.
    """
    fake = FakeECR(
        repositories=[repo("emptied")],
        images={"emptied": [{"imageDigest": "sha256:abc"}]},
    )
    answer = with_fake(monkeypatch, fake)
    assert [i.repository for i in answer.images] == ["emptied"]
    assert answer.images[0].tags == []
    assert answer.images[0].addresses() == []
    assert answer.images[0].pushed_at == ""


def test_one_repository_refusing_does_not_lose_the_other_eighteen(monkeypatch):
    """The caller asked what exists, and nineteen answers minus one beats an exception.

    A per-repository policy, a repository mid-deletion, a throttle -- any of them can fail one
    DescribeImages. Failing the whole route would turn "I could not read one repository's tags"
    into "this lab has built nothing", which is a different and false statement.
    """
    fake = FakeECR(
        repositories=[repo("good"), repo("forbidden")],
        images={"good": [image(["ok"], "2026-09-05T00:00:00Z")]},
        explode=["forbidden"],
    )
    answer = with_fake(monkeypatch, fake)
    assert sorted(i.repository for i in answer.images) == ["forbidden", "good"]
    assert next(i for i in answer.images if i.repository == "forbidden").tags == []
    assert next(i for i in answer.images if i.repository == "good").tags == ["ok"]


def test_the_newest_repository_is_first_and_the_empty_one_is_last(monkeypatch):
    """What somebody built this morning is what they mean; an empty repository is nobody's answer."""
    fake = FakeECR(
        repositories=[repo("old"), repo("empty"), repo("fresh")],
        images={
            "old": [image(["a"], "2026-01-01T00:00:00Z")],
            "fresh": [image(["b"], "2026-09-08T00:00:00Z")],
            "empty": [],
        },
    )
    answer = with_fake(monkeypatch, fake)
    assert [i.repository for i in answer.images] == ["fresh", "old", "empty"]


def test_more_than_one_page_is_said_out_loud(monkeypatch):
    """A prefix of the truth shown as if it were all of it is the failure this flag prevents."""
    fake = FakeECR(repositories=[repo("one")], images={"one": []}, next_token="more")
    assert with_fake(monkeypatch, fake).truncated is True

    quiet = FakeECR(repositories=[repo("one")], images={"one": []})
    assert with_fake(monkeypatch, quiet).truncated is False
