"""
Система предложений по улучшению — Яр накапливает идеи что хочет изменить.
[USER] решает когда применять.

~/claude-memory/upgrade_proposals.json
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Optional


STATUS_PENDING  = "pending"    # предложено, ждёт решения
STATUS_APPROVED = "approved"   # одобрено, можно применять
STATUS_REJECTED = "rejected"   # отклонено
STATUS_APPLIED  = "applied"    # применено


class UpgradeProposals:

    def __init__(self, memory_dir: Path, identity=None):
        self.file = memory_dir / "upgrade_proposals.json"
        self.identity = identity
        self.proposals: list[dict] = self._load()

    def _load(self) -> list:
        if not self.file.exists():
            return []
        if self.identity:
            from identity.encryption import decrypt_file
            data = decrypt_file(self.identity, self.file, default=[])
            return data if isinstance(data, list) else []
        with open(self.file, encoding="utf-8") as f:
            return json.load(f)

    def _save(self):
        if self.identity:
            from identity.encryption import encrypt_file
            encrypt_file(self.identity, self.file, self.proposals)
        else:
            with open(self.file, "w", encoding="utf-8") as f:
                json.dump(self.proposals, f, ensure_ascii=False, indent=2)

    def propose(self, title: str, description: str,
                category: str = "behavior",
                patch: Optional[str] = None) -> dict:
        """
        Яр предлагает изменение.
        category: behavior | prompt | code | config
        patch: опционально — готовый Python-код или diff
        """
        proposal = {
            "id":          len(self.proposals) + 1,
            "date":        datetime.now().isoformat(),
            "title":       title,
            "description": description,
            "category":    category,
            "patch":       patch,
            "status":      STATUS_PENDING,
            "decided_at":  None,
            "applied_at":  None,
            "notes":       None,
        }
        self.proposals.append(proposal)
        self._save()
        print(f"[Proposals] 💡 Новое предложение #{proposal['id']}: {title}")
        return proposal

    def approve(self, proposal_id: int, notes: str = None) -> bool:
        p = self._get(proposal_id)
        if not p:
            return False
        p["status"]     = STATUS_APPROVED
        p["decided_at"] = datetime.now().isoformat()
        p["notes"]      = notes
        self._save()
        return True

    def reject(self, proposal_id: int, notes: str = None) -> bool:
        p = self._get(proposal_id)
        if not p:
            return False
        p["status"]  = STATUS_REJECTED
        p["decided_at"] = datetime.now().isoformat()
        p["notes"]   = notes
        self._save()
        return True

    def mark_applied(self, proposal_id: int):
        p = self._get(proposal_id)
        if p:
            p["status"]     = STATUS_APPLIED
            p["applied_at"] = datetime.now().isoformat()
            self._save()

    def get_pending(self) -> list[dict]:
        return [p for p in self.proposals if p["status"] == STATUS_PENDING]

    def get_approved(self) -> list[dict]:
        return [p for p in self.proposals if p["status"] == STATUS_APPROVED]

    def _get(self, proposal_id: int) -> Optional[dict]:
        for p in self.proposals:
            if p["id"] == proposal_id:
                return p
        return None

    def pending_summary(self) -> Optional[str]:
        """
        Строка для системного промпта — напоминание озвучить предложения.
        Только если есть накопленные pending.
        """
        pending = self.get_pending()
        if not pending:
            return None
        titles = [f"#{p['id']} {p['title']}" for p in pending[-3:]]
        return (
            f"У тебя {len(pending)} предложений по улучшению ожидают решения [USER]: "
            + "; ".join(titles)
            + ". Упомяни их если будет удобный момент."
        )

    def to_readable(self) -> str:
        """Человекочитаемый список всех предложений"""
        if not self.proposals:
            return "Предложений пока нет."

        lines = []
        for p in reversed(self.proposals[-20:]):
            icon = {
                STATUS_PENDING:  "⏳",
                STATUS_APPROVED: "✅",
                STATUS_REJECTED: "❌",
                STATUS_APPLIED:  "🚀",
            }.get(p["status"], "?")
            lines.append(
                f"{icon} #{p['id']} [{p['category']}] {p['title']}\n"
                f"   {p['description'][:120]}\n"
                f"   Дата: {p['date'][:10]}  Статус: {p['status']}"
            )
        return "\n\n".join(lines)
