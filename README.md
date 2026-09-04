# Moving subnets between DHCP HA groups

Infoblox Universal DDI. Reassign subnets and their DHCP ranges from one DHCP HA
group to another, with a script or through the portal's CSV import.

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
[A worked example](#a-worked-example) · [What we found](#what-we-found) ·
[CSV fallback](#csv-fallback) · [Not tested](#not-tested)

## Read first

- **Ranges do not move with their subnet.** The range is what hands out leases.
  Move both.
- **A range with no HA group of its own serves nothing.** It does not inherit one
  from its subnet — that is Infoblox's answer, not something we watched. What we
  did see is that such a range is invisible to any search for "things on the old
  group", so neither method finds it. The script lists these separately.
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
| *(empty)* | Nothing serves this object. On a range, that means no leases |

Use the name, not a resource id. The API calls groups `dhcp/ha_group/<uuid>`, but
the CSV column takes the name.

## Run the script

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
| `--max N` | Cap the run at about N objects | `--max 5` |
| `--verify` | Re-read afterwards; report anything left on the old group | `--verify` |
| `--list-ha-groups` | Print every HA group with its id, then exit | `--list-ha-groups` |
| `--report` | Where to write the per-object CSV | `--report pilot.csv` |
| `--old-id`, `--new-id` | A resource id instead of a name | `--old-id dhcp/ha_group/1a2b...` |

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

## A worked example

One site, two groups. Everything on `DC2-DHCP-HA-OLD` moves to `DC2-DHCP-HA-NEW`.

**`--old` and `--new` take an HA group name.** Not a subnet, not a subnet id.
Spell the name as the portal spells it. Use `--old-id` and `--new-id` if you
prefer the resource id `dhcp/ha_group/<uuid>`.

**You do not name the subnets.** The script moves every subnet and every range
whose `dhcp_host` is the old group. There is no subnet filter. You pick the pair
of groups, and `--max` caps how many objects a run touches.

### 1. Get the exact names

```
export INFOBLOX_API_KEY='<your-key>'
python3 move_ha_group.py --list-ha-groups
```

One line per group: name, mode, IP space, id.

```
DC2-DHCP-HA-OLD    active-passive   Corporate   dhcp/ha_group/1a2b...
DC2-DHCP-HA-NEW    active-passive   Corporate   dhcp/ha_group/9f8e...
```

Copy the names from that output. The portal lists the same groups under
Network > DHCP > HA Groups. Both groups must sit in one IP space. The script
checks that before it writes, and the server refuses a mismatch anyway.

### 2. Dry run

```
python3 move_ha_group.py --old "DC2-DHCP-HA-OLD" --new "DC2-DHCP-HA-NEW"
```

It reads the tenant and prints the plan:

```
From : DC2-DHCP-HA-OLD  (dhcp/ha_group/1a2b...)
To   : DC2-DHCP-HA-NEW  (dhcp/ha_group/9f8e...)
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

### 3. Pilot five, then check

```
python3 move_ha_group.py --old "DC2-DHCP-HA-OLD" --new "DC2-DHCP-HA-NEW" \
  --max 5 --apply --verify
```

Open those five in the portal. The **Edit** dialog shows the HA group; the side
panel does not.

### 4. The rest, then confirm

```
python3 move_ha_group.py --old "DC2-DHCP-HA-OLD" --new "DC2-DHCP-HA-NEW" \
  --apply --verify
```

`--verify` re-reads afterwards and prints what is still on the old group.

### 5. Undo, if you need it

```
python3 move_ha_group.py --old "DC2-DHCP-HA-NEW" --new "DC2-DHCP-HA-OLD" \
  --apply --verify
```

Names swapped. Each run builds its plan from the current state, so this finds
exactly what the last run moved.

### Different subnets to different groups

The script does one pair of groups per run. For two target groups, run it twice,
once per pair, after the objects are split across two source groups. It cannot
send some subnets on the same source group one way and the rest another way.

The CSV method can: `dhcp_host` is a per-row value, so one file can send row A
to one group and row B to another. We have not tested a mixed file.

### The two CSV files

`move.csv` and `rollback.csv` are the same rows, twice. `rollback.csv` is the
untouched copy, straight from the export. `move.csv` is the copy where you set
`dhcp_host` to the new group. One is the change, the other is the way back.

They are not one file per subnet group.

## What we found

- **A partial run can be reversed.** Each run rebuilds its plan from the current
  state, so after moving three of six objects with `--max` the swapped command
  found exactly those three. That was a capped run, not an interruption or a real
  error. If you do interrupt one, the report is still written, and a fresh dry run
  shows where things actually stand.
- **`--verify` reports what is left.** After a capped run it prints
  `STILL on the old HA group: N subnets, M ranges`, and exits non-zero if the
  move was meant to be complete.
- **One HA group serves one IP space**, through its hosts, and the server enforces
  it. A subnet from another space is refused with an error naming both spaces.
- **The script's own check is skipped** when the target group has nothing assigned
  yet and so reports no IP space — the normal state of a newly built group. The
  server still refuses, so this costs a failed run, not a wrong one.
- **The plan comes from a filtered query**, not a walk of the whole tenant, so it
  returns quickly even on a large one. That filter is undocumented by Infoblox; if
  it stops working the script says so and falls back to reading every subnet and
  range, which takes minutes.
- **Scale is untested.** Writes are throttled to about five a second, so a large
  move is paced by that rather than by the API. Nothing bigger than six objects
  has been run.
- **Every object on a group is in one IP space**, so aiming at a group whose
  hosts serve another space refuses outright rather than moving part of the set.
  Inferred from one refusal, not stated by Infoblox.

## CSV fallback

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

## Not tested

Either method past six objects. Live leases — there were no clients on the lab
range. A real mid-run error; recovery from an artificial one works. Blanking a
`dhcp_host` cell in a CSV. A delete-type import.

We did not run an export in this round, only the two imports. The export screen
was inspected directly and offers no filter; the ~16 minute figure comes from
this tenant's export job history rather than from a run we timed.

---

Portal paths correct September 2026.
