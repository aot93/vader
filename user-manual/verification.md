# Verification

**What this covers:** running a verify job, the difference between a sample and a
full verify, the "due for re-verification" list, reading a tape's read-error
history, and what to do when a verify reports a mismatch.

Verification is what makes *recovery* mean something rather than "hope the tape
still reads in five years". A verify job **re-reads content from a tape's LTFS
volume, re-hashes it, and compares against the SHA256 recorded at write time.** It
never touches the original source data.

---

## 1. When to verify

- **After every write run** — the write pipeline's read-back check already covers
  this for freshly written content, but a full verify of the finished tapes is
  cheap insurance.
- **Every year, on tapes you did *not* write to.** The [Dashboard](dashboard.md)
  surfaces tapes whose last-verified date is older than `REVERIFY_MONTHS`
  (default 12) or that have never been verified. Catching a degrading tape early
  means you may still have a live source to re-archive from.
- **On any tape showing read errors** in its history, or that you have physical
  concerns about.

---

## 2. Running a verify job

There are several ways to start one, all creating the same kind of background job:

| From | How | Options you can set |
|---|---|---|
| **New verify form** | `Jobs → + Verify job` (`GET /jobs/new/verify`) | `barcode`, **Full verify** checkbox, `sample_fraction`, `drive` |
| [Tape detail](tapes.md) page | **Quick verify (sample)** button | none — barcode only, sample verify |
| [Tape detail](tapes.md) page | **Verify options…** link | opens the new verify form pre-filled with this barcode |
| [Dashboard](dashboard.md) | **Verify `<barcode>`** button in the yellow re-verify box | none — first overdue tape, sample verify |
| Dashboard | **options…** link next to that button | opens the verify form pre-filled |

Use the **New verify form** whenever you want a full verify (tick **Full verify**)
or a specific sample fraction. The plain buttons are shortcuts for a quick sample
at the default fraction. The form validates that a sample fraction is between 0
and 1.

The job:

1. Finds the tape in the library (a storage slot, or already in a drive), loads
   and mounts it.
2. Builds the list of checks from the tape's spans:
   - For a **Type-A** span: the on-tape frame **manifest** (checked against the
     span's stored hash), then **every frame the manifest lists** (each checked
     against its manifest SHA256).
   - For a **Type-B/C/D** span: the **file** itself against the span's SHA256.
3. Selects checks to run:
   - **Full** (`full=true`): every check.
   - **Sample** (default): a random subset — `DEFAULT_VERIFY_SAMPLE_FRACTION`
     (0.1 = 10%) of the checks, at least one. The sample is deterministic per
     `barcode` + job id.
4. Re-hashes each selected file and compares.
5. Unmounts and returns the tape to a free slot.

`progress_current / progress_total` on the [job page](jobs.md) is the count of
checks done / selected.

---

## 3. What the result means

The verify job result contains:

| Field | Meaning |
|---|---|
| `barcode` | The tape verified. |
| `checks_run` | How many checks actually ran. |
| `sample_fraction` | 1.0 for a full verify, else the fraction used. |
| `mismatches` | Files whose re-hash did **not** match the stored checksum. Each entry shows the label and a snippet of expected vs actual hash. |
| `read_errors` | Files that could not be read at all (missing on tape, or an OS/LTFS read error). |
| `verified` | `true` only if there were **no** mismatches **and no** read errors. |

Effects on the catalog:

- **Spans that passed** (at least one OK check, no failures) get their
  `verified_at` set, and so do the parent container / item.
- The **tape's** `last_verified_at` is set **only** when the whole run was clean
  (no mismatches, no read errors). This is what clears it off the Dashboard
  re-verify list.
- Every read error is written to the tape's **read-error history** with the LTFS
  path and error text.
- If there were mismatches, a dated line is appended to the tape's **notes**
  (e.g. `2027-03-04: verify found 2 checksum mismatch(es)`).
- **The tape is *not* automatically marked `damaged`.** That decision is yours.

---

## 4. The re-verification-due list

On the [Dashboard](dashboard.md), the yellow box lists tapes that are **all** of:

- not `scratch` and not `retired`;
- have been written to at least once (`last written` is set);
- have `last verified` either empty or older than `now − REVERIFY_MONTHS months`.

Work through them: pull each tape, queue a verify, and — if the verify is clean —
it drops off the list. If a verify is *not* clean, it stays on the list (because
`last_verified_at` is only set on a clean run) until you resolve it.

Tune the window with `REVERIFY_MONTHS` ([Configuration reference](configuration-reference.md)).

---

## 5. Reading read-error history

On the [tape detail](tapes.md) page, **Read-error history** lists every
SCSI/LTFS read failure logged against the tape during verification: timestamp,
operation, LTFS path, and the raw error text.

- **A one-off** on a single file may be transient — re-verify.
- **A growing list, or repeats on the same region**, is a physically degrading
  tape. Retire it and re-archive its content from a live source while you still
  can. Do not wait for it to become unreadable.

---

## 6. Responding to a mismatch — procedure

1. Open the verify **[job](jobs.md)** result; note which files are in `mismatches`
   and which are in `read_errors`.
2. Open the **[tape page](tapes.md)**; check notes (the dated mismatch line) and
   read-error history for a pattern.
3. Decide:
   - **Transient / single file, tape otherwise clean:** queue a **full** verify
     to confirm scope.
   - **Confirmed bad content, live source still exists:** re-archive the affected
     shots/files (a fresh write job; it is idempotent and will re-write the
     affected units).
   - **Tape physically degrading (read errors recurring):** set the tape status to
     **`damaged`** on the tape page, retire it, and re-archive from source. A
     `damaged` tape will block any future restore that needs it until you
     override — which is the point.
4. Record what you did in the tape **notes**.
