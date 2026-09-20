"""Persistent storage service for learned domain unlock rules using SQLite."""
import json
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional

from app.core.logger import logger
from app.models.unlock_rule import (
    AllowedJsPatch,
    AllowedStripHeader,
    DomainUnlockRule,
    DomainUnlockRuleCreate,
    DomainUnlockStatus,
)

# Database location: backend/data/unlock_rules.db
DB_DIR = Path(__file__).resolve().parent.parent.parent / "data"
DB_PATH = DB_DIR / "unlock_rules.db"

DEFAULT_RULE_TTL_SECONDS = 7 * 24 * 3600  # 7 days


class UnlockRulesService:
    """Manages persistent domain unlock rules with automatic expiration and re-learning."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """Initializes SQLite database and tables."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS domain_unlock_rules (
                    domain TEXT PRIMARY KEY,
                    headers_stripped TEXT NOT NULL,
                    js_patches TEXT NOT NULL,
                    status TEXT NOT NULL,
                    success_count INTEGER NOT NULL DEFAULT 1,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_expires_at ON domain_unlock_rules(expires_at)")
            conn.commit()

    def get_all_rules(self) -> List[DomainUnlockRule]:
        """Retrieves all active domain unlock rules."""
        now = time.time()
        rules: List[DomainUnlockRule] = []
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT domain, headers_stripped, js_patches, status, success_count, failure_count, version, updated_at, expires_at FROM domain_unlock_rules")
            rows = cursor.fetchall()
            for row in rows:
                domain, headers_json, patches_json, status, s_count, f_count, version, updated_at, expires_at = row
                
                # Check expiration: mark as stale_relearning if expired
                current_status = DomainUnlockStatus(status)
                if now > expires_at and current_status == DomainUnlockStatus.UNLOCKED:
                    current_status = DomainUnlockStatus.STALE_RELEARNING

                try:
                    headers = [AllowedStripHeader(h) for h in json.loads(headers_json)]
                    patches = [AllowedJsPatch(p) for p in json.loads(patches_json)]
                except (ValueError, json.JSONDecodeError):
                    continue

                rules.append(
                    DomainUnlockRule(
                        domain=domain,
                        headers_stripped=headers,
                        js_patches=patches,
                        status=current_status,
                        success_count=s_count,
                        failure_count=f_count,
                        version=version,
                        updated_at=updated_at,
                        expires_at=expires_at,
                    )
                )
        return rules

    def get_rule(self, domain: str) -> Optional[DomainUnlockRule]:
        """Gets rule for a specific domain."""
        domain_clean = domain.lower().strip()
        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT headers_stripped, js_patches, status, success_count, failure_count, version, updated_at, expires_at FROM domain_unlock_rules WHERE domain = ?",
                (domain_clean,),
            )
            row = cursor.fetchone()
            if not row:
                return None

            headers_json, patches_json, status, s_count, f_count, version, updated_at, expires_at = row
            current_status = DomainUnlockStatus(status)
            if now > expires_at and current_status == DomainUnlockStatus.UNLOCKED:
                current_status = DomainUnlockStatus.STALE_RELEARNING

            headers = [AllowedStripHeader(h) for h in json.loads(headers_json)]
            patches = [AllowedJsPatch(p) for p in json.loads(patches_json)]

            return DomainUnlockRule(
                domain=domain_clean,
                headers_stripped=headers,
                js_patches=patches,
                status=current_status,
                success_count=s_count,
                failure_count=f_count,
                version=version,
                updated_at=updated_at,
                expires_at=expires_at,
            )

    def upsert_rule(self, rule_data: DomainUnlockRuleCreate) -> DomainUnlockRule:
        """Upserts a domain rule. If exists, updates headers, patches, and increments version."""
        domain_clean = rule_data.domain.lower().strip()
        now = time.time()
        expires_at = now + DEFAULT_RULE_TTL_SECONDS

        existing = self.get_rule(domain_clean)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            if existing:
                # Merge headers and patches without duplicates
                merged_headers = sorted(list(set(existing.headers_stripped + rule_data.headers_stripped)))
                merged_patches = sorted(list(set(existing.js_patches + rule_data.js_patches)))
                new_version = existing.version + 1
                new_success = existing.success_count + 1

                headers_json = json.dumps([h.value for h in merged_headers])
                patches_json = json.dumps([p.value for p in merged_patches])

                cursor.execute(
                    """
                    UPDATE domain_unlock_rules
                    SET headers_stripped = ?, js_patches = ?, status = ?, success_count = ?, version = ?, updated_at = ?, expires_at = ?
                    WHERE domain = ?
                    """,
                    (headers_json, patches_json, rule_data.status.value, new_success, new_version, now, expires_at, domain_clean),
                )
                conn.commit()
                logger.info(
                    f"[UnlockRules] Updated rule for '{domain_clean}' (v{new_version}): "
                    f"headers={merged_headers}, patches={merged_patches}, status={rule_data.status.value}"
                )
                return DomainUnlockRule(
                    domain=domain_clean,
                    headers_stripped=merged_headers,
                    js_patches=merged_patches,
                    status=rule_data.status,
                    success_count=new_success,
                    failure_count=existing.failure_count,
                    version=new_version,
                    updated_at=now,
                    expires_at=expires_at,
                )
            else:
                headers_json = json.dumps([h.value for h in rule_data.headers_stripped])
                patches_json = json.dumps([p.value for p in rule_data.js_patches])
                cursor.execute(
                    """
                    INSERT INTO domain_unlock_rules (domain, headers_stripped, js_patches, status, success_count, failure_count, version, updated_at, expires_at)
                    VALUES (?, ?, ?, ?, 1, 0, 1, ?, ?)
                    """,
                    (domain_clean, headers_json, patches_json, rule_data.status.value, now, expires_at),
                )
                conn.commit()
                logger.info(
                    f"[UnlockRules] Created new rule for '{domain_clean}' (v1): "
                    f"headers={rule_data.headers_stripped}, patches={rule_data.js_patches}, status={rule_data.status.value}"
                )
                return DomainUnlockRule(
                    domain=domain_clean,
                    headers_stripped=rule_data.headers_stripped,
                    js_patches=rule_data.js_patches,
                    status=rule_data.status,
                    success_count=1,
                    failure_count=0,
                    version=1,
                    updated_at=now,
                    expires_at=expires_at,
                )

    def record_failure(self, domain: str) -> Optional[DomainUnlockRule]:
        """Flags domain rule as stale/relearning upon observed failure."""
        domain_clean = domain.lower().strip()
        existing = self.get_rule(domain_clean)
        if not existing:
            return None

        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE domain_unlock_rules
                SET status = ?, failure_count = failure_count + 1, updated_at = ?
                WHERE domain = ?
                """,
                (DomainUnlockStatus.STALE_RELEARNING.value, now, domain_clean),
            )
        conn.commit()
        logger.warning(
            f"[UnlockRules] Flagged '{domain_clean}' as {DomainUnlockStatus.STALE_RELEARNING.value} "
            f"(failure_count={existing.failure_count + 1})"
        )
        existing.status = DomainUnlockStatus.STALE_RELEARNING
        existing.failure_count += 1
        existing.updated_at = now
        return existing


# Global service instance
unlock_rules_service = UnlockRulesService()
