"""job_id <-> object name, and the label sanitiser.

These are the functions that decide what a caller can reach. `object_name`
refusing a malformed id is the check that keeps an arbitrary string out of a
Kubernetes API path, so it gets the most cases here.
"""

import pytest

from ddpsrun_server import naming


def test_a_fresh_id_round_trips_to_its_object_and_back():
    job_id = naming.new_job_id()
    assert naming.job_id_from_object_name(naming.object_name(job_id)) == job_id


def test_two_ids_in_a_row_differ():
    assert naming.new_job_id() != naming.new_job_id()


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "job-",                     # no random part
        "job-abc",                  # too short
        "job-a8acdef80a07a",        # too long
        "job-A8ACDEF80A07",         # uppercase hex is not what we issue
        "job-a8acdef80a0g",         # g is not hex
        "ddpsrun-a8acdef80a07",     # the object name, not the id
        "../../kube-system/secret", # the reason this check exists
        "job-a8acdef80a07/../x",
    ],
)
def test_a_string_we_did_not_issue_is_refused(bad):
    with pytest.raises(naming.NamingError):
        naming.object_name(bad)


def test_an_object_someone_applied_by_hand_has_no_job_id():
    # A PacsJob created with kubectl will not carry our prefix. Returning None
    # rather than raising lets a listing show it without crashing.
    assert naming.job_id_from_object_name("bank-exp2v2") is None
    assert naming.job_id_from_object_name("ddpsrun-not-hex-here") is None


def test_a_korean_job_name_becomes_a_usable_label():
    # Kubernetes rejects a label value with anything outside [A-Za-z0-9._-],
    # and rejects one that starts or ends with a separator.
    assert naming.label_value("bank 실험2'") == "bank---2"
    assert naming.label_value("---") == ""
    assert naming.label_value("a" * 100) == "a" * 63


def test_a_name_that_sanitises_to_nothing_is_left_out_of_the_labels():
    result = naming.labels("job-a8acdef80a07", "alice", "---")
    assert naming.DISPLAY_NAME_LABEL not in result
    assert result[naming.JOB_ID_LABEL] == "job-a8acdef80a07"
    assert result[naming.OWNER_LABEL] == "alice"
