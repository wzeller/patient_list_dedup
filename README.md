# patient_list_dedup

Flag likely-duplicate patients in a clinic patient-list CSV.

Given a CSV of patients, this tool appends four columns:

| Column | Values | Meaning |
| --- | --- | --- |
| `Likely Duplicate` | `YES` / `NO` | Whether the patient likely duplicates another patient in the file. |
| `Why` | e.g. `Fuzzy name match; Shared MRN` | The basis for a `YES` flag (blank for `NO`). |
| `Recommendation` | `Maintain this account` / `Remove this account` / `Review — …` / *(blank)* | Action for the row: keep/remove for high-confidence duplicates, or `Review` when only name or only MRN matches and the DOB differs. Review rows also carry an `If confirmed duplicate: maintain/remove this account` verdict when the cluster has a claimed account or data to rank by. |
| `Duplicate Group` | cluster number / *(blank)* | Stable id shared by all members of a duplicate cluster; blank for non-duplicates. Sort or filter on it to review clusters together (or use `--group`). |

It works on a bare 3-column file (`Name`, `Date of Birth`, `MRN`) **or** on a full
Tidepool web-app patient-list export — the metadata header block at the top of an
export is detected and passed through unchanged.

## Quick start

Requires **Python 3.9+** and nothing else (standard library only). Check with
`python3 --version`.

```bash
# 1. Get the code
git clone https://github.com/wzeller/patient_list_dedup.git
cd patient_list_dedup

# 2. Try it on the included sample (--group sorts duplicate clusters together)
python3 patient_list_dedup.py sample_patient_list_large.csv --group

# 3. Run it on your own export — quote any path that contains spaces
python3 patient_list_dedup.py "/path/to/your patients.csv" -o results.csv
```

Step 2 writes `sample_patient_list_large_dedup.csv` next to the input; step 3 writes
`results.csv`. The output is your input plus four columns — `Likely Duplicate`, `Why`,
`Recommendation`, and `Duplicate Group`. Open it in Excel / Numbers / Sheets and sort by
`Duplicate Group` (or pass `--group`) to review each cluster of duplicates together.

