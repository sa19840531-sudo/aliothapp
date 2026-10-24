// 补丁只接受审核过的发行版和精确的已打补丁版本。
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { backendClickPatchId, patchBundle, patchId } from './patch-cdp-context.mjs';

const source = readFileSync(new URL('./node_modules/playwright-core/lib/coreBundle.js', import.meta.url), 'utf8');
test('重复安装保持内容不变', () => {
  const patched = patchBundle(source);
  assert.ok(patched.includes(patchId));
  assert.ok(patched.includes(backendClickPatchId));
  assert.ok(patched.includes('backend-node-id=(\\d+);selector='));
  assert.ok(patched.includes('semantic-sha256='));
  assert.equal(patchBundle(patched), patched);
});
test('依赖变动必须重新审核，不能继续套用补丁', () => {
  assert.throws(() => patchBundle(source + '\n'), /变化|不匹配/);
  assert.throws(() => patchBundle('未知版本'), /不匹配/);
});
test('补丁内容被篡改时拒绝放行', () => {
  const patched = patchBundle(source);
  assert.throws(() => patchBundle(patched.replace('noDefaultViewport: !aliothContext', 'noDefaultViewport: true')), /变化/);
});
