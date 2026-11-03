const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

test('return preview uses GET against the GET-only DRF endpoint', () => {
  const api = fs.readFileSync(path.join(__dirname, '../js/api.js'), 'utf8');
  const view = fs.readFileSync(path.join(__dirname, '../../backend/apps/pos/views.py'), 'utf8');
  const helper = api.match(/function previewPosOrderReturn\(reference, opts = \{\}\) \{([\s\S]*?)\n  \}/);
  assert.ok(helper);
  assert.match(helper[1], /return get\(/);
  assert.match(helper[1], /return-preview\//);
  assert.match(view, /class PosOrderReturnPreviewView\(APIView\):[\s\S]*?def get\(self, request, reference\):/);
});
