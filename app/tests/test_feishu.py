import json

import pytest

from crossledger import tickets as tk

FIELDS = {"数据集": "dataset", "目的地": "destination", "用途": "purpose"}
DATASETS = {"drive-sample": {"prefix": "batch-001/", "description": ""}}
IT = {"it@example.com"}
EMAILS = {"ou_it": "IT@Example.com ", "ou_dev": "dev@example.com", "ou_it2": "it@example.com"}


def instance(status="APPROVED", applicant="ou_dev", approvers=("ou_it",), dataset="drive-sample", dest="cloud-x"):
    form = [
        {"id": "widget1", "custom_id": "", "name": "数据集", "type": "radioV2", "value": dataset},
        {"id": "widget2", "custom_id": "", "name": "目的地", "type": "radioV2", "value": [dest]},
        {"id": "widget3", "custom_id": "purpose", "name": "用途说明", "type": "textarea", "value": "training"},
    ]
    return {
        "approval_code": "APPROVAL",
        "instance_code": "INST-1",
        "serial_number": "202611250001",
        "status": status,
        "open_id": applicant,
        "form": json.dumps(form, ensure_ascii=False),
        "task_list": [{"open_id": a, "status": "APPROVED"} for a in approvers],
        "timeline": [{"type": "START", "open_id": applicant}] + [{"type": "PASS", "open_id": a} for a in approvers],
    }


def dest_lookup(name):
    return {"cloud-x": {"enabled": True, "root": "landing/"}, "old": {"enabled": False, "root": "x/"}}.get(name)


def evaluate(doc):
    t = tk.parse_instance(doc, {**FIELDS, "purpose": "purpose"})
    return t, tk.evaluate(t, approver_emails=IT, email_of=EMAILS.get, datasets=DATASETS, destination=dest_lookup)


def test_parse_instance_value_shapes():
    t = tk.parse_instance(instance(), {**FIELDS, "purpose": "purpose"})
    assert (t.dataset, t.destination, t.purpose) == ("drive-sample", "cloud-x", "training")
    assert (t.instance_code, t.serial, t.applicant, t.approvers) == ("INST-1", "202611250001", "ou_dev", ["ou_it"])
    # keyed by widget id works too
    t2 = tk.parse_instance(instance(), {"widget1": "dataset", "widget2": "destination"})
    assert (t2.dataset, t2.destination, t2.purpose) == ("drive-sample", "cloud-x", "")


def test_evaluate_ok_and_email_case():
    t, d = evaluate(instance())
    assert d.ok, d.reason
    assert (d.approver, d.approver_email) == ("ou_it", "it@example.com")
    assert tk.target_for(t, "landing") == "cloud-x:landing/drive-sample/"


@pytest.mark.parametrize(
    "doc,needle",
    [
        (instance(status="PENDING"), "not approved"),
        (instance(approvers=("ou_x",)), "no approver"),
        (instance(applicant="ou_it", approvers=("ou_it",)), "own request"),
        (instance(dataset="nope"), "dataset"),
        (instance(dest="nope"), "not registered"),
        (instance(dest="old"), "disabled"),
    ],
)
def test_evaluate_rejections(doc, needle):
    _, d = evaluate(doc)
    assert not d.ok and needle in d.reason


def test_evaluate_self_approval_with_second_it_approver_is_ok():
    _, d = evaluate(instance(applicant="ou_it", approvers=("ou_it", "ou_it2")))
    assert d.ok and d.approver == "ou_it2"


def test_request_id_stable():
    t = tk.parse_instance(instance(), FIELDS)
    assert tk.request_id(t) == tk.request_id(tk.parse_instance(instance(), FIELDS))
    assert tk.request_id(t) != tk.request_id(tk.parse_instance(instance(dest="old"), FIELDS))
    assert len(tk.request_id(t)) == 16


