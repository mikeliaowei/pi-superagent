"""Turn unittest failures in a log into GitHub Actions error annotations (one per failing test)."""

import re
import sys

text = open(sys.argv[1], encoding='utf-8', errors='replace').read()
blocks = re.split(r'\n(?==+\n(?:FAIL|ERROR): )', text)
failures = [b.strip('=\n') for b in blocks if re.match(r'=*\n?(FAIL|ERROR): ', b.lstrip('=\n') + '\n') or
            b.lstrip('=\n').startswith(('FAIL: ', 'ERROR: '))]
for block in failures[:20]:
    title = block.splitlines()[0][:200]
    body = block[-3500:].replace('%', '%25').replace('\r', '').replace('\n', '%0A')
    print(f'::error title={title.replace(",", ";").replace(":", "-")}::{body}')
names = [b.splitlines()[0] for b in failures]
if names:  # GitHub keeps only 10 error annotations per step, so name every failure in one
    print('::warning title=all failing tests::' + '%0A'.join(names))
summary = [l for l in text.splitlines() if l.startswith(('Ran ', 'FAILED', 'OK'))]
print('::notice title=unittest summary::' + ' | '.join(summary[-2:]))
