from datetime import UTC, datetime, timedelta

import pytest

from crossledger import pim
from crossledger.events import LedgerEvent, event_id
from crossledger.worker import TransferRequest, _norm_prefix


def test_duration_and_window():
    req = {
        "properties": {
            "createdOn": "2026-01-01T00:00:00Z",
            "scheduleInfo": {
                "startDateTime": "2026-01-01T01:00:00.1234567Z",
                "expiration": {"type": "AfterDuration", "duration": "PT2H30M"},
            },
        }
    }
    start, end = pim.window(req)
    assert start == datetime(2026, 1, 1, 1, 0, 0, 123456, tzinfo=UTC)
    assert end - start == timedelta(hours=2, minutes=30)
    assert pim._dur("P1DT1H") == timedelta(days=1, hours=1)


def test_event_id_deterministic():
    assert event_id("a", "b") == event_id("a", "b") != event_id("a", "c")


def test_event_requires_known_stage():
    with pytest.raises(ValueError):
        LedgerEvent(EventType="x", Stage="bogus", TicketId="T", TimeGenerated="", EventId="1")
    assert (
        LedgerEvent(EventType="x", Stage="operate", TicketId="", TimeGenerated="", EventId="1").TicketId == "UNTICKETED"
    )


def test_transfer_request_parse_keeps_unknown_fields():
    r = TransferRequest.parse(
        '{"requestId":"r","ticketId":"T","requesterId":"o","requesterName":"u",'
        '"srcPrefix":"a","dstPrefix":"","submittedAt":"now","future":1}'
    )
    assert r.extra == {"future": 1} and r.verify
    assert _norm_prefix("/a/b") == "a/b/" and _norm_prefix("") == ""
