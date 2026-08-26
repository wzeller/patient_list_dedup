#!/usr/bin/env python3
"""Flag likely-duplicate patients in a clinic patient-list CSV.

Reads a CSV that contains at least a Name, Date of Birth, and MRN column
(a bare 3-column file works; a full Tidepool web-app export works too -- its
metadata header block is detected and preserved), appends a "Likely Duplicate"
column with YES/NO values, and writes the result to a new CSV.

Duplicate rule (derived from the clinic-merge "potential duplicate" criteria):
A patient is a likely duplicate if it matches at least one *other* patient on
two or more of {Name, DOB, MRN}, OR on Name alone, OR on MRN alone. Working
through the cases, the only match that does NOT qualify is DOB-alone -- so a
patient is flagged YES iff it matches another patient's Name, or shares another
patient's MRN.

  * Name matching is FUZZY: names are compared after normalization and linked
    when their similarity ratio meets --name-threshold (default 0.90). This
    catches typos and variants ("Jon Smith" ~ "John Smith").
  * MRN matching is EXACT (after normalization). Per the spec an MRN-only match
    is the "likely typo" signal, so MRNs are compared strictly.

Normalization lowercases and strips leading/trailing whitespace, so
"Tom Snyder " matches "  TOM SNYDER". Blank Name/MRN values never match
(patients are not linked merely by both lacking an MRN).

DOB is required as input but does not affect the flag under the rule above; it
is carried through to the output and left as an extension point.

Usage:
    python patient_list_dedup.py INPUT.csv [-o OUTPUT.csv]
        [--name-threshold 0.90] [--delimiter ,]
        [--name-col "Patient Name"] [--dob-col "Date of Birth"] [--mrn-col MRN]
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from difflib import SequenceMatcher
from pathlib import Path

# Accepted header labels for each required column (case-insensitive, whitespace
# ignored). CLI flags can override these.
NAME_ALIASES = ["Patient Name", "Name", "Full Name"]
DOB_ALIASES = ["Date of Birth", "DOB", "Birth Date", "Birthdate"]
MRN_ALIASES = ["MRN", "Medical Record Number", "Medical Record #"]
CGM_DATE_ALIASES = ["CGM Last Data Date"]
BGM_DATE_ALIASES = ["BGM Last Data Date"]
CUSTODIAL_ALIASES = ["Custodial Status", "Claimed?", "Claimed"]
FLAG_COLUMN = "Likely Duplicate"
WHY_COLUMN = "Why"
REC_COLUMN = "Recommendation"
MAINTAIN = "Maintain this account"
REMOVE = "Remove this account"
DEFAULT_NAME_THRESHOLD = 0.90

# Date formats accepted for the CGM/BGM last-data-date columns.
DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d")


def normalize(value: str) -> str:
    """Lowercase and strip surrounding whitespace for match comparisons."""
    return (value or "").strip().lower()


def resolve_column(header: list[str], aliases: list[str]) -> int | None:
    """Return the index of the first header cell matching any alias, else None."""
    wanted = {normalize(a) for a in aliases}
    for i, cell in enumerate(header):
        if normalize(cell) in wanted:
            return i
    return None


def find_header_row(
    rows: list[list[str]],
    name_aliases: list[str],
    dob_aliases: list[str],
    mrn_aliases: list[str],
) -> tuple[int, int, int, int]:
    """Find the header row and the Name/DOB/MRN column indices within it.

    The header row is the first row that resolves all three required columns.
    This transparently skips any leading metadata block.
    """
    for i, row in enumerate(rows):
        name_idx = resolve_column(row, name_aliases)
        dob_idx = resolve_column(row, dob_aliases)
        mrn_idx = resolve_column(row, mrn_aliases)
        if name_idx is not None and dob_idx is not None and mrn_idx is not None:
            return i, name_idx, dob_idx, mrn_idx
    raise ValueError(
        "Could not find a header row containing all required columns "
        f"(Name: {name_aliases}, DOB: {dob_aliases}, MRN: {mrn_aliases}). "
        "Use --name-col / --dob-col / --mrn-col to specify custom header names."
    )


def _names_similar(a: str, b: str, threshold: float) -> bool:
    """True when two normalized names meet the similarity threshold.

    Uses cheap prefilters (length and quick ratios) before the full comparison.
    """
    sm = SequenceMatcher(None, a, b)
    if sm.real_quick_ratio() < threshold or sm.quick_ratio() < threshold:
        return False
    return sm.ratio() >= threshold


def analyze_name_matches(
    names: list[str], threshold: float
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Compare distinct names once, returning match kinds and fuzzy edges.

    - kinds: name -> "exact" | "fuzzy". "exact" if the name appears on more than
      one patient; else "fuzzy" if similar (>= threshold) to another name.
    - edges: pairs of distinct names that fuzzy-match, used to union groups.
    """
    counts = Counter(names)
    kinds = {n: "exact" for n, c in counts.items() if c > 1}
    edges: list[tuple[str, str]] = []
    uniques = list(counts)
    for i, a in enumerate(uniques):
        for b in uniques[i + 1 :]:
            if _names_similar(a, b, threshold):
                edges.append((a, b))
                kinds.setdefault(a, "fuzzy")
                kinds.setdefault(b, "fuzzy")
    return kinds, edges


