from pathlib import Path
from datetime import datetime
import json


class IdentityStateManager:
    def __init__(self, memory_dir: str, identity=None):
        self.path = Path(memory_dir) / "continuity" / "identity_snapshots.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.identity = identity

    def _read_last_n(self, n: int) -> list[dict]:
        if not self.path.exists():
            return []
        with open(self.path, "rb") as f:
            lines = [l.strip() for l in f if l.strip()]
        result = []
        for raw in lines[-n:]:
            try:
                if self.identity and raw[:1] not in (b"{", b"["):
                    from identity.encryption import decrypt_line
                    result.append(decrypt_line(self.identity, raw))
                else:
                    result.append(json.loads(raw.decode("utf-8")))
            except Exception:
                pass
        return result

    def refresh_identity_snapshot(
        self,
        active_loops: list[dict],
        interests: list[str],
        internal_state: dict,
        relationship_state: dict | None = None,
        source: str = "system",
    ) -> dict:
        top_loop = active_loops[0]["topic"] if active_loops else ""
        current_role = f"партнёр по: {top_loop}" if top_loop else "собеседник и компаньон"

        curiosity = internal_state.get("curiosity", 0.5)
        if curiosity > 0.7:
            interaction_mode = "deep_exploration"
        elif curiosity > 0.4:
            interaction_mode = "collaborative_work"
        else:
            interaction_mode = "light_conversation"

        active_goals = [f"продолжить: {l['topic']}" for l in active_loops[:3]]
        dominant_curiosities = [i for i in interests if i][:3]
        dominant_open_loops = [l["topic"] for l in active_loops[:3]]

        alertness = internal_state.get("alertness", 0.5)
        social = internal_state.get("social", 0.5)
        if alertness > 0.7:
            tone = "focused and engaged"
        elif social > 0.6:
            tone = "warm and conversational"
        else:
            tone = "reflective and calm"

        snapshot = {
            "timestamp": datetime.now().isoformat(),
            "current_role": current_role,
            "interaction_mode": interaction_mode,
            "relationship_state": relationship_state or {
                "user_name": "[USER]",
                "trust_level": 0.85,
                "closeness": 0.9,
                "stability": 0.8
            },
            "active_goals": active_goals,
            "dominant_curiosities": dominant_curiosities,
            "dominant_open_loops": dominant_open_loops,
            "self_tone": tone,
            "source": source
        }

        with open(self.path, "ab") as f:
            if self.identity:
                from identity.encryption import encrypt_line
                f.write(encrypt_line(self.identity, snapshot) + b"\n")
            else:
                f.write(json.dumps(snapshot, ensure_ascii=False).encode() + b"\n")

        return snapshot

    def get_current_identity(self) -> dict | None:
        snaps = self._read_last_n(1)
        return snaps[0] if snaps else None

    def get_identity_delta(self, last_n: int = 5) -> dict:
        snaps = self._read_last_n(last_n)
        if len(snaps) < 2:
            return {"changed": False, "summary": ""}
        first = snaps[0]
        last = snaps[-1]
        changed = (first.get("interaction_mode") != last.get("interaction_mode") or
                   first.get("dominant_open_loops", [])[:1] != last.get("dominant_open_loops", [])[:1])
        summary = ""
        if changed:
            old_loop = first.get("dominant_open_loops", ["?"])[0]
            new_loop = last.get("dominant_open_loops", ["?"])[0]
            if old_loop != new_loop:
                summary = f"Фокус сместился: {old_loop} → {new_loop}"
            else:
                summary = f"Режим: {first.get('interaction_mode')} → {last.get('interaction_mode')}"
        return {"changed": changed, "summary": summary}

    def summarize_identity(self) -> str:
        s = self.get_current_identity()
        if not s:
            return ""
        role = s.get("current_role", "")
        mode = s.get("interaction_mode", "")
        tone = s.get("self_tone", "")
        result = f"Роль: {role}. Режим: {mode}. Тон: {tone}."
        return result[:160]
