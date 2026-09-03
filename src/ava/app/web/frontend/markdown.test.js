import assert from 'node:assert/strict'
import test from 'node:test'

import { renderMarkdown } from './markdown.js'

const reportedOutput = `### Fix

- Added a stable, opaque per-provider/chat cache key to Codex Responses requests in src/ava/llm/codex.py.
- Added coverage confirming:
- The key remains stable across turns.
- Different provider sessions use different keys.

### Validation

- Full test suite: 78 passed
- MyPy: passed
- Focused Ruff formatting/lint: passed
- Full-tree Ruff lint: passed
- Full-tree formatting still reports pre-existing formatting differences in unrelated modified files.`

test('renders report headings and bullet lists as semantic HTML', () => {
  assert.equal(
    renderMarkdown(reportedOutput),
    '<h3>Fix</h3>' +
      '<ul>' +
      '<li>Added a stable, opaque per-provider/chat cache key to Codex Responses requests in src/ava/llm/codex.py.</li>' +
      '<li>Added coverage confirming:</li>' +
      '<li>The key remains stable across turns.</li>' +
      '<li>Different provider sessions use different keys.</li>' +
      '</ul>' +
      '<h3>Validation</h3>' +
      '<ul>' +
      '<li>Full test suite: 78 passed</li>' +
      '<li>MyPy: passed</li>' +
      '<li>Focused Ruff formatting/lint: passed</li>' +
      '<li>Full-tree Ruff lint: passed</li>' +
      '<li>Full-tree formatting still reports pre-existing formatting differences in unrelated modified files.</li>' +
      '</ul>',
  )
})

test('renders ordered and nested lists while preserving safe inline markup', () => {
  assert.equal(
    renderMarkdown('## <Results>\n3. **First**\n4. Second\n   - `nested`'),
    '<h2>&lt;Results&gt;</h2>' +
      '<ol start="3"><li><strong>First</strong></li><li>Second<ul><li><code>nested</code></li></ul></li></ol>',
  )
})
