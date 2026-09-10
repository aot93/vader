# Troubleshooting

**What this covers:** the situations an operator actually hits during a run, what
each one looks like in Vader, and the fix. For deeper hardware/OS problems see the
project `RUNBOOK.md` §8 and `docs/LTO-Archive-VM-Setup.md`.

---

## Quick table

| Symptom | Likely cause | Fix |
|---|---|---|
| Dashboard: red *Library unreachable* | Wrong `HARDWARE_BACKEND` / `CHANGER_DEVICE`, toolchain not on `PATH`, or device node moved | See §1 |
| Write job fails: *out of scratch tapes* | Not enough blank `scratch` tapes in the library to span onto | See §2 |
| Write job result has entries in `readback_mismatches` | A file did not survive the copy to LTFS intact | See §3 |
| `mkltfs` / `ltfs` *not found* | Tape toolchain not installed where the app can see it | Install `mt-st mtx sg3-utils ltfs lsscsi`; restart |
| Format refuses: *status is 'active', not 'scratch'* | Guard against wiping a catalogued tape | See §4 |
| Verify reports `mismatches` or `read_errors` | Content changed on tape, or the tape is degrading | See §5 |
| Restore shows **Cannot run yet** with no run button | A required tape is *not in the library* | See §6 |
| Restore ran but ended `failed` with `problems` | A `DAMAGED` tape was attempted and some files were unreadable | See §6 |
| Job stuck at `running` after a crash/reboot | Worker restarted mid-job | See §7 |
| Job sits at `queued` forever | Another job is `running`; worker is one-at-a-time | Wait, or cancel the running job ([Jobs](jobs.md)) |
| *database is locked* (SQLite) | Concurrent writes on SQLite | See §8 |
| Everything redirects to `/login` | `AUTH_TOKEN` is set | Log in with the shared token, or unset `AUTH_TOKEN` and restart |

---

## 1. "Library unreachable" on the Dashboard

The Dashboard and [Library](library.md) page show this when the call to read
changer status raised an error.

1. Check `HARDWARE_BACKEND`. For a real run it must be `real`; if it is
   `simulator` you are looking at the fake library.
2. Check `CHANGER_DEVICE` against `lsscsi -g` (the `mediumx` entry). If the VM
   rebooted, the `/dev/sg*` number may have changed — use `/dev/tape/by-id/...`
   or a udev rule.
3. Run the command by hand: `sudo mtx -f $CHANGER_DEVICE status`. If that fails,
   the problem is below Vader — fix it at the OS/HBA level first.
4. Confirm `mtx` / `mt` / `mkltfs` / `ltfs` are on the `PATH` the app runs with
   (a `systemd` unit has a minimal `PATH`).

Restart the app after changing any environment value.

---

## 2. Write job: "out of scratch tapes"

The allocator ran out of blank tape while placing a unit. The message says as
much and notes that the job is idempotent.

1. Load more blank cartridges into the library.
2. **[Library](library.md) → Refresh inventory** — new barcodes register as
   `scratch`.
3. If any newly loaded tape shows a non-`scratch` status, **Format** it
   (Utilities). Set its capacity on the [tape page](tapes.md) if it is unknown, so
   spanning maths are right.
4. Re-run the job: job page → **Re-run with same parameters**. Already-written
   units are skipped; it resumes where it stopped.

> In **greedy** mode the pool is limited to tapes dedicated to that source plus
> fresh scratch. If you are short, you need more *scratch* tapes specifically —
> a partly-used tape belonging to another source will not be used.

---

## 3. Write job finished but `readback_mismatches` is not empty

A file's bytes on tape did not match the source after copying. The job still
completed and the rest of the content is fine, but:

1. Open the job **Result**; note the unit + filename of each mismatch.
2. The affected sequence/item was **not** marked verified. Find it in
   [Search](search-catalog.md) — its **Verified** column will be blank.
3. A dated mismatch note is also added to each affected tape, so it shows on the
   [tape detail](tapes.md) page — but still check every write job result, since
   that is where the per-file detail is.
