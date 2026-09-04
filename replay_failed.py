#!/usr/bin/env python3
"""Re-post the leads that never reached the CRM.

`lead_store` writes every lead down before the webhook is attempted and marks
what happened to it. Anything whose last event is not "sent" is a lead somebody
is owed, and this is what sends it.

Two sources, and the second is the one that matters after an incident:

  * the local `leads.jsonl` -- instant, and correct while the instance that
    took the lead is still up.
  * `--from-cloudinary` -- the durable copy. This service has no Render disk,
    so a redeploy takes the local log with it; without this flag the mirror
    would be a store nothing ever reads, which is the same as not having one.

Usage:
    python3 replay_failed.py --dry-run              # list what is owed
    python3 replay_failed.py                        # re-post from the local log
    python3 replay_failed.py --from-cloudinary      # re-post from the mirror
    python3 replay_failed.py --lead-id abc123       # just this one

Safe to run twice: a lead that posts successfully is marked "sent" and drops
out of the set on the next run. A lead that fails again keeps its place.
"""
import argparse
import json
import os
import sys

import requests
from dotenv import load_dotenv

import lead_store
import suite_lead

load_dotenv()



def from_cloudinary() -> list:
    """Every mirrored lead for this app, newest state, paged.

    Returns [] and says why rather than raising: an unreadable mirror must not
    look like a clean sweep, so the caller checks the count it was given
    against what it expected rather than trusting silence.
    """
    if not os.getenv("CLOUDINARY_URL", "").strip():
        print("CLOUDINARY_URL is not set, so there is no mirror to read.", file=sys.stderr)
        return []
    try:
        import cloudinary
        import cloudinary.api
        import cloudinary.utils
    except ImportError:
        print("The cloudinary package is not installed here.", file=sys.stderr)
        return []

    rows, cursor = [], None
    prefix = f"leads/{lead_store.SOURCE_SLUG}/"
    while True:
        try:
            page = cloudinary.api.resources(
                resource_type="raw", type="upload", prefix=prefix,
                max_results=500, next_cursor=cursor)
        except Exception as exc:                        # noqa: BLE001
            print(f"Could not list {prefix} in Cloudinary: {exc}", file=sys.stderr)
            return rows
        for res in page.get("resources", []):
            url = res.get("secure_url") or res.get("url")
            if not url:
                continue
            try:
                body = requests.get(url, timeout=15)
                if body.status_code >= 400:
                    print(f"  ! {res.get('public_id')}: HTTP {body.status_code}", file=sys.stderr)
                    continue
                rows.append(body.json())
            except Exception as exc:                    # noqa: BLE001
                print(f"  ! {res.get('public_id')}: {exc}", file=sys.stderr)
        cursor = page.get("next_cursor")
        if not cursor:
            break
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="list what is owed and post nothing")
    ap.add_argument("--from-cloudinary", action="store_true",
                    help="read the durable mirror instead of the local log")
    ap.add_argument("--lead-id", default="", help="replay one lead by id")
    args = ap.parse_args()

    if args.from_cloudinary:
        rows = [r for r in from_cloudinary() if r.get("status") != "sent"]
        print(f"Read the Cloudinary mirror: {len(rows)} lead(s) not marked sent.")
    else:
        rows = lead_store.unsent()
        print(f"Read {lead_store.leads_path()}: {len(rows)} lead(s) not marked sent.")

    if args.lead_id:
        rows = [r for r in rows if r.get("lead_id") == args.lead_id]

    if not rows:
        if args.from_cloudinary:
            print("Nothing owed: the durable copy holds no unsent leads.")
            return 0
        # An empty local log is NOT evidence that nothing is owed. This service
        # has no Render disk, so leads.jsonl lives inside the container and dies
        # with it -- on every deploy, and on every free-tier idle spin-down,
        # which lands about fifteen minutes after the last request. Printing a
        # flat "nothing owed" off that would be the confident all-clear this
        # whole tool exists to prevent, handed over at the one moment somebody
        # is actually looking for lost leads.
        print("Nothing owed *in the local log* -- which is not the same answer.")
        if (os.getenv("CLOUDINARY_URL", "") or "").strip():
            print("\nThat log lives in the container and is destroyed on every restart\n"
                  "and every idle spin-down, so it is routinely empty. Ask the durable\n"
                  "copy before concluding anything:\n\n"
                  "    python3 replay_failed.py --from-cloudinary --dry-run\n")
        else:
            print("\nAnd CLOUDINARY_URL is not set, so there is no durable copy to check\n"
                  "either -- any lead recorded before the last restart is unrecoverable.\n")
        return 0

    for r in rows:
        f = r.get("fields") or {}
        who = f.get("contact_email") or f.get("contact_phone") or f.get("contact_name") or "(no contact)"
        print(f"  {r.get('lead_id')}  {r.get('created', '')[:19]}  {r.get('status')}  {who}")

    if args.dry_run:
        print("\n--dry-run: nothing was posted.")
        return 0

    if not suite_lead.configured():
        # Refused rather than reported as a clean run: replaying into nowhere
        # would mark every one of these "sent" and lose them a second time.
        print("\nRefusing to replay: " + suite_lead.why_not(), file=sys.stderr)
        return 2


    sent = accepted = undeliverable = failed = 0
    for r in rows:
        body = dict(r.get("fields") or {})
        if not body:
            print(f"  {r.get('lead_id')}: no fields recorded, skipping")
            failed += 1
            continue
        body["replayed"] = "true"       # so Suite can tell a replay apart
        res = suite_lead.deliver(
            source=lead_store.SOURCE_SLUG,
            page=str(r.get("page") or body.get("page") or "replay"),
            fields=suite_lead.lead_fields(body),
            pdf_url=str(body.get("report_pdf_url") or body.get("pdf_url") or ""),
            meta=suite_lead.lead_meta(
                body, extra_tags=["replayed", f"report:{r.get('kind') or 'lead'}"]),
        )
        if res["status"] == suite_lead.STATUS_DELIVERED:
            lead_store.mark(r, "sent", contact_id=res["contact_id"],
                            hub_lead_id=res["hub_lead_id"],
                            http_status=res["http_status"], replayed=True)
            print(f"  {r.get('lead_id')}: delivered, contact {res['contact_id']}")
            sent += 1
            continue
        if res["status"] == suite_lead.STATUS_ACCEPTED:
            # The Hub has it and is retrying it there. Counted as done here --
            # re-posting it on the next run would write a second lead row for
            # one visitor, which is the duplicate a single path exists to stop.
            lead_store.mark(r, "accepted", hub_lead_id=res["hub_lead_id"],
                            http_status=res["http_status"], replayed=True,
                            detail=res["detail"])
            print(f"  {r.get('lead_id')}: accepted by the Hub, delivery pending there")
            accepted += 1
            continue
        if res["status"] == suite_lead.STATUS_UNDELIVERABLE:
            # Recorded and not counted as owed: offering it again would be
            # refused identically, for ever.
            lead_store.mark(r, f"undeliverable: {res['detail'][:200]}")
            print(f"  {r.get('lead_id')}: undeliverable -- {res['detail']}")
            undeliverable += 1
            continue
        print(f"  {r.get('lead_id')}: {res['detail']}")
        lead_store.mark(r, f"failed: {res['detail'][:200]}",
                        http_status=res["http_status"])
        failed += 1

    print(f"\nDelivered {sent}, accepted by the Hub {accepted}, "
          f"undeliverable {undeliverable}, still owed {failed}.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