Only `Name`, `Date of Birth`, and `MRN` are required; `Custodial Status` and the
last-data-date columns sharpen the recommendation when present (see
[Input columns](#input-columns)). For the full options list, jump to [Usage](#usage).

## How duplicates are detected

A patient is flagged `YES` if it matches **any other patient** on either:

- **Name** — compared *fuzzily* (typo- and variant-tolerant), so `Jon Smith` matches
  `John Smith`, or
- **MRN** — compared *exactly*.

Matching is case-insensitive and ignores leading/trailing whitespace, so `Tom Snyder `
matches `  TOM SNYDER`. Blank names/MRNs never match (two patients aren't linked just
because both lack an MRN). Placeholder strings that exports sometimes write for missing
values — `null`, `NaN`, `None`, `NA`, `N/A`, `nil` — are treated as blank, so patients are
not linked by a shared literal `null` MRN, name, or DOB.

This is the faithful reduction of the clinic-merge "potential duplicate" rule (match on
two-or-more of Name/DOB/MRN, or on Name alone, or on MRN alone) — the only case that does
*not* qualify is a date-of-birth-only match. Duplicates are grouped transitively: if A
matches B by name and B matches C by MRN, then A, B, and C form one group.

## How the recommendation is chosen

Recommendations are tiered by **match confidence**, using date of birth as
corroboration so the tool never blanket-recommends removing accounts that might belong
to different people:

- **Strong match → `Maintain` / `Remove`.** A pair agrees on **two or more** of
  {name, DOB, MRN} — e.g. name+DOB, MRN+DOB, or name+MRN. These are treated as the same
  patient. Within each strong cluster exactly one account is kept and the rest removed.
- **Weak match → `Review`, never auto-removed.** Only the name matches (DOB differs) or
  only the MRN matches. These could be DOB typos *or* genuinely different people, so a
  human decides:
  - `Review — possible duplicate (name match, DOB differs)`
  - `Review — possible MRN typo (MRN match, DOB differs)`

  When the cluster has a distinguishing signal, the Review text also carries a
  conditional verdict for after the human confirms the duplicate —
  `If confirmed duplicate: maintain this account` on exactly one account per cluster
  (chosen by the same keep-account priority below, ranked across the whole cluster)
  and `If confirmed duplicate: remove this account` on the rest.

The account to **keep** is chosen by, in order:

1. **Claimed** — a `Custodial Status` of `Claimed` beats all other accounts.
2. **Has data** — an account with data beats one without.
3. **Latest data date** — the later of `CGM Last Data Date` and `BGM Last Data Date`.
4. Earliest row, as a final tie-breaker.

If a strong cluster has no distinguishing signal at all (nobody claimed, no data
anywhere), the `Recommendation` is left blank rather than guessed. Non-duplicate patients
are left blank. Note: patients are still **grouped and flagged** on name or MRN alone (so
nothing is missed) — DOB only affects the *recommended action*, not whether a row is
flagged.

## SQL query variant

[`patient_list_dedup.sql`](patient_list_dedup.sql) implements the same detection and
recommendation rules as a warehouse query (Spark/Databricks SQL) over the live patient
tables, so results can be pulled per clinic without a CSV export. It takes one parameter,
`:clinic` — a clinic id, an exact clinic name, or a partial name. **If `:clinic` is blank,
every duplicate column is `NULL`** (the query only computes matches within a selected
clinic); the first thing to check if the columns look empty.

Its output can be exported to CSV and fed straight into the CLI/app — the column names
(`fullName`, `birthDate`, `mrn`, `claimed`, `lastDataDate`) are all recognized.

### Output columns

Pass-through identity columns: `fullName`, `claimed` (true when the account has an email,
i.e. the patient claimed it), `userId`, `birthDate`, `mrn`, `Connection Provider`,
`Connection State`, `lastDataDate` (latest of the CGM and BGM last-data dates), and
`clinic_name` / `clinic_id`.

The four computed columns mirror the CLI's:

| SQL column | CLI equivalent | Values | Meaning |
| --- | --- | --- | --- |
| `is_duplicate_match` | `Likely Duplicate` | `true` / `false` / `NULL` | Whether the patient matches at least one other patient in the clinic on name (exact or fuzzy) or MRN. `NULL` when no clinic is selected. |
| `duplicate_cluster` | `Duplicate Group` | `1..N` / `NULL` | Stable id shared by every member of a duplicate cluster (numbered by lowest member `userId`). `NULL` for non-duplicates. Sort on it to review clusters together. |
| `duplicate_reason` | `Why` | e.g. `Exact name match; Shared MRN` | The basis for the match. `Fuzzy name match` means the names differ but score ≥ 0.90 similarity. |
| `recommended_action` | `Recommendation` | see below | The suggested action for the row. |

### Reading `recommended_action`

For duplicate rows, the value is one of five states:

1. **`Maintain this account`** — high-confidence duplicate (agrees with another account
   on 2+ of name/DOB/MRN); this is the account to keep in its cluster.
2. **`Remove this account`** — high-confidence duplicate; a better account (see the
   keep-account priority above: claimed, then latest data) exists in the cluster.
3. **`Review - … ; If confirmed duplicate: maintain/remove this account`** — uncertain
   duplicate (name or MRN matches but the DOB differs — possibly a typo, possibly two
   different people). A human must confirm; the suffix pre-computes the outcome of that
   confirmation, with exactly one `maintain` per cluster chosen by the same
   claimed-then-latest-data ranking.
4. **`Review - …`** with no suffix — uncertain duplicate in a cluster with *no* ranking
   signal (nobody claimed, no data anywhere), so no outcome can be pre-computed.
5. **`NULL`** — either not a duplicate at all (`is_duplicate_match` is false), or a
   high-confidence duplicate whose cluster has no ranking signal — flagged and clustered,
   but no keep/remove call is made rather than guessing.

So a typical triage flow: filter `is_duplicate_match = true`, sort by
`duplicate_cluster`, act on `Maintain`/`Remove` rows directly, review the `Review` rows
and apply their `If confirmed` verdicts, and handle `NULL`-recommendation clusters
manually (they usually mean neither account has claimed status or any data).

### Known divergences from the CLI

Deliberately small, but worth knowing when comparing outputs:

- The SQL writes `Review -` with a plain hyphen; the CLI uses an em-dash (`Review —`).
- Final tie-break when accounts have identical signals: lowest `userId` in SQL vs
  earliest file row in the CLI.
- Fuzzy similarity is Levenshtein-based in SQL vs `difflib.SequenceMatcher` in Python.
  The formula is calibrated to agree at the 0.90 threshold, but transposed-letter typos
  (`jhon` vs `john`) score lower in SQL and may miss the threshold there.
- `claimed` comes from account email presence in SQL vs the `Custodial Status` /
  `claimed` column in a CSV.

## Requirements

- **Python 3.9+** — no third-party packages (standard library only).

Check your version:

```bash
python3 --version
```

## Download

### Mac app (no Python needed)

A double-clickable macOS app is published on the
[**Releases**](https://github.com/wzeller/patient_list_dedup/releases) page (Apple
Silicon). To use it:

1. Download `PatientListDedup-macos-arm64.zip` from the latest release and unzip it.
2. **First launch only** — because the app is unsigned, macOS Gatekeeper will block it.
   Either **right-click the app → Open → Open**, or run once:
   ```bash
   xattr -dr com.apple.quarantine PatientListDedup.app
   ```
3. Choose a CSV, set options, and save the deduplicated result. Everything runs locally —
   no patient data leaves your machine.

The app is built automatically by CI; see [Building the Mac app](#building-the-mac-app).

### Run from source

Clone the repository:

```bash
git clone https://github.com/wzeller/patient_list_dedup.git
cd patient_list_dedup
```

Or, if you only need the script, download it directly:

```bash
curl -O https://raw.githubusercontent.com/wzeller/patient_list_dedup/main/patient_list_dedup.py
```

You can also launch the same GUI from source (no packaging needed):

```bash
python3 gui.py
```

## Usage

```bash
python3 patient_list_dedup.py INPUT.csv
```

This writes `INPUT_dedup.csv` next to the input. To choose the output path:

```bash
python3 patient_list_dedup.py INPUT.csv -o results.csv
```

Try it on the included samples:

```bash
# small, minimal fixture
python3 patient_list_dedup.py sample_patient_list.csv

# longer, realistic export (metadata header block + 34 patients) that
# demonstrates every feature: claimed-beats-data, latest-date tie-breaks,
# transitive groups, fuzzy spelling variants, no-signal blanks, and the
# Review tiers (namesakes and shared-MRN mismatches with differing DOBs).
# Add --group to sort duplicate clusters together.
python3 patient_list_dedup.py sample_patient_list_large.csv --group
```

### Input columns

Only three columns are **required** (header names are case-insensitive; common aliases
are accepted, including the Tidepool DB column names):

- Name — `Patient Name`, `Name`, `Full Name`, `fullName`
- Date of Birth — `Date of Birth`, `DOB`, `Birth Date`, `birthDate`
- MRN — `MRN`, `Medical Record Number`

Or override any of them with `--name-col` / `--dob-col` / `--mrn-col`.

These columns are **optional** and improve the `Recommendation` when present:

- `Custodial Status` (`Claimed` / `Unclaimed`; boolean-style `claimed` columns with
  `TRUE` / `FALSE` / `yes` / `1` values also work)
- `CGM Last Data Date`
- `BGM Last Data Date`
- `Last Data Date` — a combined column (`Last Data Date`, `lastDataDate`); used together
  with the CGM/BGM columns, taking the latest of whichever are present.

If no last-data-date column is present, the tool prints a warning and leaves
`Recommendation` blank (except where a `Claimed` account breaks a tie).

### Options

| Flag | Default | Description |
| --- | --- | --- |
| `-o`, `--output PATH` | `<input>_dedup.csv` | Output file path. |
| `--group` | off | Sort output so duplicate clusters are contiguous (cluster order, `Maintain` before `Remove`; non-duplicates last). |
| `--name-threshold FLOAT` | `0.90` | Fuzzy name-match similarity, `0`–`1`. Use `1.0` for exact-name-only. |
| `--delimiter CHAR` | auto | Force the field delimiter (`,` or `\t`). Auto-detects tab vs comma. |
| `--name-col NAME` | — | Override the Name header (repeatable). |
| `--dob-col NAME` | — | Override the DOB header (repeatable). |
| `--mrn-col NAME` | — | Override the MRN header (repeatable). |

Example with a stricter fuzzy threshold and an explicit column name:

```bash
python3 patient_list_dedup.py export.csv --name-threshold 0.95 --name-col "Patient Name"
```

### Tuning the fuzzy threshold

`0.90` is a reasonable starting point. Lower it to catch more variants (at the cost of
more false positives); raise it toward `1.0` to require near-exact names. Short names
(e.g. `Li Wu`) are the most likely to false-match at lower thresholds — test a few values
against a real export before relying on the output.

## Handling patient data (PHI)

Patient CSVs contain PHI. The repository's `.gitignore` excludes `*.csv`, `*.xlsx`,
`*.parquet`, `*_dedup.csv` outputs, and a `data/` directory so real exports are not
committed by accident. The only deliberate exceptions are the synthetic `sample_*.csv`
fixtures (no real patient data). Keep real data out of the repo.

## Development

Run against the sample and inspect the added columns:

```bash
python3 patient_list_dedup.py sample_patient_list.csv -o /tmp/out.csv
column -s, -t /tmp/out.csv
```

### Building the Mac app

The app is built and released by the
[`build-mac-app`](.github/workflows/build-mac-app.yml) GitHub Actions workflow on a macOS
runner. To cut a release that anyone can download:

```bash
git tag v1.0.0
git push --tags
```

The workflow runs the tests, builds `PatientListDedup.app` with PyInstaller, zips it, and
attaches it to the GitHub Release for that tag. A manual run (Actions → *Build macOS app* →
*Run workflow*) instead uploads the app as a downloadable build artifact.

To build locally (requires `pip install pyinstaller`):

```bash
pyinstaller --noconfirm PatientListDedup.spec   # -> dist/PatientListDedup.app
```

Notes:
- The released app is **unsigned** — users bypass Gatekeeper once (see
  [Mac app](#mac-app-no-python-needed)). To ship it without warnings, add Apple Developer
  ID signing + notarization (store credentials as GitHub secrets); the workflow can be
  extended to do this.
- CI builds for **Apple Silicon (arm64)**. For Intel Macs, build a `universal2` binary or
  add a second job.

### Tests

The suite uses only the standard library (`unittest`) — no setup required. From the
repository root:

```bash
python3 -m unittest discover -s tests
```

It covers normalization and date parsing, the fuzzy-name threshold, transitive grouping
(name ∪ MRN), the strong/weak confidence tiers (name-or-MRN-only with a DOB conflict →
`Review`, never `Remove`), the keep-account priority order and no-signal-blank case, the
`Duplicate Group` numbering, and a `process` round-trip on both a bare 3-column file and a
full export with a metadata header block (including delimiter auto-detection).
