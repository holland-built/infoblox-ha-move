#!/usr/bin/env python3
"""
Move subnets and DHCP ranges from one DHCP HA group to another.

Infoblox Universal DDI (Infoblox Portal / csp.infoblox.com).

NOT AN INFOBLOX PRODUCT. This script is not written, supported, endorsed or
distributed by Infoblox, and no support case can be raised against it. It calls
the public API and changes live DHCP configuration. Read it before running it,
use the dry run first, start with --max, and accept that you run it at your own
risk. Provided as-is, with no warranty of any kind.

Runs in DRY-RUN by default: it shows exactly what it would change and writes a
CSV report, but sends no writes. Add --apply to actually make the changes. A run
that finds nothing, or that stops at the IP-space precheck, writes no report.

Before writing anything it runs a same-IP-space precheck. HA groups are bound to
an IP space and the server rejects any subnet whose space does not match, but
the portal's picker does NOT filter on this - it offers every HA group in the
tenant and only fails at save time. Checking once up front avoids discovering
that once per subnet across a large run.

Standard library only, no pip install. Developed and run on macOS with
Python 3; nothing here is platform-specific but other platforms are untested.
"""

import argparse
import csv
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://csp.infoblox.com/api/ddi/v1"
PAGE = 1000
ENV_VAR = "INFOBLOX_API_KEY"


# ---------------------------------------------------------------- transport

class ApiError(Exception):
    """An API call failed. `status` is the HTTP code where there was one."""

    def __init__(self, message, status=None):
        Exception.__init__(self, message)
        self.status = status


def _request(method, url, key, body=None, timeout=60):
    """Make one API call and return (status, parsed body).

    Every request goes through here so authentication, TLS and error wording are
    the same everywhere. HTTP failures become ApiError carrying the status code,
    which is what lets build_plan tell "the server rejected my filter" apart
    from "the key is dead" instead of retrying an expensive read on both.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Token " + key)
    req.add_header("Accept", "application/json")
    if data:
        req.add_header("Content-Type", "application/json")
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        if e.code == 401:
            raise ApiError(
                "401 Unauthorized - the API key was rejected. Check that %s is set "
                "to a current key and has not been revoked." % ENV_VAR, e.code)
        if e.code == 403:
            raise ApiError(
                "403 Forbidden - the key is valid but this account lacks permission "
                "for %s %s" % (method, url), e.code)
        raise ApiError("HTTP %s on %s %s\n%s" % (e.code, method, url, detail), e.code)
    except urllib.error.URLError as e:
        raise ApiError("Could not reach %s (%s). Check network/proxy." % (url, e.reason))


def get_all(path, key, fields, extra=None):
    """GET a collection, following _offset paging. Returns list of dicts.

    Ordered by id so the same query gives the same order every time: the plan
    you review in a dry run has to be the plan --apply acts on.

    Advances by however many rows came back rather than by PAGE, and stops only
    on an empty page. The server is allowed to return fewer rows than _limit
    asks for; assuming a short page means the end would silently truncate the
    plan and quietly miss objects.
    """
    out, offset = [], 0
    while True:
        params = {"_fields": fields, "_limit": PAGE, "_offset": offset,
                  "_order_by": "id"}
        if extra:
            params.update(extra)
        url = BASE + path + "?" + urllib.parse.urlencode(params)
        _, payload = _request("GET", url, key)
        if "results" not in payload:
            raise ApiError("Unexpected response from %s: no 'results' field." % path)
        chunk = payload["results"] or []
        out.extend(chunk)
        if not chunk:
            return out
        offset += len(chunk)


def current_dhcp_host(collection, obj_id, key):
    """Read one object's dhcp_host as it is right now. None if unset."""
    uuid = obj_id.rsplit("/", 1)[-1]
    url = "%s/%s/%s?%s" % (BASE, collection, uuid,
                          urllib.parse.urlencode({"_fields": "id,dhcp_host"}))
    _, payload = _request("GET", url, key)
    return (payload.get("result") or {}).get("dhcp_host")


def patch_dhcp_host(collection, obj_id, new_host, key):
    """PATCH one subnet/range. obj_id is the full resource id, e.g. ipam/subnet/<uuid>."""
    uuid = obj_id.rsplit("/", 1)[-1]
    url = "%s/%s/%s" % (BASE, collection, uuid)
    status, _ = _request("PATCH", url, key, {"dhcp_host": new_host})
    return status


# ---------------------------------------------------------------- lookups

HA_FIELDS = "id,name,mode,ip_space"


