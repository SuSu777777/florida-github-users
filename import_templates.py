#!/usr/bin/env python3
"""Import the six email examples from a Word document as clean JSON templates."""
import argparse
import json
import re
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile

NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'


def paragraph_text(paragraph):
    parts = []
    for node in paragraph.iter():
        if node.tag == f'{{{NS}}}t' and node.text:
            parts.append(node.text)
        elif node.tag == f'{{{NS}}}br':
            parts.append('\n')
    return ''.join(parts).strip()


def import_templates(source):
    with ZipFile(source) as archive:
        root = ElementTree.fromstring(archive.read('word/document.xml'))
    paragraphs = [paragraph_text(p) for p in root.findall(f'.//{{{NS}}}p')]
    templates = []
    current = None
    for paragraph in paragraphs:
        heading = re.match(r'^EMAIL\s+(0[1-6])\s*/\s*(.+)$', paragraph)
        if heading:
            if current:
                templates.append(current)
            current = {'number': int(heading.group(1)), 'name': heading.group(2).title(),
                       'subject': '', 'body_lines': []}
            continue
        if current is None:
            continue
        if paragraph.startswith('Subject:') and not current['subject']:
            current['subject'] = paragraph.removeprefix('Subject:').strip()
        else:
            current['body_lines'].append(paragraph)
    if current:
        templates.append(current)
    if len(templates) != 6 or [item['number'] for item in templates] != list(range(1, 7)):
        raise ValueError('Expected exactly six numbered email templates')
    result = []
    for item in templates:
        lines = []
        for paragraph in item['body_lines']:
            for line in paragraph.splitlines():
                line = line.strip()
                if re.search(r'telegram', line, re.IGNORECASE):
                    continue
                line = re.sub(r'davidchen9071@gmail\.com', '{from_email}', line, flags=re.IGNORECASE)
                line = re.sub(r'\bDavid Chen\b|\bDavid\b', 'Wong', line)
                if line in ('Hi,', 'Hey,', 'Hello,'):
                    line = 'Hi {recipient_name},'
                lines.append(line)
                if line == 'Hi {recipient_name},':
                    lines.extend(['', 'I came across your GitHub projects and was deeply impressed by the work you’ve shared.'])
            if paragraph:
                lines.append('')
        while lines and not lines[-1]:
            lines.pop()
        body = '\n'.join(lines)
        if '{from_email}' not in body:
            body += '\nEmail: {from_email}'
        if not item['subject'] or not body.strip():
            raise ValueError(f"Template {item['number']} is incomplete")
        result.append({'number': item['number'], 'name': item['name'],
                       'subject': item['subject'], 'body': body})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--output', type=Path, default=Path(__file__).with_name('email_templates.json'))
    args = parser.parse_args()
    templates = import_templates(args.source)
    args.output.write_text(json.dumps(templates, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'Imported {len(templates)} templates to {args.output}')


if __name__ == '__main__':
    main()
