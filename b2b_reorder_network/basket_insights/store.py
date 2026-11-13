"""采购篮分析的持久化：幂等重放、版本谱系与既有运行账本关联。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

from b2b_reorder_network.network.order_store import stable_json, utc_now

ANALYSIS_RUN_KEY_PREFIX = "basket-insights"


class AnalysisStore:
    def __init__(self, database: str | Path):
        self.database = str(database)
        self._initialize()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.database, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS basket_analyses(
                    analysis_id TEXT PRIMARY KEY,
                    input_fingerprint TEXT NOT NULL,
                    source_versions TEXT NOT NULL,
                    engine_version TEXT NOT NULL,
                    baseline_case_id TEXT,
                    baseline_run_id TEXT,
                    supersedes_analysis_id TEXT,
                    result TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS basket_analyses_fingerprint
                    ON basket_analyses(input_fingerprint);
                CREATE TABLE IF NOT EXISTS basket_analysis_rules(
                    analysis_id TEXT NOT NULL,
                    rule_id TEXT NOT NULL,
                    pair TEXT NOT NULL,
                    support_order_ids TEXT NOT NULL,
                    excluded_order_ids TEXT NOT NULL,
                    stockout_order_ids TEXT NOT NULL,
                    validation_support_order_ids TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (analysis_id, rule_id)
                );
                CREATE TABLE IF NOT EXISTS basket_analysis_exclusions(
                    analysis_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    scope TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (analysis_id, seq)
                );
                """
            )
            # 轻量迁移：早期表结构缺少 stockout 列时补齐。
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(basket_analysis_rules)").fetchall()
            }
            if columns and "stockout_order_ids" not in columns:
                connection.execute(
                    "ALTER TABLE basket_analysis_rules ADD COLUMN stockout_order_ids TEXT NOT NULL DEFAULT '[]'"
                )

    def find_by_fingerprint(self, input_fingerprint: str) -> dict[str, Any] | None:
        """同一批明细重放：返回既有分析，绝不形成第二份分析记录。"""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT result FROM basket_analyses WHERE input_fingerprint=? ORDER BY created_at DESC LIMIT 1",
                (input_fingerprint,),
            ).fetchone()
        if row is None:
            return None
        import json

        return json.loads(row["result"])

    def latest_analysis(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT analysis_id, input_fingerprint, result FROM basket_analyses ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        import json

        return {
            "analysis_id": row["analysis_id"],
            "input_fingerprint": row["input_fingerprint"],
            "result": json.loads(row["result"]),
        }

    def find_baseline_run(self, baseline_case_id: str | None) -> str | None:
        """关联同库既有演示运行（runs 表由现有账本维护）。"""
        if not baseline_case_id:
            return None
        with self.connect() as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "runs" not in tables:
                return None
            row = connection.execute(
                "SELECT run_id FROM runs WHERE case_id=? ORDER BY updated_at DESC LIMIT 1",
                (baseline_case_id,),
            ).fetchone()
        return row["run_id"] if row else None

    def save(
        self,
        analysis_id: str,
        input_fingerprint: str,
        source_versions: Mapping[str, Any],
        engine_version: str,
        baseline_case_id: str | None,
        baseline_run_id: str | None,
        supersedes_analysis_id: str | None,
        result: Mapping[str, Any],
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO basket_analyses(
                    analysis_id, input_fingerprint, source_versions, engine_version,
                    baseline_case_id, baseline_run_id, supersedes_analysis_id,
                    result, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(analysis_id) DO NOTHING
                """,
                (
                    analysis_id,
                    input_fingerprint,
                    stable_json(dict(source_versions)),
                    engine_version,
                    baseline_case_id,
                    baseline_run_id,
                    supersedes_analysis_id,
                    stable_json(result),
                    utc_now(),
                ),
            )
            connection.execute(
                "DELETE FROM basket_analysis_rules WHERE analysis_id=?", (analysis_id,)
            )
            connection.execute(
                "DELETE FROM basket_analysis_exclusions WHERE analysis_id=?", (analysis_id,)
            )
            for rule in result.get("rules", []):
                evidence = rule.get("evidence_orders", {})
                connection.execute(
                    """
                    INSERT INTO basket_analysis_rules(
                        analysis_id, rule_id, pair, support_order_ids,
                        excluded_order_ids, stockout_order_ids,
                        validation_support_order_ids, payload
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        analysis_id,
                        rule["rule_id"],
                        stable_json(rule["pair"]),
                        stable_json(evidence.get("support_order_ids", [])),
                        stable_json(evidence.get("excluded_order_ids", [])),
                        stable_json(evidence.get("stockout_opportunity_order_ids", [])),
                        stable_json(evidence.get("validation_support_order_ids", [])),
                        stable_json(rule),
                    ),
                )
            seq = 0
            for scope, records in (
                ("global", result.get("exclusions", {}).get("global", [])),
                ("candidate", result.get("exclusions", {}).get("candidates", [])),
            ):
                for record in records:
                    connection.execute(
                        """
                        INSERT INTO basket_analysis_exclusions(analysis_id, seq, scope, reason, payload)
                        VALUES(?,?,?,?,?)
                        """,
                        (
                            analysis_id,
                            seq,
                            scope,
                            str(record.get("reason", record.get("scope", "unspecified"))),
                            stable_json(record),
                        ),
                    )
                    seq += 1

    def rule_evidence(self, analysis_id: str, rule_id: str) -> dict[str, Any] | None:
        """回查某条规则支持与排除的订单。"""
        import json

        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM basket_analysis_rules WHERE analysis_id=? AND rule_id=?",
                (analysis_id, rule_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "analysis_id": analysis_id,
            "rule_id": rule_id,
            "pair": json.loads(row["pair"]),
            "support_order_ids": json.loads(row["support_order_ids"]),
            "excluded_order_ids": json.loads(row["excluded_order_ids"]),
            "stockout_order_ids": json.loads(row["stockout_order_ids"]),
            "validation_support_order_ids": json.loads(row["validation_support_order_ids"]),
        }
