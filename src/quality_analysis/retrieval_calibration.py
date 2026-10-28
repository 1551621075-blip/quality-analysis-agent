"""Measured calibration for the local retriever, with untouched held-out queries.

The included reports and labels are ORIGINAL SYNTHETIC fixtures. Results from a
lexical test double are never BGE/model evaluations or production thresholds.
Callers must run this module with their actual injected local provider to obtain
measurements; no measured outputs are checked into this source file.
"""
from __future__ import annotations

import math
from typing import Literal

from pydantic import Field, model_validator

from .documents import MAX_TOP_K, DocumentStore, _fingerprint
from .schemas import Actor, DomainError, Identifier, Scope, StrictModel, digest
from .storage import now

EvaluatorKind = Literal["synthetic-lexical", "local-model", "bge-local"]
MAX_CALIBRATION_CASES = 100


class CalibrationCase(StrictModel):
    case_id: Identifier
    split: Literal["fit", "eval"]
    query: str = Field(min_length=2, max_length=2000)
    scope: Scope
    relevant_document_ids: list[Identifier] = Field(max_length=20)


class CalibrationMetrics(StrictModel):
    precision: float = Field(ge=0, le=1)
    recall: float = Field(ge=0, le=1)
    f1: float = Field(ge=0, le=1)
    negative_rejection_rate: float = Field(ge=0, le=1)
    true_positives: int = Field(ge=0)
    false_positives: int = Field(ge=0)
    false_negatives: int = Field(ge=0)
    case_count: int = Field(ge=1)
    negative_case_count: int = Field(ge=1)


class CalibrationResult(StrictModel):
    calibration_version: str
    provider_fingerprint: str = Field(min_length=1, max_length=512)
    evaluator_kind: EvaluatorKind
    dataset_kind: Literal["original-synthetic", "caller-labeled"]
    production_validated: Literal[False] = False
    index_revision: int = Field(ge=0)
    actor_id: Identifier
    acl_version: int = Field(ge=1)
    plant_ids: list[Identifier] = Field(min_length=1, max_length=30)
    threshold: float
    top_k: int = Field(ge=1, le=MAX_TOP_K)
    candidate_top_ks: list[int] = Field(min_length=1, max_length=MAX_TOP_K)
    selection_objective: Literal["fit_micro_f1_then_precision_then_rejection_then_smaller_k"]
    fit_case_ids: list[Identifier] = Field(min_length=2, max_length=MAX_CALIBRATION_CASES)
    eval_case_ids: list[Identifier] = Field(min_length=2, max_length=MAX_CALIBRATION_CASES)
    fit_dataset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    eval_dataset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    fit_metrics: CalibrationMetrics
    eval_metrics: CalibrationMetrics
    created_at: str

    @model_validator(mode="after")
    def integrity(self):
        if (set(self.fit_case_ids) & set(self.eval_case_ids)
                or len(set(self.fit_case_ids)) != len(self.fit_case_ids)
                or len(set(self.eval_case_ids)) != len(self.eval_case_ids)
                or self.fit_metrics.case_count != len(self.fit_case_ids)
                or self.eval_metrics.case_count != len(self.eval_case_ids)
                or self.top_k not in self.candidate_top_ks
                or any(not 1 <= k <= MAX_TOP_K for k in self.candidate_top_ks)):
            raise ValueError("Calibration sets, counts, or candidate top-k values disagree")
        payload = self.model_dump(mode="json", exclude={"calibration_version"})
        if self.calibration_version != "cal-" + digest(payload)[:32]:
            raise ValueError("Calibration metadata hash mismatch")
        _check_evaluator(self.provider_fingerprint, self.evaluator_kind)
        return self


def _check_evaluator(fingerprint: str, evaluator_kind: EvaluatorKind):
    fp = fingerprint.lower()
    synthetic = "synthetic" in fp or "lexical" in fp
    if evaluator_kind == "synthetic-lexical":
        if not synthetic:
            raise ValueError("Synthetic evaluator requires an explicitly synthetic provider fingerprint")
    elif synthetic:
        raise ValueError("Synthetic/lexical provider cannot be reported as a local model or BGE")
    elif evaluator_kind == "bge-local" and "bge" not in fp:
        raise ValueError("BGE evaluation requires an explicitly BGE provider fingerprint")


