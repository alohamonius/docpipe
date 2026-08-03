import io
import json
import logging

from docpipe_core.observability import JsonFormatter, emit_metric


def test_json_formatter_produces_valid_json() -> None:
    record = logging.LogRecord(
        name="docpipe.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    record.context = {"job_id": "abc"}
    entry = json.loads(JsonFormatter().format(record))
    assert entry["message"] == "hello world"
    assert entry["level"] == "INFO"
    assert entry["job_id"] == "abc"


def test_emit_metric_is_valid_emf() -> None:
    out = io.StringIO()
    emit_metric("JobCompleted", 1, dimensions={"Service": "worker"}, stream=out)
    document = json.loads(out.getvalue())
    aws = document["_aws"]
    assert aws["CloudWatchMetrics"][0]["Namespace"] == "docpipe"
    assert aws["CloudWatchMetrics"][0]["Metrics"] == [{"Name": "JobCompleted", "Unit": "Count"}]
    assert aws["CloudWatchMetrics"][0]["Dimensions"] == [["Service"]]
    assert document["JobCompleted"] == 1
    assert document["Service"] == "worker"
