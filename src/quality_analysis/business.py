from __future__ import annotations

import os
import random
import sqlite3
import tempfile
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

from .schemas import DomainError, Scope
from .storage import Store

DATASET_ID = "original-synthetic-quality-v1"


def example_scope() -> Scope:
    return Scope(
        plant_id="PLANT-A",
        product_id="WIDGET-A",
        start_date=date(2025, 1, 1),
        end_date=date(2025, 2, 1),
        metric_id="defect_rate",
    )


def _vacant(path: Path) -> bool:
    return not path.is_symlink() and (
        not path.exists() or (path.is_file() and path.stat().st_size == 0)
    )


def seed_business(store: Store, seed: int = 17) -> None:
    """Explicit local initialization, never an overwrite or a model-callable mutation.

    All records are generated here, not copied from production or another project.
    Suppliers have identical defect rates within each line/day, but different line
    allocations. Thus their marginal association is deliberately confounded.
    """
    if not _vacant(store.business_path):
        raise DomainError("business_not_empty", "Business dataset already exists; not overwritten", 409)
    rng = random.Random(seed)
    descriptor, filename = tempfile.mkstemp(prefix=".business-seed-", suffix=".sqlite", dir=store.root)
    os.close(descriptor)
    pending = Path(filename)
    published = False
    try:
        # Serializes initializers and authorized state revisions. Publish before committing
        # the revision: a crash in between fails closed at the verifier's watermark check.
        with store.transaction() as state:
            if not _vacant(store.business_path):
                raise DomainError("business_not_empty", "Business dataset already exists", 409)
            revision = int(state.execute(
                "SELECT value FROM settings WHERE key='data_revision'"
            ).fetchone()[0]) + 1
            with closing(sqlite3.connect(pending)) as db:
                db.execute("PRAGMA foreign_keys=ON")
                db.executescript("""
                    CREATE TABLE _dataset(
                        dataset_id TEXT NOT NULL, data_revision INTEGER NOT NULL,
                        seed INTEGER NOT NULL, synthetic INTEGER NOT NULL CHECK(synthetic=1));
                    CREATE TABLE material_lots(
                        material_lot TEXT PRIMARY KEY, plant_id TEXT NOT NULL,
                        product_id TEXT NOT NULL, line_id TEXT NOT NULL,
                        supplier_id TEXT NOT NULL, received_day TEXT NOT NULL,
                        material_grade TEXT NOT NULL);
                    CREATE TABLE inspections(
                        event_id TEXT PRIMARY KEY, plant_id TEXT NOT NULL,
                        product_id TEXT NOT NULL, line_id TEXT NOT NULL, day TEXT NOT NULL,
                        material_lot TEXT NOT NULL REFERENCES material_lots(material_lot),
                        supplier_id TEXT NOT NULL, shift TEXT NOT NULL,
                        defect INTEGER NOT NULL CHECK(defect IN (0,1)),
                        rework INTEGER NOT NULL CHECK(rework IN (0,1)),
                        cycle_time REAL NOT NULL CHECK(cycle_time>0));
                    CREATE TABLE work_orders(
                        order_id TEXT PRIMARY KEY, plant_id TEXT NOT NULL,
                        product_id TEXT NOT NULL, line_id TEXT NOT NULL, day TEXT NOT NULL,
                        issue TEXT NOT NULL, resolution TEXT NOT NULL);
                    CREATE INDEX inspections_scope ON inspections(plant_id,product_id,day,line_id);
                    CREATE INDEX orders_scope ON work_orders(plant_id,product_id,day,line_id);
                """)
                db.execute("INSERT INTO _dataset VALUES(?,?,?,1)", (DATASET_ID, revision, seed))
                lots, inspections, orders = [], [], []
                for plant in ("A", "B"):
                    for product in ("A", "B"):
                        for line in (1, 2):
                            for period in ("EARLY", "LATE"):
                                for supplier in ("A", "B"):
                                    lots.append((
                                        f"LOT-{plant}-{product}-{line}-{period}-{supplier}",
                                        f"PLANT-{plant}", f"WIDGET-{product}", f"LINE-{line}",
                                        f"SUPPLIER-{supplier}",
                                        "2024-12-30" if period == "EARLY" else "2025-01-15",
                                        "SYNTHETIC-GRADE-1",
                                    ))
                            for day_number in range(31):
                                day = (date(2025, 1, 1) + timedelta(days=day_number)).isoformat()
                                period = "EARLY" if day_number < 15 else "LATE"
                                elevated = (
                                    plant == "A" and product == "A" and line == 2
                                    and period == "LATE"
                                )
                                rate = 0.05 if line == 1 else (0.30 if elevated else 0.20)
                                for supplier in ("A", "B"):
                                    n = 80 if (supplier == "A") == (line == 1) else 20
                                    flags = [1] * round(n * rate) + [0] * (n - round(n * rate))
                                    rng.shuffle(flags)
                                    for index, defect in enumerate(flags):
                                        shift = "DAY" if index % 2 == 0 else "NIGHT"
                                        cycle = (
                                            (48 if line == 1 else 62) + (4 if elevated else 0)
                                            + (2 if shift == "NIGHT" else 0) + rng.gauss(0, 3)
                                        )
                                        inspections.append((
                                            f"EV-{len(inspections) + 1:07d}", f"PLANT-{plant}",
                                            f"WIDGET-{product}", f"LINE-{line}", day,
                                            f"LOT-{plant}-{product}-{line}-{period}-{supplier}",
                                            f"SUPPLIER-{supplier}", shift, defect,
                                            int(rng.random() < (0.7 if defect else 0.02)),
                                            round(max(cycle, 1.0), 4),
                                        ))
                        for ordinal, line, day, issue, resolution in (
                            (1, 1, "2025-01-09", "Synthetic: scheduled gauge inspection",
                             "Gauge checked; no controlled causal experiment was performed."),
                            (2, 2, "2025-01-18", "Synthetic: investigate line-2 quality variation",
                             "Check settings, material lots and time strata. Supplier association "
                             "alone does not establish a cause."),
                            (3, 2, "2025-01-25", "Synthetic: follow-up sampling requested",
                             "Collect balanced line-by-supplier samples before corrective claims."),
                        ):
                            orders.append((
                                f"WO-{plant}-{product}-{ordinal}", f"PLANT-{plant}",
                                f"WIDGET-{product}", f"LINE-{line}", day, issue, resolution,
                            ))
                db.executemany("INSERT INTO material_lots VALUES(?,?,?,?,?,?,?)", lots)
                db.executemany("INSERT INTO inspections VALUES(?,?,?,?,?,?,?,?,?,?,?)", inspections)
                db.executemany("INSERT INTO work_orders VALUES(?,?,?,?,?,?,?)", orders)
                db.commit()
            if not _vacant(store.business_path):
                raise DomainError("business_not_empty", "Business dataset already exists", 409)
            os.replace(pending, store.business_path)
            published = True
            state.execute("UPDATE settings SET value=? WHERE key='data_revision'", (str(revision),))
            store.audit(state, "local-seed", "business.seed", {
                "dataset_id": DATASET_ID, "seed": seed, "data_revision": revision,
                "synthetic": True, "inspection_count": len(inspections),
            })
    except BaseException:
        if published:
            store.business_path.unlink(missing_ok=True)
        raise
    finally:
        pending.unlink(missing_ok=True)
