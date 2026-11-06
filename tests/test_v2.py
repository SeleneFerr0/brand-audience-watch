"""Contract, leakage, distribution and workflow regression tests."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import numpy as np
import pandas as pd

from brand_intel.contracts import ContractError,InsufficientEvidence,source_identity,fingerprint,open_build,validate_prepared,file_hash,utc_ms
from brand_intel.data import build
from brand_intel.tasks import task_frame,rolling_folds
from brand_intel.representations import Representation
from brand_intel.count_models import CountModel
from brand_intel.evaluation import evaluate,naive_predictions
from brand_intel.runs import run
from brand_intel.preferences import split_consumers,validate,synthetic_choice_fixture,choice_arrays
import brand_intel.tasks as task_module


def fixture(root):
    events=[];products=[]
    launches=[("2020-01-06","2020-01-01"),("2020-07-06","2020-07-01"),("2021-01-04","2021-01-01")]
    for c,(cutoff,available) in enumerate(launches):
        for i in range(4):
            item=f"C{c}_{i}"
            products.append(dict(item_id=item,available_at=available,effective_at=available,title=f"Product fresh {i}",description="portable reusable gift",attributes=json.dumps({"price":i+1,"size":10+i}),evidence_uri=f"fixture://catalog/{item}"))
            for week in range(4):
                t=utc_ms(cutoff)+(week*7+1)*86400000
                events.append(dict(event_id=f"{item}_{week}",item_id=item,entity_id="synthetic_person",event_time=t,available_at=t,count=(i+1 if week==0 else 0),rating=4,text="useful gift convenient",evidence_uri=f"fixture://event/{item}_{week}"))
    pd.DataFrame(events).to_csv(root/"events.csv",index=False)
    pd.DataFrame(products).to_csv(root/"products.csv",index=False)
    return {"schema_version":2,"source":{"dataset_id":"synthetic_contract_fixture","adapter":"canonical_csv","events_path":"events.csv","products_path":"products.csv","target_unit":"events","coverage_end":"2022-01-01","invalid_policy":"error"},
            "task":{"kind":"cold_start","horizon_weeks":4,"numeric_attributes":["price","size"]},
            "backtest":{"train_cutoffs":["2020-01-06","2020-07-06"],"test_cutoffs":["2021-01-04"]},
            "models":["poisson","negative_binomial","hurdle","quantile"],"representations":["numeric","word_tfidf"],"chronos":{"enabled":False},"seed":42}


class ContractTests(unittest.TestCase):
    def test_legacy_same_category_different_horizon_is_rejected(self):
        from brand_intel.cli import require_category,FEATURES
        with tempfile.TemporaryDirectory() as d:
            r=Path(d);folder=r/"data/processed";folder.mkdir(parents=True)
            cfg={"category":"same_category","horizon_days":28}
            manifest={"category":cfg["category"],"task_fingerprint":fingerprint({"config":cfg,"features":FEATURES,"schema":2}),"files":{}}
            (folder/"task-manifest.json").write_text(json.dumps(manifest))
            require_category(r,cfg,"task-manifest.json")
            with self.assertRaisesRegex(ContractError,"configuration mismatch"):
                require_category(r,{**cfg,"horizon_days":56},"task-manifest.json")

    def test_chronos_missing_weights_are_unavailable_not_predictions(self):
        from brand_intel.backtesting import chronos_prediction
        with tempfile.TemporaryDirectory() as d:
            r=Path(d);s=fixture(r)
            s["chronos"]={"enabled":True,"revision":"a"*40,"local_files_only":True}
            prediction,status=chronos_prediction(r,s,r,{},pd.DataFrame())
            self.assertIsNone(prediction)
            self.assertEqual(status["status"],"unavailable")
            self.assertIn("weights",status["reason"])

    def test_csv_mapping_and_future_sources_are_not_amazon_specific(self):
        with tempfile.TemporaryDirectory() as d:
            r=Path(d);s=fixture(r)
            e=pd.read_csv(r/"events.csv").head(1)
            e["event_time"]=utc_ms("2026-11-05");e["available_at"]=utc_ms("2026-11-05")
            mapping={name:"source_"+name for name in e.columns}
            e.rename(columns=mapping).to_csv(r/"other.csv",index=False)
            s["source"].update(dataset_id="synthetic_future_sales",events_path="other.csv",products_path=None,event_columns=mapping,target_unit="units_sold",coverage_end="2027-01-01")
            build(r,s);con,m=open_build(r,s)
            self.assertEqual(con.execute("SELECT count(*) FROM events").fetchone()[0],1);con.close()

    def test_task_code_change_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            r=Path(d);s=fixture(r);f=r/"task.csv";f.write_text("target\n1\n")
            m={"task_fingerprint":fingerprint(s),"task_code_sha256":"obsolete","files":{"task.csv":file_hash(f)}}
            with self.assertRaisesRegex(ContractError,"implementation changed"):validate_prepared(m,s,[f])

    def test_horizon_change_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            r=Path(d);s=fixture(r);f=r/"task.csv";f.write_text("target\n1\n")
            m={"task_fingerprint":fingerprint(s),"task_code_sha256":file_hash(Path(task_module.__file__)),"files":{"task.csv":file_hash(f)}}
            validate_prepared(m,s,[f]);s["task"]["horizon_weeks"]=8
            with self.assertRaisesRegex(ContractError,"configuration changed"):validate_prepared(m,s,[f])

    def test_mutated_table_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            r=Path(d);s=fixture(r);f=r/"task.csv";f.write_text("target\n1\n")
            m={"task_fingerprint":fingerprint(s),"task_code_sha256":file_hash(Path(task_module.__file__)),"files":{"task.csv":file_hash(f)}}
            f.write_text("target\n2\n")
            with self.assertRaisesRegex(ContractError,"hash mismatch"):validate_prepared(m,s,[f])

    def test_build_identity_and_corruption(self):
        with tempfile.TemporaryDirectory() as d:
            r=Path(d);s=fixture(r);key=build(r,s)
            con,m=open_build(r,s);con.close();self.assertEqual(m["counts"]["products"],12)
            db=r/"data/builds"/key/"canonical.sqlite"
            with db.open("ab") as f:f.write(b"tampered")
            with self.assertRaisesRegex(ContractError,"content changed"):open_build(r,s)

    def test_source_file_change_requires_new_build(self):
        with tempfile.TemporaryDirectory() as d:
            r=Path(d);s=fixture(r);build(r,s)
            with (r/"events.csv").open("a") as f:f.write("\n")
            with self.assertRaisesRegex(ContractError,"No matching"):open_build(r,s)

    def test_failure_state_is_persisted(self):
        with tempfile.TemporaryDirectory() as d:
            folder=None
            with self.assertRaisesRegex(RuntimeError,"intentional"):
                with run(Path(d),"test",{}) as (folder,state):raise RuntimeError("intentional")
            self.assertEqual(json.loads((folder/"state.json").read_text())["status"],"failed")


class TaskTests(unittest.TestCase):
    def test_training_labels_exclude_future_arrivals(self):
        with tempfile.TemporaryDirectory() as d:
            r=Path(d);s=fixture(r);e=pd.read_csv(r/"events.csv")
            delayed=e.iloc[0].copy();delayed["event_id"]="late_training_label"
            delayed["count"]=999;delayed["available_at"]=utc_ms("2021-02-01")
            e.loc[len(e)]=delayed;e.to_csv(r/"events.csv",index=False)
            build(r,s);con,m=open_build(r,s)
            train=task_frame(con,s,"2020-01-06",labels_available_before="2021-01-04")
            hindsight=task_frame(con,s,"2020-01-06");con.close()
            self.assertEqual(hindsight.target.sum()-train.target.sum(),999)
            self.assertFalse(train.label_evidence.str.contains("late_training_label").any())

    def test_dynamic_attributes_are_preserved_and_available_asof(self):
        with tempfile.TemporaryDirectory() as d:
            r=Path(d);s=fixture(r)
            e=pd.read_csv(r/"events.csv");e["attributes"]=[json.dumps({"promo_index":1})]*len(e)
            row=e.iloc[0].copy();row["event_id"]="later_promotion";row["event_time"]=utc_ms("2020-12-01");row["available_at"]=utc_ms("2021-02-01");row["attributes"]=json.dumps({"promo_index":99})
            e.loc[len(e)]=row;e.to_csv(r/"events.csv",index=False)
            s["task"].update(kind="trend",min_prior_events=1,dynamic_numeric_attributes=["promo_index"])
            build(r,s);con,m=open_build(r,s);f=task_frame(con,s,"2021-01-04");con.close()
            self.assertTrue((f.dyn_promo_index==1).all())

    def test_full_canonical_cold_start_backtest(self):
        from brand_intel.backtesting import backtest
        with tempfile.TemporaryDirectory() as d:
            r=Path(d);s=fixture(r);build(r,s);folder=backtest(r,s)
            state=json.loads((folder/"state.json").read_text())
            report=json.loads((folder/"summary.json").read_text())
            self.assertEqual(state["status"],"completed")
            self.assertEqual(report["required_model_failures"],[])
            self.assertEqual(report["task_kind"],"cold_start")
            self.assertIn("zero",report["folds"][0]["scores"])

    def test_cold_start_uses_known_attributes_without_history(self):
        with tempfile.TemporaryDirectory() as d:
            r=Path(d);s=fixture(r);build(r,s);con,m=open_build(r,s)
            f=task_frame(con,s,"2021-01-04");con.close()
            self.assertEqual(set(f.item_id),{f"C2_{i}" for i in range(4)})
            self.assertTrue((f.prior_count==0).all())
            self.assertTrue(f.text.str.contains("portable").all())
            self.assertTrue(f.text.str.contains('"size"').all())
            self.assertTrue(f.feature_evidence.str.contains("catalog").all())
            self.assertEqual(f.lead_week.nunique(),4)

    def test_delayed_events_and_future_text_do_not_enter_features(self):
        with tempfile.TemporaryDirectory() as d:
            r=Path(d);s=fixture(r)
            e=pd.read_csv(r/"events.csv");e.loc[len(e)]={"event_id":"delayed","item_id":"C0_0","entity_id":"u","event_time":utc_ms("2020-06-01"),"available_at":utc_ms("2021-02-01"),"count":999,"rating":5,"text":"secret_future_token","evidence_uri":"fixture://delayed"};e.to_csv(r/"events.csv",index=False)
            s["task"]["kind"]="trend";s["task"]["min_prior_events"]=1
            build(r,s);con,m=open_build(r,s);f=task_frame(con,s,"2021-01-04");con.close()
            self.assertFalse(f.text.str.contains("secret_future_token").any())
            self.assertLess(f.prior_count.max(),999)

    def test_unknown_metadata_availability_is_not_backdated(self):
        with tempfile.TemporaryDirectory() as d:
            r=Path(d);s=fixture(r);build(r,s)
            # Normalized DB simulated here solely to test availability filtering.
            con=sqlite3.connect(":memory:");con.row_factory=sqlite3.Row
            con.execute("CREATE TABLE events(event_id TEXT,item_id TEXT,entity_id TEXT,event_time INTEGER,available_at INTEGER,count REAL,rating REAL,text TEXT,evidence_uri TEXT)")
            con.execute("CREATE TABLE products(item_id TEXT,available_at INTEGER,effective_at INTEGER,title TEXT,description TEXT,attributes TEXT,evidence_uri TEXT,availability_known INTEGER)")
            con.execute("INSERT INTO products VALUES ('A',NULL,NULL,'title','desc','{}','fixture://A',0)")
            with self.assertRaises(InsufficientEvidence):task_frame(con,s,"2021-01-04")
            con.close()

    def test_rolling_training_labels_are_mature(self):
        s={"task":{"horizon_weeks":4},"backtest":{"train_cutoffs":["2022-12-05","2022-12-26"],"test_cutoffs":["2023-01-02"]}}
        self.assertEqual(list(rolling_folds(s))[0][1],["2022-12-05"])

    def test_text_vocabulary_is_training_only(self):
        train=pd.DataFrame({"x":[1,2,3],"text":["portable gift","useful box","portable box"]})
        test=pd.DataFrame({"x":[9],"text":["futureleakword"]})
        t=Representation(["x"],"word_tfidf");a=t.fit_transform(train);b=t.transform(test)
        self.assertNotIn("futureleakword",t.vectorizer.vocabulary_)
        self.assertEqual(a.shape[1],b.shape[1])


class CountTests(unittest.TestCase):
    def test_zero_ranking_is_not_candidate_order_dependent(self):
        f=pd.DataFrame({"count_7d":np.zeros(20),"count_28d":np.zeros(20)})
        y=np.r_[np.ones(10)*3,np.zeros(10)]
        pred=naive_predictions(f)["zero"]
        self.assertEqual(evaluate(y,pred,2)["precision_at_k"],.5)
        self.assertEqual(evaluate(y[::-1],pred,2)["precision_at_k"],.5)

    def setUp(self):
        rng=np.random.default_rng(42);self.x=rng.normal(size=(250,2));mu=np.exp(.2+.3*self.x[:,0]);self.y=rng.negative_binomial(2,2/(2+mu))

    def test_all_models_predict_valid_outputs(self):
        for kind in ["poisson","negative_binomial","hurdle","quantile"]:
            with self.subTest(kind=kind):
                model=CountModel(kind).fit(self.x,self.y);p=model.predict(self.x[:20],2)
                scores=evaluate(self.y[:20],p,2)
                self.assertTrue(np.isfinite(scores["mean_pinball_loss"]))
                if kind=="quantile":self.assertIsNone(scores["hit_brier"])
                else:self.assertIsNotNone(scores["nll"])

    def test_hurdle_probability_mass_and_tail(self):
        m=CountModel("hurdle").fit(self.x,self.y);p=m.predict(self.x[:1],1)
        pi=p["hit_probability"][0]
        zero=np.exp(p["log_prob"](np.array([0.])))[0]
        self.assertAlmostEqual(pi+zero,1,places=8)
        mass=sum(float(np.exp(p["log_prob"](np.array([float(k)])))[0]) for k in range(200))
        self.assertAlmostEqual(mass,1,places=7)

    def test_zero_baseline_is_explicit_and_can_win_mae(self):
        f=pd.DataFrame({"count_7d":[3,3,3,3],"count_28d":[12,12,12,12]})
        p=naive_predictions(f);y=np.array([0,0,0,1])
        self.assertLess(evaluate(y,p["zero"],2)["mae_point"],evaluate(y,p["recent_mean"],2)["mae_point"])

    def test_invalid_quantiles_are_rejected(self):
        with self.assertRaisesRegex(ValueError,"quantile order"):
            evaluate(np.array([0]),{"point":np.array([1]),"quantiles":np.array([[3,2,1]]),"point_semantics":"test"},2)


class PreferenceTests(unittest.TestCase):
    def test_holdout_is_disjoint_and_synthetic_requires_optin(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"choices.csv";synthetic_choice_fixture(p,consumers=12,tasks=3);df=pd.read_csv(p)
            with self.assertRaisesRegex(ContractError,"opt-in"):validate(df,["price","attribute"])
            validate(df,["price","attribute"],True)
            train,test=split_consumers(df)
            self.assertFalse(set(train.respondent_id)&set(test.respondent_id))

    def test_singular_price_design_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"choices.csv";synthetic_choice_fixture(p,consumers=6,tasks=3);df=pd.read_csv(p);df["attribute"]=df.price
            with self.assertRaisesRegex(InsufficientEvidence,"rank deficient"):validate(df,["price","attribute"],True)

    def test_variable_choice_sets_are_supported(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"choices.csv";synthetic_choice_fixture(p,consumers=6,tasks=3);df=pd.read_csv(p)
            # In one task remove an unchosen inside alternative, retaining the
            # real choice and outside option; other tasks still have 3 choices.
            removable=df[(df.respondent_id=="synthetic_0")&(df.choice_set_id=="task_0")&(df.chosen==0)&(df.is_outside_option==0)].index[0]
            df=df.drop(removable)
            validate(df,["price","attribute"],True)
            x,mask,y,people,users=choice_arrays(df,["price","attribute"],np.ones(2))
            self.assertEqual(mask.sum(),len(df))
            self.assertEqual(set(mask.sum(axis=1)),{2,3})
            self.assertTrue(mask[np.arange(len(y)),y].all())


if __name__=="__main__":unittest.main()
