"""Offline tools for a normalized supplier catalog, not an XML/Excel converter."""
import argparse
import json
from pathlib import Path

import numpy as np

from app.catalog import Catalog, digest
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


def prepare(directory: Path, model_dir: Path) -> None:
    catalog = Catalog.model_validate_json((directory / "catalog.json").read_text(encoding="utf-8"))
    if catalog.synthetic:
        raise ValueError("Test fixtures must not become the working catalog")
    metadata = json.loads((model_dir / "model.meta.json").read_text(encoding="utf-8"))
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
    args = parser.parse_args()
    if args.command == "download-model":
        download_model(args.directory, args.revision)
    else:
        prepare(args.directory, args.model_dir)
