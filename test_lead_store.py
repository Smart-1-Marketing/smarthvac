#!/usr/bin/env python3
"""The lead is written down before it is delivered, and every failure is loud.

Run: python3 test_lead_store.py

Drives the real module against a throwaway directory. No pytest, no network:
Cloudinary is off (CLOUDINARY_URL unset) so the mirror is skipped, which is
itself one of the things asserted -- an app with no Cloudinary must still keep
its local record rather than losing the lead twice over.
"""
import json
import pathlib
import importlib
import os
import shutil
import sys
import tempfile

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def main():
    tmp = tempfile.mkdtemp(prefix="leadstore-test-")
    os.environ["LEADS_DIR"] = tmp
    os.environ.pop("LEADS_FILE", None)
    os.environ.pop("CLOUDINARY_URL", None)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import lead_store

    print("the record is written before anything is attempted")
    row = lead_store.record({"contact_email": "a@b.com", "company": "Acme Co"})
    check("record returns a lead_id", bool(row.get("lead_id")))
    check("it starts pending", row.get("status") == "pending", row.get("status"))
    check("the log exists on disk", os.path.exists(lead_store.leads_path()))
    check("the lead is in unsent()", any(r["lead_id"] == row["lead_id"]
                                         for r in lead_store.unsent()))

    print("\nan unset webhook leaves a replayable record, not nothing")
    # This is the defect in one line: the old code returned here and the lead
    # ceased to exist. The record must survive the same branch.
    lead_store.mark(row, "failed: no webhook URL set")
    owed = {r["lead_id"]: r for r in lead_store.unsent()}
    check("still owed after a failure", row["lead_id"] in owed)
    check("the reason is kept", "no webhook" in owed[row["lead_id"]]["status"])
    check("the fields survive for replay",
          owed[row["lead_id"]]["fields"].get("contact_email") == "a@b.com")

    print("\na delivered lead drops out of the owed set")
    lead_store.mark(row, "sent", http_status=200)
    check("no longer owed", row["lead_id"] not in {r["lead_id"] for r in lead_store.unsent()})
    check("but still on file", row["lead_id"] in {r["lead_id"] for r in lead_store.rows()})

    print("\nthe log is append-only and reduces to the latest state")
    lines = [l for l in open(lead_store.leads_path()).read().splitlines() if l.strip()]
    check("three events were appended, not overwritten", len(lines) == 3, f"got {len(lines)}")
    check("rows() collapses them to one", len(lead_store.rows()) == 1)
    check("and keeps the last status", lead_store.rows()[0]["status"] == "sent")

    print("\na torn line does not hide the leads above it")
    with open(lead_store.leads_path(), "a") as fh:
        fh.write('{"lead_id": "half-writ')          # what a crash mid-append leaves
    check("the good rows still read", len(lead_store.rows()) == 1)

    print("\nnothing in it raises, whatever it is handed")
    try:
        lead_store.mark(None, "sent")               # not a row
        lead_store.record({"x": None, "y": ""})     # empties dropped
        os.environ["LEADS_FILE"] = "/proc/nonexistent/leads.jsonl"
        lead_store.record({"a": "b"})               # unwritable path
        os.environ["LEADS_FILE"] = ""
        check("survives bad input and an unwritable log", True)
    except Exception as exc:                        # noqa: BLE001
        check("survives bad input and an unwritable log", False, repr(exc))

    print("\nlong values are capped so a line stays small")
    os.environ.pop("LEADS_FILE", None)
    big = lead_store.record({"note": "x" * 99999})
    check("capped at MAX_VALUE",
          len(big["fields"]["note"]) == lead_store.MAX_VALUE,
          len(big["fields"]["note"]))

    print("\nthe three answers the Hub gives are three states, not two")
    os.environ.pop("LEADS_FILE", None)
    os.environ["LEADS_DIR"] = tmp3 = tempfile.mkdtemp(prefix="leadstore-hub-")
    importlib.reload(lead_store)

    delivered = lead_store.mark(lead_store.record({"email": "a@example.com"}),
                                "sent", contact_id="ghl_1")
    at_hub = lead_store.mark(lead_store.record({"email": "b@example.com"}),
                             "accepted", hub_lead_id="hub_1")
    nobody = lead_store.mark(lead_store.record({"website": "https://x.example"}),
                             "undeliverable: nothing to contact them on")
    owed = lead_store.mark(lead_store.record({"email": "d@example.com"}),
                           "failed: Couldn't reach the Hub")

    unsent_ids = {r["lead_id"] for r in lead_store.unsent()}
    check("a delivered lead is not owed", delivered["lead_id"] not in unsent_ids)
    # The Hub holds it and is retrying it there. Re-posting it from here would
    # write a second lead row for one visitor, which is the duplicate a single
    # write path exists to prevent.
    check("a lead the Hub accepted is not owed either",
          at_hub["lead_id"] not in unsent_ids)
    # An abandoned form that never reached the contact step. Left as a failure
    # it would sit in the owed count for ever and be replayed on every run --
    # a backlog that can never clear.
    check("a lead with nobody to contact is not owed",
          nobody["lead_id"] not in unsent_ids)
    check("a real failure still is", owed["lead_id"] in unsent_ids)
    check("owed counts exactly the one that is", len(unsent_ids) == 1, unsent_ids)

    # Counted apart rather than merged: "stored at the Hub" and "in the CRM"
    # are different claims, and so is "there was nobody to write down".
    check("the Hub's copies are counted on their own",
          [r["lead_id"] for r in lead_store.accepted()] == [at_hub["lead_id"]])
    check("the contactless ones are counted on their own",
          [r["lead_id"] for r in lead_store.undeliverable()] == [nobody["lead_id"]])
    check("a delivered lead keeps the contact id that proves it",
          delivered.get("contact_id") == "ghl_1", delivered.get("contact_id"))

    shutil.rmtree(tmp3, ignore_errors=True)

    print("\nnothing is delivered to a webhook any more")
    src = pathlib.Path("app.py").read_text() + pathlib.Path("replay_failed.py").read_text()
    # The constant is still read on /health, to say a leftover value should be
    # cleared -- what must not come back is a POST to it.
    check("no module posts to a webhook URL",
          "requests.post(WEBHOOK_URL" not in src and "post(WEBHOOK_URL" not in src)
    check("delivery goes through the one Hub path",
          "suite_lead.deliver(" in src)

    shutil.rmtree(tmp, ignore_errors=True)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
