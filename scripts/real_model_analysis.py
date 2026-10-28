"""Real BGE calibration and Qwen analysis on newly seeded synthetic data only."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from quality_analysis.business import example_scope, seed_business
from quality_analysis.documents import DocumentStore, original_reports
from quality_analysis.managed_provider import ManagedLocalProvider
from quality_analysis.retrieval_calibration import calibrate, original_calibration_cases
from quality_analysis.schemas import AnalyzeRequest, Confirmation, FollowupRequest
from quality_analysis.service import AnalysisService
from quality_analysis.storage import Store
from quality_analysis.verifier import Verifier


def verify_report(snapshot: dict, planned: dict) -> None:
    """Fixed original-fixture oracle, independent of the model's SQL projection."""
    expected = {
        "line_id": {"LINE-1": (3100, 155), "LINE-2": (3100, 780)},
        "material_lot": {
            "LOT-A-A-1-EARLY-A": (1200, 60), "LOT-A-A-1-EARLY-B": (300, 15),
            "LOT-A-A-1-LATE-A": (1280, 64), "LOT-A-A-1-LATE-B": (320, 16),
            "LOT-A-A-2-EARLY-A": (300, 60), "LOT-A-A-2-EARLY-B": (1200, 240),
            "LOT-A-A-2-LATE-A": (320, 96), "LOT-A-A-2-LATE-B": (1280, 384),
        },
    }
    sections = snapshot["report"]["sections"]
    assert sections and {s["sq_id"] for s in sections} == set(snapshot["authorized"])
    for section in sections:
        assert section["outcome"] == "observed" and section["evidence"]
        sql_evidence = [e for e in section["evidence"] if e["source"] == "sql"]
        assert sql_evidence, "A comparison needs actual SQL evidence, not metadata alone"
        group_by = planned[section["sq_id"]]["group_by"]
        for evidence in sql_evidence:
            assert not {"rows", "columns", "row_count", "executed_sql", "parameters"} & evidence["payload"].keys(), (
                "Raw model SQL projection must not appear as verified report numbers")
            statistics = evidence["payload"]["statistics"]
            assert statistics["metric_id"] == "defect_rate"
            assert statistics["group_by"] == group_by and statistics["population_n"] == 6200
            groups = statistics["groups"]
            assert {g["group"]: (g["n"], g["numerator"]) for g in groups} == expected[group_by]
            assert all(g["denominator"] == g["n"] and g["adequate"]
                       and abs(g["estimate"] - g["numerator"] / g["n"]) < 1e-12
                       and g["ci95"][0] <= g["estimate"] <= g["ci95"][1]
                       and g["significance"] is None for g in groups)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    root = Path(args.root)
    if root.exists():
        raise RuntimeError("Acceptance requires a new directory; existing data is never overwritten")
    root.mkdir(parents=True)
    result = {"dataset": "original-synthetic-quality-v1", "status": "failed",
              "production_validated": False, "steps": []}
    started = time.monotonic()
    try:
        provider = ManagedLocalProvider(args.config)
        result["provider"] = provider.fingerprint
        store = Store(root / "data")
        seed_business(store)
        scope = example_scope()
        actor = store.authenticate(store.provision_user("acceptance-admin", "admin", [scope.plant_id]))
        documents = DocumentStore(store, provider)
        for report in original_reports(scope.plant_id):
            documents.ingest(report, actor)
        result["steps"].append({"name": "ingest", "documents": 8, "passed": True})
        print("Eight original reports embedded using actual BGE-M3", flush=True)
        calibration = calibrate(documents, original_calibration_cases(scope.plant_id, language="zh"),
                                actor, evaluator_kind="bge-local", dataset_kind="original-synthetic")
        result["calibration"] = calibration.model_dump(mode="json")
        documents.install_calibration(calibration, actor)
        print(json.dumps({"fit_metrics": calibration.fit_metrics.model_dump(),
                          "heldout_metrics": calibration.eval_metrics.model_dump()}, ensure_ascii=False), flush=True)
        service = AnalysisService(store, provider, documents, Verifier(store, documents))
        prepared = service.analyze(AnalyzeRequest(request_id="local-core-v1", scope=scope,
            question="请比较2025年1月授权产品各条产线的缺陷率，先做总体对比，再列出材料批次分层的后续分析。不要认定因果。"), actor)
        result["prepared"] = prepared
        assert prepared["status"] == "awaiting_confirmation", prepared.get("last_error")
        assert prepared["tools"] == 1, "Only cohort metadata may run before confirmation"
        result["steps"].append({"name": "prepare", "passed": True})
        core = [sq["sq_id"] for sq in prepared["plan"]["subquestions"] if sq["tier"] == "core"]
        extended = [sq["sq_id"] for sq in prepared["plan"]["subquestions"] if sq["tier"] == "extended"]
        planned = {sq["sq_id"]: sq for sq in prepared["plan"]["subquestions"]}
        assert all(planned[sq]["group_by"] == "line_id" for sq in core), "Core requested line comparison"
        assert extended, "Requested followup plan was not produced"
        assert planned[extended[0]]["group_by"] == "material_lot", "Followup requested material grouping"
        completed = service.confirm(prepared["session_id"], Confirmation(action_id="confirm-v1",
            plan_fingerprint=prepared["plan_fingerprint"], selected_sq_ids=core), actor)
        result["core"] = completed
        assert completed["status"] == "completed", completed.get("last_error")
        verify_report(completed, planned)
        result["steps"].append({"name": "core_analysis", "passed": True})
        print("Real model core analysis completed with program-verified evidence", flush=True)
        assert set(completed["authorized"]) == set(core), "No extended SQ authorized in the core round"
        old_ids = {e["evidence_id"] for s in completed["report"]["sections"] for e in s["evidence"]}
        followup = service.followup(prepared["session_id"], FollowupRequest(request_id="follow-v1",
            question="现在授权执行所选后续分析及必要依赖，仍然只描述观察与关联。", selected_sq_ids=extended[:1]), actor)
        result["followup"] = followup
        assert followup["status"] == "completed", followup.get("last_error")
        assert extended[0] in followup["authorized"]
        verify_report(followup, planned)
        new_ids = {e["evidence_id"] for s in followup["report"]["sections"] for e in s["evidence"]}
        assert new_ids and not new_ids & old_ids
        result["steps"].append({"name": "authorized_followup", "passed": True})
        result["status"] = "passed"
    except Exception as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)
        print(result["error"], flush=True)
    finally:
        result["elapsed_s"] = round(time.monotonic() - started, 3)
        with (root / "acceptance.json").open("x") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"status": result["status"], "steps": result["steps"],
                      "elapsed_s": result["elapsed_s"]}, ensure_ascii=False), flush=True)
    return int(result["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
