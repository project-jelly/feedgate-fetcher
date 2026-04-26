import time

from feedgate_fetcher.metrics import FETCH_DURATION, FETCH_ERROR_TOTAL, FETCH_TOTAL, observe_fetch
from feedgate_fetcher.models import ErrorCode


def _duration_sample_count(result: str) -> float:
    return float(sum(bucket.get() for bucket in FETCH_DURATION.labels(result=result)._buckets))


def test_observe_fetch_records_success_total_and_duration_sample() -> None:
    total = FETCH_TOTAL.labels(result="success")
    before_total = total._value.get()
    before_duration_count = _duration_sample_count("success")

    observe_fetch("success", time.perf_counter())

    assert total._value.get() == before_total + 1
    assert _duration_sample_count("success") == before_duration_count + 1


def test_observe_fetch_records_error_total_duration_and_error_code() -> None:
    total = FETCH_TOTAL.labels(result="error")
    error_total = FETCH_ERROR_TOTAL.labels(error_code=ErrorCode.TIMEOUT)
    before_total = total._value.get()
    before_duration_count = _duration_sample_count("error")
    before_error_total = error_total._value.get()

    observe_fetch("error", time.perf_counter(), error_code=ErrorCode.TIMEOUT)

    assert total._value.get() == before_total + 1
    assert _duration_sample_count("error") == before_duration_count + 1
    assert error_total._value.get() == before_error_total + 1


def test_observe_fetch_without_error_code_does_not_record_error_total() -> None:
    error_total = FETCH_ERROR_TOTAL.labels(error_code=ErrorCode.TIMEOUT)
    before_error_total = error_total._value.get()

    observe_fetch("error", time.perf_counter())

    assert error_total._value.get() == before_error_total
