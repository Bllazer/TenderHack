"""Record health, resource versions and OpenAPI of the actually running services."""
import json
from pathlib import Path
from app.schemas import AnalysisCreate, AnalysisOut, Fact, ENGINE_VERSION
from real_semantics_stage2 import http

manifest = json.loads(Path('resources/catalog-build/manifest.json').read_text(encoding='utf-8'))
records = []
for base in ('http://127.0.0.1:8000', 'http://127.0.0.1:8080'):
    for path in ('/api/health/live', '/api/health/ready', '/api/v1/catalog/status', '/api/openapi.json'):
        status, body = http(base, path)
        assert status == 200, (base, path, body)
        if path == '/api/openapi.json':
            schemas = body['components']['schemas']
            assert set(schemas['Fact']['properties']) == set(Fact.model_fields)
            assert set(schemas['AnalysisOut']['properties']) == set(AnalysisOut.model_fields)
            assert set(schemas['AnalysisCreate']['properties']) == set(AnalysisCreate.model_fields)
        elif path != '/api/health/live':
            assert body['catalog_version'] == manifest['catalog_version']
            if path == '/api/health/ready': assert body['engine_version'] == ENGINE_VERSION
            else: assert body['ready'] and body['positions'] == 60087 and body['final_positions'] == 54190
        records.append(dict(base=base, route='GET ' + path, http_status=status, response=body))
for name in ('after.json', 'flow.json'):
    for record in json.loads((Path('docs/reviews/stage3') / name).read_text(encoding='utf-8')):
        if record.get('request') and record['http_status'] == 200:
            result = AnalysisOut.model_validate(record['response'])
            assert result.catalog_version == manifest['catalog_version'] and result.engine_version == ENGINE_VERSION
Path('docs/reviews/stage3/runtime.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
print('live/ready/status/OpenAPI=200 on both ports; HTTP responses validate with actual Pydantic models')