class UnionFind:
    """Minimal union-find over integer row indices."""

    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, x: int) -> int:
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:  # path compression
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def parse_date(value: str) -> date | None:
    """Parse a last-data-date cell, treating blanks/NA/NaN as absent."""
    s = (value or "").strip()
    if not s or s.lower() in {"na", "nan", "n/a", "none", "null"}:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def build_groups(
    norm_names: list[str], norm_mrns: list[str], threshold: float
) -> tuple[UnionFind, dict[str, str]]:
    """Union patients that match on (fuzzy) name or (exact) MRN. Returns the
    UnionFind plus the per-name match kinds (for the "Why" column)."""
    uf = UnionFind(len(norm_names))

    # Group by exact MRN.
    mrn_to_rows: dict[str, list[int]] = defaultdict(list)
    for i, mrn in enumerate(norm_mrns):
        if mrn:
            mrn_to_rows[mrn].append(i)
    for rows in mrn_to_rows.values():
        for j in rows[1:]:
            uf.union(rows[0], j)

    # Group by name: exact same-name rows, then fuzzy edges between names.
    name_to_rows: dict[str, list[int]] = defaultdict(list)
    for i, name in enumerate(norm_names):
        if name:
            name_to_rows[name].append(i)
    for rows in name_to_rows.values():
        for j in rows[1:]:
            uf.union(rows[0], j)

    kinds, edges = analyze_name_matches([n for n in norm_names if n], threshold)
    for a, b in edges:
        uf.union(name_to_rows[a][0], name_to_rows[b][0])

    return uf, kinds


def compute_columns(
    data_rows: list[list[str]],
    name_idx: int,
    mrn_idx: int,
    cgm_idx: int | None,
    bgm_idx: int | None,
    custodial_idx: int | None,
    name_threshold: float,
) -> list[tuple[str, str, str]]:
    """Return a (flag, why, recommendation) triple per data row."""

    def cell(row: list[str], idx: int | None) -> str:
        if idx is None or idx >= len(row):
            return ""
        return row[idx]

    norm_names = [normalize(cell(r, name_idx)) for r in data_rows]
    norm_mrns = [normalize(cell(r, mrn_idx)) for r in data_rows]

    uf, name_kinds = build_groups(norm_names, norm_mrns, name_threshold)

    # Latest data date per row = later of the CGM/BGM last-data dates.
    last_dates: list[date | None] = []
    for row in data_rows:
        dates = [d for d in (parse_date(cell(row, cgm_idx)), parse_date(cell(row, bgm_idx))) if d]
        last_dates.append(max(dates) if dates else None)

    # Whether each patient's account is claimed (custodial status).
    claimed = [normalize(cell(r, custodial_idx)) == "claimed" for r in data_rows]

    # Collect group membership by root.
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(data_rows)):
        groups[uf.find(i)].append(i)

    # Recommendation: within each duplicate group, keep the single best account.
    # Preference order: Claimed > has-data > latest data date > earliest row.
    def rank(i: int) -> tuple:
        return (claimed[i], last_dates[i] is not None, last_dates[i] or date.min)

    recommendation = [""] * len(data_rows)
    for members in groups.values():
        if len(members) < 2:
            continue  # singletons are not duplicates
        # Need at least one signal (a claimed account or any data) to decide.
        if not any(claimed[i] or last_dates[i] is not None for i in members):
            continue
        keep = max(members, key=rank)  # ties resolve to the earliest row
        for i in members:
            recommendation[i] = MAINTAIN if i == keep else REMOVE

    mrn_counts = Counter(m for m in norm_mrns if m)
    name_reason = {"exact": "Exact name match", "fuzzy": "Fuzzy name match"}
    results: list[tuple[str, str, str]] = []
    for i, (name, mrn) in enumerate(zip(norm_names, norm_mrns)):
        is_dup = len(groups[uf.find(i)]) > 1
        reasons = []
        if name and name in name_kinds:
            reasons.append(name_reason[name_kinds[name]])
        if mrn and mrn_counts[mrn] > 1:
            reasons.append("Shared MRN")
        flag = "YES" if is_dup else "NO"
        why = "; ".join(reasons) if is_dup else ""
        results.append((flag, why, recommendation[i]))
    return results


