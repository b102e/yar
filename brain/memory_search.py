"""
Семантический поиск по долгосрочной памяти — ChromaDB + sentence-transformers.

Хранилище:  ~/claude-memory/chroma/
Модель:     paraphrase-multilingual-MiniLM-L12-v2  (~120MB, офлайн, русский)
Типы:       fact | observation | thought | highlight

Устаревшие записи помечаются superseded=True и не удаляются —
история изменений сохраняется, можно спросить «как ты сначала думал X».

Установка:
    pip install chromadb sentence-transformers
"""

import hashlib
import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path


class MemorySearch:
    MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    COLLECTION = "yar_memory"

    # Минимальный порог релевантности (cosine similarity 0..1)
    MIN_RELEVANCE = 0.25
    FALLBACK_MIN_RELEVANCE = 0.05

    TYPE_LABELS = {
        "fact":        "факт",
        "observation": "наблюдение",
        "thought":     "мысль",
        "highlight":   "разговор",
        "research":    "исследование",
        "emotional_journal": "эмоция",
    }

    def __init__(self, memory_dir: Path, identity=None):
        self.memory_dir  = Path(memory_dir)
        self._identity   = identity
        self._client     = None
        self._collection = None
        self._ef         = None
        self._mode       = "off"   # chroma | fallback | off
        self._fallback_file = self.memory_dir / "memory_search_fallback.json"
        self._docs: dict[str, dict] = {}
        self._ready      = False
        self._init()

    # ── Инициализация ────────────────────────────────────────────────────────

    def _init(self):
        # Chroma 1.5.x использует pydantic.v1 и на Python 3.14 часто ломается.
        # Сразу переключаемся на fallback, чтобы не получать шумные ошибки/варнинги.
        if sys.version_info >= (3, 14):
            self._init_fallback()
            return

        try:
            import chromadb
            from chromadb.utils import embedding_functions

            chroma_path = str(self.memory_dir / "chroma")
            self._client = chromadb.PersistentClient(path=chroma_path)
            self._ef = embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name=self.MODEL_NAME,
            )
            self._collection = self._client.get_or_create_collection(
                name=self.COLLECTION,
                embedding_function=self._ef,
                metadata={"hnsw:space": "cosine"},
            )
            self._ready = True

            # Первый запуск — индекс пустой → переиндексируем из существующих данных
            if self._collection.count() == 0:
                self.reindex_all()
            else:
                print(f"🔍 MemorySearch: {self._collection.count()} документов в индексе")
            self._mode = "chroma"

        except ImportError as e:
            print(f"[MemorySearch] ⚠️  Пакет не установлен ({e}). "
                  f"Запусти: pip install chromadb sentence-transformers")
            self._init_fallback()
        except Exception as e:
            # На Python 3.14 chromadb может падать из-за pydantic.v1.
            # Переключаемся на локальный TF-IDF fallback, чтобы RAG не отключался.
            print(f"[MemorySearch] ⚠️  Ошибка инициализации Chroma: {e}")
            self._init_fallback()

    def _init_fallback(self) -> None:
        self._mode = "fallback"
        self._ready = True
        self._docs = self._load_fallback_docs()
        print(f"🔍 MemorySearch: fallback-режим (TF-IDF), документов: {len(self._docs)}")
        if not self._docs:
            self.reindex_all()

    # ── Публичный API ────────────────────────────────────────────────────────

    def add(self, text: str, doc_type: str, date: str,
            doc_id: str = None, metadata_extra: dict = None) -> str:
        """Добавить документ в индекс. Возвращает id."""
        if not self._ready or not text or not text.strip():
            return ""
        if self._mode == "fallback":
            _id = doc_id or self._make_id(text, doc_type, date)
            meta = {
                "type": doc_type,
                "date": date,
                "superseded": False,
            }
            if metadata_extra:
                meta.update(metadata_extra)
            self._docs[_id] = {"id": _id, "text": text.strip(), "meta": meta}
            self._save_fallback_docs()
            return _id
        try:
            _id  = doc_id or self._make_id(text, doc_type, date)
            meta = {
                "type":       doc_type,
                "date":       date,
                "superseded": False,
            }
            if metadata_extra:
                meta.update(metadata_extra)
            self._collection.add(
                ids=[_id],
                documents=[text.strip()],
                metadatas=[meta],
            )
            return _id
        except Exception as e:
            # Дубликат — документ уже в индексе, это нормально при переиндексации
            if "already" in str(e).lower() or "duplicate" in str(e).lower():
                return _id
            print(f"[MemorySearch] add error: {e}")
            return ""

    def update(self, old_id: str, new_text: str, doc_type: str, date: str) -> str:
        """Пометить старый документ устаревшим и добавить новый.
        Старая запись остаётся в индексе с superseded=True — история сохранена."""
        if not self._ready:
            return ""
        if self._mode == "fallback":
            old = self._docs.get(old_id)
            if old:
                old_meta = old.get("meta", {})
                old_meta["superseded"] = True
                old_meta["superseded_at"] = date
                old["meta"] = old_meta
                self._docs[old_id] = old
                self._save_fallback_docs()
            return self.add(new_text, doc_type, date,
                            metadata_extra={"updates": old_id})
        try:
            old = self._collection.get(ids=[old_id], include=["metadatas"])
            if old and old["ids"]:
                old_meta = old["metadatas"][0]
                old_meta["superseded"]    = True
                old_meta["superseded_at"] = date
                self._collection.update(ids=[old_id], metadatas=[old_meta])
        except Exception as e:
            print(f"[MemorySearch] update (mark superseded) error: {e}")

        return self.add(new_text, doc_type, date,
                        metadata_extra={"updates": old_id})

    def query(self, text: str, n: int = 8,
              include_superseded: bool = False) -> str:
        """Семантический поиск. Возвращает готовую строку для системного промпта."""
        if not self._ready or not text or not text.strip():
            return ""
        if self._mode == "fallback":
            return self._query_fallback(text, n=n,
                                        include_superseded=include_superseded)
        try:
            count = self._collection.count()
            if count == 0:
                return ""

            kwargs: dict = {
                "query_texts": [text.strip()],
                "n_results":   min(n, count),
                "include":     ["documents", "metadatas", "distances"],
            }
            if not include_superseded:
                kwargs["where"] = {"superseded": {"$ne": True}}

            results   = self._collection.query(**kwargs)
            docs      = results["documents"][0]
            metas     = results["metadatas"][0]
            distances = results["distances"][0]

            if not docs:
                return ""

            lines = []
            for doc, meta, dist in zip(docs, metas, distances):
                # cosine distance 0=идентично → relevance = 1 - dist
                relevance = 1.0 - dist
                if relevance < self.MIN_RELEVANCE:
                    continue
                label    = self.TYPE_LABELS.get(meta.get("type", ""), meta.get("type", ""))
                date_str = meta.get("date", "")[:10]
                lines.append(f"[{label} {date_str}] {doc}")

            return "\n".join(lines) if lines else ""

        except Exception as e:
            print(f"[MemorySearch] query error: {e}")
            return ""

    def reindex_all(self):
        """Перестроить индекс с нуля из memory.json + файлов thoughts/ observations/."""
        if not self._ready:
            return

        print("🔄 Переиндексирую долгосрочную память...")
        count_before = self._count()
        if self._mode == "fallback":
            self._docs = {}
            self._save_fallback_docs()

        memory_json = self.memory_dir / "memory.json"
        if memory_json.exists():
            if self._identity is not None:
                try:
                    from identity.encryption import decrypt_file
                    data = decrypt_file(self._identity, memory_json)
                except Exception:
                    data = {}
            else:
                try:
                    with open(memory_json, encoding="utf-8") as f:
                        data = json.load(f)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    data = {}

            # Факты о владельце
            for entry in data.get("facts", []):
                fact = entry.get("fact", "")
                date = entry.get("date", datetime.now().isoformat())[:10]
                if fact:
                    self.add(fact, "fact", date)

            # Highlights из сессий (первые слова каждого разговора)
            for session in data.get("sessions", []):
                date = session.get("date", "")[:10]
                for h in session.get("highlights", []):
                    if h:
                        self.add(h, "highlight", date)

        # Мысли из thoughts/YYYY-MM-DD.txt
        thoughts_dir = self.memory_dir / "thoughts"
        if thoughts_dir.exists():
            for tf in sorted(thoughts_dir.glob("*.txt")):
                date = tf.stem
                for line in tf.read_text(encoding="utf-8").split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    # Формат строки: [HH:MM] текст мысли
                    if line.startswith("["):
                        parts = line.split("] ", 1)
                        if len(parts) == 2 and parts[1].strip():
                            self.add(parts[1].strip(), "thought", date)

        # Наблюдения через камеру из observations/YYYY-MM-DD.txt
        obs_dir = self.memory_dir / "observations"
        if obs_dir.exists():
            for of in sorted(obs_dir.glob("*.txt")):
                date = of.stem
                for line in of.read_text(encoding="utf-8").split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("["):
                        parts = line.split("] ", 1)
                        if len(parts) == 2 and parts[1].strip():
                            self.add(parts[1].strip(), "observation", date)

        # Эмоциональный журнал Яра.
        ej_file = self.memory_dir / "emotional_journal.jsonl"
        if ej_file.exists():
            for line in ej_file.read_text(encoding="utf-8").splitlines():
                raw = line.strip()
                if not raw:
                    continue
                try:
                    item = json.loads(raw)
                except Exception:
                    continue
                emotion = str(item.get("emotion", "")).strip()
                note = str(item.get("note", "")).strip()
                trigger = str(item.get("trigger", "")).strip()
                intensity = item.get("intensity", 0.0)
                date = str(item.get("session_ts", "") or item.get("at", ""))[:10]
                if not date:
                    date = datetime.now().strftime("%Y-%m-%d")
                text = (
                    f"Яр чувствовал [{emotion}] (intensity=[{intensity}]): "
                    f"{note}. Триггер: {trigger}"
                )
                self.add(text, "emotional_journal", date)

        added = self._count() - count_before
        print(f"✅ MemorySearch: проиндексировано +{added} документов "
              f"(всего {self._count()})")

    def index_session(self, session_path: Path):
        """Индексировать одну конкретную сохранённую сессию conversations/*.json."""
        if not self._ready:
            return
        path = Path(session_path)
        if not path.exists():
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return

        date = str(data.get("date", ""))[:10] or datetime.now().strftime("%Y-%m-%d")
        messages = data.get("messages", [])
        if not isinstance(messages, list):
            return

        for m in messages:
            if not isinstance(m, dict):
                continue
            if str(m.get("role", "")) != "user":
                continue
            text = str(m.get("content", "")).strip()
            if text:
                self.add(text[:300], "highlight", date)

    # ── Вспомогательное ──────────────────────────────────────────────────────

    def _count(self) -> int:
        if self._mode == "fallback":
            return len(self._docs)
        if self._collection is None:
            return 0
        return self._collection.count()

    def _load_fallback_docs(self) -> dict:
        if self._fallback_file.exists():
            try:
                with open(self._fallback_file, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return {d["id"]: d for d in data if isinstance(d, dict) and d.get("id")}
            except Exception as e:
                print(f"[MemorySearch] fallback load error: {e}")
        return {}

    def _save_fallback_docs(self) -> None:
        try:
            with open(self._fallback_file, "w", encoding="utf-8") as f:
                json.dump(list(self._docs.values()), f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[MemorySearch] fallback save error: {e}")

    def _query_fallback(self, text: str, n: int, include_superseded: bool) -> str:
        docs = []
        for d in self._docs.values():
            meta = d.get("meta", {})
            if not include_superseded and meta.get("superseded") is True:
                continue
            if d.get("text"):
                docs.append(d)
        if not docs:
            return ""

        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
            from sklearn.metrics.pairwise import cosine_similarity

            corpus = [text.strip()] + [d["text"] for d in docs]
            vect = TfidfVectorizer(ngram_range=(1, 2))
            matrix = vect.fit_transform(corpus)
            sims = cosine_similarity(matrix[0:1], matrix[1:]).flatten()
            scored = sorted(zip(docs, sims), key=lambda x: float(x[1]), reverse=True)
        except Exception:
            # Без sklearn — резерв по пересечению токенов.
            query_tokens = set(self._tokenize(text))
            scored = []
            for d in docs:
                dtok = set(self._tokenize(d["text"]))
                denom = math.sqrt(len(query_tokens) * len(dtok)) or 1.0
                overlap = len(query_tokens & dtok)
                score = overlap / denom
                scored.append((d, score))
            scored.sort(key=lambda x: x[1], reverse=True)

        lines = []
        for d, score in scored[:n]:
            if float(score) < self.FALLBACK_MIN_RELEVANCE:
                continue
            meta = d.get("meta", {})
            label = self.TYPE_LABELS.get(meta.get("type", ""), meta.get("type", ""))
            date_str = str(meta.get("date", ""))[:10]
            lines.append(f"[{label} {date_str}] {d['text']}")
        return "\n".join(lines)

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        return re.findall(r"[a-zA-Zа-яА-Я0-9_]{3,}", text.lower())

    @staticmethod
    def _make_id(text: str, doc_type: str, date: str) -> str:
        """Детерминированный id из контента — исключает дубли при переиндексации."""
        raw = f"{doc_type}:{date}:{text.strip()}"
        return hashlib.md5(raw.encode()).hexdigest()
