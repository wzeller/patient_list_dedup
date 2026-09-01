"""Unit tests for patient_list_dedup.

Run from the repository root:

    python3 -m unittest discover -s tests
"""
import csv
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

# Make the top-level module importable when tests run from any directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import patient_list_dedup as pld  # noqa: E402


# Column order used to build synthetic rows for compute_columns tests.
NAME, DOB, MRN, CUST, CGM, BGM = range(6)


def rows_to_records(rows):
    """Wrap [name, dob, mrn, custodial, cgm, bgm] tuples as list rows."""
    return [list(r) for r in rows]


def compute(rows, threshold=0.90):
    return pld.compute_columns(
        rows_to_records(rows),
        name_idx=NAME,
        mrn_idx=MRN,
        dob_idx=DOB,
        cgm_idx=CGM,
        bgm_idx=BGM,
        last_idx=None,
        custodial_idx=CUST,
        name_threshold=threshold,
    )


class NormalizeTests(unittest.TestCase):
    def test_lowercases_and_strips(self):
        self.assertEqual(pld.normalize("  TOM SNYDER "), "tom snyder")

    def test_none_and_empty(self):
        self.assertEqual(pld.normalize(""), "")
        self.assertEqual(pld.normalize(None), "")


class ParseDateTests(unittest.TestCase):
    def test_iso(self):
        self.assertEqual(pld.parse_date("2025-03-13"), date(2025, 3, 13))

    def test_us_formats(self):
        self.assertEqual(pld.parse_date("03/13/2025"), date(2025, 3, 13))
        self.assertEqual(pld.parse_date("3/13/25"), date(2025, 3, 13))
        self.assertEqual(pld.parse_date("2025/03/13"), date(2025, 3, 13))

    def test_blank_and_na(self):
        for value in ("", "   ", "NA", "n/a", "NaN", "none", "null"):
            self.assertIsNone(pld.parse_date(value), value)

    def test_unparseable(self):
        self.assertIsNone(pld.parse_date("March 13"))


class NameSimilarityTests(unittest.TestCase):
    def test_typo_matches_at_default(self):
        self.assertTrue(pld._names_similar("jon smith", "john smith", 0.90))

    def test_typo_fails_at_strict(self):
        self.assertFalse(pld._names_similar("jon smith", "john smith", 0.99))

    def test_unrelated_names(self):
        self.assertFalse(pld._names_similar("jane doe", "bob green", 0.90))


class BuildGroupsTests(unittest.TestCase):
    def test_transitive_name_then_mrn(self):
        # A~B by name (exact), B~C by MRN -> one group {A,B,C}; D separate.
        names = ["amy lee", "amy lee", "cara vale", "solo"]
        mrns = ["1", "9", "9", "42"]
        uf, _ = pld.build_groups(names, mrns, 0.90)
        roots = [uf.find(i) for i in range(4)]
        self.assertEqual(roots[0], roots[1])
        self.assertEqual(roots[1], roots[2])
        self.assertNotEqual(roots[0], roots[3])

    def test_blank_mrn_does_not_link(self):
        names = ["alice", "bob"]
        mrns = ["", ""]
        uf, _ = pld.build_groups(names, mrns, 0.90)
        self.assertNotEqual(uf.find(0), uf.find(1))


