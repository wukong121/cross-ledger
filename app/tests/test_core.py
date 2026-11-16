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


def test_classify_access():
    from crossledger.collector import Window, classify_access

    end = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    w = Window(
        "T-1",
        "alice",
        "alice@x",
        "source-reader",
        "/subscriptions/s/resourceGroups/r/providers/Microsoft.Storage/storageAccounts/src",
        end - timedelta(hours=1),
        end,
        "revoked",
        "req-1",
    )
    after = end + timedelta(minutes=2)
    assert classify_access("alice", "src", after, False, [w], None)[:3] == (
        "data.access.denied",
        "Denied",
        "UNTICKETED",
    )
    assert classify_access("alice", "src", after, True, [w], None)[:3] == (
        "data.accessed.revocation-lag",
        "Info",
        "T-1",
    )
    late = end + timedelta(minutes=30)
    assert classify_access("alice", "src", late, True, [w], None)[0] == "data.accessed.outside-window"
    assert classify_access("scanner", "src", late, True, [w], "Defender")[:2] == (
        "data.accessed.platform-service",
        "Info",
    )
    # another account is never covered by this window
    assert classify_access("alice", "land", after, True, [w], None)[0] == "data.accessed.outside-window"
