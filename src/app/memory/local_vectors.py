"""Offline Chinese embeddings and rebuildable SQLite vector cache.

No network, credentials or chat-model calls. Search is exact cosine similarity
over the permitted source set; the cache is never the authority for source data.
"""
import hashlib
import json
import os
import threading
from pathlib import Path

from app.config import BUNDLED_MODEL_ROOT
from app.db import connect

_lock = threading.RLock()
_encoder = None


def model_dir():
    return Path(os.environ.get("LINGXI_RAG_MODEL_DIR") or BUNDLED_MODEL_ROOT / "bge-small-zh-v1.5")


class Encoder:
    def __init__(self):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        root = model_dir()
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        for name in ("model.onnx", "tokenizer.json"):
            if hashlib.sha256((root / name).read_bytes()).hexdigest() != manifest["files"][name]:
                raise RuntimeError("本地向量模型校验失败，请重新安装模型")
        self.identity = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        self.prefix = manifest["query_prefix"]
        self.tokenizer = Tokenizer.from_file(str(root / "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=512)
        self.tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(root / "model.onnx"), options,
                                           providers=["CPUExecutionProvider"])

    def encode(self, texts, query=False):
        import numpy as np

        batch = self.tokenizer.encode_batch([self.prefix + t if query else t for t in texts])
        data = {
            "input_ids": np.asarray([t.ids for t in batch], dtype=np.int64),
            "attention_mask": np.asarray([t.attention_mask for t in batch], dtype=np.int64),
            "token_type_ids": np.asarray([t.type_ids for t in batch], dtype=np.int64),
        }
        output = self.session.run(None, {i.name: data[i.name] for i in self.session.get_inputs()})[0]
        vectors = output[:, 0, :] if output.ndim == 3 else output
        vectors = vectors.astype("float32")
        vectors /= np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)
        if vectors.shape[1] != 512 or not np.isfinite(vectors).all():
            raise RuntimeError("本地向量模型返回无效向量")
        return vectors


def encoder():
    global _encoder
    with _lock:
        if _encoder is None:
            _encoder = Encoder()
        return _encoder


