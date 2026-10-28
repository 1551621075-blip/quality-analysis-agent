"""Real local model contract checks on fixed original synthetic inputs.

Never falls back to SyntheticProvider. Logs every completed attempt; failures
remain failures. This is neither production acceptance nor a causal benchmark.
"""
from __future__ import annotations

import argparse
import json
import math
import tempfile
import time
from pathlib import Path

from quality_analysis.business import seed_business
from quality_analysis.managed_provider import ManagedLocalProvider
from quality_analysis.schemas import DomainError, Hypotheses, Intent, Plan, Scope
from quality_analysis.service import AnalysisService
from quality_analysis.storage import Store
from quality_analysis.verifier import Verifier


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--checks", nargs="+", choices=["intent", "plan", "hypothesizer", "embedding", "rerank"],
                        default=["intent", "plan", "hypothesizer", "embedding", "rerank"])
    args = parser.parse_args()
    output = Path(args.output)
    if output.exists():
        raise RuntimeError("Refusing to overwrite acceptance results")
    provider = ManagedLocalProvider(args.config)
    scope = Scope(plant_id="PLANT-A", product_id="WIDGET-A", start_date="2025-01-01",
                  end_date="2025-02-01").model_dump(mode="json")
    request = {"question": "比较本月各条产线的缺陷率，不要直接认定根因。", "scope": scope}
    results = {"provider": provider.fingerprint, "dataset": "fixed-original-synthetic-contracts-v1",
               "production_validated": False, "selected_checks": args.checks, "attempts": [], "diagnostics": {}}

    def run(name, function):
        started = time.monotonic()
        record = {"name": name, "passed": False}
        try:
            record["result"] = function()
            record["passed"] = True
        except DomainError as exc:
            record["error"] = exc.code
        except (ValueError, AssertionError, KeyError, TypeError) as exc:
            record["error"] = type(exc).__name__ + ": " + str(exc)
        record["elapsed_s"] = round(time.monotonic() - started, 3)
        results["attempts"].append(record)
        print(json.dumps(record, ensure_ascii=False), flush=True)

    def intent():
        reply = provider.complete("intent", {"question": request["question"]}, Intent)
        assert reply.value.kind == "quality_analysis", reply.value.model_dump()
        return {"output": reply.value.model_dump(), "tokens": reply.tokens}

    def plan():
        reply = provider.complete("plan", {"request": request, "prior": {"hits": [], "low_prior": True},
            "allowed_sources": ["sql", "metadata", "work_orders", "document"],
            "requirement": "制定core产线缺陷率比较及extended材料批次分层计划，先确认，不执行查询。"}, Plan)
        core = [s for s in reply.value.subquestions if s.tier == "core"]
        assert core and all(s.group_by == "line_id" for s in core), reply.value.model_dump()
        assert any(s.tier == "extended" and s.group_by == "material_lot" for s in reply.value.subquestions)
        return {"output": reply.value.model_dump(), "tokens": reply.tokens}

    def hypothesis():
        reply = provider.complete("hypothesizer", {"request": request,
            "sq": {"sq_id": "core-rate", "title": "比较产线缺陷率", "tier": "core",
                   "depends_on": [], "sources": ["sql", "metadata"], "group_by": "line_id"},
            "prior": {"hits": [], "low_prior": True}, "dependencies": [],
            "sql_contract": "inspections字段line_id,defect。只SELECT line_id,COUNT(*) AS n,"
                            "SUM(defect) AS defects FROM inspections GROUP BY line_id；group_by=line_id。"
                            "defect是0/1，所有范围过滤由程序保证。"}, Hypotheses)
        results["diagnostics"]["hypothesizer"] = {"output": reply.value.model_dump(), "tokens": reply.tokens}
        print(json.dumps({"diagnostic_model_reply": "hypothesizer", **results["diagnostics"]["hypothesizer"]},
                         ensure_ascii=False), flush=True)
        tools = [t for h in reply.value.hypotheses for t in h.tools]
        assert all(t.tool in {"sql", "metadata"} for t in tools), reply.value.model_dump()
        assert any(t.tool == "sql" for t in tools), "Comparison requires executed SQL, not metadata alone"
        for tool in tools:
            AnalysisService._guard_sq_grouping({"group_by": "line_id"}, tool, scope["metric_id"])
        with tempfile.TemporaryDirectory(prefix="quality-verifier-smoke-") as temp:
            store = Store(Path(temp))
            seed_business(store)
            actor = store.authenticate(store.provision_user("smoke-analyst", "analyst", ["PLANT-A"]))
            verified = [Verifier(store).execute(tool, Scope.model_validate(scope), actor)
                        for hypothesis in reply.value.hypotheses for tool in hypothesis.tools]
            assert verified and all(item.status == "ok" for item in verified), [v.model_dump() for v in verified]
        return {"output": reply.value.model_dump(), "tokens": reply.tokens,
                "verified_results": [v.model_dump() for v in verified]}

    def embedding():
        values = provider.embed(["产品表面裂纹怎么调查？", "质量报告：产品表面裂纹需检查材料和温度。",
                                 "食堂午餐米饭蔬菜", "产品表面裂纹怎么调查？"])
        assert len(values) == 4 and all(len(v) == 1024 for v in values)
        assert all(math.isfinite(x) for v in values for x in v)
        norms = [math.sqrt(sum(x * x for x in v)) for v in values]
        assert all(abs(n - 1) < 1e-4 for n in norms)
        similarities = [sum(a * b for a, b in zip(values[0], v, strict=True)) for v in values[1:]]
        assert similarities[0] > similarities[1] and similarities[2] > .9999
        return {"dimensions": 1024, "norms": norms, "similarities": similarities}

    def rerank():
        scores = provider.rerank("产品表面裂纹怎么调查？", ["食堂午餐米饭蔬菜",
            "质量报告：产品表面裂纹需检查材料和温度。", "仓库纸箱数量不足"])
        assert len(scores) == 3 and all(math.isfinite(v) for v in scores)
        assert max(range(3), key=scores.__getitem__) == 1, scores
        return {"scores_in_input_order": scores, "scores_are_not_probabilities": True}

    for name, function in [("intent", intent), ("plan", plan), ("hypothesizer", hypothesis),
                           ("embedding", embedding), ("rerank", rerank)]:
        if name in args.checks:
            run(name, function)
    results["passed"] = sum(r["passed"] for r in results["attempts"])
    results["total"] = len(results["attempts"])
    with output.open("x") as stream:
        json.dump(results, stream, ensure_ascii=False, indent=2)
    return int(results["passed"] != results["total"])


if __name__ == "__main__":
    raise SystemExit(main())