class FlagAndWhyTests(unittest.TestCase):
    def test_singleton_is_no(self):
        rows = [["Solo", "1999-09-09", "99", "Claimed", "2025-01-01", ""]]
        flag, why, rec, group = compute(rows)[0]
        self.assertEqual(flag, "NO")
        self.assertEqual(why, "")
        self.assertEqual(rec, "")
        self.assertEqual(group, "")

    def test_exact_name_match(self):
        rows = [
            ["Tom Snyder ", "1972-03-03", "555", "Unclaimed", "", ""],
            ["  TOM SNYDER", "1999-09-09", "556", "Unclaimed", "", ""],
        ]
        results = compute(rows)
        self.assertEqual([r[0] for r in results], ["YES", "YES"])
        self.assertEqual(results[0][1], "Exact name match")

    def test_fuzzy_name_match(self):
        rows = [
            ["Jon Smith", "1985-01-01", "111", "Unclaimed", "", ""],
            ["John Smith", "1985-01-01", "222", "Unclaimed", "", ""],
        ]
        results = compute(rows)
        self.assertEqual([r[0] for r in results], ["YES", "YES"])
        self.assertEqual(results[0][1], "Fuzzy name match")

    def test_shared_mrn_only(self):
        rows = [
            ["Alice Brown", "2000-02-02", "777", "Unclaimed", "2025-04-01", ""],
            ["Bob Green", "1995-07-07", "777", "Unclaimed", "", "2025-01-05"],
        ]
        results = compute(rows)
        self.assertEqual([r[0] for r in results], ["YES", "YES"])
        self.assertEqual(results[0][1], "Shared MRN")

    def test_dob_only_is_not_a_duplicate(self):
        rows = [
            ["Jane Doe", "1990-05-12", "1", "Unclaimed", "", ""],
            ["Mary Poe", "1990-05-12", "2", "Unclaimed", "", ""],
        ]
        self.assertEqual([r[0] for r in compute(rows)], ["NO", "NO"])

    def test_combined_reason(self):
        rows = [
            ["Amy Lee", "2001-01-01", "10", "Unclaimed", "", ""],
            ["Amy Lee", "2001-01-01", "10", "Unclaimed", "", ""],
        ]
        self.assertEqual(compute(rows)[0][1], "Exact name match; Shared MRN")


class RecommendationTests(unittest.TestCase):
    def test_latest_data_date_wins(self):
        rows = [
            ["Jon Smith", "1985-01-01", "111", "Unclaimed", "2025-02-15", ""],
            ["John Smith", "1985-01-01", "222", "Unclaimed", "2025-03-01", ""],
        ]
        recs = [r[2] for r in compute(rows)]
        self.assertEqual(recs, ["Remove this account", "Maintain this account"])

    def test_claimed_beats_data(self):
        # Row 0 unclaimed WITH data; row 1 claimed WITHOUT data -> claimed kept.
        rows = [
            ["Ann Fox", "1980-01-01", "1", "Unclaimed", "2025-05-01", ""],
            ["Ann Fox", "1980-01-01", "2", "Claimed", "", ""],
        ]
        recs = [r[2] for r in compute(rows)]
        self.assertEqual(recs, ["Remove this account", "Maintain this account"])

    def test_boolean_claimed_column_recognized(self):
        # Query-style exports use TRUE/FALSE for the claimed column.
        rows = [
            ["Ann Fox", "1980-01-01", "1", "FALSE", "2025-05-01", ""],
            ["Ann Fox", "1980-01-01", "2", "TRUE", "", ""],
        ]
        recs = [r[2] for r in compute(rows)]
        self.assertEqual(recs, ["Remove this account", "Maintain this account"])

    def test_has_data_beats_no_data(self):
        rows = [
            ["Ken Ito", "1980-01-01", "1", "Unclaimed", "", ""],
            ["Ken Ito", "1980-01-01", "2", "Unclaimed", "2025-01-01", ""],
        ]
        recs = [r[2] for r in compute(rows)]
        self.assertEqual(recs, ["Remove this account", "Maintain this account"])

    def test_no_signal_group_is_blank(self):
        rows = [
            ["Carol White", "1991-04-04", "", "Unclaimed", "", ""],
            ["Carol White2", "1991-04-04", "", "Unclaimed", "", ""],
        ]
        results = compute(rows)
        self.assertEqual([r[0] for r in results], ["YES", "YES"])  # still flagged
        self.assertEqual([r[2] for r in results], ["", ""])  # but no recommendation

    def test_bgm_date_used_when_cgm_blank(self):
        rows = [
            ["Lee Roy", "1980-01-01", "1", "Unclaimed", "", "2025-06-01"],
            ["Lee Roy", "1980-01-01", "2", "Unclaimed", "", "2025-01-01"],
        ]
        recs = [r[2] for r in compute(rows)]
        self.assertEqual(recs, ["Maintain this account", "Remove this account"])


