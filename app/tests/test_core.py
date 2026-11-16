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


def test_destination_from_secret_maps_rclone_options(tmp_path):
    from crossledger.worker import Destination

    d = Destination.from_dict(
        {
            "name": "domestic",
            "bucket": "datasets",
            "type": "s3",
            "provider": "Other",
            "endpoint": "https://oss.example.com",
            "access_key_id": "AK",
            "secret_access_key": "SK",
            "ca_cert_pem": "-----BEGIN CERTIFICATE-----\nX\n-----END CERTIFICATE-----\n",
        }
    )
    assert (d.name, d.bucket) == ("domestic", "datasets")
    assert d.env["RCLONE_CONFIG_DST_TYPE"] == "s3" and d.env["RCLONE_CONFIG_DST_ENDPOINT"] == "https://oss.example.com"
    assert "RCLONE_CONFIG_DST_BUCKET" not in d.env and "RCLONE_CONFIG_DST_CA_CERT_PEM" not in d.env
    assert open(d.ca_cert_file).read().startswith("-----BEGIN CERTIFICATE-----")


def test_destination_ca_is_added_to_system_roots(tmp_path, monkeypatch):
    from crossledger import worker

    roots = tmp_path / "roots.pem"
    roots.write_text("-----BEGIN CERTIFICATE-----\nSYSTEM\n-----END CERTIFICATE-----\n")
    monkeypatch.setattr(worker, "SYSTEM_CA_BUNDLES", (str(roots),))
    d = worker.Destination.from_dict(
        {
            "bucket": "b",
            "type": "s3",
            "ca_cert_pem": "-----BEGIN CERTIFICATE-----\nPRIVATE\n-----END CERTIFICATE-----\n",
        }
    )
    bundle = open(d.ca_cert_file).read()
    assert "SYSTEM" in bundle and "PRIVATE" in bundle


def test_activation_valid_at_submission_time():
    from crossledger.pim import activation_at

    role = "/subscriptions/s/providers/Microsoft.Authorization/roleDefinitions/abc"
    act = {"name": "act", "properties": {
        "requestType": "SelfActivate", "status": "Provisioned", "roleDefinitionId": role,
        "ticketInfo": {"ticketNumber": "T-1"}, "createdOn": "2026-01-01T10:00:00Z",
        "scheduleInfo": {"startDateTime": "2026-01-01T10:00:00Z", "expiration": {"duration": "PT2H"}}}}
    revoke = {"name": "rev", "properties": {
        "requestType": "SelfDeactivate", "status": "Revoked", "roleDefinitionId": role,
        "createdOn": "2026-01-01T10:30:00Z"}}
    t = lambda hm: datetime(2026, 1, 1, *hm, tzinfo=UTC)  # noqa: E731
    assert activation_at([act], "abc", "T-1", t((10, 15)))["name"] == "act"
    assert activation_at([act], "abc", "OTHER", t((10, 15))) is None          # wrong ticket
    assert activation_at([act], "abc", "T-1", t((12, 30))) is None            # expired
    assert activation_at([act, revoke], "abc", "T-1", t((10, 20)))["name"] == "act"  # submitted before hand-back
    assert activation_at([act, revoke], "abc", "T-1", t((10, 45))) is None    # submitted after hand-back
