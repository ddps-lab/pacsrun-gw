"""The log relay's line filter.

`redact` is the only pure function in `k8s.py`; the rest needs a cluster. What
it protects: the driver writes its own bookkeeping onto the same stdout as the
user's training output, and those names are ours, not theirs.
"""

from ddpsrun_server.k8s import redact


def test_the_keepalive_line_is_dropped_entirely():
    # The driver prints one of these every 30 seconds for the whole life of the
    # job, purely to hold the log stream open. A user reading a training curve
    # does not want one between every progress line.
    assert redact("PACSRUN_KEEPALIVE") is None
    assert redact("  PACSRUN_KEEPALIVE  ") is None


def test_an_internal_name_is_masked_but_the_line_survives():
    # The line may be the user's own output with our token appended to it, so
    # dropping the whole line would throw away something they need.
    assert redact("done, PACSRUN_EXIT=0") == "done, <internal>=0"
    assert redact("PACSRUN_ARTIFACT=/workspace/out.tar") == "<internal>=/workspace/out.tar"


def test_ordinary_output_passes_through_unchanged():
    line = "{'loss': 0.42, 'epoch': 1.0}"
    assert redact(line) == line
    progress = " 25%|##        | 25/99 [03:45<08:37,  6.99s/it]"
    assert redact(progress) == progress


def test_a_word_that_merely_starts_with_the_prefix_in_prose_is_left_alone():
    # The pattern requires the uppercase form with a word boundary, so a user's
    # own lowercase variable is not touched.
    assert redact("pacsrun_exit is not ours") == "pacsrun_exit is not ours"