def test_form_options_and_catalog_diff():
    definition = {
        "form": json.dumps(
            [
                {"id": "w1", "name": "数据集", "type": "radioV2", "option": [{"value": "a", "text": "drive-sample"}]},
                {
                    "id": "w2",
                    "name": "目的地",
                    "type": "radioV2",
                    "option": [{"value": "b", "text": "cloud-x"}, {"value": "c", "text": "gone"}],
                },
            ]
        )
    }
    opts = tk.form_options(definition, FIELDS)
    assert opts == {"dataset": ["drive-sample"], "destination": ["cloud-x", "gone"]}
    diff = tk.catalog_diff(opts, DATASETS, ["cloud-x", "idc"])
    assert diff == {
        "dataset": {"missing_in_form": [], "unknown_in_form": []},
        "destination": {"missing_in_form": ["idc"], "unknown_in_form": ["gone"]},
    }
    assert tk.catalog_diff(opts, DATASETS, ["cloud-x", "gone"]) is None


def test_destination_new_format():
    from crossledger.destinations import doc_from_secret
    from crossledger.worker import Destination

    tags = {
        "xl-kind": "destination",
        "provider": "Other",
        "endpoint": "https://s3.example.com",
        "access_key_id": "AKID",
        "root": "/landing/team-a",
        "description": "d",
    }
    doc = doc_from_secret("SECRET", tags)
    assert doc["allow"] == ["landing/team-a/"]
    d = Destination.from_dict("cloud-x", doc)
    assert d.env == {
        "RCLONE_CONFIG_DST_TYPE": "s3",
        "RCLONE_CONFIG_DST_PROVIDER": "Other",
        "RCLONE_CONFIG_DST_ENDPOINT": "https://s3.example.com",
        "RCLONE_CONFIG_DST_ACCESS_KEY_ID": "AKID",
        "RCLONE_CONFIG_DST_SECRET_ACCESS_KEY": "SECRET",
    }
    # legacy JSON value still accepted
    legacy = doc_from_secret('{"rclone": {"type": "s3"}, "allow": ["b/"]}', {"xl-kind": "destination"})
    assert legacy["allow"] == ["b/"] and legacy["rclone"] == {"type": "s3"}


class FakeFeishu:
    def __init__(self, docs):
        self.docs = docs  # list of successive get_instance results (last one repeats)
        self.notes = []

    def get_instance(self, code):
        return self.docs.pop(0) if len(self.docs) > 1 else self.docs[0]

    def user_email(self, oid):
        return EMAILS.get(oid, "")

    def notify(self, oid, text):
        self.notes.append((oid, text))


class FakeLedger:
    def __init__(self):
        self.events = []

    def emit(self, *evs):
        self.events += list(evs)

    def types(self):
        return [e.EventType for e in self.events]


@pytest.fixture
def intake_settings(monkeypatch):
    from crossledger import config, worker

    s = config.Settings(
        datasets=DATASETS,
        feishu_app_id="app",
        feishu_approval_code="APPROVAL",
        feishu_fields=FIELDS,
        approver_emails=sorted(IT),
    )
    monkeypatch.setattr(worker, "settings", lambda: s)
    monkeypatch.setattr(config, "settings", lambda: s)
    return s


def _patch_worker(monkeypatch, fs, captured):
    from crossledger import feishu, worker
    from crossledger.markers import MemoryMarkers

    dest = worker.Destination(name="cloud-x", allow=["landing/"], env={})
    monkeypatch.setattr(worker.Destination, "from_key_vault", classmethod(lambda cls, n: dest))
    monkeypatch.setattr(feishu, "client", lambda: fs)
    markers = MemoryMarkers()
    monkeypatch.setattr("crossledger.markers.Markers", lambda: markers)

    def fake_execute(req, ledger, attempt, d, target, authorization):
        captured.append((req, str(target), authorization))
        return True

    monkeypatch.setattr(worker, "execute", fake_execute)
    return markers


