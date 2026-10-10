"""Offline-reviewed PP/SPGZ vocabulary. It is never fed to constraint checker C.

Scope bindings express reviewed applicability for filling a card, not official
identity of PP and KTRU classifications. The XLSX observations are not rules.
"""
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from app.catalog import AllowedValue, Property


def checksum(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({k: freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(freeze(v) for v in value)
    return value


class CardSchemas:
    def __init__(self, path):
        payload = json.loads(Path(path).read_text(encoding='utf-8'))
        self.version = payload['version']
        if checksum(payload['data']) != payload['sha256']:
            raise ValueError('Card schema checksum mismatch')
        data = payload['data']
        self.profiles = freeze({p['scope']: p for p in data['profiles']})
        self.fields = freeze({p['id']: p for p in data['fields']})
        self.lexicon = freeze(data['lexicon'])
        self.properties = MappingProxyType({key: Property(id=key, name=row['name'],
            source='spgz' if row['spgz_ids'] else 'portal', evidence=row['evidence'],
            value_type='text', verified=True, required=None,
            allowed=tuple(AllowedValue(kind='enum', value=v, source=row['evidence']) for v in row['vocabulary']))
            for key, row in self.fields.items()})

    def context(self, scope, properties):
        profile = self.profiles.get(scope)
        if not profile:
            return properties, (), ()
        # Reviewed anchors must still be present in the selected snapshot.
        ids = {p.id for p in properties}
        if not set(profile['anchors']) <= ids:
            raise ValueError('Card schema scope anchors mismatch')
        by_name = {p.name for p in properties}
        extra, evidence = [], [profile['evidence'] + ';' + profile['basis']]
        for key in profile['fields']:
            row = self.fields[key]
            evidence.append(row['evidence'])
            preferred = profile.get('preferred_fields', {}).get(row['name'])
            if row['name'] in by_name or preferred in by_name:
                continue  # KTRU has priority; PP never changes its vocabulary/rules.
            extra.append(self.properties[key])
        return (*properties, *extra), tuple(extra), tuple(evidence)


def read_xlsx(path):
    """Only the existing XLSX schema sheet, offline. No XML/KTRU conversion."""
    from zipfile import ZipFile
    from xml.etree import ElementTree as ET
    ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with ZipFile(path) as archive:
        root = ET.fromstring(archive.read('xl/sharedStrings.xml'))
        strings = [''.join(x.itertext()) for x in root.findall('s:si', ns)]
        with archive.open('xl/worksheets/sheet2.xml') as stream:
            for _, row in ET.iterparse(stream, events=('end',)):
                if row.tag != '{' + ns['s'] + '}row':
                    continue
                result = {'row': row.attrib['r']}
                for cell in row.findall('s:c', ns):
                    value = cell.find('s:v', ns)
                    text = value.text if value is not None else ''
                    if cell.attrib.get('t') == 's' and text:
                        text = strings[int(text)]
                    result[''.join(c for c in cell.attrib['r'] if c.isalpha())] = text or ''
                yield result
                row.clear()


def prepare(source_dir, review_path, output):
    import re
    review = json.loads(Path(review_path).read_text(encoding='utf-8'))
    fields, hashes = [], {}
    for filename, selected in review['sources'].items():
        path = Path(source_dir) / filename
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != selected['sha256']:
            raise ValueError('Unreviewed XLSX revision: ' + filename)
        hashes[filename] = digest
        requested = {str(f['row']): f for f in selected['fields']}
        found = set()
        for row in read_xlsx(path):
            directive = requested.get(row['row'])
            if not directive:
                continue
            if row['A'] != directive['category'] or row['D'] != directive['name']:
                raise ValueError('Schema row changed: ' + filename + ':' + row['row'])
            found.add(row['row'])
            ids = [v.strip() for v in row.get('I', '').split('|') if v.strip()]
            vocab = []
            for chunk in row.get('K', '').split('|'):
                match = re.fullmatch(r'\s*(\d+):\s*(.*)', chunk)
                if match and match[1] in ids:
                    vocab.extend(v.strip() for v in match[2].split(';') if v.strip())
            # A reviewed lexical list is for extraction, not normative PP values.
            vocab = directive.get('lexical_vocabulary', vocab)
            observation = row.get('G', '')
            fields.append(dict(id=directive['id'], name=row['D'], category=row['A'], kpgz=row['B'],
                spgz_ids=ids, spgz_names=row.get('J', ''), source_type=row.get('F'), schema_type=row.get('E'),
                unit=row.get('H') or None, vocabulary=sorted(set(vocab)),
                vocabulary_is_constraint=False, observed_values_sha256=hashlib.sha256(observation.encode()).hexdigest(),
                observed_values_length=len(observation),
                evidence=f'XLSX:{filename}@sha256:{digest}#Лист2!{row["row"]};review:{review["version"]}'))
        if found != set(requested):
            raise ValueError('Missing reviewed schema rows')
    data = dict(profiles=review['profiles'], fields=fields, lexicon=review['lexicon'], source_hashes=hashes,
                binding_semantics='reviewed_applicability_not_catalog_identity')
    Path(output).write_text(json.dumps(dict(version=review['version'], sha256=checksum(data), data=data),
                                     ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('source_dir')
    parser.add_argument('--review', default='resources/card-schema-review.json')
    parser.add_argument('--output', default='resources/card-schemas.json')
    args = parser.parse_args()
    prepare(args.source_dir, args.review, args.output)
