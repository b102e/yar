"""
Lifecycle manager для памяти Яра.

Цели:
- безопасная миграция facts schema (без удаления)
- tiering facts: core/active/archived/stale
- релевантный отбор facts для summary
- мягкое архивирование сырых логов (conversations/emotions/research)
"""

from __future__ import annotations

import gzip
import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional


class MemoryLifecycleManager:
    VALID_TIERS = {"core", "active", "archived", "stale"}
    VALID_FACT_STATUS = {"confirmed", "unconfirmed", "contradicted", "archived"}

    def __init__(self, memory_dir: str, memory=None):
        self.memory_dir = Path(memory_dir).expanduser()
        self.memory = memory
        self.archive_dir = self.memory_dir / "archive"
        self.archive_conversations_dir = self.archive_dir / "conversations"
        self.archive_emotions_dir = self.archive_dir / "emotions"
        self.archive_research_dir = self.archive_dir / "research"
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.archive_conversations_dir.mkdir(parents=True, exist_ok=True)
        self.archive_emotions_dir.mkdir(parents=True, exist_ok=True)
        self.archive_research_dir.mkdir(parents=True, exist_ok=True)

    # ── Facts lifecycle ─────────────────────────────────────────────────────

    def migrate_facts_schema(self) -> dict:
        now = datetime.now().isoformat()
        facts = self._facts()
        changed = 0
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            if self._ensure_fact_schema(fact, now):
                changed += 1
        rebalance = self.rebalance_fact_tiers()
        return {
            "facts_total": len([f for f in facts if isinstance(f, dict)]),
            "facts_changed": changed,
            **rebalance,
        }

    def rebalance_fact_tiers(self) -> dict:
        now = datetime.now()
        counts = {"core": 0, "active": 0, "archived": 0, "stale": 0}
        for fact in self._facts():
            if not isinstance(fact, dict):
                continue
            tier = self.classify_fact(fact, now=now)
            fact["tier"] = tier
            if tier == "archived":
                fact["fact_status"] = "archived"
                fact["archival_reason"] = fact.get("archival_reason") or "old_rarely_used"
            elif fact.get("fact_status") == "archived":
                fact["fact_status"] = "confirmed"
                fact["archival_reason"] = None
            counts[tier] += 1
            fact["updated_at"] = now.isoformat()
        return counts

    def classify_fact(self, fact: dict, now: Optional[datetime] = None) -> str:
        now = now or datetime.now()
        rel_id, rel_proj = self._estimate_relevance(fact)
        fact["identity_relevance"] = rel_id
        fact["project_relevance"] = rel_proj

        conf = self._confidence(fact)
        use_count = int(fact.get("use_count", 0) or 0)
        status = str(fact.get("fact_status", "confirmed"))
        last_conf_dt = self._parse_dt(fact.get("last_confirmed_at")) or self._parse_dt(fact.get("last_confirmed"))
        last_used_dt = self._parse_dt(fact.get("last_used_at"))
        created_dt = self._parse_dt(fact.get("created_at")) or now

        age_days = max(0, (now - created_dt).days)
        conf_age_days = 999 if not last_conf_dt else max(0, (now - last_conf_dt).days)
        used_age_days = 999 if not last_used_dt else max(0, (now - last_used_dt).days)

        if status == "contradicted" or (conf < 0.35 and conf_age_days > 45):
            return "stale"
        if conf_age_days > 180 and use_count == 0 and max(rel_id, rel_proj) < 0.5:
            return "stale"
        if (rel_id >= 0.72 or rel_proj >= 0.78) and conf >= 0.6:
            return "core"
        if conf_age_days <= 60 or used_age_days <= 30 or use_count >= 2:
            return "active"
        if age_days > 90 and conf >= 0.35:
            return "archived"
        return "active"

    def update_fact_usage(self, fact: dict) -> None:
        if not isinstance(fact, dict):
            return
        now = datetime.now().isoformat()
        fact["last_used_at"] = now
        fact["updated_at"] = now
        fact["use_count"] = int(fact.get("use_count", 0) or 0) + 1

    def update_fact_confirmation(self, fact: dict, confidence_hint: Optional[float] = None) -> None:
        if not isinstance(fact, dict):
            return
        now = datetime.now().isoformat()
        fact["last_confirmed_at"] = now
        fact["updated_at"] = now
        fact["fact_status"] = "confirmed"
        conf = self._confidence(fact)
        if confidence_hint is None:
            confidence_hint = min(1.0, conf + 0.08)
        try:
            hint = max(0.0, min(1.0, float(confidence_hint)))
        except Exception:
            hint = conf
        fact["confidence"] = round((conf + hint) / 2.0, 3)

    def select_relevant_facts_for_summary(self, limit: int = 15) -> list[dict]:
        facts = [f for f in self._facts() if isinstance(f, dict)]
        if not facts:
            return []
        now = datetime.now()
        for fact in facts:
            self._ensure_fact_schema(fact, now.isoformat())
            fact["tier"] = self.classify_fact(fact, now=now)

        core = [f for f in facts if f.get("tier") == "core"]
        active = [f for f in facts if f.get("tier") == "active"]
        recent = sorted(
            facts,
            key=lambda f: self._sort_ts(
                f.get("updated_at") or f.get("last_confirmed_at") or f.get("date")
            ),
            reverse=True,
        )[:12]

        core = self._sorted_by_score(core)
        active = self._sorted_by_score(active)
        recent = self._sorted_by_score(recent)

        selected = []
        self._append_unique(selected, core[:5], limit)
        self._append_unique(selected, active[:7], limit)
        self._append_unique(selected, recent[:3], limit)

        if len(selected) < limit:
            archived = self._sorted_by_score([f for f in facts if f.get("tier") == "archived"])
            self._append_unique(selected, archived, limit)

        # Использование факта обновляем только когда он реально пошёл в summary.
        for fact in selected:
            self.update_fact_usage(fact)
        return selected[:limit]

    # ── Archive policy ──────────────────────────────────────────────────────

    def archive_old_conversations(self, hot_keep: int = 30) -> dict:
        conv_dir = self.memory_dir / "conversations"
        conv_dir.mkdir(parents=True, exist_ok=True)
        files = [p for p in conv_dir.glob("*.json") if p.is_file()]
        files = [p for p in files if not p.name.startswith("checkpoint")]
        files = sorted(files, key=lambda p: p.name)
        to_archive = files[:-max(1, int(hot_keep))] if len(files) > hot_keep else []
        moved = 0
        for src in to_archive:
            ym = self._month_from_name(src.name)
            dst_dir = self.archive_conversations_dir / ym
            dst_dir.mkdir(parents=True, exist_ok=True)
            gz_path = dst_dir / f"{src.stem}.json.gz"
            try:
                with open(src, "rb") as fr, gzip.open(gz_path, "wb", compresslevel=6) as fw:
                    shutil.copyfileobj(fr, fw)
                src.unlink(missing_ok=True)
                moved += 1
            except Exception:
                continue
        return {"archived_conversations": moved, "hot_files": len(files) - moved}

    def rotate_emotional_journal(self) -> dict:
        src = self.memory_dir / "emotional_journal.jsonl"
        if not src.exists():
            return {"rotated_emotions": 0}
        now_ym = datetime.now().strftime("%Y-%m")
        hot_lines = []
        buckets: dict[str, list[str]] = {}
        with open(src, encoding="utf-8") as f:
            for line in f:
                raw = line.strip()
                if not raw:
                    continue
                item = self._safe_json(raw)
                ts = self._extract_ts(item)
                ym = ts[:7] if ts and len(ts) >= 7 else now_ym
                if ym == now_ym:
                    hot_lines.append(raw)
                else:
                    buckets.setdefault(ym, []).append(raw)
        rotated = 0
        for ym, lines in buckets.items():
            dst = self.archive_emotions_dir / f"emotional_journal_{ym}.jsonl"
            with open(dst, "a", encoding="utf-8") as out:
                for ln in lines:
                    out.write(ln + "\n")
                    rotated += 1
        with open(src, "w", encoding="utf-8") as out:
            for ln in hot_lines:
                out.write(ln + "\n")
        return {"rotated_emotions": rotated, "hot_emotions": len(hot_lines)}

    def archive_research_logs(self, hot_days: int = 30) -> dict:
        src = self.memory_dir / "research" / "log.jsonl"
        if not src.exists():
            return {"archived_research_rows": 0}
        cutoff = datetime.now() - timedelta(days=max(1, int(hot_days)))
        hot = []
        buckets: dict[str, list[str]] = {}
        with open(src, encoding="utf-8") as f:
            for line in f:
                raw = line.strip()
                if not raw:
                    continue
                item = self._safe_json(raw)
                ts = self._extract_ts(item)
                dt = self._parse_dt(ts)
                if dt and dt >= cutoff:
                    hot.append(raw)
                    continue
                ym = ts[:7] if ts and len(ts) >= 7 else datetime.now().strftime("%Y-%m")
                buckets.setdefault(ym, []).append(raw)
        moved = 0
        for ym, lines in buckets.items():
            dst = self.archive_research_dir / f"log_{ym}.jsonl"
            with open(dst, "a", encoding="utf-8") as out:
                for ln in lines:
                    out.write(ln + "\n")
                    moved += 1
        with open(src, "w", encoding="utf-8") as out:
            for ln in hot:
                out.write(ln + "\n")
        return {"archived_research_rows": moved, "hot_research_rows": len(hot)}

    def run_maintenance_cycle(self) -> dict:
        result = {}
        result.update(self.migrate_facts_schema())
        result.update(self.archive_old_conversations(hot_keep=30))
        result.update(self.rotate_emotional_journal())
        result.update(self.archive_research_logs(hot_days=30))
        return result

    # ── Internal helpers ────────────────────────────────────────────────────

    def _facts(self) -> list[dict]:
        if self.memory and isinstance(getattr(self.memory, "long_term", None), dict):
            return self.memory.long_term.setdefault("facts", [])
        mem_file = self.memory_dir / "memory.json"
        if not mem_file.exists():
            return []
        try:
            data = json.loads(mem_file.read_text(encoding="utf-8"))
        except Exception:
            return []
        return data.setdefault("facts", [])

    def _ensure_fact_schema(self, fact: dict, now_iso: str) -> bool:
        changed = False
        text = str(fact.get("fact", "")).strip()
        if text and "text" not in fact:
            fact["text"] = text
            changed = True
        elif not text and fact.get("text"):
            fact["fact"] = str(fact.get("text", "")).strip()
            changed = True

        if fact.get("tier") not in self.VALID_TIERS:
            fact["tier"] = "active"
            changed = True
        if fact.get("fact_status") not in self.VALID_FACT_STATUS:
            fact["fact_status"] = "confirmed"
            changed = True

        created = fact.get("created_at") or fact.get("date") or fact.get("added")
        if not created:
            created = now_iso
            changed = True
        fact["created_at"] = str(created)

        updated = fact.get("updated_at") or fact.get("last_confirmed_at") or fact.get("last_confirmed") or created
        if not updated:
            updated = now_iso
            changed = True
        fact["updated_at"] = str(updated)

        if "last_confirmed_at" not in fact:
            fact["last_confirmed_at"] = fact.get("last_confirmed") or None
            changed = True
        if "last_used_at" not in fact:
            fact["last_used_at"] = None
            changed = True
        if "use_count" not in fact:
            fact["use_count"] = 0
            changed = True
        if "identity_relevance" not in fact:
            fact["identity_relevance"] = 0.0
            changed = True
        if "project_relevance" not in fact:
            fact["project_relevance"] = 0.0
            changed = True
        if "archival_reason" not in fact:
            fact["archival_reason"] = None
            changed = True
        if "confidence" not in fact or fact.get("confidence") is None:
            confirmations = int(fact.get("confirmations", 1) or 1)
            base_conf = min(0.9, 0.45 + confirmations * 0.08)
            fact["confidence"] = round(base_conf, 3)
            changed = True
        return changed

    @staticmethod
    def _month_from_name(name: str) -> str:
        # Ожидаем YYYY-MM-DD_*.json
        if len(name) >= 7 and name[4] == "-" and name[7:8] in {"-", "_"}:
            return name[:7]
        if len(name) >= 7 and name[4] == "-" and name[7] == "-":
            return name[:7]
        return datetime.now().strftime("%Y-%m")

    @staticmethod
    def _safe_json(raw: str) -> dict:
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _parse_dt(value: Any) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except Exception:
            return None

    @staticmethod
    def _extract_ts(item: dict) -> str:
        if not isinstance(item, dict):
            return ""
        for k in ("timestamp", "ts", "at", "date", "read_at", "added", "created_at"):
            v = item.get(k)
            if isinstance(v, str) and v:
                return v
        return ""

    def _estimate_relevance(self, fact: dict) -> tuple[float, float]:
        text = (str(fact.get("fact", "")) + " " + str(fact.get("context", ""))).lower()
        identity_keys = [
            "владимир", "яр", "отношен", "друг", "семь", "сын", "характер",
            "практик", "дзогч", "личн", "идентич",
        ]
        project_keys = [
            "проект", "дрон", "ardupilot", "saas", "internetpercasa", "код",
            "архитект", "сервер", "telegram", "вилла", "oliv", "работ",
        ]
        id_hits = sum(1 for k in identity_keys if k in text)
        pr_hits = sum(1 for k in project_keys if k in text)
        id_rel = max(0.0, min(1.0, 0.2 * id_hits))
        pr_rel = max(0.0, min(1.0, 0.2 * pr_hits))
        return round(id_rel, 3), round(pr_rel, 3)

    @staticmethod
    def _confidence(fact: dict) -> float:
        try:
            return max(0.0, min(1.0, float(fact.get("confidence", 0.5) or 0.5)))
        except Exception:
            return 0.5

    def _fact_score(self, fact: dict) -> float:
        tier = str(fact.get("tier", "active"))
        tier_boost = {"core": 1.0, "active": 0.75, "archived": 0.35, "stale": 0.15}.get(tier, 0.4)
        conf = self._confidence(fact)
        use_count = min(10, int(fact.get("use_count", 0) or 0))
        recency = self._recency_boost(fact)
        id_rel = float(fact.get("identity_relevance", 0.0) or 0.0)
        pr_rel = float(fact.get("project_relevance", 0.0) or 0.0)
        return (
            tier_boost * 0.35
            + conf * 0.25
            + recency * 0.15
            + (use_count / 10.0) * 0.10
            + id_rel * 0.075
            + pr_rel * 0.075
        )

    def _sorted_by_score(self, facts: list[dict]) -> list[dict]:
        return sorted(facts, key=self._fact_score, reverse=True)

    @staticmethod
    def _sort_ts(value: Any) -> datetime:
        if not value:
            return datetime.min
        try:
            return datetime.fromisoformat(str(value))
        except Exception:
            return datetime.min

    def _recency_boost(self, fact: dict) -> float:
        dt = self._parse_dt(fact.get("last_used_at")) or self._parse_dt(fact.get("last_confirmed_at")) or self._parse_dt(fact.get("updated_at"))
        if not dt:
            return 0.0
        days = max(0, (datetime.now() - dt).days)
        if days <= 7:
            return 1.0
        if days <= 30:
            return 0.7
        if days <= 90:
            return 0.4
        return 0.1

    def _append_unique(self, target: list[dict], source: list[dict], limit: int):
        for fact in source:
            if len(target) >= limit:
                return
            text = str(fact.get("fact", "")).strip().lower()
            if not text:
                continue
            if any(str(x.get("fact", "")).strip().lower() == text for x in target):
                continue
            target.append(fact)