def test_worker_ticket_ignores_message_fields(monkeypatch, intake_settings):
    from crossledger import worker

    captured, fs, ledger = [], FakeFeishu([instance()]), FakeLedger()
    markers = _patch_worker(monkeypatch, fs, captured)
    raw = json.dumps({"kind": "ticket", "instanceCode": "INST-1", "srcPrefix": "secret/", "target": "evil:x/"})
    assert worker.handle_message(raw, ledger)
    req, target, auth = captured[0]
    assert (req.srcPrefix, target, req.ticketId) == ("batch-001/", "cloud-x:landing/drive-sample/", "INST-1")
    assert auth["approver"] == "it@example.com"
    assert markers.exists("transfer/INST-1/started")
    assert ledger.types() == ["ticket.closed"]


def test_worker_ticket_withdrawn_before_and_after_start(monkeypatch, intake_settings):
    from crossledger import worker

    captured, ledger = [], FakeLedger()
    _patch_worker(monkeypatch, FakeFeishu([instance(status="CANCELED")]), captured)
    worker.handle_message(json.dumps({"kind": "ticket", "instanceCode": "INST-1"}), ledger)
    assert ledger.types() == ["ticket.withdrawn"] and not captured

    ledger = FakeLedger()
    _patch_worker(monkeypatch, FakeFeishu([instance(), instance(status="REVERTED")]), captured)
    worker.handle_message(json.dumps({"kind": "ticket", "instanceCode": "INST-1"}), ledger)
    assert ledger.types() == ["ticket.withdrawn.after-start"]
    assert ledger.events[0].Status == "Anomaly"


def test_worker_rejects_non_it_ticket_and_self_service_when_disabled(monkeypatch, intake_settings):
    from crossledger import worker

    captured, ledger, fs = [], FakeLedger(), FakeFeishu([instance(approvers=("ou_x",))])
    _patch_worker(monkeypatch, fs, captured)
    worker.handle_message(json.dumps({"kind": "ticket", "instanceCode": "INST-1"}), ledger)
    assert ledger.types() == ["transfer.rejected"] and not captured and fs.notes

    ledger = FakeLedger()
    raw = '{"requestId":"r","ticketId":"T","requesterId":"o","requesterName":"u","srcPrefix":"a","target":"cloud-x:b/"}'
    worker.handle_message(raw, ledger)
    assert ledger.types() == ["transfer.rejected"] and "disabled" in ledger.events[0].Message


def _intake(monkeypatch, s, docs, markers=None):
    from crossledger import connector
    from crossledger.markers import MemoryMarkers

    markers = markers or MemoryMarkers()
    ledger, sent, fs = FakeLedger(), [], FakeFeishu(docs)
    res = connector.handle(
        "INST-1",
        fs=fs,
        ledger=ledger,
        markers=markers,
        send_transfer=lambda b, m: sent.append((b, m)),
        s=s,
        destination=dest_lookup,
    )
    return res, ledger, sent, markers


def test_intake_approved_queues_once(monkeypatch, intake_settings):
    res, ledger, sent, markers = _intake(monkeypatch, intake_settings, [{**instance(), "start_time": "1795600000000"}])
    assert res == "queued"
    # first seen already approved: the submission is still recorded, at the ticket's creation time
    assert ledger.types() == ["ticket.submitted", "ticket.approved", "transfer.queued"]
    assert ledger.events[0].TimeGenerated.startswith("2026-11-25") and markers.exists("intake/INST-1/PENDING")
    assert sent == [
        ({"kind": "ticket", "instanceCode": "INST-1"}, tk.request_id(tk.parse_instance(instance(), FIELDS)))
    ]
    assert ledger.events[1].ActorName == "it@example.com"
    res2, ledger2, sent2, _ = _intake(monkeypatch, intake_settings, [instance()], markers)
    assert res2 == "duplicate" and not ledger2.events and not sent2


def test_intake_pending_and_rejections(monkeypatch, intake_settings):
    res, ledger, _, _ = _intake(monkeypatch, intake_settings, [instance(status="PENDING")])
    assert ledger.types() == ["ticket.submitted"] and ledger.events[0].Stage == "request"
    res, ledger, sent, _ = _intake(monkeypatch, intake_settings, [instance(approvers=("ou_x",))])
    assert res == "rejected" and ledger.types() == ["ticket.submitted", "transfer.rejected"] and not sent
    res, ledger, _, _ = _intake(monkeypatch, intake_settings, [instance(status="REJECTED")])
    assert ledger.types() == ["ticket.submitted", "ticket.rejected"]


