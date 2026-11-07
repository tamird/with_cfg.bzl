"""Check the conditional-list supplier's transition paths and canonical documentation."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET

W = Path(os.environ["RUNNER_TEMP"]) / "conditional-list"
SRC = W / "src"
OUT = W / "out"
OWNER = "//with_cfg/tests:set_empty_list_test"
SUBJECT = OWNER + "_subject"
PRODUCT = "8cd26b34acdb2da2a1b56331c267d8dcc0f29622"
RESULT = {"source": PRODUCT, "versions": {}, "failures": {}, "complete": False}
NATIVE = ["--jobs=3","--loading_phase_threads=3","--remote_cache=","--remote_executor=","--bes_backend=",
          "--spawn_strategy=processwrapper-sandbox","--lockfile_mode=off"]


def save():
    (OUT / "supplier.json").write_text(json.dumps(RESULT, indent=2, sort_keys=True) + "\n")


def execute(ver, tag, args):
    p = subprocess.run([str(W/("bazel-" + ver)), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g", *args],
                       cwd=SRC, capture_output=True)
    (W/(tag+".stdout")).write_bytes(p.stdout)
    (W/(tag+".stderr")).write_bytes(p.stderr)
    entry = {"exit":p.returncode}
    if p.returncode:
        lines=(p.stderr+p.stdout).decode(errors="replace").splitlines()
        ids=[i for i,s in enumerate(lines) if re.search(r"ERROR|FAIL|error:|WARNING:.*(canonical|format)|^@@|^diff |^[-+]  ",s)][:12]
        selected=sorted(set(range(max(len(lines)-4,0),len(lines))) | {j for i in ids for j in range(i,min(i+3,len(lines)))})
        entry["diagnostic"]=[re.sub(r"https?://[^\s)]+","<public URL>",lines[i]).replace(str(W),"<work>")[:400] for i in selected]
    RESULT.setdefault("commands",{})[tag]=entry
    save()
    return p


def read_outputs(result):
    files={}
    for f in result.get("testActionOutput", []):
        if f.get("name") not in ("test.xml","test.log"):
            continue
        u=urlparse(f["uri"])
        assert u.scheme == "file"
        data=Path(unquote(u.path)).read_bytes()
        digest=hashlib.sha256(data).hexdigest()
        if f.get("digest"): assert digest == f["digest"]
        if "length" in f: assert len(data)==int(f["length"])
        if f["name"]=="test.xml":
            xml=ET.fromstring(data)
            assert next(xml.iter("testcase"), None) is not None
            assert all(next(xml.iter(k), None) is None for k in ("failure", "error", "skipped"))
        files[f["name"]]=[digest,len(data)]
    assert set(files)=={"test.xml","test.log"}
    return files


def test(ver):
    label="supplier-"+ver
    info=RESULT["versions"][ver]={}
    k=execute(ver,label+"-kind",["query", "--lockfile_mode=off", "--output=label_kind",SUBJECT])
    if not k.returncode:
        kind=k.stdout.decode().strip()
        info["subject_kind"]=kind
    bep=W/(label+".bep")
    owners=[OWNER] if ver=="7.7.1" else ["//with_cfg/tests:rule_test","//with_cfg/tests:setting_test","//with_cfg/tests:select_test","//docs:all"]
    tested=execute(ver,label+"-test",["test",*NATIVE,"--test_output=errors","--nocache_test_results",
                                    "--build_event_json_file="+str(bep),*owners])
    rows={}
    if bep.exists():
        for s in bep.read_text().splitlines():
            e=json.loads(s)
            if "testResult" not in e: continue
            owner=e["id"]["testResult"]["label"]
            result=e["testResult"]
            rows[owner]={"status":result["status"],"cache":result.get("cachedLocally",False),
                         "strategy":result.get("executionInfo",{}).get("strategy")}
            if owner==OWNER and result["status"]=="PASSED":
                rows[owner]["outputs"]=read_outputs(result)
    info["tests"]=rows
    save()
    assert k.returncode==tested.returncode==0
    kind=info["subject_kind"]
    if ver == "7.7.1":
        assert kind == "alias rule " + SUBJECT, kind
    else:
        assert kind.endswith(" rule " + SUBJECT) and not kind.startswith("alias "), kind
    assert OWNER in rows and rows[OWNER]["status"]=="PASSED" and not rows[OWNER]["cache"] and rows[OWNER].get("outputs")
    assert all(v["status"]=="PASSED" for v in rows.values())
    if ver=="9.2.0": assert any(k.startswith("//docs:") for k in rows) and len(rows)>9
    info["verified"]=True
    save()


def main():
    assert subprocess.check_output(["git","rev-parse","HEAD"],cwd=SRC,text=True).strip()==PRODUCT
    for ver in ("9.2.0","7.7.1"):
        try: test(ver)
        except Exception as e:
            RESULT["failures"][ver]=type(e).__name__+" "+str(e)[:410]
            save()
    # Run the public canonical style command independently of either test outcome.
    p=execute("9.2.0","format",["run",*NATIVE,"//.github/workflows:buildifier.check"])
    if p.returncode: RESULT["failures"]["format"]="canonical buildifier exit "+str(p.returncode)
    RESULT["complete"]=not RESULT["failures"]
    save()
    return 0 if RESULT["complete"] else 1


if __name__=="__main__": sys.exit(main())
