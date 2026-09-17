"""Reject oversized decoded images before native allocation."""

import pytest

from msu_hub_bot.media.limits import MediaDimensionsError, validate_dimensions
from msu_hub_bot.execution.executor import ExecutorBusy
from msu_hub_bot.telegram.files import DownloadTooLarge
from msu_hub_bot.telemetry import Outcome, failure_outcome, safe_failure


@pytest.mark.parametrize("dimensions", [(4000, 4000), (8192, 1), (1, 8192)])
def test_dimensions_at_limits(dimensions):
    validate_dimensions(*dimensions)


@pytest.mark.parametrize("dimensions", [(0, 10), (10, -1), (4000, 4001), (8193, 1), (1, 8193)])
def test_dimensions_reject_oversize_or_empty_inputs(dimensions):
    with pytest.raises(MediaDimensionsError):
        validate_dimensions(*dimensions)


@pytest.mark.parametrize(
    "error,reason",
    [(ExecutorBusy, "worker_busy"), (DownloadTooLarge, "media_too_large"), (MediaDimensionsError, "media_dimensions")],
)
def test_media_admission_rejections_have_fixed_private_diagnostics(error, reason):
    exception = error("synthetic private payload")
    assert failure_outcome(exception) is Outcome.REJECTED
    assert safe_failure(exception) == {"error.type": error.__name__, "error.reason": reason}