def q(value):
    """Quote a string for use as a literal inside a _filter expression.

    The filter grammar escapes an embedded delimiter by doubling it, so a quote
    becomes two quotes and a backslash is left alone. json.dumps was tried here
    and is wrong for this: it emits backslash escapes and unicode escape sequences that
    the grammar does not promise to decode. Group names are the realistic case,
    since resource ids are plain hex and dashes.
    """
    return '"%s"' % str(value).replace('"', '""')


def find_ha_group(name, key):
    """Look up one HA group by name. Raises if there is not exactly one."""
    flt = 'name==%s' % q(name)
    rows = get_all("/dhcp/ha_group", key, HA_FIELDS, {"_filter": flt})
    if not rows:
        raise ApiError(
            'No HA group named "%s". Run with --list-ha-groups to see the names.' % name)
    if len(rows) > 1:
        raise ApiError('More than one HA group named "%s" - use --old-id/--new-id.' % name)
    return rows[0]


def find_ha_group_by_id(group_id, key):
    """Fetch one HA group by resource id, e.g. dhcp/ha_group/<uuid>."""
    uuid = group_id.rsplit("/", 1)[-1]
    url = "%s/dhcp/ha_group/%s?%s" % (
        BASE, uuid, urllib.parse.urlencode({"_fields": HA_FIELDS}))
    _, payload = _request("GET", url, key)
    row = payload.get("result") or {}
    if not row:
        raise ApiError("No HA group with id %s" % group_id)
    return row


def resolve_group(name, group_id, key):
    """Take whichever of name or id was given and return the group."""
    return find_ha_group_by_id(group_id, key) if group_id else find_ha_group(name, key)


def list_ha_groups(key):
    """Every HA group in the tenant, for --list-ha-groups."""
    rows = get_all("/dhcp/ha_group", key, HA_FIELDS)
    rows.sort(key=lambda r: (r.get("name") or "").lower())
    return rows


def space_names(key):
    """Map ip_space resource id -> friendly name, for readable messages."""
    try:
        rows = get_all("/ipam/ip_space", key, "id,name")
    except ApiError:
        return {}
    return {r["id"]: r.get("name") or r["id"] for r in rows}


# ---------------------------------------------------------------- planning

def _server_filter(old_id):
    """Build a server-side _filter expression for objects on one HA group."""
    return "dhcp_host==%s" % q(old_id)


def build_plan(old_id, key, max_changes=None):
    """Return (subnets, ranges) that currently point at old_id.

    Asks the server to filter on dhcp_host. That turns
    a full walk of every subnet and range in the tenant into one small request -
    on a large tenant the difference is minutes versus under a second.

    The server-side filter on dhcp_host is not documented, so it is treated as an
    optimisation only: whatever comes back is still checked client-side by keep()
    below, and any failure falls back to the full unfiltered walk. The result is
    identical either way.
    """
    expr = _server_filter(old_id)
    extra = {"_filter": expr} if expr else None

    try:
        subnets = get_all("/ipam/subnet", key, "id,address,cidr,name,space,dhcp_host", extra)
        ranges = get_all("/ipam/range", key,
                         "id,start,end,name,space,dhcp_host,parent", extra)
    except ApiError as e:
        # Only retry unfiltered when the server rejected the filter itself. An
        # auth failure or an outage must surface, not trigger a slow full walk
        # of every subnet and range in the tenant.
        if extra is None or e.status not in (400, 422):
            raise
        print("  (server rejected the filter; falling back to a full read, "
              "which is slow on a large tenant)")
        subnets = get_all("/ipam/subnet", key, "id,address,cidr,name,space,dhcp_host")
        ranges = get_all("/ipam/range", key,
                         "id,start,end,name,space,dhcp_host,parent")

    def keep(o):
        if o.get("dhcp_host") != old_id:
            return False
        return True

    s = [o for o in subnets if keep(o)]
    r = [o for o in ranges if keep(o)]
    if max_changes is not None:
        s, r = _cap(s, r, max_changes)
    return s, r


