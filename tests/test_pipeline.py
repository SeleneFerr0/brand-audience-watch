"""Synthetic fixtures test invariants; they are never empirical market evidence."""
import csv
import gzip
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from brand_intel.cli import FEATURES, ingest, snapshots, stamp, validate_choices, require_category, DAY


def database():
    con=sqlite3.connect(":memory:")
    con.execute("CREATE TABLE events (event_id TEXT PRIMARY KEY,product_id TEXT,reviewer_id TEXT,ts INTEGER,rating REAL,verified INTEGER,scent INTEGER,texture INTEGER,packaging INTEGER,value INTEGER)")
    return con


def event(con,key,product,date,rating=4):
    con.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)",(key,product,"public_hash",stamp(date),rating,1,1,0,0,0))


class TemporalTests(unittest.TestCase):
    def test_rejects_mixed_category_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/"data/processed").mkdir(parents=True)
            (root/"data/processed/task-manifest.json").write_text(json.dumps({"category":"A"}))
            with self.assertRaisesRegex(ValueError,"different category"):
                require_category(root,{"category":"B"},"task-manifest.json")

    def test_future_feedback_changes_labels_but_not_features(self):
        con=database()
        for i,d in enumerate(["2022-12-01","2022-12-20","2022-12-31"]):
            event(con,str(i),"A",d)
        before=snapshots(con,"2023-01-01",90,3)[0]
        event(con,"future","A","2023-01-02",1)
        after=snapshots(con,"2023-01-01",90,3)[0]
        self.assertEqual([before[x] for x in FEATURES],[after[x] for x in FEATURES])
        self.assertEqual(after["future_reviews"],before["future_reviews"]+1)

    def test_cutoff_and_label_end_are_half_open(self):
        con=database()
        event(con,"past","A","2022-12-31")
        event(con,"boundary","A","2023-01-01")
        event(con,"end","A","2023-04-01")
        event(con,"new","UNSEEN","2023-01-02")
        rows=snapshots(con,"2023-01-01",90,1)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["prior_reviews"],1)
        self.assertEqual(rows[0]["future_reviews"],1)

    def test_ingestion_deduplicates_and_rejects_invalid_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/"data/raw").mkdir(parents=True)
            row={"parent_asin":"A","user_id":"PUBLIC","timestamp":stamp("2022-01-01"),"rating":5,"text":"Good scent","verified_purchase":True}
            with gzip.open(root/"data/raw/Test.jsonl.gz","wt") as f:
                for r in [row,row,{**row,"rating":99}]: f.write(json.dumps(r)+"\n")
            ingest(root,{"category":"Test"})
            report=json.loads((root/"data/processed/data-quality.json").read_text())
            self.assertEqual(report["events"],1)
            self.assertEqual(report["invalid_rows"],1)
            self.assertEqual(report["exact_duplicates_removed"],1)
            con=sqlite3.connect(root/"data/processed/reviews.sqlite")
            stored=con.execute("SELECT reviewer_id,scent FROM events").fetchone()
            self.assertNotEqual(stored[0],"PUBLIC")
            self.assertEqual(stored[1],1)


class ChoiceTests(unittest.TestCase):
    def validate(self, changes=None):
        rows=[dict(respondent_id="U",choice_set_id="S",alternative_id="A",price=10,attribute_score=1,chosen=1,is_outside_option=0,data_origin="observed_choice_experiment"),
              dict(respondent_id="U",choice_set_id="S",alternative_id="Outside",price=0,attribute_score=0,chosen=0,is_outside_option=1,data_origin="observed_choice_experiment")]
        if changes: changes(rows)
        with tempfile.TemporaryDirectory() as tmp:
            file=Path(tmp)/"choices.csv"
            with file.open("w",newline="") as f:
                w=csv.DictWriter(f,fieldnames=rows[0]); w.writeheader(); w.writerows(rows)
            return validate_choices(file)

    def test_accepts_structurally_valid_observed_choices(self):
        self.assertEqual(len(self.validate()),2)

    def test_reviews_cannot_be_relabeled_as_choices_by_default(self):
        with self.assertRaisesRegex(ValueError,"Only observed"):
            self.validate(lambda rows:rows[0].update(data_origin="amazon_review"))

    def test_requires_exactly_one_choice(self):
        with self.assertRaisesRegex(ValueError,"exactly one"):
            self.validate(lambda rows:rows[1].update(chosen=1))

    def test_rejects_negative_price(self):
        with self.assertRaisesRegex(ValueError,"nonnegative"):
            self.validate(lambda rows:rows[0].update(price=-10))

    def test_requires_an_outside_option(self):
        with self.assertRaisesRegex(ValueError,"outside"):
            self.validate(lambda rows:rows[1].update(is_outside_option=0))


if __name__=="__main__": unittest.main()
