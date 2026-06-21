from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any

import lancedb
import pyarrow as pa
from sentence_transformers import SentenceTransformer

from .knowledge import KnowledgeSample


class LanceVectorStore:
    def __init__(self, db_path: str, table_name: str, embed_model: str):
        self.db = lancedb.connect(db_path)
        self.table_name = table_name
        self.embedder = SentenceTransformer(embed_model)
        self._encode_cache: dict[str, list[float]] = {}
        self._ensure_table()

    def _encode(self, text: str) -> list[float]:
        if text not in self._encode_cache:
            if len(self._encode_cache) > 128:
                self._encode_cache.clear()
            with ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(self.embedder.encode, [text], convert_to_numpy=True)
                try:
                    result = future.result(timeout=30)
                except FuturesTimeoutError:
                    raise RuntimeError("embedding encode 超时（>30s）")
            self._encode_cache[text] = result[0].tolist()
        return self._encode_cache[text]

    def _schema(self) -> pa.Schema:
        return pa.schema(
            [
                pa.field("source_id", pa.string()),
                pa.field("source_type", pa.string()),
                pa.field("sample_type", pa.string()),
                pa.field("context", pa.string()),
                pa.field("context_3", pa.string()),
                pa.field("context_5", pa.string()),
                pa.field("dialogue_summary", pa.string()),
                pa.field("reply", pa.string()),
                pa.field("technique", pa.string()),
                pa.field("thinking", pa.string()),
                pa.field("summary", pa.string()),
                pa.field("position", pa.string()),
                pa.field("reply_style", pa.string()),
                pa.field("usage", pa.string()),
                pa.field("precautions", pa.string()),
                pa.field("metadata", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), 384)),
            ]
        )

    def _ensure_table(self) -> None:
        if self.table_name not in self.db.table_names():
            self.db.create_table(self.table_name, schema=self._schema(), mode="create")
        self.table = self.db.open_table(self.table_name)

    def rebuild(self, samples: list[KnowledgeSample]) -> None:
        self.db.create_table(self.table_name, data=[], schema=self._schema(), mode="overwrite")
        self.table = self.db.open_table(self.table_name)
        self.add(samples)

    def add(self, samples: list[KnowledgeSample]) -> None:
        if not samples:
            return
        vectors = self.embedder.encode([sample.context for sample in samples], convert_to_numpy=True).tolist()
        rows = []
        for index, sample in enumerate(samples):
            rows.append(
                {
                    "source_id": sample.source_id,
                    "source_type": sample.source_type,
                    "sample_type": sample.sample_type,
                    "context": sample.context,
                    "context_3": sample.context_3,
                    "context_5": sample.context_5,
                    "dialogue_summary": sample.dialogue_summary,
                    "reply": sample.reply,
                    "technique": sample.technique,
                    "thinking": sample.thinking,
                    "summary": sample.summary,
                    "position": sample.position,
                    "reply_style": sample.reply_style,
                    "usage": json.dumps(sample.usage, ensure_ascii=False),
                    "precautions": json.dumps(sample.precautions, ensure_ascii=False),
                    "metadata": json.dumps(sample.metadata, ensure_ascii=False),
                    "vector": vectors[index],
                }
            )
        self.table.add(rows)

    def sync(self, samples: list[KnowledgeSample]) -> None:
        expected_ids = {sample.source_id for sample in samples}
        existing_ids = self.source_ids()
        if existing_ids != expected_ids:
            self.rebuild(samples)

    def source_ids(self) -> set[str]:
        if self.count() == 0:
            return set()
        frame = self.table.to_pandas()
        if "source_id" not in frame:
            return set()
        return set(frame["source_id"].tolist())

    def query(
        self,
        query_text: str,
        technique: str | None = None,
        sample_type: str | None = None,
        n_results: int = 4,
    ) -> list[dict[str, Any]]:
        query_vec = self._encode(query_text)
        search = self.table.search(query_vec).limit(n_results)
        filters = []
        if technique:
            safe_technique = technique.replace("'", "''")
            filters.append(f"technique = '{safe_technique}'")
        if sample_type:
            safe_type = sample_type.replace("'", "''")
            filters.append(f"sample_type = '{safe_type}'")
        if filters:
            search = search.where(" AND ".join(filters), prefilter=True)
        frame = search.to_pandas()
        if frame.empty:
            return []
        rows: list[dict[str, Any]] = []
        for _, row in frame.iterrows():
            rows.append(
                {
                    "source_id": row["source_id"],
                    "source_type": row["source_type"],
                    "sample_type": row["sample_type"],
                    "context": row["context"],
                    "context_3": row["context_3"],
                    "context_5": row["context_5"],
                    "dialogue_summary": row["dialogue_summary"],
                    "reply": row["reply"],
                    "technique": row["technique"],
                    "thinking": row["thinking"],
                    "summary": row["summary"],
                    "position": row["position"],
                    "reply_style": row["reply_style"],
                    "usage": json.loads(row["usage"] or "[]"),
                    "precautions": json.loads(row["precautions"] or "[]"),
                    "metadata": json.loads(row["metadata"] or "{}"),
                }
            )
        return rows

    def count(self) -> int:
        return self.table.count_rows()
