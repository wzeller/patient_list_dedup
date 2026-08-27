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
NAME_ALIASES = ["Patient Name", "Name", "Full Name", "fullName"]
DOB_ALIASES = ["Date of Birth", "DOB", "Birth Date", "Birthdate", "birthDate"]
MRN_ALIASES = ["MRN", "Medical Record Number", "Medical Record #"]
CGM_DATE_ALIASES = ["CGM Last Data Date"]
BGM_DATE_ALIASES = ["BGM Last Data Date"]
CUSTODIAL_ALIASES = ["Custodial Status", "Claimed?", "Claimed"]
FLAG_COLUMN = "Likely Duplicate"
WHY_COLUMN = "Why"
REC_COLUMN = "Recommendation"
GROUP_COLUMN = "Duplicate Group"
MAINTAIN = "Maintain this account"
REMOVE = "Remove this account"
REVIEW_NAME = "Review — possible duplicate (name match, DOB differs)"
REVIEW_MRN = "Review — possible MRN typo (MRN match, DOB differs)"
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


def dob_key(value: str) -> str:
    """Normalized date-of-birth key for equality tests.

    Parses to an ISO date when possible (so 05/12/1990 and 1990-05-12 agree);
    otherwise falls back to the normalized raw string. Blank -> "" (never agrees).
    """
    d = parse_date(value)
    return d.isoformat() if d else normalize(value)


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
    dob_idx: int,
    cgm_idx: int | None,
    bgm_idx: int | None,
    custodial_idx: int | None,
    name_threshold: float,
) -> list[tuple[str, str, str, str]]:
    """Return a (flag, why, recommendation, group) tuple per data row.

    Duplicate pairs are tiered by confidence, using DOB as corroboration:

    - STRONG (safe to merge -> Maintain/Remove): the pair agrees on two or more
      of {name, DOB, MRN} -- e.g. name+DOB, MRN+DOB, or name+MRN.
    - WEAK (-> Review, never auto-Remove): only the name matches (DOB differs) or
      only the MRN matches. These may be DOB typos or genuinely different people,
      so a human decides.

    group is a stable cluster number (as a string) for duplicate rows, numbered
    by first appearance; blank for non-duplicate rows.
    """

    def cell(row: list[str], idx: int | None) -> str:
        if idx is None or idx >= len(row):
            return ""
        return row[idx]

    n = len(data_rows)
    norm_names = [normalize(cell(r, name_idx)) for r in data_rows]
    norm_mrns = [normalize(cell(r, mrn_idx)) for r in data_rows]
    dob_keys = [dob_key(cell(r, dob_idx)) for r in data_rows]

    # All-edges review clusters (name-fuzzy OR MRN-exact) + name match kinds.
    uf, name_kinds = build_groups(norm_names, norm_mrns, name_threshold)
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        groups[uf.find(i)].append(i)

    def names_match(i: int, j: int) -> bool:
        a, b = norm_names[i], norm_names[j]
        if not a or not b:
            return False
        return a == b or _names_similar(a, b, name_threshold)

    # Candidate duplicate pairs = rows that share an MRN, an exact name, or a
    # fuzzy-matched name. Classify each as strong (>=2 of name/DOB/MRN) or weak.
    name_to_rows: dict[str, list[int]] = defaultdict(list)
    mrn_to_rows: dict[str, list[int]] = defaultdict(list)
    for i in range(n):
        if norm_names[i]:
            name_to_rows[norm_names[i]].append(i)
        if norm_mrns[i]:
            mrn_to_rows[norm_mrns[i]].append(i)

    candidates: set[tuple[int, int]] = set()
    for rows in list(name_to_rows.values()) + list(mrn_to_rows.values()):
        for a in range(len(rows)):
            for b in range(a + 1, len(rows)):
                candidates.add((rows[a], rows[b]))
    _, fuzzy_edges = analyze_name_matches([x for x in norm_names if x], name_threshold)
    for na, nb in fuzzy_edges:
        for i in name_to_rows[na]:
            for j in name_to_rows[nb]:
                candidates.add((min(i, j), max(i, j)))

    strong = UnionFind(n)
    weak_types: dict[int, set[str]] = defaultdict(set)
    for i, j in candidates:
        name_ok = names_match(i, j)
        mrn_ok = bool(norm_mrns[i]) and norm_mrns[i] == norm_mrns[j]
        dob_ok = bool(dob_keys[i]) and dob_keys[i] == dob_keys[j]
        if not (name_ok or mrn_ok):
            continue
        if name_ok + mrn_ok + dob_ok >= 2:  # strong: agrees on 2+ fields
            strong.union(i, j)
        else:  # weak: only name or only MRN
            if name_ok:
                weak_types[i].add("name")
                weak_types[j].add("name")
            if mrn_ok:
                weak_types[i].add("mrn")
                weak_types[j].add("mrn")

    last_dates: list[date | None] = []
    for row in data_rows:
        dates = [d for d in (parse_date(cell(row, cgm_idx)), parse_date(cell(row, bgm_idx))) if d]
        last_dates.append(max(dates) if dates else None)
    claimed = [normalize(cell(r, custodial_idx)) == "claimed" for r in data_rows]

    # Maintain/Remove within STRONG clusters only. Preference order:
    # Claimed > has-data > latest data date > earliest row.
    def rank(i: int) -> tuple:
        return (claimed[i], last_dates[i] is not None, last_dates[i] or date.min)

    strong_groups: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        strong_groups[strong.find(i)].append(i)

    recommendation = [""] * n
    for members in strong_groups.values():
        if len(members) < 2:
            continue
        if not any(claimed[i] or last_dates[i] is not None for i in members):
            continue  # confident duplicates, but no signal to pick a keeper
        keep = max(members, key=rank)
        for i in members:
            recommendation[i] = MAINTAIN if i == keep else REMOVE

    # Weak-only duplicates (in a review cluster but no strong action) -> Review.
    for i in range(n):
        if recommendation[i] or len(groups[uf.find(i)]) < 2:
            continue
        if len(strong_groups[strong.find(i)]) > 1:
            continue  # strong duplicate with no signal -> leave blank
        reviews = []
        if "name" in weak_types[i]:
            reviews.append(REVIEW_NAME)
        if "mrn" in weak_types[i]:
            reviews.append(REVIEW_MRN)
        recommendation[i] = "; ".join(reviews)

    # Number duplicate clusters by first appearance (stable, deterministic).
    group_number: dict[int, int] = {}
    next_group = 1
    for i in range(n):
        root = uf.find(i)
        if len(groups[root]) > 1 and root not in group_number:
            group_number[root] = next_group
            next_group += 1

    mrn_counts = Counter(m for m in norm_mrns if m)
    name_reason = {"exact": "Exact name match", "fuzzy": "Fuzzy name match"}
    results: list[tuple[str, str, str, str]] = []
    for i, (name, mrn) in enumerate(zip(norm_names, norm_mrns)):
        root = uf.find(i)
        is_dup = len(groups[root]) > 1
        reasons = []
        if name and name in name_kinds:
            reasons.append(name_reason[name_kinds[name]])
        if mrn and mrn_counts[mrn] > 1:
            reasons.append("Shared MRN")
        flag = "YES" if is_dup else "NO"
        why = "; ".join(reasons) if is_dup else ""
        group = str(group_number[root]) if is_dup else ""
        results.append((flag, why, recommendation[i], group))
    return results


