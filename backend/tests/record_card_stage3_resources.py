"""Record exact local immutable artifacts, without rebuilding or loading the catalog."""
import hashlib
import json
from pathlib import Path


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


root = Path('resources/catalog-build')
manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
for name, expected in manifest['sha256'].items():
    assert digest(root / name) == expected
old = json.loads(Path('.tools/catalog-before-stage3/manifest.json').read_text(encoding='utf-8'))
assert manifest['sha256']['vectors.npy'] == old['sha256']['vectors.npy']
assert manifest['sha256']['tokens.json'] == old['sha256']['tokens.json']
model = json.loads(Path('resources/model/model.meta.json').read_text(encoding='utf-8'))
for name, expected in model['sha256'].items():
    assert digest(Path('resources/model') / name) == expected
card = json.loads(Path('resources/card-schemas.json').read_text(encoding='utf-8'))
data = dict(manifest=manifest, previous_catalog_version=old['catalog_version'], model=model,
            model_meta_sha256=digest('resources/model/model.meta.json'),
            card_schema_version=card['version'], card_schema_data_sha256=card['sha256'],
            card_schema_file_sha256=digest('resources/card-schemas.json'),
            source_xlsx_sha256=card['data']['source_hashes'],
            clean_actual_sha256=digest('../ktru_clean/ktru_actual_all.jsonl.gz'),
            embeddings_recomputed=False, tokens_recomputed=False,
            card_schema_scope_count=len(card['data']['profiles']), card_schema_field_count=len(card['data']['fields']))
Path('docs/reviews/stage3/artifacts.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
print('Artifact checks passed:', manifest['catalog_version'], card['version'])
