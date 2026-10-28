import hashlib
import sqlite3
from contextlib import closing

import pytest

from quality_analysis.business import DATASET_ID, example_scope, seed_business
from quality_analysis.schemas import DomainError
from quality_analysis.storage import Store


def _rows(path, table):
    with closing(sqlite3.connect(path)) as db:
        return db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()


def test_seed_is_original_deterministic_scoped_and_revisioned(tmp_path):
    first, second, changed = (Store(tmp_path / name) for name in ("a", "b", "c"))
    for store in (first, second):
        seed_business(store)
    seed_business(changed, seed=19)
    assert first.revisions() == (1, 0)
    assert _rows(first.business_path, "_dataset") == [(DATASET_ID, 1, 17, 1)]
    for table, count in (("inspections", 24800), ("work_orders", 12), ("material_lots", 32)):
        original = _rows(first.business_path, table)
        assert len(original) == count
        assert original == _rows(second.business_path, table)
    assert _rows(first.business_path, "inspections") != _rows(changed.business_path, "inspections")
    with closing(sqlite3.connect(first.business_path)) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        assert db.execute("SELECT MIN(day),MAX(day) FROM inspections").fetchone() == (
            "2025-01-01", "2025-01-31",
        )
        assert len(db.execute("SELECT DISTINCT plant_id,product_id FROM inspections").fetchall()) == 4
    assert example_scope().model_dump(mode="json") == {
        "plant_id": "PLANT-A", "product_id": "WIDGET-A", "start_date": "2025-01-01",
        "end_date": "2025-02-01", "metric_id": "defect_rate", "line_ids": [], "min_group_n": 20,
    }


def test_seed_refuses_overwrite_and_does_not_increment_revision(tmp_path):
    store = Store(tmp_path)
    seed_business(store)
    before = hashlib.sha256(store.business_path.read_bytes()).hexdigest()
    with pytest.raises(DomainError, match="already exists") as error:
        seed_business(store, seed=88)
    assert error.value.code == "business_not_empty"
    assert before == hashlib.sha256(store.business_path.read_bytes()).hexdigest()
    assert store.revisions() == (1, 0)


def test_seed_refuses_non_sqlite_nonempty_or_symlink(tmp_path):
    store = Store(tmp_path / "state")
    store.business_path.write_bytes(b"not an empty business store")
    with pytest.raises(DomainError):
        seed_business(store)
    assert store.revisions() == (0, 0)
    store.business_path.unlink()
    target = tmp_path / "untouched"
    target.write_bytes(b"")
    store.business_path.symlink_to(target)
    with pytest.raises(DomainError):
        seed_business(store)
    assert target.read_bytes() == b""


def test_supplier_association_is_confounded_by_line_and_day(tmp_path):
    store = Store(tmp_path)
    seed_business(store)
    with closing(sqlite3.connect(store.business_path)) as db:
        where = "WHERE plant_id='PLANT-A' AND product_id='WIDGET-A'"
        marginal = dict(db.execute(
            f"SELECT supplier_id,AVG(defect) FROM inspections {where} GROUP BY supplier_id"
        ))
        assert marginal["SUPPLIER-B"] > marginal["SUPPLIER-A"]
        strata = db.execute(
            "SELECT line_id,day,AVG(CASE WHEN supplier_id='SUPPLIER-A' THEN defect END),"
            "AVG(CASE WHEN supplier_id='SUPPLIER-B' THEN defect END) "
            f"FROM inspections {where} GROUP BY line_id,day"
        ).fetchall()
        assert len(strata) == 62
        assert all(a == b for _, _, a, b in strata)