def init_table():
    from app import migrations, stage5
    marker = stage5.DATA_DIR / "backups/rag-schema-backup.json"
    if not marker.exists():
        backup = migrations.backup(stage5.DATA_DIR)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"backup": backup}), encoding="utf-8")
    with connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS rag_vectors(
            source_id TEXT NOT NULL, chunk_no INTEGER NOT NULL, fingerprint TEXT NOT NULL,
            model TEXT NOT NULL, scope TEXT NOT NULL, owner_id TEXT NOT NULL,
            kind TEXT NOT NULL, session_id INTEGER, document TEXT NOT NULL, vector BLOB NOT NULL,
            PRIMARY KEY(source_id,chunk_no))""")
        db.execute("CREATE INDEX IF NOT EXISTS rag_scope ON rag_vectors(scope,owner_id)")
        # A withdrawn, edited or deleted message must not remain in the cache.
        for action in ("UPDATE", "DELETE"):
            db.execute(f"""CREATE TRIGGER IF NOT EXISTS rag_message_{action.lower()}
                AFTER {action} ON messages BEGIN
                DELETE FROM rag_vectors WHERE source_id='message-' || OLD.id; END""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS rag_session_delete AFTER DELETE ON sessions
            BEGIN DELETE FROM rag_vectors WHERE session_id=OLD.id; END""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS rag_fact_version AFTER INSERT ON fact_versions
            BEGIN DELETE FROM rag_vectors WHERE kind='memory' AND scope=NEW.scope
                AND owner_id=NEW.owner_id; END""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS rag_event_version AFTER INSERT ON memory7_event_versions
            BEGIN DELETE FROM rag_vectors WHERE source_id='memory-event-' || NEW.event_id; END""")


def chunks(document):
    text = document["value"]
    # BGE accepts 512 tokens. These bounded character chunks keep Chinese text,
    # labels and the query prefix within that capacity, with context overlap.
    for number, start in enumerate(range(0, max(1, len(text)), 300)):
        value = text[start:start + 360]
        if not value:
            continue
        yield {**document, "value": value, "chunk_no": number,
               "chunk_start": start, "chunk_end": start + len(value)}
        if start + 360 >= len(text):
            break


def source_current(db, doc):
    """Validate again inside the write transaction after slow inference."""
    start, end = doc.get('chunk_start', 0), doc.get('chunk_end')
    if doc.get('kind') == 'history':
        row = db.execute("SELECT * FROM messages WHERE id=? AND archived=0 AND delivery_status IN ('complete','retained')",
                         (doc.get('message_id'),)).fetchone()
        return bool(row and row['session_id'] == doc.get('session_id') and row['role'] == doc.get('role')
                    and row['content'][start:end] == doc['value'])
    if doc.get('kind') == 'memory':
        from app.memory import stage7
        scope = doc.get('scope')
        if scope not in ('companion', 'shared', 'work'):
            return False
        sid = int(doc['owner_id']) if scope == 'work' else None
        live = next((item for item in stage7.list_items(scope, sid, db) if item['id'] == doc.get('id')), None)
        return bool(live and live['revision'] == doc.get('revision') and live['version_id'] == doc.get('version_id')
                    and live['value'][start:end] == doc['value'])
    return False


def rank(query, documents, limit=5, minimum=0.55):
    """Filter sources before scoring; callers must revalidate after inference."""
    if not documents or not query.strip():
        return []
    import numpy as np

    with _lock:
        model = encoder()
        wanted, missing = [], []
        with connect() as db:
            for source in documents:
                cached = {r["chunk_no"]: r for r in db.execute(
                    "SELECT * FROM rag_vectors WHERE source_id=?", (source["source_id"],))}
                for doc in chunks(source):
                    fingerprint = hashlib.sha256(json.dumps(doc, ensure_ascii=False,
                                                            sort_keys=True).encode()).hexdigest()
                    row = cached.get(doc["chunk_no"])
                    item = [doc, fingerprint, None]
                    if row and row["fingerprint"] == fingerprint and row["model"] == model.identity:
                        vector = np.frombuffer(row["vector"], dtype="float32")
                        if vector.shape == (512,) and np.isfinite(vector).all():
                            item[2] = vector
                    if item[2] is None:
                        missing.append(item)
                    wanted.append(item)
        # Encode outside SQLite write transactions. A crash leaves completed
        # rows reusable; the next request retries remaining rows automatically.
        for start in range(0, len(missing), 8):
            batch = missing[start:start + 8]
            vectors = model.encode([i[0]["key"] + "：" + i[0]["value"] for i in batch])
            with connect() as db:
                db.execute('BEGIN IMMEDIATE')
                for item, vector in zip(batch, vectors):
                    item[2] = vector
                    doc, fingerprint, _ = item
                    if not source_current(db, doc):
                        continue
                    db.execute("""INSERT OR REPLACE INTO rag_vectors VALUES(?,?,?,?,?,?,?,?,?,?)""",
                               (doc["source_id"], doc["chunk_no"], fingerprint, model.identity,
                                doc["scope"], doc["owner_id"], doc["kind"], doc.get("session_id"),
                                json.dumps(doc, ensure_ascii=False), vector.tobytes()))
        query_vector = model.encode([query], query=True)[0]
        scores = np.asarray([float(np.dot(item[2], query_vector)) for item in wanted])
        results, seen = [], set()
        for index in np.argsort(-scores, kind="stable"):
            doc = wanted[index][0]
            if scores[index] < minimum or doc["source_id"] in seen:
                continue
            seen.add(doc["source_id"])
            results.append({**doc, "score": float(scores[index]), "match_methods": ["vector"]})
            if len(results) >= limit:
                break
        return results


def status():
    root = model_dir()
    return {"mode": "local_cpu", "model": "BAAI/bge-small-zh-v1.5", "dimensions": 512,
            "installed": all((root / n).is_file() for n in ("model.onnx", "tokenizer.json", "manifest.json")),
            "loaded": _encoder is not None, "network_required": False,
            "index": "SQLite float32 vectors, exact cosine search"}