def _cap(subnets, ranges, limit):
    """Cut the plan down to at most `limit` objects without splitting a subnet
    from its own ranges.

    Work is grouped into units first: a subnet together with every range inside
    it, and separately each range whose parent subnet is not itself moving. Units
    are taken whole until the next one would not fit, so a capped run never
    leaves a subnet on the new group with its ranges still on the old one.

    Two earlier versions of this were wrong. Slicing each list to `limit`
    independently split subnets from their ranges. Capping subnets and then
    taking all of their ranges honoured the pairing but blew past `limit`, and
    silently discarded ranges whose parent was not moving, so repeated capped
    runs would never move them at all.
    """
    by_parent = {}
    for o in ranges:
        by_parent.setdefault(o.get("parent"), []).append(o)

    units = [[sub] + by_parent.pop(sub.get("id"), []) for sub in subnets]
    # Ranges left over belong to subnets that are not moving. They are real work
    # and must not be dropped, so each becomes a unit in its own right.
    for leftovers in by_parent.values():
        units.extend([o] for o in leftovers)

    ks, kr, used = [], [], 0
    for unit in units:
        if used and used + len(unit) > limit:
            continue
        for o in unit:
            (ks if "cidr" in o else kr).append(o)
        used += len(unit)
    return ks, kr


def find_unassigned_ranges(subnets, key, max_lookups=200):
    """Ranges inside the moving subnets that have no dhcp_host of their own.

    Called "unassigned" rather than "inherited" on purpose: Infoblox told us a
    range serves leases only when its own dhcp_host is set, so a null one is not
    quietly inheriting the subnet's group, it is serving nothing.

    Nothing in the tenant points these at the old HA group, so build_plan() cannot
    see them, and this tool does not change them.

    They matter because subnet and range assignments are independent in Universal
    DDI: a range issues leases only when its own dhcp_host is populated. A range
    left null is not served, whatever its subnet points at. Reporting them is the
    only way the operator finds out.

    Returns (ranges, checked) - checked is False if the lookup was skipped.
    """
    if not subnets:
        return [], True
    if len(subnets) > max_lookups:
        return [], False
    found = []
    for sub in subnets:
        sid = sub.get("id")
        if not sid:
            continue
        try:
            rs = get_all("/ipam/range", key, "id,start,end,name,space,dhcp_host,parent",
                         {"_filter": "parent==%s" % q(sid)})
        except ApiError:
            # Partway through, so whatever was found is incomplete. Say so; the
            # caller must not treat a short list as the whole answer.
            return found, False
        found.extend([r for r in rs if not r.get("dhcp_host")])
    return found, True


def check_spaces(objects, target_space):
    """Split objects into (compatible, incompatible) against the target HA group's
    IP space. If the group reports no ip_space, everything passes - we do not have
    enough information to judge, and the server remains the authority."""
    if not target_space:
        return list(objects), []
    ok, bad = [], []
    for o in objects:
        (ok if o.get("space") == target_space else bad).append(o)
    return ok, bad


def describe(kind, o):
    """One-line label for an object, for printing and for the report."""
    if kind == "subnet":
        return "%s/%s" % (o.get("address", "?"), o.get("cidr", "?"))
    return "%s-%s" % (o.get("start", "?"), o.get("end", "?"))


