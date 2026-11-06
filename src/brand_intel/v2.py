"""Data-source-independent task runner. Legacy CLI remains for old artifacts."""
import argparse,json
from pathlib import Path
from .contracts import load_spec,save_json,source_identity,fingerprint
from .runs import run


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root",type=Path,default=Path.cwd())
    commands=parser.add_subparsers(dest="command",required=True)
    for name in ["data-build","backtest"]:
        p=commands.add_parser(name);p.add_argument("--spec",type=Path,required=True)
    p=commands.add_parser("preferences");p.add_argument("--spec",type=Path,required=True);p.add_argument("--input",type=Path,required=True);p.add_argument("--allow-synthetic-validation",action="store_true")
    p=commands.add_parser("synthetic-choice-fixture");p.add_argument("--output",type=Path,required=True)
    args=parser.parse_args();root=args.root.resolve()
    if args.command=="synthetic-choice-fixture":
        from .preferences import synthetic_choice_fixture
        synthetic_choice_fixture(root/args.output)
        print("Wrote explicitly synthetic validation fixture; not empirical consumer evidence")
    elif args.command=="preferences":
        from .preferences import fit_preferences
        spec=json.loads((root/args.spec).read_text())
        print(fit_preferences(root,spec,root/args.input,args.allow_synthetic_validation))
    else:
        spec=load_spec(root/args.spec,root)
        if args.command=="data-build":
            from .data import build
            with run(root,"data_build",spec) as (folder,state):
                data_id=build(root,spec);state["data_id"]=data_id
            print(data_id)
        else:
            from .backtesting import backtest
            print(backtest(root,spec))


if __name__=="__main__": main()