def process(
    input_path: Path,
    output_path: Path,
    delimiter: str | None,
    name_threshold: float,
    name_aliases: list[str],
    dob_aliases: list[str],
    mrn_aliases: list[str],
    group_sort: bool = False,
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
        data_rows, name_idx, mrn_idx, dob_idx, cgm_idx, bgm_idx, custodial_idx, name_threshold
    )

    # Row output order. Default: original file order. With --group: duplicate
    # clusters first (by cluster number), then Maintain, Remove, Review, blank
    # within each; non-duplicates last.
    def rec_rank(rec: str) -> int:
        if rec == MAINTAIN:
            return 0
        if rec == REMOVE:
            return 1
        if rec.startswith("Review"):
            return 2
        return 3

    order = range(len(data_rows))
    if group_sort:
        order = sorted(
            range(len(data_rows)),
            key=lambda i: (
                0 if results[i][0] == "YES" else 1,
                int(results[i][3]) if results[i][3] else 1_000_000_000,
                rec_rank(results[i][2]),
                i,
            ),
        )

    with output_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, delimiter=delimiter)
        for row in preamble:
            writer.writerow(row)
        writer.writerow(header + [FLAG_COLUMN, WHY_COLUMN, REC_COLUMN, GROUP_COLUMN])
        for i in order:
            flag, why, rec, group = results[i]
            writer.writerow(data_rows[i] + [flag, why, rec, group])

    return sum(1 for flag, _, _, _ in results if flag == "YES")


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
    parser.add_argument(
        "--group",
        action="store_true",
        help="Sort output so duplicate clusters are contiguous "
        "(cluster order, Maintain before Remove; non-duplicates last).",
    )
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
            group_sort=args.group,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Wrote {output}")
    print(f"Flagged {yes_count} patient(s) as Likely Duplicate = YES")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
