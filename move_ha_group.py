#!/usr/bin/env python3
"""
Move subnets and DHCP ranges from one DHCP host or HA group to another.

Infoblox Universal DDI (Infoblox Portal / csp.infoblox.com).

NOT AN INFOBLOX PRODUCT. This script is not written, supported, endorsed or
distributed by Infoblox, and no support case can be raised against it. It calls
the public API and changes live DHCP configuration. Read it before running it,
use the dry run first, start with --max, and accept that you run it at your own
risk. Provided as-is, with no warranty of any kind.

Runs in DRY-RUN by default: it shows exactly what it would change and writes a
CSV report, but sends no writes. Add --apply to actually make the changes. A run
that finds nothing, or that stops at the IP-space precheck, writes no report.

A subnet's dhcp_host holds either an HA group or one DHCP host, so --old and
--new take either. The kind is worked out from the name, and from the collection
prefix when an id is given.

Before writing anything it runs a same-IP-space precheck. A target is bound to
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
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://csp.infoblox.com/api/ddi/v1"
PAGE = 1000
ENV_VAR = "INFOBLOX_API_KEY"
REPORT_DEFAULT = "ha-move-report.csv"


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
HOST_FIELDS = "id,name,ip_space,type"

# The two things a dhcp_host value can point at. An HA group is the usual
# target; a DHCP host is one host running the DHCP service, which is what a site
# has before anyone builds a pair for it. Both answer to the same field, so both
# belong on --old and --new.
#
# "DHCP host" is Infoblox's own word for the dhcp/host collection, and it is not
# the same list as the appliances under infra/host. In one lab tenant dhcp/host
# held 167 rows against 101 there, so do not treat either as a subset of the
# other.
COLLECTIONS = (
    ("dhcp/ha_group", "HA group", HA_FIELDS),
    ("dhcp/host", "DHCP host", HOST_FIELDS),
)


def q(value):
    """Quote a string for use as a literal inside a _filter expression.

    The filter grammar escapes an embedded delimiter by doubling it, so a quote
    becomes two quotes and a backslash is left alone. json.dumps was tried here
    and is wrong for this: it emits backslash escapes and unicode escape sequences that
    the grammar does not promise to decode. Group names are the realistic case,
    since resource ids are plain hex and dashes.
    """
    return '"%s"' % str(value).replace('"', '""')


def _find_named(collection, fields, name, key):
    """Every object in one collection with this exact name."""
    return get_all("/" + collection, key, fields, {"_filter": "name==%s" % q(name)})


def find_target(name, key):
    """Look up one HA group, or one DHCP host, by name.

    Auto-detect rather than a second flag. Whoever runs this reads a name off a
    subnet and does not necessarily know which kind of thing owns it. Both
    collections are searched. A name that exists in both is refused rather than
    guessed at, because picking the wrong one moves live DHCP to the wrong place.
    """
    hits = []
    for collection, kind, fields in COLLECTIONS:
        for row in _find_named(collection, fields, name, key):
            row["kind"] = kind
            hits.append(row)
    if not hits:
        raise ApiError(
            'Nothing named "%s". Run --list-ha-groups and --list-hosts to see '
            'the names.' % name)
    if len(hits) > 1:
        found = ", ".join("%s %s" % (h["kind"], h["id"]) for h in hits)
        raise ApiError(
            'More than one thing named "%s": %s. Use --old-id/--new-id to say '
            'which.' % (name, found))
    return hits[0]


def find_by_id(res_id, key):
    """Fetch one HA group, or one DHCP host, by resource id.

    The id says which collection it lives in - dhcp/ha_group/<uuid> against
    dhcp/host/<number> - so there is nothing to detect here.
    """
    for collection, kind, fields in COLLECTIONS:
        if res_id.startswith(collection + "/"):
            ident = res_id.rsplit("/", 1)[-1]
            url = "%s/%s/%s?%s" % (BASE, collection, ident,
                                   urllib.parse.urlencode({"_fields": fields}))
            _, payload = _request("GET", url, key)
            row = payload.get("result") or {}
            if not row:
                raise ApiError("No %s with id %s" % (kind, res_id))
            row["kind"] = kind
            return row
    raise ApiError('Unrecognised id "%s". Expected dhcp/ha_group/<uuid> or '
                   'dhcp/host/<id>.' % res_id)


def resolve_target(name, res_id, key):
    """Take whichever of name or id was given and return the group or host."""
    return find_by_id(res_id, key) if res_id else find_target(name, key)


def list_ha_groups(key):
    """Every HA group in the tenant, for --list-ha-groups."""
    rows = get_all("/dhcp/ha_group", key, HA_FIELDS)
    rows.sort(key=lambda r: (r.get("name") or "").lower())
    return rows


def list_hosts(key):
    """Every DHCP host in the tenant, for --list-hosts."""
    rows = get_all("/dhcp/host", key, HOST_FIELDS)
    rows.sort(key=lambda r: (r.get("name") or "").lower())
    return rows


def find_space(name, key):
    """Look up one IP space by name, or take a resource id as given."""
    if name.startswith("ipam/ip_space/"):
        return name
    rows = get_all("/ipam/ip_space", key, "id,name", {"_filter": "name==%s" % q(name)})
    if not rows:
        raise ApiError('No IP space named "%s".' % name)
    if len(rows) > 1:
        raise ApiError('More than one IP space named "%s". Give the resource id.' % name)
    return rows[0]["id"]


def space_names(key):
    """Map ip_space resource id -> friendly name, for readable messages."""
    try:
        rows = get_all("/ipam/ip_space", key, "id,name")
    except ApiError:
        return {}
    return {r["id"]: r.get("name") or r["id"] for r in rows}


# ---------------------------------------------------------------- planning

def _server_filter(old_id):
    """Build a server-side _filter expression for objects on one group or server."""
    return "dhcp_host==%s" % q(old_id)


def build_plan(old_id, key, max_changes=None, space_id=None, cidrs=None):
    """Return (subnets, ranges) that currently point at old_id.

    space_id and cidrs narrow that set. Without them the answer is everything on
    old_id, which is right for retiring a group and wrong for the far commoner
    job of moving one site. --max caps a count but chooses for you; these two say
    which. Narrowing happens before the cap, so --max counts what is left.

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
        if space_id and o.get("space") != space_id:
            return False
        return True

    s = [o for o in subnets if keep(o)]
    r = [o for o in ranges if keep(o)]

    if cidrs:
        wanted = set(cidrs)
        s = [o for o in s if "%s/%s" % (o.get("address"), o.get("cidr")) in wanted]
        # A typo here would otherwise read as "nothing to do" and exit 0, which
        # looks exactly like a finished job.
        missing = sorted(wanted - set("%s/%s" % (o.get("address"), o.get("cidr"))
                                      for o in s))
        if missing:
            raise ApiError(
                "These --subnet values are not on that source: %s. Check the "
                "spelling, and that they still point at it." % ", ".join(missing))
        # A range goes only if its own subnet goes. Naming a subnet and leaving
        # its ranges behind is the mistake this whole tool exists to stop.
        parents = set(o["id"] for o in s)
        r = [o for o in r if o.get("parent") in parents]

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

    Nothing in the tenant points these at the old target, so build_plan() cannot
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
    """Split objects into (compatible, incompatible) against the target's
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
    cols = ["object_type", "id", "parent_id", "label", "name", "space",
            "current_dhcp_host", "new_dhcp_host", "action", "result"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for row in rows:
            w.writerow(row)


def _looks_like_cidr(value):
    """True for an IPv4 address and prefix. Checks the numbers, not the shape.

    A shape test alone passes 999.999.999.999/99, which then reads as a subnet
    that is simply not on the source - a wrong answer dressed as a real one.
    """
    m = re.match(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})/(\d{1,2})$", value)
    if not m:
        return False
    octets = [int(g) for g in m.groups()[:4]]
    return all(o <= 255 for o in octets) and int(m.group(5)) <= 32


def _why_narrowed(args):
    """The flags that explain why a run left objects behind."""
    bits = []
    if args.max is not None:
        bits.append("--max")
    if args.space:
        bits.append("--space")
    if args.subnet:
        bits.append("--subnet")
    return ", ".join(bits)


def _narrowing(args, space_label):
    """One line saying what --space and --subnet cut the plan down to."""
    bits = []
    if args.space:
        bits.append("IP space %s" % space_label)
    if args.subnet:
        bits.append("subnet " + ", ".join(args.subnet))
    return "; ".join(bits)


# ------------------------------------------------------------- fix-ranges

def fix_ranges(old, key, args, sname, space_id=None):
    """Give every range with no dhcp_host the one its parent subnet uses.

    A separate run from a move, deliberately. A move rewrites a field that
    already has a value and is undone by swapping two names, which we have run.
    This fills a field that is empty, and its undo is to blank the field again,
    which the API does accept. Mixed into one run, a half failure leaves you
    guessing which half to reverse.

    These ranges serve no leases at all, so this switches them on rather than
    moving them. Nothing points them at the old target, which is why a move
    cannot see them and only warns.
    """
    print("On   : %s  (%s, %s)" % (old.get("name"), old["kind"], old["id"]))
    if space_id or args.subnet:
        print("Only : %s" % _narrowing(args, sname(space_id)))
    print("Mode : %s" % ("APPLY - changes will be written"
                         if args.apply else "DRY RUN - no changes"))
    print("")
    print("Reading subnets and ranges ...")

    subnets, _ = build_plan(old["id"], key, None, space_id, args.subnet)
    print("  subnets found: %d" % len(subnets))
    if not subnets:
        print("")
        print("Nothing points at that %s. No action." % old["kind"])
        return 0

    unassigned, checked = find_unassigned_ranges(subnets, key)
    if not checked:
        print("")
        print("  NOTE: the lookup did not finish (too many subnets, or a call")
        print("        failed). What is listed is real, but there may be more.")
        print("        This run cannot finish the job. Its exit code says so.")
    if not unassigned:
        print("")
        if not checked:
            # An unfinished scan that found nothing is not the same answer as a
            # finished scan that found nothing. Saying "no action" here would
            # tell the operator the job is done when it was never looked at.
            print("Found none, but the scan did not finish, so this is not a clean")
            print("bill of health. Narrow it with --space or --subnet and run again.")
            return 1
        print("Every range inside those subnets already has a dhcp_host. No action.")
        return 0

    # One range is one object, and a range has nothing under it, so the cap is a
    # plain count here. The move needs whole units; this does not.
    total = len(unassigned)
    if args.max is not None:
        unassigned = unassigned[:args.max]

    # Each range takes the value its own parent subnet uses.
    by_id = dict((s["id"], s) for s in subnets)
    rows = []
    for r in unassigned:
        parent = by_id.get(r.get("parent")) or {}
        rows.append({
            "object_type": "range",
            "id": r["id"],
            "parent_id": r.get("parent"),
            "label": describe("range", r),
            "name": r.get("name") or "",
            "space": sname(r.get("space")),
            "current_dhcp_host": "",
            "new_dhcp_host": parent.get("dhcp_host") or "",
            "action": "PATCH dhcp_host",
            "result": "" if args.apply else "dry-run (not sent)",
        })

    print("")
    print("  ranges with no dhcp_host: %d%s"
          % (total, "" if args.max is None else " (%d selected by --max)" % len(rows)))
    print("")
    for row in rows[:40]:
        print("  %-34s -> %s" % (row["label"], row["new_dhcp_host"] or "(parent has none)"))
    if len(rows) > 40:
        print("  ... and %d more (see the report)" % (len(rows) - 40))
    print("")

    if not args.apply:
        write_report(args.report, rows)
        print("DRY RUN complete. Nothing was changed.")
        print("Full plan written to: %s" % os.path.abspath(args.report))
        print("Re-run the same command with --apply to set these.")
        return 0

    ok = fail = skipped = 0
    try:
        for row in rows:
            if not row["new_dhcp_host"]:
                row["result"] = "SKIPPED: parent subnet has no dhcp_host either"
                skipped += 1
                print("  SKIPPED %s: parent has none" % row["label"])
                continue
            try:
                # Someone may have set it since the plan was built. Only fill a
                # range that is still empty; never overwrite a real value.
                live = current_dhcp_host("ipam/range", row["id"], key)
                if live:
                    row["result"] = "SKIPPED: now set to %s" % live
                    skipped += 1
                    print("  SKIPPED %s: now %s" % (row["label"], live))
                    continue
                # And the value comes from the parent as it is now. Writing the
                # snapshot's value would point the range at a host the subnet
                # left, which is the exact breakage this mode exists to undo.
                parent_now = current_dhcp_host("ipam/subnet", row["parent_id"], key)
                if parent_now != row["new_dhcp_host"]:
                    row["result"] = ("SKIPPED: parent subnet is now %s"
                                     % (parent_now or "unset"))
                    skipped += 1
                    print("  SKIPPED %s: parent moved to %s"
                          % (row["label"], parent_now or "unset"))
                    continue
                status = patch_dhcp_host("ipam/range", row["id"], row["new_dhcp_host"], key)
                row["result"] = "HTTP %s" % status
                ok += 1
            except ApiError as e:
                row["result"] = "FAILED: %s" % str(e).splitlines()[0]
                fail += 1
                print("  FAILED %s: %s" % (row["label"], row["result"]))
            time.sleep(0.2)
    finally:
        write_report(args.report, rows)

    print("")
    print("Applied. set=%d failed=%d skipped=%d" % (ok, fail, skipped))
    print("Report written to: %s" % os.path.abspath(args.report))

    if args.verify:
        print("")
        print("Verifying ...")
        left, rechecked = find_unassigned_ranges(subnets, key)
        if not rechecked:
            # The flag comes before the count. An unfinished re-scan cannot say
            # "clean", and it cannot say a leftover is the only one either.
            print("  The re-scan did not finish, so this is not proof. Run it again.")
            return 1
        if left:
            print("  STILL with no dhcp_host: %d range(s)%s"
                  % (len(left), "" if args.max is None else " (--max)"))
            # Under --max leftovers are the point of the run. A failed write
            # never is, so it still decides the exit code.
            return 1 if (fail or args.max is None) else 0
        print("  Clean: every range inside those subnets now has a dhcp_host.")

    # A scan that never finished cannot report a finished job, however many
    # ranges it did set.
    return 1 if (fail or not checked) else 0


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
        description="Move subnets and ranges between DHCP hosts and HA groups in Universal DDI.",
        epilog="Dry run by default. Nothing is written unless you pass --apply.")
    p.add_argument("--old", help="Name of the HA group or DHCP host to move away from")
    p.add_argument("--new", help="Name of the HA group or DHCP host to move to")
    p.add_argument("--old-id", help="Resource id instead of --old, e.g. dhcp/ha_group/<uuid> or dhcp/host/<id>")
    p.add_argument("--new-id", help="Resource id instead of --new")
    p.add_argument("--space", metavar="NAME",
                   help="Only objects in this IP space, by name or resource id")
    p.add_argument("--subnet", action="append", metavar="CIDR", default=None,
                   help="Only this subnet and its ranges, e.g. 10.20.30.0/24. "
                        "Repeat for more than one")
    p.add_argument("--max", type=int, metavar="N",
                   help="Pilot: about N objects, taken as whole subnet-plus-its-ranges "
                        "units, so a subnet with many ranges can exceed N rather than "
                        "be split from them")
    p.add_argument("--apply", action="store_true",
                   help="Actually write the changes. Without this it is a dry run.")
    p.add_argument("--verify", action="store_true",
                   help="After the run, re-query and confirm nothing is left on the old group")
    p.add_argument("--report", default=REPORT_DEFAULT, help="CSV report path")
    p.add_argument("--list-ha-groups", action="store_true", help="List HA groups and exit")
    p.add_argument("--list-hosts", action="store_true",
                   help="List DHCP hosts (one host running DHCP) and exit")
    p.add_argument("--fix-ranges", action="store_true",
                   help="Separate job: give every range with no dhcp_host the one "
                        "its parent subnet uses. Takes --old, never --new")
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
        # Listing is its own run. Silently ignoring the rest of a command line
        # would let someone believe a move had been considered.
        if args.list_ha_groups and args.list_hosts:
            p.error("--list-ha-groups and --list-hosts list different things; "
                    "run one at a time")
        if args.list_ha_groups or args.list_hosts:
            # "is not None" rather than truthiness: --max 0 is still an
            # argument someone typed, and ignoring it hides their mistake.
            busy = [n for n, v in (("--old", args.old), ("--old-id", args.old_id),
                                   ("--new", args.new), ("--new-id", args.new_id),
                                   ("--space", args.space), ("--subnet", args.subnet),
                                   ("--apply", args.apply), ("--verify", args.verify),
                                   ("--fix-ranges", args.fix_ranges))
                    if v is not None and v is not False]
            if args.max is not None:
                busy.append("--max")
            if args.report != REPORT_DEFAULT:
                busy.append("--report")
            if busy:
                p.error("a list flag only lists. Drop %s, or drop the list flag"
                        % ", ".join(busy))

        if args.list_ha_groups:
            names = space_names(key)
            for g in list_ha_groups(key):
                print("%-45s %-20s %-28s %s" % (
                    g.get("name"), g.get("mode", ""),
                    names.get(g.get("ip_space"), g.get("ip_space") or "-"), g.get("id")))
            return 0

        if args.list_hosts:
            names = space_names(key)
            for h in list_hosts(key):
                print("%-45s %-13s %-28s %s" % (
                    h.get("name"), h.get("type") or "-",
                    names.get(h.get("ip_space"), h.get("ip_space") or "-"), h.get("id")))
            return 0

        if args.fix_ranges:
            if args.new or args.new_id:
                p.error("--fix-ranges takes --old only; it sets ranges to match "
                        "their own subnet, so there is nothing to give as --new")
        elif not (args.old or args.old_id) or not (args.new or args.new_id):
            p.error("need --old/--new (or --old-id/--new-id)")
        if not (args.old or args.old_id):
            p.error("need --old (or --old-id)")
        if args.old and args.old_id:
            p.error("give --old or --old-id, not both")
        if args.new and args.new_id:
            p.error("give --new or --new-id, not both")
        for c in args.subnet or []:
            if not _looks_like_cidr(c):
                p.error('--subnet takes an IPv4 address and a prefix, e.g. '
                        '10.20.30.0/24, not "%s"' % c)
        if args.max is not None and args.max < 1:
            p.error("--max must be 1 or more")
        if args.verify and not args.apply:
            p.error("--verify only means something with --apply; a dry run already "
                    "shows what is still outstanding")

        old = resolve_target(args.old, args.old_id, key)
        space_id = find_space(args.space, key) if args.space else None

        names = space_names(key)

        def sname(s):
            return names.get(s, s or "-")

        if args.fix_ranges:
            return fix_ranges(old, key, args, sname, space_id)

        new = resolve_target(args.new, args.new_id, key)

        if old["id"] == new["id"]:
            print("ERROR: source and target are the same.", file=sys.stderr)
            return 2

        # A dhcp/host row is not always something a subnet can point at. A host
        # of type nios_ddi is a NIOS appliance seen through this API, and the
        # server answers "Cannot assign host of type: NIOS DDI to Subnet object"
        # on every write. Refuse once here rather than once per object.
        if new.get("type") == "nios_ddi":
            print("ERROR: %s is a NIOS DDI host. A subnet or range cannot point at"
                  % new.get("name"), file=sys.stderr)
            print("       one; the server refuses every write. Pick a uddi "
                  "host or an", file=sys.stderr)
            print("       HA group. --list-hosts shows the type.", file=sys.stderr)
            return 2

        target_space = new.get("ip_space")

        print("From : %s  (%s, %s)" % (old.get("name"), old["kind"], old["id"]))
        print("To   : %s  (%s, %s)" % (new.get("name"), new["kind"], new["id"]))
        print("       target IP space: %s" % sname(target_space))
        print("Mode : %s" % ("APPLY - changes will be written" if args.apply else "DRY RUN - no changes"))
        if space_id or args.subnet:
            print("Only : %s" % _narrowing(args, sname(space_id)))
        print("")
        print("Reading subnets and ranges ...")

        subnets, ranges = build_plan(old["id"], key, args.max,
                                     space_id, args.subnet)
        print("  subnets found: %d" % len(subnets))
        print("  ranges  found: %d" % len(ranges))

        if not subnets and not ranges:
            print("")
            print("Nothing points at that %s. No action." % old["kind"])
            return 0

        # --- unassigned ranges --------------------------------------------------
        # Ranges with no dhcp_host of their own are pointed at no group, so
        # nothing points them at the old group and the plan above cannot see them.
        unassigned, checked = find_unassigned_ranges(subnets, key)
        if not checked:
            print("")
            print("  NOTE: the check for ranges with no dhcp_host of their own did not")
            print("        finish (too many subnets, or a lookup failed). Any listed")
            print("        below are real, but there may be more.")
        if unassigned:
            print("")
            print("  WARNING: %d range(s) inside these subnets have no dhcp_host of their own."
                  % len(unassigned))
            print("           A range serves leases only when its own dhcp_host is set, so")
            print("           these are not serving now and this tool does not change them.")
            for o in unassigned[:10]:
                print("             %s" % describe("range", o))
            if len(unassigned) > 10:
                print("             ... and %d more" % (len(unassigned) - 10))
            print("           Set a dhcp_host on each one - normally the same group or")
            print("           host as the subnet it sits in.")


        # --- IP-space precheck -------------------------------------------------
        # HA groups are bound to an IP space. The portal's picker does not filter
        # on this and only fails at save; check once here instead of per object.
        s_ok, s_bad = check_spaces(subnets, target_space)
        r_ok, r_bad = check_spaces(ranges, target_space)
        bad = s_bad + r_bad

        print("")
        if not target_space:
            print("PRECHECK: target %s reports no IP space - skipping the check."
                  % new["kind"])
            print("          The server is still the authority; failures will be per object.")
        elif bad:
            print("PRECHECK FAILED: %d object(s) are in a different IP space than the" % len(bad))
            print("                 target %s (%s)." % (new["kind"], sname(target_space)))
            print("                 The server will reject these.")
            for o in bad[:10]:
                kind = "subnet" if o in s_bad else "range"
                print("    %-7s %-24s space=%s" % (kind, describe(kind, o), sname(o.get("space"))))
            if len(bad) > 10:
                print("    ... and %d more" % (len(bad) - 10))
            print("")
            print("Refusing to start. Pick a target in the same IP space as the")
            print("objects you are moving.")
            return 2
        else:
            print("PRECHECK OK: all objects are in the target's IP space (%s)."
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
                    "parent_id": o.get("parent") or "",
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
        stranded, astray = set(), set()
        try:
            for row in rows:
                coll = "ipam/subnet" if row["object_type"] == "subnet" else "ipam/range"
                # Subnets are written first. If one of them failed, its ranges
                # must stay with it: a range on the new target under a subnet
                # still on the old one is the split this tool exists to prevent.
                if row["object_type"] == "range" and row.get("parent_id") in stranded:
                    row["result"] = "SKIPPED: its subnet failed to move"
                    skipped += 1
                    print("  SKIPPED range %s: its subnet failed" % row["label"])
                    continue
                try:
                    # The plan was built moments ago, but someone else may have
                    # touched this object since. Only move what still points at
                    # the group we were asked to move away from.
                    live = current_dhcp_host(coll, row["id"], key)
                    if live != old["id"]:
                        # Already on the target is a job someone else finished.
                        # Anywhere else is a third party moving it mid-run, and
                        # the run must not report that as done.
                        elsewhere = live != new["id"]
                        row["result"] = ("SKIPPED: now on %s" % (live or "nothing")
                                         if elsewhere
                                         else "SKIPPED: already on the target")
                        skipped += 1
                        if elsewhere:
                            astray.add(row["id"])
                        print("  SKIPPED %s %s: now %s" % (
                            row["object_type"], row["label"], live or "unset"))
                        continue
                    status = patch_dhcp_host(coll, row["id"], new["id"], key)
                    row["result"] = "HTTP %s" % status
                    ok += 1
                except ApiError as e:
                    row["result"] = "FAILED: %s" % str(e).splitlines()[0]
                    fail += 1
                    if row["object_type"] == "subnet":
                        stranded.add(row["id"])
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
            # Only objects the server accepted. One that failed is already
            # counted and printed as a failure; calling it "did not persist"
            # sends the reader looking for a second, different problem.
            written = set(row["id"] for row in rows
                          if str(row["result"]).startswith("HTTP"))
            still = [o for o in ls + lr if o.get("id") in written]
            unwritten = len(still)
            if unwritten:
                print("  FAILED TO PERSIST: %d object(s) the server accepted are still"
                      % unwritten)
                print("                     on the old group. Re-run the dry run.")
                for o in still[:10]:
                    print("    %s" % (o.get("address") or o.get("start") or o.get("id")))
            elif ls or lr:
                # Leftovers are expected only when the run was narrowed. On a run
                # that asked for everything they are a surprise, and calling them
                # "not selected" would hide it behind an exit code of 0.
                narrowed = args.max is not None or args.space or args.subnet
                if narrowed:
                    print("  Everything this run selected has moved. %d subnets and %d "
                          "ranges are still on the old group and were not selected "
                          "(%s)." % (len(ls), len(lr), _why_narrowed(args)))
                else:
                    print("  UNEXPECTED: nothing narrowed this run, yet %d subnets and"
                          % len(ls))
                    print("              %d ranges still point at the old %s. They"
                          % (len(lr), old["kind"]))
                    print("              appeared after the plan was built. Re-run "
                          "the dry run.")
                    unwritten = len(ls) + len(lr)
            else:
                print("  Clean: nothing still points at the old %s." % old["kind"])

        # Non-zero if a write failed, or if something we wrote did not stick.
        # A caller scripting this needs a failed move to look like a failure.
        if fail or unwritten or astray:
            return 1
        return 0

    except ApiError as e:
        # Piped output buffers stdout but not stderr, so an unflushed plan would
        # print after the error that stopped it.
        sys.stdout.flush()
        print("ERROR: %s" % e, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