def _metrics(cases: list[CalibrationCase], rankings, threshold: float, top_k: int):
    tp = fp = fn = rejected = negatives = 0
    for case in cases:
        # Reproduce search exactly: threshold first, then bounded chunk top-k,
        # then count document labels without double-crediting overlapping chunks.
        accepted = [hit for hit in rankings[case.case_id] if hit.score >= threshold][:top_k]
        predicted = {hit.document_id for hit in accepted}
        relevant = set(case.relevant_document_ids)
        tp += len(predicted & relevant)
        fp += len(predicted - relevant)
        fn += len(relevant - predicted)
        if not relevant:
            negatives += 1
            rejected += not predicted
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return CalibrationMetrics(
        precision=precision, recall=recall,
        f1=2 * precision * recall / (precision + recall) if precision + recall else 0.0,
        negative_rejection_rate=rejected / negatives,
        true_positives=tp, false_positives=fp, false_negatives=fn,
        case_count=len(cases), negative_case_count=negatives,
    )


def calibrate(
    documents: DocumentStore,
    cases: list[CalibrationCase],
    actor: Actor,
    *,
    evaluator_kind: EvaluatorKind,
    dataset_kind: Literal["original-synthetic", "caller-labeled"] = "caller-labeled",
    top_ks: tuple[int, ...] = (1, 2, 4, 8),
) -> CalibrationResult:
    """Fit on fit-only labels, evaluate once on disjoint held-out labels.

    Each split must contain positive and no-answer cases. Query texts, case IDs,
    and positively labeled document IDs must not overlap across splits. Metadata
    binds results to the actual provider, actor/ACL, and index snapshot. This does
    not install a threshold; use ``documents.install_calibration(result, actor)``
    explicitly, and do not treat synthetic-corpus metrics as production evidence.
    """
    if not isinstance(cases, list) or not 4 <= len(cases) <= MAX_CALIBRATION_CASES:
        raise DomainError("invalid_calibration", "Calibration requires 4 to 100 labeled cases")
    cases = [CalibrationCase.model_validate(
        case.model_dump() if isinstance(case, CalibrationCase) else case
    ) for case in cases]
    if evaluator_kind not in {"synthetic-lexical", "local-model", "bge-local"}:
        raise DomainError("invalid_evaluator", "An explicit supported evaluator kind is required")
    if dataset_kind not in {"original-synthetic", "caller-labeled"}:
        raise DomainError("invalid_calibration", "Dataset provenance must be explicit")
    if (not top_ks or len(top_ks) > MAX_TOP_K or any(
        isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= MAX_TOP_K for k in top_ks
    )):
        raise DomainError("invalid_calibration", "Candidate top-k values are out of bounds")
    top_ks = tuple(sorted(set(top_ks)))
    fingerprint = _fingerprint(documents.provider)
    try:
        _check_evaluator(fingerprint, evaluator_kind)
    except ValueError as exc:
        raise DomainError("invalid_evaluator", str(exc)) from exc
    fit = [case for case in cases if case.split == "fit"]
    evaluation = [case for case in cases if case.split == "eval"]
    for split in (fit, evaluation):
        if not any(case.relevant_document_ids for case in split) or not any(
            not case.relevant_document_ids for case in split
        ):
            raise DomainError("invalid_calibration", "Each split needs positive and no-answer cases")
    if (len({case.case_id for case in cases}) != len(cases)
            or len({" ".join(case.query.casefold().split()) for case in cases}) != len(cases)
            or any(len(set(c.relevant_document_ids)) != len(c.relevant_document_ids) for c in cases)):
        raise DomainError("calibration_leakage", "Case IDs, normalized queries, and labels must be unique")
    fit_docs = {doc for case in fit for doc in case.relevant_document_ids}
    eval_docs = {doc for case in evaluation for doc in case.relevant_document_ids}
    if fit_docs & eval_docs:
        raise DomainError("calibration_leakage", "Fit and evaluation document labels must be disjoint")
    # The built-in provenance label is reserved for the exact original fixture;
    # callers with other labels must explicitly describe them as caller-labeled.
    if dataset_kind == "original-synthetic":
        by_plant = {}
        for case in cases:
            by_plant.setdefault(case.scope.plant_id, []).append(case)
        for plant, subset in by_plant.items():
            english = original_calibration_cases(plant)
            chinese = original_calibration_cases(plant, language="zh")
            actual = sorted(c.model_dump_json() for c in subset)
            if not any(actual == sorted(c.model_dump_json() for c in expected)
                       for expected in (english, chinese, english + chinese)):
                raise DomainError("invalid_calibration", "Original synthetic query fixtures were modified")
    with documents.store.transaction() as db:
        documents._current(db, actor)
        revision = documents._revision(db)
        if dataset_kind == "original-synthetic":
            from .documents import original_reports

            for plant in by_plant:
                for report in original_reports(plant):
                    head = documents._head(db, report.document_id, actor)
                    source = db.execute("""
                        SELECT * FROM document_versions WHERE document_id=? AND version=?
                    """, (report.document_id, head["active_version"])).fetchone()
                    manifest = {"title": report.title, "plant_id": report.plant_id,
                                "text": report.text, "summaries": report.summaries}
                    if source is None or source["source_manifest_hash"] != digest(manifest):
                        raise DomainError("invalid_calibration", "Original synthetic source was modified")
        for case in cases:
            if case.scope.plant_id not in actor.plants:
                raise DomainError("forbidden", "Calibration scope is not authorized", 403)
            for document_id in case.relevant_document_ids:
                head = documents._head(db, document_id, actor)
                if head["plant_id"] != case.scope.plant_id:
                    raise DomainError("invalid_calibration", "Labeled document is outside query scope")
    rankings = {}
    # Fit retrieval runs first; held-out scores and labels never enter selection.
    for case in fit:
        result = documents._search(case.query, case.scope, actor, calibration_top_k=max(top_ks))
        if result.index_revision != revision:
            raise DomainError("stale_calibration", "Index changed while collecting fit scores", 409)
        rankings[case.case_id] = result.hits
    scores = {hit.score for case in fit for hit in rankings[case.case_id]}
    if not scores:
        raise DomainError("invalid_calibration", "Fit corpus produced no retrieval candidates")
    reject_all = math.nextafter(max(scores), math.inf)
    if math.isfinite(reject_all):
        scores.add(reject_all)
    choices = []
    for threshold in sorted(scores):
        for top_k in top_ks:
            measured = _metrics(fit, rankings, threshold, top_k)
            key = (measured.f1, measured.precision, measured.negative_rejection_rate,
                   -top_k, threshold)
            choices.append((key, threshold, top_k, measured))
    _, threshold, top_k, fit_metrics = max(choices, key=lambda choice: choice[0])
    for case in evaluation:
        result = documents._search(case.query, case.scope, actor, calibration_top_k=max(top_ks))
        if result.index_revision != revision:
            raise DomainError("stale_calibration", "Index changed while collecting held-out scores", 409)
        rankings[case.case_id] = result.hits
    with documents.store.transaction() as db:
        documents._current(db, actor)
        if documents._revision(db) != revision or _fingerprint(documents.provider) != fingerprint:
            raise DomainError("stale_calibration", "Provider or index changed during calibration", 409)
    payload = {
        "provider_fingerprint": fingerprint, "evaluator_kind": evaluator_kind,
        "dataset_kind": dataset_kind, "production_validated": False,
        "index_revision": revision, "actor_id": actor.actor_id, "acl_version": actor.acl_version,
        "plant_ids": sorted({case.scope.plant_id for case in cases}),
        "threshold": threshold, "top_k": top_k, "candidate_top_ks": list(top_ks),
        "selection_objective": "fit_micro_f1_then_precision_then_rejection_then_smaller_k",
        "fit_case_ids": [case.case_id for case in fit],
        "eval_case_ids": [case.case_id for case in evaluation],
        "fit_dataset_hash": digest([case.model_dump(mode="json") for case in fit]),
        "eval_dataset_hash": digest([case.model_dump(mode="json") for case in evaluation]),
        "fit_metrics": fit_metrics.model_dump(mode="json"),
        "eval_metrics": _metrics(evaluation, rankings, threshold, top_k).model_dump(mode="json"),
        "created_at": now(),
    }
    return CalibrationResult(calibration_version="cal-" + digest(payload)[:32], **payload)