def process(
    input_path: Path,
    output_path: Path,
    delimiter: str | None,
    name_threshold: float,
    name_aliases: list[str],
    dob_aliases: list[str],
    mrn_aliases: list[str],
) -> int:
    """Read input, append the flag column, write output. Returns the YES count."""
    text = input_path.read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    if not lines:
        raise ValueError("Input file is empty.")

    def parse(delim: str) -> list[list[str]]:
        return list(csv.reader(lines, delimiter=delim))

    # Try candidate delimiters until one locates the patient-table header. A
    # forced delimiter is the only candidate; otherwise try comma then tab.
    candidates = [delimiter] if delimiter else [",", "\t"]
    rows = None
    last_error: ValueError | None = None
    for delim in candidates:
        parsed = parse(delim)
        try:
            header_idx, name_idx, dob_idx, mrn_idx = find_header_row(
                parsed, name_aliases, dob_aliases, mrn_aliases
            )
        except ValueError as exc:
            last_error = exc
            continue
        rows, delimiter = parsed, delim
        break
    if rows is None:
        raise last_error

    preamble = rows[:header_idx]
    header = rows[header_idx]
    data_rows = [r for r in rows[header_idx + 1 :] if any(c.strip() for c in r)]

    # Optional columns that drive the Recommendation. Absent -> None.
    cgm_idx = resolve_column(header, CGM_DATE_ALIASES)
    bgm_idx = resolve_column(header, BGM_DATE_ALIASES)
    custodial_idx = resolve_column(header, CUSTODIAL_ALIASES)
    if cgm_idx is None and bgm_idx is None:
        print(
            "warning: no CGM/BGM last-data-date column found; "
            "Recommendation will be blank unless a Claimed account breaks a tie.",
            file=sys.stderr,
        )

    results = compute_columns(
        data_rows, name_idx, mrn_idx, cgm_idx, bgm_idx, custodial_idx, name_threshold
    )

    with output_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, delimiter=delimiter)
        for row in preamble:
            writer.writerow(row)
        writer.writerow(header + [FLAG_COLUMN, WHY_COLUMN, REC_COLUMN])
        for row, (flag, why, rec) in zip(data_rows, results):
            writer.writerow(row + [flag, why, rec])

    return sum(1 for flag, _, _ in results if flag == "YES")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Flag likely-duplicate patients in a patient-list CSV."
    )
    parser.add_argument("input", type=Path, help="Path to the patient-list CSV.")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output path (default: <input>_dedup.csv next to the input).",
    )
    parser.add_argument(
        "--name-threshold",
        type=float,
        default=DEFAULT_NAME_THRESHOLD,
        help="Fuzzy name-match similarity threshold in [0,1] "
        f"(default: {DEFAULT_NAME_THRESHOLD}; use 1.0 for exact-only).",
    )
    parser.add_argument(
        "--delimiter",
        help="Force the field delimiter (e.g. ',' or '\\t'). "
        "Default: auto-detect tab vs comma.",
    )
    parser.add_argument("--name-col", action="append", help="Header name for the Name column.")
    parser.add_argument("--dob-col", action="append", help="Header name for the DOB column.")
    parser.add_argument("--mrn-col", action="append", help="Header name for the MRN column.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if not args.input.is_file():
        print(f"error: input file not found: {args.input}", file=sys.stderr)
        return 2
    if not 0.0 <= args.name_threshold <= 1.0:
        print("error: --name-threshold must be between 0 and 1.", file=sys.stderr)
        return 2

    output = args.output or args.input.with_name(f"{args.input.stem}_dedup.csv")
    delimiter = args.delimiter
    if delimiter == "\\t":  # allow literal backslash-t from the shell
        delimiter = "\t"

    try:
        yes_count = process(
            args.input,
            output,
            delimiter,
            args.name_threshold,
            args.name_col or NAME_ALIASES,
            args.dob_col or DOB_ALIASES,
            args.mrn_col or MRN_ALIASES,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Wrote {output}")
    print(f"Flagged {yes_count} patient(s) as Likely Duplicate = YES")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