class NullPlaceholderTests(unittest.TestCase):
    def test_null_like_mrn_does_not_link(self):
        rows = [
            ["Alice Brown", "2000-02-02", "null", "Unclaimed", "", ""],
            ["Bob Green", "1995-07-07", "NaN", "Unclaimed", "", ""],
            ["Carol Diaz", "1988-08-08", "NA", "Unclaimed", "", ""],
        ]
        self.assertEqual([r[0] for r in compute(rows)], ["NO", "NO", "NO"])

    def test_null_like_dob_does_not_count_as_agreement(self):
        # Same name, both DOB "null": DOB must NOT count as agreeing, so this
        # stays a weak (name-only) match -> Review, not a strong Remove.
        rows = [
            ["Kim Lee", "null", "1", "Unclaimed", "2025-01-01", ""],
            ["Kim Lee", "NaN", "2", "Unclaimed", "", ""],
        ]
        self.assertEqual(
            [r[2] for r in compute(rows)],
            [
                f"{pld.REVIEW_NAME}; {pld.IF_CONFIRMED_MAINTAIN}",
                f"{pld.REVIEW_NAME}; {pld.IF_CONFIRMED_REMOVE}",
            ],
        )

    def test_real_mrn_still_links(self):
        rows = [
            ["Alice Brown", "2000-02-02", "777", "Unclaimed", "2025-04-01", ""],
            ["Bob Green", "1995-07-07", "777", "Unclaimed", "", ""],
        ]
        self.assertEqual([r[0] for r in compute(rows)], ["YES", "YES"])


class ConfidenceTierTests(unittest.TestCase):
    def test_name_only_dob_differs_is_review_not_remove(self):
        # Row 0 is Claimed, so it is the conditional keeper even though row 1
        # has the later data date.
        rows = [
            ["John Smith", "1980-01-01", "1", "Claimed", "2025-01-01", ""],
            ["Jon Smith", "1990-05-05", "2", "Unclaimed", "2025-02-01", ""],
        ]
        results = compute(rows)
        self.assertEqual([r[0] for r in results], ["YES", "YES"])   # still flagged
        self.assertEqual(
            [r[2] for r in results],
            [
                f"{pld.REVIEW_NAME}; {pld.IF_CONFIRMED_MAINTAIN}",
                f"{pld.REVIEW_NAME}; {pld.IF_CONFIRMED_REMOVE}",
            ],
        )

    def test_name_and_mrn_match_dob_differs_is_strong(self):
        rows = [
            ["Ann Fox", "1980-01-01", "55", "Claimed", "", ""],
            ["Ann Fox", "1999-09-09", "55", "Unclaimed", "2025-01-01", ""],
        ]
        recs = [r[2] for r in compute(rows)]
        self.assertEqual(recs, [pld.MAINTAIN, pld.REMOVE])

    def test_mrn_only_dob_differs_is_review(self):
        rows = [
            ["Alice Brown", "2000-02-02", "777", "Unclaimed", "2025-04-01", ""],
            ["Bob Green", "1995-07-07", "777", "Unclaimed", "", ""],
        ]
        results = compute(rows)
        self.assertEqual([r[0] for r in results], ["YES", "YES"])
        self.assertEqual(
            [r[2] for r in results],
            [
                f"{pld.REVIEW_MRN}; {pld.IF_CONFIRMED_MAINTAIN}",
                f"{pld.REVIEW_MRN}; {pld.IF_CONFIRMED_REMOVE}",
            ],
        )

    def test_mixed_cluster_removes_strong_reviews_weak(self):
        # A & B are the same person (name+DOB); C shares only the name with a
        # different DOB. C must be Review, never an unconditional Remove; its
        # conditional verdict ranks the whole cluster, where Claimed A wins.
        rows = [
            ["Kim Lee", "1980-01-01", "1", "Claimed", "2025-01-01", ""],   # A
            ["Kim Lee", "1980-01-01", "2", "Unclaimed", "", ""],           # B
            ["Kim Lee", "1995-05-05", "3", "Unclaimed", "2025-03-01", ""], # C
        ]
        results = compute(rows)
        self.assertEqual([r[0] for r in results], ["YES", "YES", "YES"])
        self.assertEqual(
            [r[2] for r in results],
            [pld.MAINTAIN, pld.REMOVE, f"{pld.REVIEW_NAME}; {pld.IF_CONFIRMED_REMOVE}"],
        )

    def test_name_dob_match_still_strong(self):
        rows = [
            ["Jon Smith", "1985-01-01", "111", "Unclaimed", "2025-02-15", ""],
            ["John Smith", "1985-01-01", "222", "Unclaimed", "2025-03-01", ""],
        ]
        recs = [r[2] for r in compute(rows)]
        self.assertEqual(recs, [pld.REMOVE, pld.MAINTAIN])