def test_intake_withdrawn_after_start(monkeypatch, intake_settings):
    from crossledger.markers import MemoryMarkers

    _, ledger, _, _ = _intake(monkeypatch, intake_settings, [instance(status="CANCELED")])
    assert ledger.types() == ["ticket.submitted", "ticket.withdrawn"]
    m = MemoryMarkers()
    m.put("transfer/INST-1/started")
    m.put("intake/INST-1/PENDING")  # submission already recorded
    res, ledger, _, _ = _intake(monkeypatch, intake_settings, [instance(status="REVERTED")], m)
    assert res == "after-start" and ledger.types() == ["ticket.withdrawn.after-start"]
    assert ledger.events[0].Status == "Anomaly"
    # the instance vanished from the ticketing system
    res, ledger, _, _ = _intake(monkeypatch, intake_settings, [None])
    assert ledger.types() == ["ticket.source.deleted"] and ledger.events[0].Status == "Warning"
    res, ledger, _, _ = _intake(monkeypatch, intake_settings, [None], m)
    assert ledger.types() == ["ticket.source.deleted"] and ledger.events[0].Status == "Anomaly"


def test_event_message_filters_definition():
    from crossledger.connector import event_message

    ev = {"type": "approval_instance", "approval_code": "APPROVAL", "instance_code": "I", "status": "APPROVED"}
    body, mid = event_message(ev, "uuid-1", "APPROVAL")
    assert body["instanceCode"] == "I" and mid == "uuid-1"
    assert event_message({**ev, "approval_code": "OTHER"}, "u", "APPROVAL") is None
    assert event_message({**ev, "type": "leave"}, "u", "APPROVAL") is None


def test_catalog_drift_event_daily_id(monkeypatch, intake_settings):
    from crossledger import ticket_audit

    class Def(FakeFeishu):
        def get_definition(self, code):
            form = [
                {"id": "w1", "name": "数据集", "type": "radioV2", "option": [{"value": "a", "text": "drive-sample"}]},
                {"id": "w2", "name": "目的地", "type": "radioV2", "option": [{"value": "b", "text": "cloud-x"}]},
            ]
            return {"form": json.dumps(form)}

    fs = Def([instance()])
    assert ticket_audit.catalog_event(fs, ["cloud-x"], intake_settings) is None
    e1 = ticket_audit.catalog_event(fs, ["cloud-x", "idc"], intake_settings)
    e2 = ticket_audit.catalog_event(fs, ["cloud-x", "idc"], intake_settings)
    assert e1.EventType == "catalog.drift" and "idc" in e1.Message and e1.EventId == e2.EventId


def test_reconcile_requeues_unhandled(intake_settings):
    from crossledger import ticket_audit
    from crossledger.markers import MemoryMarkers

    class Lister(FakeFeishu):
        def list_instances(self, code, start, end):
            return ["INST-1"]

    m, sent = MemoryMarkers(), []
    m.put("intake/OLD/APPROVED", {"approvalCode": "APPROVAL"})
    m.put("intake/OTHER-APP/APPROVED", {"approvalCode": "PREVIOUS-DEFINITION"})  # not ours any more
    fs = Lister([instance()])
    fs.get_instance = lambda c: instance() if c == "INST-1" else None  # OLD was deleted
    assert ticket_audit.reconcile(fs, m, lambda b, mid: sent.append(mid), intake_settings) == 2
    assert sent == ["reconcile-INST-1-APPROVED", "reconcile-OLD-DELETED"]
    m.put("intake/INST-1/APPROVED")
    m.put("intake/OLD/DELETED")
    assert ticket_audit.reconcile(fs, m, lambda b, mid: sent.append(mid), intake_settings) == 0


