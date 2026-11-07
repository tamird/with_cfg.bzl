"""Collect the original native supplier tests for list prepending."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree


WORK = Path(os.environ["RUNNER_TEMP"]) / "prepend"
SRC = WORK / "src"
PRODUCT = "01433cd771f82130524fd1a867fa995004b55ae3"
ORDER = "//prepend:order_test"
EMPTY = "//prepend:empty_test"
DOC = "//docs:update_test"
REPORT = {"source": PRODUCT, "runs": {}, "verified": False}
LOCAL = [
    "--lockfile_mode=off", "--jobs=3", "--loading_phase_threads=3",
    "--remote_executor=", "--remote_cache=", "--bes_backend=", "--spawn_strategy=processwrapper-sandbox",
]


def save():
    (WORK / "result.json").write_text(json.dumps(REPORT, indent=2, sort_keys=True) + "\n")


def diagnostic(text):
    lines = text.decode(errors="replace").splitlines()
    useful = [i for i, line in enumerate(lines) if re.search(r"ERROR|FAIL|^@@|^[-+] |error:", line)]
    indices = sorted({j for i in useful[:8] for j in range(i, min(i + 3, len(lines)))}
                     | set(range(max(0, len(lines) - 3), len(lines))))
    return [re.sub(r"https?://\S+", "<public URL>", lines[i]).replace(str(WORK), "<work>")[:280] for i in indices]


def output(entry):
    uri = urlsplit(entry["uri"])
    assert uri.scheme == "file" and not uri.netloc
    path = Path(unquote(uri.path))
    assert 0 < path.stat().st_size <= 1 << 20
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    assert entry.get("digest", digest) == digest
    assert int(entry.get("length", len(data))) == len(data)
    return data, {"sha256": digest, "bytes": len(data)}


def record(e):
    result = e["testResult"]
    ident = e["id"]["testResult"]
    info = result.get("executionInfo", {})
    row = {"status": result["status"], "strategy": info.get("strategy"),
           "cached": bool(result.get("cachedLocally") or info.get("cachedRemotely")),
           "configuration": ident["configuration"]["id"],
           "run": int(ident.get("run", 1)), "shard": int(ident.get("shard", 1)),
           "attempt": int(ident.get("attempt", 1)), "files": {}}
    for f in result["testActionOutput"]:
        if f["name"] not in ("test.xml", "test.log"):
            continue
        data, ref = output(f)
        assert f["name"] not in row["files"]
        row["files"][f["name"]] = ref
        if f["name"] == "test.xml":
            xml = ElementTree.fromstring(data)
            cases = list(xml.iter("testcase"))
            row["xml_cases"] = len(cases)
            row["xml_clean"] = bool(cases) and all(
                el.tag not in {"failure", "error", "skipped"} and
                el.get("status") not in {"notrun", "skipped"} and el.get("result") not in {"suppressed", "skipped"}
                and all(not int(el.get(key, 0)) for key in ("errors", "failures", "skipped"))
                for el in xml.iter()
            )
        elif result["status"] != "PASSED":
            row["diagnostic"] = diagnostic(data)
    return row


def run(version, name, cwd, targets, extra=()):
    key = f"{version}-{name}"
    bep = WORK / (key + ".bep")
    bazel = [WORK / ("bazel-" + version), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g"]
    row = REPORT["runs"][key] = {"results": {}, "verified": False}
    save()
    use_tests = name != "format"
    cmd = [*bazel, "test" if use_tests else "run", *LOCAL]
    if use_tests:
        cmd += ["--nocache_test_results", "--flaky_test_attempts=1", "--test_sharding_strategy=disabled",
                "--test_timeout=180", "--test_output=errors", "--build_event_json_file=" + str(bep)]
    proc = subprocess.run([*cmd, *extra, *targets], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    row["exit"] = proc.returncode
    if proc.returncode:
        row["diagnostic"] = diagnostic(proc.stdout)
    save()
    if use_tests and bep.exists():
        finished = []
        for line in bep.open():
            e = json.loads(line)
            if "finished" in e:
                finished.append(e["finished"].get("exitCode", {}).get("code", 0))
            if "testResult" in e:
                label = e["id"]["testResult"]["label"]
                assert label in targets and label not in row["results"]
                row["results"][label] = record(e)
                save()
        row["finished"] = finished
        assert finished == [0] and set(row["results"]) == set(targets)
        for result in row["results"].values():
            assert result["status"] == "PASSED" and not result["cached"]
            assert result["strategy"] in {"processwrapper-sandbox", "local", "standalone", "linux-sandbox"}
            assert result["run"] == result["shard"] == result["attempt"] == 1
            assert result["xml_clean"] and set(result["files"]) == {"test.xml", "test.log"}
    elif use_tests:
        raise ValueError("Bazel produced no build event file")
    assert not proc.returncode
    row["verified"] = True
    save()


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SRC, text=True).strip() == PRODUCT
    for version in ("7.7.1", "9.2.0"):
        examples = [ORDER, EMPTY]
        if version == "9.2.0":
            examples.extend(["//builder_reuse:first_cc_test", "//builder_reuse:second_cc_test", "//builder_reuse:third_cc_test"])
        checks = [("examples", SRC / "examples", examples, []),
                  ("incoming", SRC / "examples", [EMPTY], ["--//prepend:options=caller,other"])]
        if version == "9.2.0":
            checks.extend([("docs", SRC, [DOC], []), ("format", SRC, ["//.github/workflows:buildifier.check"], [])])
        for name, directory, targets, flags in checks:
            try:
                run(version, name, directory, targets, flags)
            except Exception as err:
                REPORT["runs"][f"{version}-{name}"]["error"] = repr(err)[:350]
                save()
    REPORT["verified"] = len(REPORT["runs"]) == 6 and all(row["verified"] for row in REPORT["runs"].values())
    save()
    return not REPORT["verified"]


if __name__ == "__main__":
    raise SystemExit(main())