class ConditionalVerdictTests(unittest.TestCase):
    def test_no_signal_review_has_no_verdict(self):
        # Weak pair with nobody Claimed and no data dates: bare Review text.
        rows = [
            ["Pat Doe", "1980-01-01", "1", "Unclaimed", "", ""],
            ["Pat Doe", "1990-09-09", "2", "Unclaimed", "", ""],
        ]
        self.assertEqual([r[2] for r in compute(rows)], [pld.REVIEW_NAME, pld.REVIEW_NAME])

    def test_claimed_beats_data_for_verdict(self):
        # Claimed account without data outranks unclaimed account with data.
        rows = [
            ["Pat Doe", "1980-01-01", "1", "Unclaimed", "2025-05-01", ""],
            ["Pat Doe", "1990-09-09", "2", "Claimed", "", ""],
        ]
        self.assertEqual(
            [r[2] for r in compute(rows)],
            [
                f"{pld.REVIEW_NAME}; {pld.IF_CONFIRMED_REMOVE}",
                f"{pld.REVIEW_NAME}; {pld.IF_CONFIRMED_MAINTAIN}",
            ],
        )

    def test_latest_data_picks_verdict_when_none_claimed(self):
        rows = [
            ["Pat Doe", "1980-01-01", "1", "Unclaimed", "2025-02-15", ""],
            ["Pat Doe", "1990-09-09", "2", "Unclaimed", "2025-03-01", ""],
        ]
        self.assertEqual(
            [r[2] for r in compute(rows)],
            [
                f"{pld.REVIEW_NAME}; {pld.IF_CONFIRMED_REMOVE}",
                f"{pld.REVIEW_NAME}; {pld.IF_CONFIRMED_MAINTAIN}",
            ],
        )

    def test_exactly_one_conditional_keeper_per_cluster(self):
        rows = [
            ["Pat Doe", "1980-01-01", "1", "Unclaimed", "2025-01-01", ""],
            ["Pat Doe", "1990-09-09", "2", "Unclaimed", "2025-02-01", ""],
            ["Pat Doe", "1999-12-31", "3", "Unclaimed", "2025-03-01", ""],
        ]
        recs = [r[2] for r in compute(rows)]
        self.assertEqual(
            sum(rec.endswith(pld.IF_CONFIRMED_MAINTAIN) for rec in recs), 1
        )
        self.assertEqual(
            sum(rec.endswith(pld.IF_CONFIRMED_REMOVE) for rec in recs), 2
        )


class GroupColumnTests(unittest.TestCase):
    def test_distinct_clusters_get_distinct_numbers(self):
        rows = [
            ["Amy Lee", "2001-01-01", "1", "Unclaimed", "", ""],   # cluster 1
            ["Ken Ito", "1990-01-01", "5", "Unclaimed", "", ""],   # cluster 2
            ["Amy Lee", "2001-01-01", "2", "Unclaimed", "", ""],   # cluster 1
            ["Ken Ito", "1990-01-01", "6", "Unclaimed", "", ""],   # cluster 2
            ["Solo", "1970-01-01", "9", "Unclaimed", "", ""],      # not a dup
        ]
        groups = [r[3] for r in compute(rows)]
        self.assertEqual(groups, ["1", "2", "1", "2", ""])

    def test_transitive_members_share_one_number(self):
        rows = [
            ["Robert Lee", "1990-02-02", "300003", "Unclaimed", "", ""],
            ["Robert Lee", "1990-02-02", "300099", "Unclaimed", "", ""],  # name link
            ["Bob Lee", "1990-02-02", "300003", "Unclaimed", "", ""],     # MRN link
        ]
        groups = [r[3] for r in compute(rows)]
        self.assertEqual(groups, ["1", "1", "1"])