def test_destination_change_events():
    from crossledger.ticket_audit import destination_change_events

    rows = [
        {
            "TimeGenerated": "2026-11-25T01:00:00Z",
            "Op": "SecretSet",
            "Secret": "dest-cloud-x",
            "Caller": "it@x",
            "Ref": "c1",
        },
        {
            "TimeGenerated": "2026-11-25T01:01:00Z",
            "Op": "SecretSet",
            "Secret": "dest-cloud-x-ca",
            "Caller": "it@x",
            "Ref": "c2",
        },
    ]
    evs = destination_change_events(rows)
    assert [e.TicketId for e in evs] == ["DEST-cloud-x", "DEST-cloud-x"]
    assert "private CA" in evs[1].Message and "value not recorded" in evs[0].Message


def test_unmapped_dataset_widget_is_explained():
    doc = instance()
    form = json.loads(doc["form"])
    form[0]["name"] = "数据集名称"
    doc["form"] = json.dumps(form, ensure_ascii=False)
    _, d = evaluate(doc)
    assert not d.ok and "no dataset value" in d.reason and "数据集名称 (radioV2)" in d.reason


def test_completion_message_tells_where_to_check():
    from crossledger.worker import completion_message

    msg = completion_message(
        "202611250002",
        "drive-sample",
        "cloud-x:train-data/drive-sample/",
        "INST-1",
        {"matched": 21, "bytes": 43288659, "manifest": "cloud-x:train-data/_manifests/INST-1/r.json"},
    )
    assert "请到目的地检查数据" in msg and "cloud-x:train-data/drive-sample/" in msg
    assert "21 个，41.3 MB" in msg and "_manifests/INST-1/r.json" in msg and "INST-1" in msg


def test_worker_notifies_applicant_on_completion(monkeypatch, intake_settings):
    from crossledger import worker

    captured, fs = [], FakeFeishu([instance()])
    _patch_worker(monkeypatch, fs, captured)
    worker.handle_message(json.dumps({"kind": "ticket", "instanceCode": "INST-1"}), FakeLedger())
    assert fs.notes and fs.notes[-1][0] == "ou_dev" and "请到目的地检查数据" in fs.notes[-1][1]


def test_intake_concurrent_replicas_handle_ticket_once(intake_settings):
    """Two connector replicas (rollout overlap) get the PENDING and the APPROVED message of the
    same ticket at the same time — the ticket must be approved / queued / notified only once."""
    import threading
    import time

    from crossledger import connector
    from crossledger.markers import MemoryMarkers

    class SlowFeishu(FakeFeishu):  # slow API calls after the "already handled?" check
        def user_email(self, oid):
            time.sleep(0.05)
            return super().user_email(oid)

    markers, ledger, sent, fs = MemoryMarkers(), FakeLedger(), [], SlowFeishu([instance()])
    lock = threading.Lock()

    def run():
        connector.handle(
            "INST-1",
            fs=fs,
            ledger=ledger,
            markers=markers,
            send_transfer=lambda b, m: (lock.acquire(), sent.append(m), lock.release()),
            s=intake_settings,
            destination=dest_lookup,
        )

    threads = [threading.Thread(target=run) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(sent) == 1 and ledger.types().count("ticket.approved") == 1 and len(fs.notes) == 1


def test_completion_message_when_data_already_present():
    from crossledger.worker import completion_message

    msg = completion_message(
        "S", "drive-sample", "cloud-x:a/drive-sample/", "I", {"matched": 21, "files": 0, "bytes": 0}
    )
    assert "本次新传 0 个" in msg and "其余在目的地已存在且一致" in msg


def test_trace_id_goes_to_rclone_as_client_request_id(monkeypatch):
    from crossledger import worker

    seen = []
    monkeypatch.setattr(
        worker,
        "_rclone",
        lambda args, dest, stdin=None: (
            seen.append(args) or type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        ),
    )
    req = worker.TransferRequest("RID", "INST-1", "", "", "a/", "cloud-x:b/")
    d = worker.Destination("cloud-x", ["b/"], {})
    worker.run_copy("src:x", "dst:y", d, trace=worker.trace_id(req))
    worker.run_check("src:x", "dst:y", d, trace=worker.trace_id(req))
    for args in seen:
        assert args[args.index("--header") + 1] == "x-ms-client-request-id: xl:INST-1:RID"
