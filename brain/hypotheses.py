"""
Механизм гипотез — Яр делает предположения о [USER]е
и проверяет их через несколько сессий.
"""

import json
import uuid
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path


class HypothesisManager:
    MAX_ACTIVE = 10
    MIN_CHECKS = 3
    CONFIRM_THRESHOLD = 0.75
    REJECT_THRESHOLD = 0.25

    def __init__(self, memory_dir: Path, identity=None):
        self.memory_dir = Path(memory_dir)
        self.path = self.memory_dir / "hypotheses.jsonl"
        self.identity = identity

    def add(self, hypothesis: str, initial_confidence: float = 0.5,
            source: str = "observation") -> str:
        text = str(hypothesis or "").strip()
        if not text:
            return ""

        existing = self.get_active()
        for h in existing:
            if self._similar(str(h.get("hypothesis", "")), text):
                return str(h.get("id", ""))

        if len(existing) >= self.MAX_ACTIVE:
            self._drop_weakest()

        hid = f"hyp_{uuid.uuid4().hex[:8]}"
        try:
            conf = float(initial_confidence)
        except Exception:
            conf = 0.5
        conf = max(0.0, min(1.0, conf))

        entry = {
            "id": hid,
            "hypothesis": text,
            "created": datetime.now().isoformat(),
            "source": source,
            "status": "pending",
            "evidence_for": [],
            "evidence_against": [],
            "confidence": conf,
            "checked_sessions": 0,
            "last_checked": None,
            "resolved_at": None,
            "resolved_reason": None,
        }
        if self.identity:
            from identity.encryption import encrypt_json
            with open(self.path, "ab") as f:
                f.write(encrypt_json(self.identity, entry) + b"\n")
        else:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        print(f"[Hypothesis] 💭 Новая гипотеза: {text[:80]}")
        return hid

    def update(self, hid: str, evidence: str, supports: bool):
        if not hid:
            return
        entries = self._load_all()
        changed = False

        for e in entries:
            if str(e.get("id", "")) != str(hid):
                continue
            if str(e.get("status", "pending")) != "pending":
                break

            ev_text = str(evidence or "").strip()
            if not ev_text:
                break

            bucket = "evidence_for" if supports else "evidence_against"
            e.setdefault(bucket, [])
            e[bucket].append({
                "text": ev_text,
                "at": datetime.now().isoformat(),
            })

            e["checked_sessions"] = int(e.get("checked_sessions", 0)) + 1
            e["last_checked"] = datetime.now().isoformat()

            n_for = len(e.get("evidence_for", []))
            n_against = len(e.get("evidence_against", []))
            n_total = n_for + n_against
            if n_total > 0:
                e["confidence"] = round((n_for + 1) / (n_total + 2), 2)

            if int(e.get("checked_sessions", 0)) >= self.MIN_CHECKS:
                conf = float(e.get("confidence", 0.5))
                if conf >= self.CONFIRM_THRESHOLD:
                    e["status"] = "confirmed"
                    e["resolved_at"] = datetime.now().isoformat()
                    e["resolved_reason"] = f"подтверждено: {n_for}/{n_total} сессий"
                    print(f"[Hypothesis] ✅ Подтверждено: {str(e.get('hypothesis', ''))[:80]}")
                    self._promote_to_fact(e)
                elif conf <= self.REJECT_THRESHOLD:
                    e["status"] = "rejected"
                    e["resolved_at"] = datetime.now().isoformat()
                    e["resolved_reason"] = f"опровергнуто: {n_against}/{n_total} сессий"
                    print(f"[Hypothesis] ❌ Опровергнуто: {str(e.get('hypothesis', ''))[:80]}")
                else:
                    e["status"] = "complex"
                    e["resolved_at"] = datetime.now().isoformat()
                    e["resolved_reason"] = f"смешанный результат: {n_for}/{n_total} сессий"
                    print(f"[Hypothesis] ⚖️ Complex: {str(e.get('hypothesis', ''))[:80]}")

            changed = True
            break

        if changed:
            self._save_all(entries)

    def get_active(self) -> list[dict]:
        return [e for e in self._load_all() if str(e.get("status", "")) == "pending"]

    def get_for_prompt(self, max_items: int = 3) -> str:
        active = self.get_active()
        if not active:
            return ""

        active.sort(key=lambda x: str(x.get("last_checked") or x.get("created") or ""))
        top = active[:max(1, int(max_items))]

        lines = []
        for h in top:
            conf = float(h.get("confidence", 0.5))
            checks = int(h.get("checked_sessions", 0))
            lines.append(
                f"- [{h.get('id','')}] {h.get('hypothesis','')} "
                f"(уверенность: {conf:.0%}, проверок: {checks})"
            )
        return "ГИПОТЕЗЫ ДЛЯ ПРОВЕРКИ:\n" + "\n".join(lines)

    def get_confirmed(self) -> list[dict]:
        return [e for e in self._load_all() if str(e.get("status", "")) == "confirmed"]

    def _promote_to_fact(self, hypothesis: dict):
        promoted_path = self.memory_dir / "hypotheses_promoted.jsonl"
        with open(promoted_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "fact": hypothesis.get("hypothesis", ""),
                "confidence": hypothesis.get("confidence", 0.5),
                "emotional_weight": None,
                "emotional_tags": ["паттерн", "подтверждено"],
                "context": f"подтверждено за {hypothesis.get('checked_sessions', 0)} сессий",
                "source": "hypothesis",
                "promoted_at": datetime.now().isoformat(),
            }, ensure_ascii=False) + "\n")

    def _drop_weakest(self):
        entries = self._load_all()
        active = [e for e in entries if str(e.get("status", "")) == "pending"]
        if not active:
            return
        weakest = min(active, key=lambda x: float(x.get("checked_sessions", 0)) * float(x.get("confidence", 0.5)))
        wid = weakest.get("id")
        for e in entries:
            if e.get("id") == wid:
                e["status"] = "dropped"
                e["resolved_at"] = datetime.now().isoformat()
                e["resolved_reason"] = "вытеснена новой гипотезой"
                break
        self._save_all(entries)

    @staticmethod
    def _similar(a: str, b: str) -> bool:
        return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio() > 0.7

    def _load_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        if self.identity:
            from identity.encryption import decrypt_json
            with open(self.path, "rb") as f:
                for line in f:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        # Migration: legacy plaintext line
                        if raw[:1] == b"{":
                            out.append(json.loads(raw.decode("utf-8")))
                        else:
                            out.append(decrypt_json(self.identity, raw))
                    except Exception:
                        continue
        else:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        out.append(json.loads(raw))
                    except Exception:
                        continue
        return out

    def _save_all(self, entries: list[dict]):
        if self.identity:
            from identity.encryption import encrypt_json
            with open(self.path, "wb") as f:
                for e in entries:
                    f.write(encrypt_json(self.identity, e) + b"\n")
        else:
            with open(self.path, "w", encoding="utf-8") as f:
                for e in entries:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
