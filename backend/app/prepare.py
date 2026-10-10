"""Offline tools for a normalized supplier catalog, not an XML/Excel converter."""
import argparse
import json
import gc
import shutil
from pathlib import Path

import numpy as np

from app.catalog import Catalog, digest, load_snapshot
from app.search import ANALYZER_VERSION, MODEL_ID, Analyzer, E5Encoder


def download_model(directory: Path, revision: str) -> None:
    from huggingface_hub import snapshot_download
    if len(revision) != 40 or any(char not in "0123456789abcdef" for char in revision):
        raise ValueError("Pass the exact 40-character Hugging Face commit SHA")
    snapshot_download(MODEL_ID, revision=revision, local_dir=directory,
                      allow_patterns=["config.json", "model.safetensors", "tokenizer*", "special_tokens_map.json",
                                      "sentencepiece.bpe.model"])
    metadata = {"model_id": MODEL_ID, "revision": revision,
                "sha256": {path.name: digest(path) for path in directory.iterdir() if path.is_file() and path.name != "model.meta.json"}}
    (directory / "model.meta.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")


def prepare(directory: Path, model_dir: Path, reuse_from: Path | None = None) -> None:
    if reuse_from is not None and directory.resolve() == reuse_from.resolve():
        raise ValueError("Reuse into a separate directory; preserve the validated bundle")
    previous_documents = None
    if reuse_from is not None:
        previous = load_snapshot(reuse_from)
        previous_documents = [(c.id, c.search_text) for c in previous.catalog.categories]
        previous_manifest = json.loads((reuse_from / "manifest.json").read_text(encoding="utf-8"))
        del previous
        gc.collect()
    catalog = Catalog.model_validate_json((directory / "catalog.json").read_bytes())
    if catalog.synthetic:
        raise ValueError("Test fixtures must not become the working catalog")
    metadata = json.loads((model_dir / "model.meta.json").read_text(encoding="utf-8"))
    if reuse_from is not None:
        if previous_documents != [(c.id, c.search_text) for c in catalog.categories]:
            raise ValueError("Search documents/order changed: vector reuse forbidden")
        if (previous_manifest["model_id"] != metadata["model_id"] or
            previous_manifest["model_revision"] != metadata["revision"] or
            previous_manifest["analyzer_version"] != ANALYZER_VERSION):
            raise ValueError("Incompatible model/analyzer: index reuse forbidden")
        for name, checksum in metadata["sha256"].items():
            if digest(model_dir / name) != checksum:
                raise ValueError("Model checksum mismatch")
        for name in ("vectors.npy", "tokens.json"):
            shutil.copyfile(reuse_from / name, directory / name)
        manifest = {**previous_manifest, "catalog_version": catalog.catalog_version,
                    "sha256": {name: digest(directory / name) for name in ("catalog.json", "vectors.npy", "tokens.json")},
                    "reused_from_catalog_sha256": previous_manifest["sha256"]["catalog.json"],
                    "ordered_search_documents_unchanged": True}
        (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(f"Reused {len(catalog.categories)} exact ordered search documents; no embeddings computed")
        return
    encoder = E5Encoder(model_dir, metadata["revision"])
    texts = [category.search_text for category in catalog.categories]
    if any(len(encoder.tokenizer.encode("passage: " + text)) > 512 for text in texts):
        raise ValueError("Catalog search records must fit in 512 tokens")
    np.save(directory / "vectors.npy", encoder.encode(texts, "passage: "), allow_pickle=False)
    analyzer = Analyzer()
    (directory / "tokens.json").write_text(json.dumps([analyzer.tokens(text) for text in texts], ensure_ascii=False), encoding="utf-8")
    manifest = {"catalog_version": catalog.catalog_version, "analyzer_version": ANALYZER_VERSION,
                "model_id": MODEL_ID, "model_revision": metadata["revision"],
                "sha256": {name: digest(directory / name) for name in ("catalog.json", "vectors.npy", "tokens.json")}}
    # Publish this bundle only after command succeeds. Readiness rejects partial artifacts.
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    download = sub.add_parser("download-model")
    download.add_argument("directory", type=Path)
    download.add_argument("--revision", required=True)
    build = sub.add_parser("index")
    build.add_argument("directory", type=Path)
    build.add_argument("--model-dir", required=True, type=Path)
    build.add_argument("--reuse-from", type=Path)
    args = parser.parse_args()
    if args.command == "download-model":
        download_model(args.directory, args.revision)
    else:
        prepare(args.directory, args.model_dir, reuse_from=args.reuse_from)
