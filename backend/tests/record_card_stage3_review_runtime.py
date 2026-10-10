"""Verify deployed schema/resource identity without preparing an index or model."""
import hashlib
import argparse
import json
from pathlib import Path
from app.schemas import AnalysisOut, AnalysisCreate, Fact, ENGINE_VERSION
from real_semantics_stage2 import http

parser = argparse.ArgumentParser()
parser.add_argument('--base', default='http://127.0.0.1:8000')
parser.add_argument('--frontend', default='http://127.0.0.1:8080')
args = parser.parse_args()
output = Path('docs/reviews/stage3-followup')
previous = json.loads(Path('docs/reviews/stage3/artifacts.json').read_text(encoding='utf-8'))
manifest = json.loads(Path('resources/catalog-build/manifest.json').read_text(encoding='utf-8'))
model = json.loads(Path('resources/model/model.meta.json').read_text(encoding='utf-8'))
assert manifest == previous['manifest'] and model == previous['model']
verified = {}
for root, entries in [('resources/catalog-build', manifest['sha256']), ('resources/model', model['sha256'])]:
    for name, expected in entries.items():
        with (Path(root) / name).open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        assert actual == expected, name
        verified[root + '/' + name] = actual
for filename, key in [('resources/card-schemas.json', 'card_schema_file_sha256'), ('../ktru_clean/ktru_actual_all.jsonl.gz', 'clean_actual_sha256'),
                      ('resources/model/model.meta.json', 'model_meta_sha256')]:
    with Path(filename).open('rb') as stream:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
    assert actual == previous[key]
    verified[filename] = actual
output.mkdir(parents=True, exist_ok=True)
(output / 'artifacts.json').write_text(json.dumps(dict(manifest=manifest, model=model, verified=verified,
    engine_version=ENGINE_VERSION, embeddings_recomputed=False, catalog_rebuilt=False), ensure_ascii=False, indent=2), encoding='utf-8')
records = []
for base in (args.base, args.frontend):
    for path in ('/api/health/live', '/api/health/ready', '/api/v1/catalog/status', '/api/openapi.json'):
        status, result = http(base, path)
        assert status == 200
        if path.endswith('openapi.json'):
            for schema in (AnalysisOut, AnalysisCreate, Fact):
                assert set(result['components']['schemas'][schema.__name__]['properties']) == set(schema.model_fields)
        elif path.endswith('ready'):
            assert result['catalog_version'] == manifest['catalog_version'] and result['engine_version'] == ENGINE_VERSION
        records.append(dict(base=base, route=path, http_status=status, response=result))
for filename in ('after.json', 'original-scenarios.json', 'original-flow.json'):
    for record in json.loads((output / filename).read_text(encoding='utf-8')):
        if record.get('request') and record['http_status'] == 200:
            response = AnalysisOut.model_validate(record['response'])
            assert response.engine_version == ENGINE_VERSION and response.catalog_version == manifest['catalog_version']
(output / 'runtime.json').write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
print('Actual artifact SHA verified unchanged; both ports live/ready/catalog/OpenAPI=200; HTTP schemas agree')
