from docpipe_core.queue import JobQueue


def test_send_receive_delete(sqs_queue) -> None:
    client, url = sqs_queue
    queue = JobQueue(url, sqs_client=client)

    message_id = queue.send("job42")
    assert message_id

    messages = queue.receive(wait_seconds=0)
    assert len(messages) == 1
    assert messages[0].job_id == "job42"

    queue.delete(messages[0].receipt_handle)
    assert queue.receive(wait_seconds=0) == []


def test_receive_empty(sqs_queue) -> None:
    client, url = sqs_queue
    queue = JobQueue(url, sqs_client=client)
    assert queue.receive(wait_seconds=0) == []
