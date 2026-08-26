# patient_list_dedup

Flag likely-duplicate patients in a clinic patient-list CSV.

Given a CSV of patients, this tool appends three columns:

| Column | Values | Meaning |
| --- | --- | --- |
| `Likely Duplicate` | `YES` / `NO` | Whether the patient likely duplicates another patient in the file. |
| `Why` | e.g. `Fuzzy name match; Shared MRN` | The basis for a `YES` flag (blank for `NO`). |
| `Recommendation` | `Maintain this account` / `Remove this account` / *(blank)* | Within a duplicate group, which single account to keep. |

It works on a bare 3-column file (`Name`, `Date of Birth`, `MRN`) **or** on a full
Tidepool web-app patient-list export — the metadata header block at the top of an
export is detected and passed through unchanged.

## How duplicates are detected

A patient is flagged `YES` if it matches **any other patient** on either:

- **Name** — compared *fuzzily* (typo- and variant-tolerant), so `Jon Smith` matches
  `John Smith`, or
- **MRN** — compared *exactly*.

Matching is case-insensitive and ignores leading/trailing whitespace, so `Tom Snyder `
matches `  TOM SNYDER`. Blank names/MRNs never match (two patients aren't linked just
because both lack an MRN).

This is the faithful reduction of the clinic-merge "potential duplicate" rule (match on
two-or-more of Name/DOB/MRN, or on Name alone, or on MRN alone) — the only case that does
*not* qualify is a date-of-birth-only match. Duplicates are grouped transitively: if A
matches B by name and B matches C by MRN, then A, B, and C form one group.

## How the recommendation is chosen

Within each duplicate group, exactly one account is kept (`Maintain this account`) and
the rest are marked `Remove this account`. The account to keep is chosen by, in order:

1. **Claimed** — a `Custodial Status` of `Claimed` beats all other accounts.
2. **Has data** — an account with data beats one without.
3. **Latest data date** — the later of `CGM Last Data Date` and `BGM Last Data Date`.
4. Earliest row, as a final tie-breaker.

If a group has no distinguishing signal at all (nobody claimed, no data anywhere), the
`Recommendation` is left blank rather than guessed. Non-duplicate patients are left blank.

## Requirements

- **Python 3.9+** — no third-party packages (standard library only).

Check your version:

```bash
python3 --version
```

## Download

Clone the repository:

```bash
git clone https://github.com/wzeller/patient_list_dedup.git
cd patient_list_dedup
```

Or, if you only need the script, download it directly:

```bash
curl -O https://raw.githubusercontent.com/wzeller/patient_list_dedup/main/patient_list_dedup.py
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

# longer, realistic export (metadata header block + 30 patients) that
# demonstrates every feature: claimed-beats-data, latest-date tie-breaks,
# transitive groups, fuzzy spelling variants, and no-signal blanks
python3 patient_list_dedup.py sample_patient_list_large.csv
```

### Input columns

Only three columns are **required** (header names are case-insensitive; common aliases
are accepted, e.g. `Name` or `Patient Name`, `DOB` or `Date of Birth`):

- Name
- Date of Birth
- MRN

These columns are **optional** and improve the `Recommendation` when present:

- `Custodial Status` (`Claimed` / `Unclaimed`)
- `CGM Last Data Date`
- `BGM Last Data Date`

If neither date column is present, the tool prints a warning and leaves `Recommendation`
blank (except where a `Claimed` account breaks a tie).

### Options

| Flag | Default | Description |
| --- | --- | --- |
| `-o`, `--output PATH` | `<input>_dedup.csv` | Output file path. |
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
`*.parquet`, and a `data/` directory so real exports are not committed by accident (the
synthetic `sample_patient_list.csv` is the one deliberate exception). Keep real data out
of the repo.

## Development

Run against the sample and inspect the added columns:

```bash
python3 patient_list_dedup.py sample_patient_list.csv -o /tmp/out.csv
column -s, -t /tmp/out.csv
```

### Tests

The suite uses only the standard library (`unittest`) — no setup required. From the
repository root:

```bash
python3 -m unittest discover -s tests
```

It covers normalization and date parsing, the fuzzy-name threshold, transitive grouping
(name ∪ MRN), the flag/why/recommendation logic for each priority tier and the
no-signal-blank case, and a `process` round-trip on both a bare 3-column file and a full
export with a metadata header block (including delimiter auto-detection).