class ProcessRoundTripTests(unittest.TestCase):
    def _run(self, text, **kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            inp = Path(tmp) / "in.csv"
            out = Path(tmp) / "out.csv"
            inp.write_text(text, encoding="utf-8")
            yes = pld.process(
                inp,
                out,
                delimiter=kwargs.get("delimiter"),
                name_threshold=kwargs.get("threshold", 0.90),
                name_aliases=pld.NAME_ALIASES,
                dob_aliases=pld.DOB_ALIASES,
                mrn_aliases=pld.MRN_ALIASES,
            )
            rows = list(csv.reader(out.read_text(encoding="utf-8").splitlines()))
            return yes, rows

    def test_minimal_three_columns(self):
        text = "Name,DOB,MRN\nAmy Lee,2001-01-01,10\nAmy Lee,2001-01-01,11\nSolo,1999-09-09,99\n"
        yes, rows = self._run(text)
        self.assertEqual(yes, 2)
        self.assertEqual(
            rows[0][-4:],
            ["Likely Duplicate", "Why", "Recommendation", "Duplicate Group"],
        )
        self.assertEqual(rows[1][-4], "YES")
        self.assertEqual(rows[3][-4], "NO")

    def test_metadata_block_preserved(self):
        text = (
            "Report Date Time,2025-03-14 10:33 AM\n"
            "Total Patients,3\n"
            "\n"
            "Patient Name,Patient User ID,Date of Birth,MRN\n"
            "Jane Doe,a1,1990-05-12,123\n"
            "Jane Doe,a2,1990-05-12,999\n"
            "Solo Patient,a3,1980-01-01,42\n"
        )
        yes, rows = self._run(text)
        self.assertEqual(yes, 2)
        # Metadata rows pass through untouched.
        self.assertEqual(rows[0], ["Report Date Time", "2025-03-14 10:33 AM"])
        self.assertEqual(rows[1], ["Total Patients", "3"])
        self.assertEqual(rows[2], [])  # blank separator line
        # Header row gains the new columns.
        self.assertEqual(
            rows[3][-4:],
            ["Likely Duplicate", "Why", "Recommendation", "Duplicate Group"],
        )

    def test_tab_delimited_autodetected(self):
        text = "Name\tDOB\tMRN\nAmy Lee\t2001-01-01\t10\nAmy Lee\t2001-01-01\t11\n"
        with tempfile.TemporaryDirectory() as tmp:
            inp = Path(tmp) / "in.tsv"
            out = Path(tmp) / "out.tsv"
            inp.write_text(text, encoding="utf-8")
            yes = pld.process(
                inp, out, None, 0.90, pld.NAME_ALIASES, pld.DOB_ALIASES, pld.MRN_ALIASES
            )
            header = out.read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(yes, 2)
        self.assertIn("Likely Duplicate\tWhy\tRecommendation\tDuplicate Group", header)

    def test_missing_required_column_raises(self):
        with self.assertRaises(ValueError):
            self._run("Name,DOB\nAmy,2001-01-01\n")

    def test_combined_last_data_date_column(self):
        # A single "Last Data Date" column drives the keep decision.
        text = (
            "Name,DOB,MRN,Custodial Status,Last Data Date\n"
            "Kim Lee,1980-01-01,1,Unclaimed,2025-01-01\n"
            "Kim Lee,1980-01-01,2,Unclaimed,2025-06-01\n"
        )
        yes, rows = self._run(text)
        ri = rows[0].index("Recommendation")
        self.assertEqual([r[ri] for r in rows[1:]], ["Remove this account", "Maintain this account"])


if __name__ == "__main__":
    unittest.main()
