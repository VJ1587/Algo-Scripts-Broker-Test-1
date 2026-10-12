"""Build self-contained MQL5 scanner scripts; no strategy selector or runtime dependency.

Templates hold mathematical/data helpers only. Each output contains one entry method.
Run this file after changing templates, then compile all outputs in MetaEditor.
"""
from pathlib import Path
import json
import re
import shutil
import hashlib

ROOT = Path(__file__).resolve().parent


def build():
    manifest = []
    common = (ROOT / "templates/common.mql").read_text()
    for template in sorted((ROOT / "templates/scalp").glob("*.mql")):
        name = "Futures_Scalp_" + template.stem
        source = header(name) + common + profile_helpers() + futures_method(template) + scalp_start()
        deliver(name, source, "futures scalp", manifest)
    daily = (ROOT / "templates/daily.mql").read_text()
    for name, asset, grade in [
        ("Daily_FX_Major_A", "fx_major", "A"),
        ("Daily_FX_Major_B", "fx_major", "B"),
        ("Daily_FX_Cross_A", "fx_cross", "A"),
        ("Daily_Gold_A", "gold", "A"),
        ("Daily_Gold_B", "gold", "B"),
        ("Daily_SP500_A", "spx", "A"),
        ("Daily_Oil_A", "oil", "A"),
    ]:
        source = header(name) + common + daily.replace("@ASSET@", asset).replace("@GRADE@", grade)
        deliver(name, source, "daily position", manifest)
    scaling = ROOT.parent / "gold_es_scaling"
    if (scaling / "Gold_ES_Scaling.mq5").exists():
        for suffix in (".mq5", ".ex5"):
            shutil.copy2(scaling / ("Gold_ES_Scaling" + suffix), ROOT / "Scripts" / ("Gold_ES_Scaling" + suffix))
        shutil.copy2(scaling / "validation" / "Gold_ES_Scaling.log", ROOT / "validation" / "Gold_ES_Scaling.log")
        manifest.append({"file":"Gold_ES_Scaling.mq5", "family":"gold ES scaling Python bridge",
                         "strategy_id":"gold_es_scaling", "version":"2.8.0", "orders":False,
                         "feed":"gold_es_scaling.tsv",
                         "source_pine_sha256":hashlib.sha256((scaling / "source" / "gold_es_scaling_v2_8_0.pine").read_bytes()).hexdigest()})
    institutional = ROOT.parents[1] / "institutional_trading_system" / "mt5"
    bridge_manifest = institutional / "manifest.json"
    if bridge_manifest.exists():
        for item in json.loads(bridge_manifest.read_text()):
            for suffix in (".mq5", ".ex5"):
                source_file = (institutional / "Scripts" / item["file"]).with_suffix(suffix)
                if not source_file.exists():
                    raise RuntimeError(f"Build/compile institutional bridge first: {source_file}")
                shutil.copy2(source_file, ROOT / "Scripts" / source_file.name)
            log_name = Path(item["file"]).stem + ".log"
            shutil.copy2(institutional / "validation" / log_name, ROOT / "validation" / log_name)
            manifest.append(item)
    (ROOT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Built {len(manifest)} separate scripts in {ROOT / 'Scripts'}")


METHODS = {
    "MOMO_Base": ("momo", {"InpEntryTicks": "entry_ticks"}),
    "Trend": ("trend", {"InpMaxGapTicks": "max_gap_ticks"}),
    "RMA": ("rma", {"InpMinimumExtensionTicks": "min_ext_ticks"}),
    "FFMA": ("ffma", {"InpMinDistanceTicks": "min_dist_ticks"}),
    "Double_Bottom_Top": ("dbdt", {"InpMinimumMoveTicks": "min_move_ticks"}),
    "ORB_MOMO": ("orb", {}),
    "ORB_Trend": ("orb", {"InpExtensionTicks": "trend_ext_ticks", "InpStopTicks": "trend_stop_ticks"}),
    "One_Minute_Range_Break": ("range_break", {"InpMaxDepth": "max_depth"}),
}


def profiles():
    return json.loads((ROOT / "futures_profiles.json").read_text())


def profile_helpers():
    values=profiles()["products"]
    tick_args=",".join(str(values[p]["tick"]) for p in ("ES","NQ","CL","GC"))
    return '''
enum FuturesProduct { PRODUCT_SELECT=0, PRODUCT_ES_MES=1, PRODUCT_NQ_MNQ=2, PRODUCT_CL_MCL=3, PRODUCT_GC_MGC=4 };
input FuturesProduct InpProduct=PRODUCT_SELECT; // REQUIRED: choose price-rule family for the attached symbol
// Micro contracts share parent price-distance rules; P&L uses the broker lot specification.
double Resolved(double override_value,double es,double nq,double cl,double gc)
{
   if(override_value<0) return 0; // negative disables an optional second target
   if(override_value>0) return override_value;
   if(InpProduct==PRODUCT_ES_MES) return es;
   if(InpProduct==PRODUCT_NQ_MNQ) return nq;
   if(InpProduct==PRODUCT_CL_MCL) return cl;
   return gc;
}
bool InitializeProfile()
{
   if(InpProduct==PRODUCT_SELECT) { Print("Select InpProduct: ES/MES, NQ/MNQ, CL/MCL or GC/MGC before scanning"); return false; }
   rule_tick=Resolved(InpRuleTick,@TICKS@);
   Print(STRATEGY," product=",EnumToString(InpProduct)," rule tick=",DoubleToString(rule_tick,5)," broker tick=",DoubleToString(broker_tick,5));
   return rule_tick>0;
}
'''.replace("@TICKS@",tick_args)


def futures_method(template):
    key,mapping=METHODS[template.stem]
    settings=profiles()["methods"][key]
    mapping=dict(mapping)
    code=template.read_text()
    if key != "range_break":
        target_key="momo_targets_ticks" if template.stem=="ORB_MOMO" else "trend_targets_ticks" if template.stem=="ORB_Trend" else "targets_ticks"
        targets=settings[target_key]
        target1="InpTarget1Ticks" if key=="orb" else "InpTargetTicks"
        mapping[target1]={p:targets[p][0] for p in targets}
        mapping["InpTarget2Ticks"]={p:targets[p][1] if len(targets[p])>1 else 0 for p in targets}
        if key!="orb":
            code=code.replace("void Scan(","input double InpTarget2Ticks=0; // 0 profile default; -1 disables\nvoid Scan(",1)
            code=code.replace("InpTargetTicks,0,", "InpTargetTicks,InpTarget2Ticks,")
    for name,field in mapping.items():
        values=settings[field] if isinstance(field,str) else field
        expression="Resolved("+name+","+",".join(str(values[p]) for p in ("ES","NQ","CL","GC"))+")"
        lines=[]
        for line in code.splitlines():
            if re.match(r"input double "+name+r"=",line):
                line=re.sub(r"=([^;]+);", "=0; // 0 uses selected product profile",line, count=1)
            else:
                line=re.sub(r"\b"+name+r"\b",expression,line)
            lines.append(line)
        code="\n".join(lines)+"\n"
    if template.stem=="ORB_Trend":
        code=code.replace("   if(extended<0)","   if(extended<0)")
        # ES source: extension must exceed both 40 ticks and two current M5 ATRs.
        code=code.replace("if(distance>=Resolved(InpExtensionTicks,40,160,25,20)*rule_tick)",
                          "if(distance>=MathMax(Resolved(InpExtensionTicks,40,160,25,20)*rule_tick, InpProduct==PRODUCT_ES_MES ? 2.0*f.a[f.k] : 0.0))")
    return code


def header(name):
    return f'''// Generated by build_scripts.py. Edit templates, then rebuild.
// Source: scanner cfg-0.13.0 / scalp-cfg-0.1.0. Signal-only; no order calls.
#property strict
#property script_show_inputs
#property version "1.00"
#property description "{name}: standalone scanner, closed bars only, no orders"
const string STRATEGY="{name}";
'''


def deliver(name, source, family, manifest):
    out = ROOT / "Scripts" / (name + ".mq5")
    out.parent.mkdir(exist_ok=True)
    if not out.exists() or out.read_text(encoding="utf-8") != source:
        out.write_text(source, encoding="utf-8")
    manifest.append({"file": out.name, "family": family, "orders": False})


def scalp_start():
    return '''
int OnStart()
{
   if(!Initialize() || !InitializeProfile()) return 1;
   Frame f5,f1;
   if(!LoadFrame(PERIOD_M5,1200,f5)) return 2;
   if(NEEDS_M1 && !LoadFrame(PERIOD_M1,600,f1)) return 2;
   if(!OpenReport()) return 3;
   Scan(f5,f1);
   FinishReport();
   return 0;
}
'''


if __name__ == "__main__":
    build()
