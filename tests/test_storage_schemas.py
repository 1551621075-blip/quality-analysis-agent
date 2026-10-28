import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from quality_analysis.schemas import Actor, DomainError, Plan, Scope, canonical
from quality_analysis.storage import Store


def test_user_auth_acl_and_no_token_in_database(tmp_path):
    store = Store(tmp_path)
    token = store.provision_user("reader", "operator", ["plant-a"])
    actor = store.authenticate(token)
    assert actor.role == "operator"
    assert token.encode() not in store.path.read_bytes()
    with pytest.raises(DomainError, match="身份令牌"):
        store.authenticate("wrong")
    with pytest.raises(DomainError, match="已存在"):
        store.provision_user("reader", "admin", ["plant-b"])
    with store.transaction() as db:
        db.execute("UPDATE users SET plants=?,acl_version=acl_version+1 WHERE actor_id='reader'",
                   (canonical(["plant-b"]),))
    with pytest.raises(DomainError, match="权限已变化"):
        store.current_actor(actor)
    assert store.authenticate(token).plants == ["plant-b"]


def test_transactions_close_and_rollback(tmp_path):
    store = Store(tmp_path)
    with pytest.raises(RuntimeError), store.transaction() as db:
        db.execute("INSERT INTO settings VALUES('test','1')")
        raise RuntimeError("rollback")
    with store.connect() as db:
        assert not db.execute("SELECT 1 FROM settings WHERE key='test'").fetchone()
    with pytest.raises(Exception, match="closed"):
        db.execute("SELECT 1")


def test_content_artifacts_hash_dedup_and_corruption(tmp_path):
    store = Store(tmp_path)
    payload = {"数字": [1, 2], "message": "合成"}
    with ThreadPoolExecutor(max_workers=4) as pool:
        refs = list(pool.map(lambda _: store.write_artifact("example", payload), range(8)))
    assert len({r.sha256 for r in refs}) == 1
    assert store.read_artifact(refs[0]) == payload
    assert refs[0].sha256 == hashlib.sha256(canonical(payload).encode()).hexdigest()
    path = store.artifacts / (refs[0].sha256 + ".json")
    path.write_text(json.dumps({"changed": True}))
    with pytest.raises(DomainError, match="哈希"):
        store.read_artifact(refs[0])
    with pytest.raises(DomainError, match="完整性"):
        store.write_artifact("example", payload)


def test_artifact_symlink_and_nonfinite_rejected(tmp_path):
    store = Store(tmp_path / "state")
    ref = store.write_artifact("test", {"x": 1})
    path = store.artifacts / (ref.sha256 + ".json")
    destination = tmp_path / "outside"
    destination.write_text(path.read_text())
    path.unlink()
    path.symlink_to(destination)
    with pytest.raises(DomainError):
        store.read_artifact(ref)
    with pytest.raises(DomainError):
        store.write_artifact("test", {"x": 1})
    with pytest.raises(ValueError):
        store.write_artifact("bad", {"x": float("nan")})


def sq(key, tier="core", deps=None):
    return {"sq_id": key, "title": "合成子问题", "tier": tier,
            "depends_on": deps or [], "sources": ["sql"]}


@pytest.mark.parametrize("items", [
    [sq("a"), sq("a")], [sq("a", deps=["absent"])],
    [sq("a", deps=["b"]), sq("b", deps=["a"])],
    [sq("a", deps=["b"]), sq("b", "extended")], [sq("a", "extended")],
])
def test_invalid_plan_graph(items):
    with pytest.raises(ValueError):
        Plan(subquestions=items)


def test_dependency_closure_order():
    plan = Plan(subquestions=[sq("a"), sq("b", "extended", ["a"]),
                              sq("c", "extended", ["a", "b"])])
    assert plan.closure(["c"]) == ["a", "b", "c"]
    for invalid in (["no"], [], ["a", "a"]):
        with pytest.raises(DomainError):
            plan.closure(invalid)


@pytest.mark.parametrize("overrides", [
    {"end_date": "2025-01-01"}, {"end_date": "2024-12-31"},
    {"end_date": "2027-01-01"}, {"line_ids": ["a", "a"]},
    {"metric_id": "secret"}, {"plant_id": "../escape"},
])
def test_scope_requires_explicit_valid_period(overrides):
    values = {"plant_id": "plant-a", "product_id": "product-a",
              "start_date": "2025-01-01", "end_date": "2025-02-01"} | overrides
    with pytest.raises(ValueError):
        Scope(**values)


def test_actor_never_accepts_unknown_role():
    with pytest.raises(ValueError):
        Actor(actor_id="test", role="root", plants=["plant-a"], acl_version=1)
