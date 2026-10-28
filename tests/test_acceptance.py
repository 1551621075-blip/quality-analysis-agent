"""Acceptance checks must reject success-shaped but incomplete model reports."""
from __future__ import annotations

import copy
import importlib
from pathlib import Path

import pytest

from quality_analysis.business import example_scope, seed_business
from quality_analysis.schemas import ToolRequest
from quality_analysis.service import AnalysisService
from quality_analysis.storage import Store
from quality_analysis.verifier import Verifier


@pytest.fixture
def oracle(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    check = importlib.import_module("real_model_analysis").verify_report
    store = Store(tmp_path)
    seed_business(store)
    actor = store.authenticate(store.provision_user("oracle", "admin", ["PLANT-A"]))
    planned, sections = {}, []
    for grouping in ("line_id", "material_lot"):
        request = ToolRequest(tool="sql", group_by=grouping,
            sql=f"SELECT {grouping},COUNT(*) AS n,SUM(defect) AS defects FROM inspections GROUP BY {grouping}")
        result = Verifier(store).execute(request, example_scope(), actor)
        assert result.status == "ok"
        planned[grouping] = {"group_by": grouping}
        rendered = result.model_dump()
        rendered["payload"] = AnalysisService._report_payload(rendered)
        sections.append({"sq_id": grouping, "outcome": "observed", "evidence": [rendered]})
    return check, {"authorized": list(planned), "report": {"sections": sections}}, planned


def test_untrusted_projection_is_not_rendered_as_verified_numbers(tmp_path):
    store = Store(tmp_path)
    seed_business(store)
    actor = store.authenticate(store.provision_user("projection", "analyst", ["PLANT-A"]))
    tool = ToolRequest(tool="sql", group_by="line_id", sql=(
        "SELECT line_id,COUNT(*) AS n,0 AS defects FROM inspections GROUP BY line_id"))
    raw = Verifier(store).execute(tool, example_scope(), actor).model_dump()
    frozen = copy.deepcopy(raw)
    assert [row["defects"] for row in raw["payload"]["rows"]] == [0, 0]
    report = AnalysisService._report_payload(raw)
    assert set(report) == {"statistics", "provenance", "display_note"}
    assert [g["numerator"] for g in report["statistics"]["groups"]] == [155, 780]
    assert raw == frozen
    report["statistics"]["groups"][0]["numerator"] = -1
    assert raw == frozen


def test_acceptance_oracle_checks_both_real_sql_groupings(oracle):
    check, snapshot, planned = oracle
    check(snapshot, planned)


@pytest.mark.parametrize("mutation", ["metadata_only", "wrong_group", "partial_cohort", "wrong_count",
                                     "missing_section", "unauthorized_section", "unresolved", "raw_projection"])
def test_acceptance_oracle_rejects_incomplete_or_wrong_observation(oracle, mutation):
    check, original, planned = oracle
    snapshot = copy.deepcopy(original)
    section = snapshot["report"]["sections"][0]
    statistics = section["evidence"][0]["payload"]["statistics"]
    if mutation == "metadata_only":
        section["evidence"][0]["source"] = "metadata"
    elif mutation == "wrong_group":
        statistics["group_by"] = "shift"
    elif mutation == "partial_cohort":
        statistics["population_n"] = 3100
    elif mutation == "wrong_count":
        statistics["groups"][0]["numerator"] += 1
    elif mutation == "missing_section":
        snapshot["report"]["sections"].pop()
    elif mutation == "unauthorized_section":
        snapshot["authorized"].pop()
    elif mutation == "raw_projection":
        section["evidence"][0]["payload"]["rows"] = [{"defects": 0}]
    else:
        section["outcome"] = "unresolved"
    with pytest.raises(AssertionError):
        check(snapshot, planned)
