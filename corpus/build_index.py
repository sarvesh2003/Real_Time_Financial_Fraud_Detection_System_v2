"""
Builds the local Chroma index from the verified investigation traces in
traces.jsonl. Advisory-only retrieval source for the eventual LangGraph
agent - never a substitute for live evidence (see the corroboration rule
in Project_Upgrade.md).

Embeds only initial_symptoms + investigation_path, deliberately excluding
root_cause: an agent retrieving similar past cases only ever has symptoms
to search with, not the answer. Indexing on the answer would make
retrieval trivially circular.

Usage: python build_index.py
"""

import json
from pathlib import Path

import chromadb

CORPUS_DIR = Path(__file__).resolve().parent
TRACES_FILE = CORPUS_DIR / "traces.jsonl"
CHROMA_DIR = CORPUS_DIR / "chroma_db"
COLLECTION_NAME = "fault_investigations"


def load_traces() -> list[dict]:
    records = []
    with TRACES_FILE.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def embedding_text(record: dict) -> str:
    symptoms = " ".join(record["initial_symptoms"])
    path = " ".join(record["investigation_path"])
    return f"{symptoms} {path}"


def main():
    records = load_traces()
    print(f"Loaded {len(records)} traces from {TRACES_FILE.name}")

    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    # Fresh build every run - drop and recreate rather than upsert, so the
    # index always exactly matches the current traces.jsonl on disk.
    try:
        client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass
    collection = client.create_collection(COLLECTION_NAME)

    ids = [str(r["fault_id"]) for r in records]
    documents = [embedding_text(r) for r in records]
    metadatas = [
        {
            "fault_id": r["fault_id"],
            "fault_family": r["fault_family"],
            "fault_name": r["fault_name"],
            "root_cause": r["root_cause"],
            "confidence": r["confidence"],
        }
        for r in records
    ]

    collection.add(ids=ids, documents=documents, metadatas=metadatas)
    print(f"Indexed {collection.count()} records into '{COLLECTION_NAME}' at {CHROMA_DIR}")


if __name__ == "__main__":
    main()
