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

import argparse
import hashlib
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
    parser = argparse.ArgumentParser(description="Build a held-out incident retrieval index")
    parser.add_argument("--output-dir", type=Path, default=CHROMA_DIR)
    parser.add_argument("--exclude-fault-id", type=int, action="append", default=[])
    args = parser.parse_args()

    excluded = set(args.exclude_fault_id)
    records = [record for record in load_traces() if int(record["fault_id"]) not in excluded]
    if not records:
        parser.error("no corpus records remain after fault exclusions")
    print(f"Indexing {len(records)} traces; held-out fault IDs: {sorted(excluded)}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(args.output_dir))
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
    manifest = {
        "schema_version": "2.0",
        "excluded_fault_ids": sorted(excluded),
        "included_fault_ids": sorted(int(record["fault_id"]) for record in records),
        "corpus_sha256": hashlib.sha256(TRACES_FILE.read_bytes()).hexdigest(),
        "indexed_count": collection.count(),
    }
    (args.output_dir / "index_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Indexed {collection.count()} records into '{COLLECTION_NAME}' at {args.output_dir}")


if __name__ == "__main__":
    main()
