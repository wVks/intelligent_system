#!/usr/bin/env python3
"""Recalculate supplied counts only; does not run or judge StudyNavigator."""
import json
from pathlib import Path
root = Path(__file__).resolve().parent
counts = json.loads((root / 'counts.json').read_text(encoding='utf-8'))
keys = ('total', 'successful', 'timeout', 'missing')
assert all(type(counts[k]) is int and counts[k] >= 0 for k in keys)
assert counts['total'] > 0
assert counts['successful'] + counts['timeout'] + counts['missing'] == counts['total']
report = {'basis': counts['basis'], 'mode': counts['mode'], 'provider': counts['provider'],
          'case_success_rate': counts['successful'] / counts['total'],
          'timeout_rate': counts['timeout'] / counts['total'],
          'missing_rate': counts['missing'] / counts['total'],
          'successful_cases': counts['successful'], 'total_cases': counts['total'],
          'limitation': 'Arithmetic on provided aggregates; not an application rerun or semantic review.'}
print(json.dumps(report, ensure_ascii=False, indent=2))
