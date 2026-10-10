"""Repeat original-XML comparisons using hash-checked source excerpts."""
import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree as ET
from inspect_stage2_xml import verify_characteristics

rows = json.loads(Path('docs/reviews/stage3/source-selection.json').read_text(encoding='utf-8'))
results = []
for row in rows:
    for directory in (Path('docs/reviews/stage3/xml'), Path('docs/reviews/stage2/xml')):
        xml = directory / (row['code'] + '.xml')
        if not xml.exists():
            continue
        source = json.loads((directory / 'sources.json').read_text(encoding='utf-8'))
        expected = source['positions'][row['code']]
        assert hashlib.sha256(xml.read_bytes()).hexdigest() == expected['sha256']
        root = ET.fromstring(xml.read_bytes())
        data = next(c for c in root if c.tag.endswith('}data'))
        verify_characteristics(data, row)
        results.append(dict(code=row['code'], name=row['name'], xml=str(xml),
                            xml_sha256=expected['sha256'], clean_matches_actual_xml=True))
        break
    else:
        raise AssertionError('Original source missing: ' + row['code'])
Path('docs/reviews/stage3/source-checks.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
print('Source comparisons passed:', len(results))