def write_report(path, rows):
    """Write the per-object CSV: one row per object, with its result."""
    cols = ["object_type", "id", "label", "name", "space",
            "current_dhcp_host", "new_dhcp_host", "action", "result"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for row in rows:
            w.writerow(row)


# ---------------------------------------------------------------- main

def main():
    """Parse arguments, build the plan, show it, and write it if asked.

    The order matters and is deliberate: resolve both groups, refuse if they are
    the same, build the plan, warn about ranges with no group of their own, run
    the IP-space precheck, print the plan, and only then write. A dry run stops
    before the write and is the default. Subnets are written before ranges so a
    range is never left pointing at a host that no longer serves its subnet.

    Returns a shell exit code: 0 fine, 1 something failed or was left behind,
    2 refused before writing anything.
    """
    p = argparse.ArgumentParser(
        description="Move subnets and ranges between DHCP HA groups in Universal DDI.",
        epilog="Dry run by default. Nothing is written unless you pass --apply.")
    p.add_argument("--old", help="Name of the HA group to move away from")
    p.add_argument("--new", help="Name of the HA group to move to")
    p.add_argument("--old-id", help="Resource id instead of --old, e.g. dhcp/ha_group/<uuid>")
    p.add_argument("--new-id", help="Resource id instead of --new")
    p.add_argument("--max", type=int, metavar="N",
                   help="Pilot: about N objects, taken as whole subnet-plus-its-ranges "
                        "units, so a subnet with many ranges can exceed N rather than "
                        "be split from them")
    p.add_argument("--apply", action="store_true",
                   help="Actually write the changes. Without this it is a dry run.")
    p.add_argument("--verify", action="store_true",
                   help="After the run, re-query and confirm nothing is left on the old group")
    p.add_argument("--report", default="ha-move-report.csv", help="CSV report path")
    p.add_argument("--list-ha-groups", action="store_true", help="List HA groups and exit")
    p.epilog = ((p.epilog or "") +
                "\n\nNot an Infoblox product: unsupported, unaffiliated, "
                "no warranty. Use at your own risk.")
    args = p.parse_args()

    key = os.environ.get(ENV_VAR, "").strip()
    if not key:
        print("ERROR: environment variable %s is not set." % ENV_VAR, file=sys.stderr)
        print("  macOS/Linux:  export %s='your-key'" % ENV_VAR, file=sys.stderr)
        print("  Windows:      $env:%s='your-key'" % ENV_VAR, file=sys.stderr)
        return 2

    try:
        if args.list_ha_groups:
            names = space_names(key)
            for g in list_ha_groups(key):
                print("%-45s %-20s %-28s %s" % (
                    g.get("name"), g.get("mode", ""),
                    names.get(g.get("ip_space"), g.get("ip_space") or "-"), g.get("id")))
            return 0

        if not (args.old or args.old_id) or not (args.new or args.new_id):
            p.error("need --old/--new (or --old-id/--new-id)")
        if args.old and args.old_id:
            p.error("give --old or --old-id, not both")
        if args.new and args.new_id:
            p.error("give --new or --new-id, not both")
        if args.max is not None and args.max < 1:
            p.error("--max must be 1 or more")
        if args.verify and not args.apply:
            p.error("--verify only means something with --apply; a dry run already "
                    "shows what is still on the old group")

        old = resolve_group(args.old, args.old_id, key)
        new = resolve_group(args.new, args.new_id, key)

        if old["id"] == new["id"]:
            print("ERROR: source and target HA group are the same.", file=sys.stderr)
            return 2

        names = space_names(key)
        target_space = new.get("ip_space")

        def sname(s):
            return names.get(s, s or "-")

        print("From : %s  (%s)" % (old.get("name"), old["id"]))
        print("To   : %s  (%s)" % (new.get("name"), new["id"]))
        print("       target HA group IP space: %s" % sname(target_space))
        print("Mode : %s" % ("APPLY - changes will be written" if args.apply else "DRY RUN - no changes"))
        print("")
        print("Reading subnets and ranges ...")

        subnets, ranges = build_plan(old["id"], key, args.max)
        print("  subnets found: %d" % len(subnets))
        print("  ranges  found: %d" % len(ranges))

        if not subnets and not ranges:
            print("")
            print("Nothing points at that HA group. No action.")
            return 0

        # --- unassigned ranges --------------------------------------------------
        # Ranges with no dhcp_host of their own are pointed at no group, so
        # nothing points them at the old group and the plan above cannot see them.
        unassigned, checked = find_unassigned_ranges(subnets, key)
        if not checked:
            print("")
            print("  NOTE: the check for ranges with no HA group of their own did not")
            print("        finish (too many subnets, or a lookup failed). Any listed")
            print("        below are real, but there may be more.")
        if unassigned:
            print("")
            print("  WARNING: %d range(s) inside these subnets have no HA group of their own."
                  % len(unassigned))
            print("           A range serves leases only when its own dhcp_host is set, so")
            print("           these are not serving now and this tool does not change them.")
            for o in unassigned[:10]:
                print("             %s" % describe("range", o))
            if len(unassigned) > 10:
                print("             ... and %d more" % (len(unassigned) - 10))
            print("           Set an HA group on each one - normally the same group as the")
            print("           subnet it sits in.")


        # --- IP-space precheck -------------------------------------------------
        # HA groups are bound to an IP space. The portal's picker does not filter
        # on this and only fails at save; check once here instead of per object.
        s_ok, s_bad = check_spaces(subnets, target_space)
        r_ok, r_bad = check_spaces(ranges, target_space)
        bad = s_bad + r_bad

        print("")
        if not target_space:
            print("PRECHECK: target HA group reports no IP space - skipping the check.")
            print("          The server is still the authority; failures will be per object.")
        elif bad:
            print("PRECHECK FAILED: %d object(s) are in a different IP space than the" % len(bad))
            print("                 target HA group (%s)." % sname(target_space))
            print("                 The server will reject these.")
            for o in bad[:10]:
                kind = "subnet" if o in s_bad else "range"
                print("    %-7s %-24s space=%s" % (kind, describe(kind, o), sname(o.get("space"))))
            if len(bad) > 10:
                print("    ... and %d more" % (len(bad) - 10))
            print("")
            print("Refusing to start. Pick an HA group whose hosts are in the same")
            print("IP space as the objects you are moving.")
            return 2
        else:
            print("PRECHECK OK: all objects are in the target HA group's IP space (%s)."
                  % sname(target_space))

        subnets, ranges = s_ok, r_ok
        if not subnets and not ranges:
            print("")
            print("Nothing left to move after the precheck.")
            return 2

        print("")
        print("  subnets to move: %d" % len(subnets))
        print("  ranges  to move: %d" % len(ranges))
        print("")

        rows = []
        for kind, items in (("subnet", subnets), ("range", ranges)):
            for o in items:
                rows.append({
                    "object_type": kind,
                    "id": o["id"],
                    "label": describe(kind, o),
                    "name": o.get("name") or "",
                    "space": sname(o.get("space")),
                    "current_dhcp_host": o.get("dhcp_host") or "",
                    "new_dhcp_host": new["id"],
                    "action": "PATCH dhcp_host",
                    "result": "" if args.apply else "dry-run (not sent)",
                })

        # Preview: subnets first, then ranges - matching apply order.
        for row in rows[:40]:
            print("  %-7s %-34s %s" % (row["object_type"], row["label"], row["name"]))
        if len(rows) > 40:
            print("  ... and %d more (see the report)" % (len(rows) - 40))
        print("")

        if not args.apply:
            write_report(args.report, rows)
            print("DRY RUN complete. Nothing was changed.")
            print("Full plan written to: %s" % os.path.abspath(args.report))
            print("Re-run the same command with --apply to make these changes.")
            return 0

        # Subnets before ranges: a range whose host no longer serves the parent
        # subnet stops issuing leases, so close that window from the top down.
        ok = fail = skipped = 0
        try:
            for row in rows:
                coll = "ipam/subnet" if row["object_type"] == "subnet" else "ipam/range"
                try:
                    # The plan was built moments ago, but someone else may have
                    # touched this object since. Only move what still points at
                    # the group we were asked to move away from.
                    live = current_dhcp_host(coll, row["id"], key)
                    if live != old["id"]:
                        row["result"] = "SKIPPED: no longer on the old group"
                        skipped += 1
                        print("  SKIPPED %s %s: now %s" % (
                            row["object_type"], row["label"], live or "unset"))
                        continue
                    status = patch_dhcp_host(coll, row["id"], new["id"], key)
                    row["result"] = "HTTP %s" % status
                    ok += 1
                except ApiError as e:
                    row["result"] = "FAILED: %s" % str(e).splitlines()[0]
                    fail += 1
                    print("  FAILED %s %s: %s" % (row["object_type"], row["label"], row["result"]))
                # Deliberate throttle: roughly five writes a second, to stay well
                # clear of any API rate limit on a large run.
                time.sleep(0.2)
        finally:
            # Always leave a report behind, including after Ctrl-C or a crash,
            # so there is a record of what was already written.
            write_report(args.report, rows)

        print("")
        print("Applied. changed=%d failed=%d skipped=%d" % (ok, fail, skipped))
        print("Report written to: %s" % os.path.abspath(args.report))

        # Verification has to distinguish two different leftovers: objects this
        # run never selected (expected under --max) from objects it did select
        # and believed it had written. Only the second is a failure. Comparing
        # against the planned ids says which, where a bare count could not.
        unwritten = 0
        if args.verify:
            print("")
            print("Verifying ...")
            ls, lr = build_plan(old["id"], key)
            planned = set(row["id"] for row in rows)
            still = [o for o in ls + lr if o.get("id") in planned]
            unwritten = len(still)
            if unwritten:
                print("  FAILED TO PERSIST: %d object(s) this run wrote are still on"
                      % unwritten)
                print("                     the old group. Re-run the dry run.")
                for o in still[:10]:
                    print("    %s" % (o.get("address") or o.get("start") or o.get("id")))
            elif ls or lr:
                print("  Everything this run selected has moved. %d subnets and %d "
                      "ranges are still on the old group and were not selected%s."
                      % (len(ls), len(lr), " (--max)" if args.max else ""))
            else:
                print("  Clean: nothing still points at the old HA group.")

        # Non-zero if a write failed, or if something we wrote did not stick.
        # A caller scripting this needs a failed move to look like a failure.
        if fail or unwritten:
            return 1
        return 0

    except ApiError as e:
        print("ERROR: %s" % e, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