def original_calibration_cases(
    plant_id: str = "P1", *, language: Literal["en", "zh"] = "en",
) -> list[CalibrationCase]:
    """Original fit + held-out queries in one language; English remains the API default.

    Chinese counterparts retain the same document split, never move a held-out
    report into fitting, and use language-specific case IDs. Combining full English
    and Chinese sets is allowed but is not independent cross-language evidence.
    """
    if language not in {"en", "zh"}:
        raise DomainError("invalid_calibration", "Fixture language must be en or zh")
    scope = Scope(plant_id=plant_id, product_id="SYNTHETIC", start_date="2025-01-01",
                  end_date="2025-02-01")
    records = [
        ("fit-seal", "fit", "Could leaving valve seals in open trays overnight explain lip ridges?",
         ["SYN-SEAL-01"]),
        ("fit-torque", "fit", "Why does spindle torque indication rise after a cold start while the reference stays stable?",
         ["SYN-TORQUE-02"]),
        ("fit-rinse", "fit", "Which rinse trial links a shorter wash contact time to residual ionic conductivity?",
         ["SYN-RINSE-03"]),
        ("fit-label", "fit", "Find the barcode reading exercise comparing glossy and matte labels on carton corners.",
         ["SYN-LABEL-04"]),
        ("fit-none-1", "fit", "What is the cafeteria's vegetarian lunch menu next Tuesday?", []),
        ("fit-none-2", "fit", "Show measured silicon wafer doping uniformity for ion implantation.", []),
        ("eval-braze", "eval", "Did interrupted shielding gas coincide with radiographic braze voids?",
         ["SYN-BRAZE-05"]),
        ("eval-bearing", "eval", "Does the motor bearing whistle exercise support weighing grease rather than reducing lubrication?",
         ["SYN-BEARING-06"]),
        ("eval-adhesive", "eval", "What happened to panel peel strength when mating exceeded adhesive open time?",
         ["SYN-ADHESIVE-07"]),
        ("eval-probe", "eval", "Could a replaced probe tip explain a pocket depth step without changing the cutting program?",
         ["SYN-PROBE-08"]),
        ("eval-none-1", "eval", "Provide the annual employee holiday travel reimbursement policy.", []),
        ("eval-none-2", "eval", "Which fermentation culture improved pharmaceutical bioreactor cell viability?", []),
    ]
    chinese_queries = [
        "阀门密封圈敞口隔夜存放后唇边起棱，是否有密封托盘对照的记录？",
        "主轴冷启动后扭矩示值逐步升高，但参考工具稳定，应该核查热机零点吗？",
        "哪份漂洗试验比较了接触时间缩短与表面离子残留、电导率的关系？",
        "寻找弯曲纸箱角部亮面和哑光条码标签漏读、扫描反光的对照练习。",
        "下周二食堂提供哪些素食午餐？",
        "请给出离子注入硅晶圆的掺杂均匀性实测报告。",
        "保护气短时中断是否伴随钎焊接头射线检查孔洞增多？",
        "电机轴承啸叫的加脂量试验，为何建议称量润滑脂而不是直接减脂？",
        "涂胶后超过晾置时间才合拢，面板剥离载荷出现了什么变化？",
        "夹具测头换针后槽深示值阶跃，能否先检查偏置而不改切削程序？",
        "今年员工假期旅行费用的报销政策是什么？",
        "哪种发酵培养基提高了制药生物反应器内的细胞存活率？",
    ]
    if language == "zh":
        records = [(key + "-zh", split, query, labels)
                   for (key, split, _, labels), query in zip(records, chinese_queries, strict=True)]
    return [CalibrationCase(case_id=key, split=split, query=query, scope=scope,
                            relevant_document_ids=labels) for key, split, query, labels in records]


def original_heldout_cases(
    plant_id: str = "P1", *, language: Literal["en", "zh"] = "en",
) -> list[CalibrationCase]:
    return [case for case in original_calibration_cases(plant_id, language=language)
            if case.split == "eval"]