4. If you still have the live source, **re-run the write job** (idempotent) — it
   re-writes unverified units. If the mismatch persists, suspect the **drive**
   (clean it — [Library](library.md) → Utilities → Clean) or the **tape** (Format
   a different scratch tape and let the job span onto it instead; retire the bad
   one).

---

## 4. Format refuses the tape

Format (`mkltfs`) blocks any tape whose catalog status is not `scratch`, to stop
you wiping a tape that still has catalogued content.

- If the tape really is spent and you mean to reuse it: tick **force** on the
  Format form. This resets it to `scratch`, zeroes its used bytes, bumps the
  write-pass count, and clears any "dedicated to" marker.
- If you did **not** mean to wipe it: do nothing — the guard just saved you.
- The tape must also be **loaded in the drive number** you enter on the form.

---

## 5. Verify reports mismatches or read errors

See [Verification](verification.md) §6 for the full procedure. In short:

- **`mismatches`** (checksum differs): a dated line is added to the tape notes; the
  tape is **not** auto-damaged. Run a **full** verify to see the true scope, then
  re-archive the affected content from a live source if you have one.
- **`read_errors`** (could not read at all): logged to the tape's read-error
  history. A one-off may be transient; a recurring pattern means the tape is
  failing — set its status to `damaged` on the [tape page](tapes.md), retire it,
  and re-archive from source.

---

## 6. Restore warnings ("not in the library" / "DAMAGED")

The restore request is `pending` and the warnings box lists the reason. The two
kinds behave differently:

- **"not in the library"** — a hard block. The page shows **Cannot run yet** and
  no run button. The plan names the barcode and its last known location.
  Physically fetch that tape, put it in a slot, and **[Library](library.md) →
  Refresh inventory** so Vader sees it. Then prepare the restore again (from
  [Search](search-catalog.md)) — the new request should be `ready`.
- **"flagged DAMAGED"** — *not* a block. The page shows a red **Run despite
  warnings** button. Click it to attempt the tape: any file that cannot be read
  is listed in the job result's `problems` and the request ends `failed`, but
  everything readable is still recovered. If you would rather not risk it, use
  the offsite sibling (check the tape's **Offsite pair barcode**) or a live
  source instead.

---

## 7. Job stuck at "running" after a crash or reboot

The worker only sets a job to `interrupted` when the **application starts**. So:

1. Make sure the app is actually running again (`systemctl status`, or restart
   `uvicorn`).
2. On startup the worker flips any leftover `running` job to `interrupted`.
3. Open [Jobs](jobs.md) → that job → **Re-run with same parameters**.
4. For a write job, top up scratch tapes first if the crash happened near an
   allocation limit.

If a job genuinely appears hung while the app is up (heartbeat not advancing for a
long time on a `running` job), it is probably blocked on a slow or failing drive —
**Request cancel** (it stops at the next safe boundary), investigate the drive,
then re-run.

---

## 8. "database is locked" (SQLite)

Expected only under concurrent write pressure on SQLite. Vader already sets WAL
mode and a 15-second busy timeout, which covers normal single-operator use.

- For the **real annual run, use PostgreSQL** (`DATABASE_URL=postgresql+psycopg://…`).
  SQLite is for evaluation and training only.
- If you hit it on SQLite: retry the action; avoid running the seed script or
  multiple app instances against the same file at once.

---

## 9. Things that look wrong but are not

| Observation | Explanation |
|---|---|
| A second job stays `queued` while one runs | The worker runs exactly one job at a time. Normal. |
| A greedy job leaves tapes half empty | That is the point of greedy mode — isolation over efficiency. |
| A shot shows two tapes with `p1/2` and `p2/2` | It spanned at write time. A restore will load both. |
| Dashboard "content items" shows an object string, not a number | A known display glitch in one template; the real counts are in [Search](search-catalog.md) and the full-catalog CSV. |
| `last verified` stays blank after a verify that found problems | The tape's verified date is only set on a fully clean verify. Resolve the problem and verify again. |
