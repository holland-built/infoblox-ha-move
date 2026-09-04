# Moving subnets between DHCP hosts and HA groups

Infoblox Universal DDI. Reassign subnets and their DHCP ranges from one DHCP
host or HA group to another, with a script or through the portal's CSV import.

> Lab work in a vendor tenant, September 2026. The CSV method was run twice on
> one subnet and one range, moving it and putting it back; the script was run
> many times against at most six objects. Nothing here has touched a production
> tenant. Start with a small sample in your own environment.

> [!WARNING]
> **`move_ha_group.py` is not an Infoblox product.** It is not written,
> supported, endorsed or distributed by Infoblox, so do not expect support for
> the script itself. It calls the public Universal DDI API and changes live DHCP
> configuration. Read it before you run it, run the dry run first, start with
> `--max`, and use it at your own risk. Provided as-is, with no warranty and no
> liability for anything it does to your tenant.
>
> The CSV method below uses supported Infoblox portal features. The script is the
> unofficial part.

[Read first](#read-first) · [Run the script](#run-the-script) ·
[A worked example](#a-worked-example) ·
[Fix ranges](#fix-ranges-that-serve-nothing) ·
[What we found](#what-we-found) · [CSV fallback](#csv-fallback) ·
[Not tested](#not-tested)

Most sections below fold. Read first is always open.

Links between sections do not open a closed fold, so open the one you are sent
to.

## Read first

- **Ranges do not move with their subnet.** The range is what hands out leases.
  Move both.
- **A range with no `dhcp_host` of its own serves nothing.** It does not inherit
  one from its subnet — that is Infoblox's answer, not something we watched. What
  we did see is that such a range is invisible to any search for "things on the
  old group", so neither method finds it. The script lists these separately, and
  `--fix-ranges` sets them.
- **Leases do not move either.** Clients keep their address until renewal, and at
  renewal may keep it, get another, or get none. Assign ranges before leases start
  expiring. This is Infoblox guidance, not something tested here.

Use the script if you can run Python and get an API key: it shows you the plan
before it writes, and can be capped to a few objects. Otherwise use the CSV
method — same job, more steps, and one option that can delete a lot of a tenant.

### What goes in `dhcp_host`

| Value | Means |
|---|---|
| `DC2-DHCP-HA` | An HA group, by name, exactly as the portal spells it, spaces included |
| `site-a-dhcp01` | A DHCP host, by name. One host running DHCP, which is what a site has before anyone builds a pair for it |
| *(empty)* | Nothing serves this object. On a range, that means no leases |

One field, two kinds of value. That is why a site on one host and a site on an
HA group are the same job, not two.

Use the name, not a resource id. The API calls groups `dhcp/ha_group/<uuid>` and
hosts `dhcp/host/<number>`, but the CSV column takes the name.

`dhcp/host` is not the same list as the appliances under `infra/host`. In one lab
tenant it held 167 rows against 101 there. Read the DHCP list, not the appliance
list.

## Run the script

<details>
<summary>What you need, the key check, and the four runs</summary>

- Python 3.7+, standard library only. Installing Python is out of scope; if you
  cannot, use the CSV method.
- Outbound HTTPS to `csp.infoblox.com`.
- An API key with write access: Portal > your name > User Profile > API Keys.
  Expired keys fail with `401`; make a new one rather than re-pasting.

The export lives only in the shell that ran it. In a terminal window you export
once, then run the script in that same window. Inside an agent or a CI step each
line often gets a fresh shell, so put both on one line:

```
export INFOBLOX_API_KEY='<your-key>' && python3 move_ha_group.py --list-ha-groups
```

### Is the key set?

Ask the shell you are about to run in. This prints the length, never the key:

```
[ -n "$INFOBLOX_API_KEY" ] && echo "set, ${#INFOBLOX_API_KEY} characters" \
  || echo "not set in this shell"
```

`not set` means the export did not reach this shell. Export it again here.

A set variable is not a working key. `--list-ha-groups` is the real test: it
either lists your groups or fails with `401`. A `401` means the key is wrong or
expired, so make a new one rather than re-pasting.

```
export INFOBLOX_API_KEY=<your-key>

# 1. exact group names
python3 move_ha_group.py --list-ha-groups

# 2. dry run: prints the plan, writes a report, changes nothing
python3 move_ha_group.py --old "<old>" --new "<new>"

# 3. pilot: cap it, then check
python3 move_ha_group.py --old "<old>" --new "<new>" --max 5 --apply --verify

# 4. the rest
python3 move_ha_group.py --old "<old>" --new "<new>" --apply --verify

# rollback: same command, names swapped
python3 move_ha_group.py --old "<new>" --new "<old>" --apply --verify
```

### Options

| Flag | What it does | Example |
|---|---|---|
| `--apply` | Writes. Without it, every run is a dry run | `--apply` |
| `--space NAME` | Only objects in one IP space | `--space Corporate` |
| `--subnet CIDR` | Only this subnet and its ranges. Repeat for more | `--subnet 10.20.30.0/24` |
| `--max N` | Cap the run at about N objects | `--max 5` |
| `--verify` | Re-read afterwards; report anything left on the old group | `--verify` |
| `--list-ha-groups` | Print every HA group with its id, then exit | `--list-ha-groups` |
| `--list-hosts` | Print every DHCP host with its id, then exit | `--list-hosts` |
| `--fix-ranges` | A separate job, not a move. See below | `--fix-ranges --old "<name>"` |
| `--report` | Where to write the per-object CSV | `--report pilot.csv` |
| `--old-id`, `--new-id` | A resource id instead of a name | `--old-id dhcp/ha_group/1a2b...` |

### Choosing which subnets move

Without a filter the run takes everything on the source. That is right when you
are retiring a group. It is wrong for the commoner job of moving one site.

```
--space "Corporate"                       one IP space
--subnet 10.20.30.0/24                    one subnet and its ranges
--subnet 10.20.30.0/24 --subnet 10.40.0.0/16    several
```

A named subnet always brings its own ranges. Leaving a range behind is the
mistake this tool exists to stop, so it is not offered.

Narrowing happens before `--max`, so the cap counts what is left.

Verified: a run with `--subnet` moved that subnet and its range and left the
other subnet on the source untouched.

### How `--max` counts

`--max` is the blast-radius control. Reach for it on the first run: move five,
check them in the portal, then run again without it.

It counts objects, not subnets. A subnet is one object and each range is another.

It takes whole units. A unit is a subnet with every range inside it, or a lone
range whose parent subnet is not moving. It never splits a subnet from its
ranges, so the count is approximate.

Say the old group holds this, and you pass `--max 5`:

| Unit | Objects | `--max 5` |
|---|---|---|
| Subnet A + 2 ranges | 3 | moves — 3 used |
| Subnet B + 4 ranges | 5 | skipped — 3 + 5 is over 5 |
| Subnet C, no ranges | 1 | moves — 4 used |
| Lone range in a subnet staying put | 1 | moves — 5 used |

Five objects moved, out of ten. A skipped unit does not stop the run: the script
keeps going and takes later units that still fit.

The first unit is the exception. It is always taken, even when it is bigger than
N on its own. `--max 2` against a subnet with six ranges moves all seven objects.

You do not choose which units. Run the dry run first and read the report to see
what the next run would take.

</details>


## A worked example

<details>
<summary>A lone host, or one pair to another, step by step, with output</summary>


Site A runs one DHCP host, `site-a-dhcp01`. A new pair, `SITE-AB-HA`, is built
and ready. Every subnet and range on the host moves onto the pair.

**Pair to pair is this same walkthrough.** Only the name after `--old` changes.
The steps, the flags and the undo are identical, and
[Group to group](#group-to-group) at the end shows the one line that differs.

**`--old` and `--new` take a name.** An HA group or a DHCP host, either side, in
any combination. Not a subnet, and not a subnet id. Spell it as the portal
spells it.

The script works out which kind it is. A name that exists as both is refused
rather than guessed at. Use `--old-id` and `--new-id` to settle that, or any
time you would rather be exact.

**By default you do not name the subnets.** The script moves every subnet and
every range whose `dhcp_host` is the source. `--space` and `--subnet` narrow
that, and `--max` caps how many objects a run touches. See
[Choosing which subnets move](#choosing-which-subnets-move).

### 1. Get the exact names

Two lists, because groups and hosts live apart:

```
export INFOBLOX_API_KEY='<your-key>'
python3 move_ha_group.py --list-hosts
```

Name, type, IP space, id. A `nios_ddi` host cannot serve a subnet, and is
refused before any write:

```
site-a-dhcp01                uddi   Corporate   dhcp/host/10001
site-b-dhcp01                uddi   Corporate   dhcp/host/10002
old-grid-member              nios_ddi      -           dhcp/host/10003
```

```
python3 move_ha_group.py --list-ha-groups
```

Name, mode, IP space, id:

```
SITE-AB-HA         active-passive   Corporate   dhcp/ha_group/9f8e...
SITE-B-HA-OLD      active-passive   Corporate   dhcp/ha_group/1a2b...
```

Copy the names from that output. Every object you move must sit in the target's
IP space. The script checks each one before it writes, and the server refuses a
mismatch anyway.

### 2. Dry run

```
python3 move_ha_group.py --old "site-a-dhcp01" --new "SITE-AB-HA"
```

It reads the tenant and prints the plan:

```
From : site-a-dhcp01  (DHCP host, dhcp/host/10001)
To   : SITE-AB-HA  (HA group, dhcp/ha_group/9f8e...)
       target IP space: Corporate
Mode : DRY RUN - no changes

  subnets to move: 4
  ranges  to move: 6

  subnet  10.20.30.0/24                        Floor 2 data
  range   10.20.30.50-10.20.30.200             Floor 2 pool

DRY RUN complete. Nothing was changed.
Full plan written to: /path/ha-move-report.csv
```

A dry run sends no writes. It writes one local file, `ha-move-report.csv`, with
a row per object. Read that file before step 3.

Watch for a warning about ranges with no `dhcp_host`. Those are dark today and a
move will not touch them. See [Fix ranges](#fix-ranges-that-serve-nothing).

### 3. Pilot five, then check

```
python3 move_ha_group.py --old "site-a-dhcp01" --new "SITE-AB-HA" \
  --max 5 --apply --verify
```

Open those five in the portal. The **Edit** dialog shows the assignment; the side
panel does not.

### 4. The rest, then confirm

```
python3 move_ha_group.py --old "site-a-dhcp01" --new "SITE-AB-HA" \
  --apply --verify
```

`--verify` re-reads afterwards and prints what is still on the source.

### 5. Undo, if you need it

```
python3 move_ha_group.py --old "SITE-AB-HA" --new "site-a-dhcp01" \
  --apply --verify
```

Names swapped, and the host is now the target. Each run builds its plan from the
current state, so this finds everything now on the pair.

That is not always the same set. Anything else already on the pair comes back
with yours. If the pair held objects before your move, add `--subnet` to name
only what you moved, and read the dry run before applying.

### Group to group

Identical, with a group name on both sides:

```
python3 move_ha_group.py --old "SITE-B-HA-OLD" --new "SITE-AB-HA" --apply --verify
```

Everything above applies unchanged: the dry run, `--max`, `--verify`, and the
swapped command to undo it.

### Two sites at once

One new pair, two sources. Site A runs a lone host. Site B sits in an old pair
whose partner is gone.

```
site-a-dhcp01   one DHCP host           subnets carry dhcp_host = site-a-dhcp01
SITE-B-HA-OLD   a degraded HA group     subnets carry dhcp_host = SITE-B-HA-OLD
```

Two sources, so two runs. Same target both times:

```
python3 move_ha_group.py --old "site-a-dhcp01" --new "SITE-AB-HA" --apply --verify
python3 move_ha_group.py --old "SITE-B-HA-OLD" --new "SITE-AB-HA" --apply --verify
```

Dry run each one first, as above.

**Consider not moving the second set at all.** Edit the degraded group in the
portal, swap the dead host for the new one, rename it. Every subnet on it follows
with no subnet edits. Then only the single-host site needs a run.

Which is less work depends on the counts. Read both dry runs before choosing.

### Different subnets to different groups

The script does one source and one target per run. For two targets, run it
twice. It cannot send some subnets on one source one way and the rest another
way.

The CSV method can: `dhcp_host` is a per-row value, so one file can send row A
to one group and row B to another. We have not tested a mixed file.

### The two CSV files

`move.csv` and `rollback.csv` are the same rows, twice. `rollback.csv` is the
untouched copy, straight from the export. `move.csv` is the copy where you set
`dhcp_host` to the new group. One is the change, the other is the way back.

They are not one file per subnet group.

</details>


## Fix ranges that serve nothing

<details>
<summary>The warning, the fix, and the flag's rules</summary>


A range hands out leases only when its own `dhcp_host` is set. An empty one
serves nothing, and it does not inherit from its subnet. Nothing points those
ranges at the source, so a move cannot see them and only warns.

### How you find out

Any move run tells you, dry run included:

```
  WARNING: 2 range(s) inside these subnets have no dhcp_host of their own.
           A range serves leases only when its own dhcp_host is set, so
           these are not serving now and this tool does not change them.
             10.0.0.2-10.0.0.4
             10.0.0.5-10.0.0.10
```

Those two hand out no leases today. They did not break during the move. They
were already dark.

### Fixing them

`--fix-ranges` gives each one the value its own parent subnet uses. Dry run
first, as always:

```
python3 move_ha_group.py --fix-ranges --old "DC2-DHCP-HA"
```

```
On   : DC2-DHCP-HA  (HA group, dhcp/ha_group/1a2b...)
Mode : DRY RUN - no changes

Reading subnets and ranges ...
  subnets found: 4

  ranges with no dhcp_host: 2

  10.0.0.2-10.0.0.4                  -> dhcp/ha_group/1a2b...
  10.0.0.5-10.0.0.10                 -> dhcp/ha_group/1a2b...

DRY RUN complete. Nothing was changed.
```

Then write them:

```
python3 move_ha_group.py --fix-ranges --old "DC2-DHCP-HA" --apply --verify
```

```
Applied. set=2 failed=0 skipped=0

Verifying ...
  Clean: every range inside those subnets now has a dhcp_host.
```

### The flag

| | |
|---|---|
| Takes | `--old` or `--old-id`, naming an HA group or a DHCP host |
| Refuses | `--new`. The value comes from each range's own subnet |
| Writes | Only with `--apply`. Without it, a dry run and a report |
| Pairing | `--verify` needs `--apply`. On a dry run it is refused, since a dry run already shows what is outstanding |
| Caps | `--max N` is a plain count here. A range has nothing under it |
| Skips | A range that gained a value since the plan was built |
| Skips | A range whose parent subnet has moved since the plan was built |
| Undo | Set `dhcp_host` back to empty. Tested through the API, `null` or `""` |
| Quiet | A run that finds nothing writes no report. It says so and stops |

### Where it fits

Run it before the move. The ranges then carry the old name, the move sees them,
and everything lands together. Run it after and they get the new name directly.
Either order works.

**It is a separate run, on purpose.** A move rewrites a field that already has a
value. This fills a field that is empty. One run, one kind of undo.

</details>


## What we found

<details>
<summary>Everything the lab tenant actually did</summary>


- **A DHCP host works as the target too.** Every rollback wrote one: two subnets
  and a range went from an HA group back onto a host, `HTTP 200` each, twice over.
  So all four directions have been written, not only planned.
- **An HA group holds exactly two hosts, always.** Sending one is refused with
  `Expects two hosts in the group`, and `port` in the payload is refused as read
  only. A host cannot be freed from a group; the group has to go first. That is
  why moving onto a host means picking one that was never paired.
- **A DHCP host as the source works, both ways.** Run in a lab tenant with
  `--apply`: two subnets and a range moved off a host onto an HA group, three
  objects changed, none failed, and `--verify` came back clean. The swapped
  command put all three back on the host. Two objects of the three were
  pre-existing, not built for the test.
- **A subnet cannot point at a host that is already in an HA group.** Creating
  one is refused with `400 The Host is already assigned to a HA Group`. So once
  a host joins a pair, the group name is the value that works, not the host
  name. Seen on create; we did not try the same thing as an edit.
- **A target reporting no IP space is decided by the server, not by us.** The
  script says it is skipping its precheck and lets the writes go. One such group
  took them. A newly built group in an earlier round refused them. So the skip
  buys you the server's answer, per object, and nothing more.
- **Not every DHCP host can serve a subnet.** A host has a `type`. A
  `uddi` host works. A `nios_ddi` host is refused on every write with
  `Cannot assign host of type: NIOS DDI to Subnet object`. That tenant held 119
  of the first and 48 of the second, which is also why `dhcp/host` outnumbers
  `infra/host`. `--list-hosts` prints the type, and a `nios_ddi` target is now
  refused before anything is written.
- **A host already in an HA group cannot be a subnet's `dhcp_host`.** Refused on
  create and on edit alike, with `The Host is already assigned to a HA Group`.
  Point the subnet at the group instead, which works.
- **`--fix-ranges` works.** Two ranges serving nothing were set to their parent
  subnet's host, `set=2 failed=0`, and `--verify` came back clean. Both were then
  put back as they were.
- **A `dhcp_host` can be emptied through the API.** `null` and `""` both leave it
  null, and setting it again restores it. So switching a range off is a real undo
  for the line above. This is the API; the CSV import is still untested here.
- **A partial run can be reversed.** Each run rebuilds its plan from the current
  state, so after moving three of six objects with `--max` the swapped command
  found exactly those three. That was a capped run, not an interruption or a real
  error. If you do interrupt one, the report is still written, and a fresh dry run
  shows where things actually stand.
- **`--verify` reports what is left.** After a narrowed run it names the flag
  that narrowed it and exits 0. After a run that asked for everything, anything
  left is called out as unexpected and the exit code is 1. Objects the server
  refused are counted as failures, not as leftovers.
- **One HA group serves one IP space**, through its hosts, and the server enforces
  it. A subnet from another space is refused with an error naming both spaces.
- **A newly built group has nothing assigned yet** and so reports no IP space,
  which is what makes the script skip its own check. In that round the server
  still refused, so the skip cost a failed run, not a wrong one.
- **The plan comes from a filtered query**, not a walk of the whole tenant, so it
  returns quickly even on a large one. That filter is undocumented by Infoblox; if
  it stops working the script says so and falls back to reading every subnet and
  range, which takes minutes. That fallback covers a rejected filter, `400` or
  `422`. Any other failure stops the run instead of quietly reading everything.
- **Scale is untested.** Writes are throttled to about five a second, so a large
  move is paced by that rather than by the API. Nothing bigger than six objects
  has been run.
- **Every object on a group is in one IP space**, so aiming at a group whose
  hosts serve another space refuses outright rather than moving part of the set.
  Inferred from one refusal, not stated by Infoblox.

</details>


## CSV fallback

<details>
<summary>The portal route: export, edit one column, import</summary>


The `dhcp_host` column takes a DHCP host name exactly as it takes an HA group
name. A site on one host and a site on a pair are the same edit, in the same
column. A `nios_ddi` host is refused here too, by the same server rule.

> **Import type must be "Add new records and update existing records."** Never one
> with **"delete"** in the label: by their own wording those remove every object
> missing from the imported file, and your file is trimmed. We did not test one,
> for obvious reasons.

| Step | Do this | Watch for |
|---|---|---|
| **1. Export** | Integrations > Data Import / Export > Export. Tick Subnets and Ranges, CSV, skip failed records | No filter: the object-types screen offers checkboxes per type and nothing to narrow by IP space or HA group, so you get the whole tenant. A previous export of this tenant took ~16 minutes per its job history; the two imports we timed took ~15 minutes each |
| **2. Two files** | Copy the download to `rollback.csv` and trim it to the subnets you are moving **and their range rows**. Copy that to `move.csv` | Keep both `HEADER-` lines untouched. Rows you delete are never visited by an add-and-update import |
| **3. Edit `move.csv`** | Set `dhcp_host` to the new group's name on every subnet **and** range row. Change nothing else | Do not blank the cell. We do not know what an empty value does on import and did not test it |
| **4. Diff** | Compare `move.csv` against `rollback.csv` | Only `dhcp_host` should differ, only on data rows. Note your row counts |
| **5. Import** | Import tab, pick `move.csv`, tick Subnets and Ranges, import type as above, skip failed records, Start | Another ~15 minutes |
| **6. Check** | Counts match your row numbers, error log empty, spot-check one subnet and one range | Wait for **Import complete** first: an unreached type shows `0 of 0`, which looks like finding none. Use the **Edit** dialog — the side panel never showed HA group |

Anything wrong: import `rollback.csv` the same way.

`rollback.csv` restores **every column** of those rows as they were when you
exported, not just `dhcp_host`. If anyone changed anything else on those objects
in the meantime, a rollback silently undoes it. Re-export first if that is a
risk, or put `dhcp_host` back by hand.

</details>


## Not tested

<details>
<summary>What nobody here has run</summary>


Either method past six objects. Live leases — there were no clients on the lab
range. A real mid-run error; recovery from an artificial one works. Blanking a
`dhcp_host` cell in a CSV. A delete-type import.

We did not run an export in this round, only the two imports. The export screen
was inspected directly and offers no filter; the ~16 minute figure comes from
this tenant's export job history rather than from a run we timed.

</details>

---

Portal paths correct September 2026.

