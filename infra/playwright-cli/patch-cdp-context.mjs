// 让锁版 CLI 的 CDP 连接继承平台已创建的设备环境，避免截图恢复成默认倍率。
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

export const originalSha256 = '7aa0bf8b6b69d32065912e3d8f7e3c18c62de4d668f770a8e039811d3cf9c6a0';
export const patchId = 'alioth-cdp-context-inheritance-v1';
export const backendClickPatchId = 'alioth-cli-backend-node-click-v1';
const sha256 = value => createHash('sha256').update(value).digest('hex');
const oldMethod = `          const persistent = {
            noDefaultViewport: true,
            ...options.noDefaults ? { acceptDownloads: "internal-browser-default" } : {}
          };`;
const newMethod = `          // ${patchId}：仅由平台进程传入已验证的设备参数，不读取页面或 Agent 配置。
          const aliothContext = process.env.ALIOTH_CLI_CONTEXT_OPTIONS ? JSON.parse(process.env.ALIOTH_CLI_CONTEXT_OPTIONS) : null;
          const persistent = {
            noDefaultViewport: !aliothContext,
            ...options.noDefaults ? { acceptDownloads: "internal-browser-default" } : {},
            ...aliothContext ? {
              viewport: aliothContext.viewport,
              screen: aliothContext.screen,
              deviceScaleFactor: aliothContext.deviceScaleFactor,
              isMobile: aliothContext.isMobile,
              hasTouch: aliothContext.hasTouch
            } : {}
          };`;

const oldTargetLocatorAnchor = `        return Promise.all(params2.map(async (param) => {
          if (!param.target.match(/^(f\\d+)?e\\d+$/)) {`;
// 兼容上一轮已安装的 backend-node 补丁；升级时先精确还原，再安装本版本。
const legacyTargetLocatorAnchor = `        return Promise.all(params2.map(async (param) => {
          // ${backendClickPatchId}：只允许平台传入已绑定的 Chromium backendNodeId。
          const backendNodeMatch = param.target.match(/^backend-node-id=(\\d+);selector=([A-Za-z0-9_-]+)$/);
          if (backendNodeMatch) {
            const backendNodeId = Number(backendNodeMatch[1]);
            if (!Number.isSafeInteger(backendNodeId) || backendNodeId <= 0)
              throw new Error('Invalid backend node id.');
            let selector;
            try {
              selector = Buffer.from(backendNodeMatch[2], 'base64url').toString('utf8');
            } catch {
              throw new Error('Invalid backend node selector.');
            }
            if (!selector || selector.length > 3000)
              throw new Error('Invalid backend node selector.');
            const handle = await this.page.$(selector);
            if (!handle)
              throw new Error('Backend node is no longer attached.');
            try {
              // client handle 转为锁版 server handle，仅用于 CDP 身份核验；点击仍由
              // client ElementHandle 执行，因此替换节点会以 detached/mismatch 失败。
              const serverHandle = this.page._connection.toImpl(handle);
              const client = serverHandle?._context?.delegate?._client;
              if (!client || !serverHandle?._objectId)
                throw new Error('Backend node binding unavailable.');
              const described = await client.send('DOM.describeNode', {
                objectId: serverHandle._objectId
              });
              const describedId = described?.node?.backendNodeId;
              if (describedId !== backendNodeId)
                throw new Error('Backend node binding changed.');
              return {
                locator: handle,
                resolved: 'backend-node-id=' + backendNodeId,
                selector: 'backend-node-id=' + backendNodeId,
                cleanup: () => handle.dispose()
              };
            } catch (error) {
              await handle.dispose().catch(() => {});
              throw error;
            }
          }
          if (!param.target.match(/^(f\\d+)?e\\d+$/)) {`;
const newTargetLocatorAnchor = `        return Promise.all(params2.map(async (param) => {
          // ${backendClickPatchId}：只允许平台传入已绑定的 Chromium backendNodeId 和语义摘要。
          const backendNodeMatch = param.target.match(/^backend-node-id=(\\d+);selector=([A-Za-z0-9_-]+);semantic-sha256=([a-f0-9]{64})$/);
          if (backendNodeMatch) {
            const backendNodeId = Number(backendNodeMatch[1]);
            if (!Number.isSafeInteger(backendNodeId) || backendNodeId <= 0)
              throw new Error('Invalid backend node id.');
            const expectedSemanticDigest = backendNodeMatch[3];
            let selector;
            try {
              selector = Buffer.from(backendNodeMatch[2], 'base64url').toString('utf8');
            } catch {
              throw new Error('Invalid backend node selector.');
            }
            if (!selector || selector.length > 3000)
              throw new Error('Invalid backend node selector.');
            const handle = await this.page.$(selector);
            if (!handle)
              throw new Error('Backend node is no longer attached.');
            try {
              // client handle 转为锁版 server handle，仅用于 CDP 身份核验；点击仍由
              // client ElementHandle 执行，因此替换节点会以 detached/mismatch 失败。
              const serverHandle = this.page._connection.toImpl(handle);
              const client = serverHandle?._context?.delegate?._client;
              if (!client || !serverHandle?._objectId)
                throw new Error('Backend node binding unavailable.');
              const described = await client.send('DOM.describeNode', {
                objectId: serverHandle._objectId
              });
              const describedId = described?.node?.backendNodeId;
              if (describedId !== backendNodeId)
                throw new Error('Backend node binding changed.');
              const semantic = await handle.evaluate(element => ({
                aria_label: element.getAttribute('aria-label'),
                href: element.href || null,
                role: element.getAttribute('role'),
                text: element.innerText || '',
                type: element.getAttribute('type')
              }));
              const semanticPayload = JSON.stringify({
                aria_label: semantic.aria_label,
                href: semantic.href,
                role: semantic.role,
                text: semantic.text,
                type: semantic.type
              });
              const actualSemanticDigest = require('crypto')
                .createHash('sha256').update(semanticPayload).digest('hex');
              if (actualSemanticDigest !== expectedSemanticDigest)
                throw new Error('Backend node semantics changed.');
              return {
                locator: handle,
                resolved: 'backend-node-id=' + backendNodeId,
                selector: 'backend-node-id=' + backendNodeId,
                cleanup: () => handle.dispose()
              };
            } catch (error) {
              await handle.dispose().catch(() => {});
              throw error;
            }
          }
          if (!param.target.match(/^(f\\d+)?e\\d+$/)) {`;

const oldClickBlock = `        const { locator: locator2, selector } = await tab2.targetLocator(params2);
        const options = {
          button: params2.button,
          modifiers: params2.modifiers,
          ...tab2.actionTimeoutOptions
        };
        response2.addAction({
          name: "click",
          selector,
          button: params2.button ?? "left",
          modifiers: fromKeyboardModifiers(params2.modifiers),
          clickCount: params2.doubleClick ? 2 : 1
        });
        await tab2.waitForCompletion(async () => {
          if (params2.doubleClick)
            await locator2.dblclick(options);
          else
            await locator2.click(options);
        });`;
const newClickBlock = `        const resolvedTarget = await tab2.targetLocator(params2);
        const { locator: locator2, selector } = resolvedTarget;
        const options = {
          button: params2.button,
          modifiers: params2.modifiers,
          ...tab2.actionTimeoutOptions
        };
        response2.addAction({
          name: "click",
          selector,
          button: params2.button ?? "left",
          modifiers: fromKeyboardModifiers(params2.modifiers),
          clickCount: params2.doubleClick ? 2 : 1
        });
        try {
          await tab2.waitForCompletion(async () => {
            if (params2.doubleClick)
              await locator2.dblclick(options);
            else
              await locator2.click(options);
          });
        } finally {
          if (resolvedTarget.cleanup)
            await resolvedTarget.cleanup();
        }`;

function restorePatched(source) {
  if (!source.includes(patchId) || !source.includes(backendClickPatchId))
    throw new Error('CLI 补丁不完整，必须重新审核');
  const appliedTargetLocatorAnchor = source.includes(newTargetLocatorAnchor)
    ? newTargetLocatorAnchor
    : legacyTargetLocatorAnchor;
  if (source.split(newMethod).length !== 2
      || source.split(appliedTargetLocatorAnchor).length !== 2
      || source.split(newClickBlock).length !== 2)
    throw new Error('CLI 补丁内容或接入位置已变化');
  return source
    .replace(newClickBlock, oldClickBlock)
    .replace(appliedTargetLocatorAnchor, oldTargetLocatorAnchor)
    .replace(newMethod, oldMethod);
}

function applyPatched(source) {
  if (source.split(oldMethod).length !== 2
      || source.split(oldTargetLocatorAnchor).length !== 2
      || source.split(oldClickBlock).length !== 2)
    throw new Error('CLI 补丁接入位置不唯一');
  return source
    .replace(oldMethod, newMethod)
    .replace(oldTargetLocatorAnchor, newTargetLocatorAnchor)
    .replace(oldClickBlock, newClickBlock);
}

export function patchBundle(source) {
  // 已应用补丁也必须能完整反向还原为已审核的发行版，不能只检查标记文字。
  if (source.includes(backendClickPatchId)) {
    const restored = restorePatched(source);
    if (sha256(restored) !== originalSha256) throw new Error('CLI 补丁或依赖文件已变化');
    return source.includes(newTargetLocatorAnchor) ? source : applyPatched(restored);
  }
  let base = source;
  if (source.includes(patchId)) {
    if (source.split(newMethod).length !== 2)
      throw new Error('CLI 环境补丁内容或接入位置已变化');
    base = source.replace(newMethod, oldMethod);
    if (sha256(base) !== originalSha256) throw new Error('CLI 补丁或依赖文件已变化');
  } else if (sha256(base) !== originalSha256) {
    throw new Error('CLI 依赖摘要不匹配，必须重新审核补丁');
  }
  return applyPatched(base);
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const bundle = new URL('./node_modules/playwright-core/lib/coreBundle.js', import.meta.url);
  const source = readFileSync(bundle, 'utf8');
  const patched = patchBundle(source);
  if (process.argv.includes('--check')) {
    if (source !== patched) throw new Error('CLI 尚未应用必要兼容补丁');
  } else if (source !== patched) {
    writeFileSync(bundle, patched, 'utf8');
  }
  process.stdout.write(JSON.stringify({ patch: patchId, backendClickPatch: backendClickPatchId, sha256: sha256(patched) }) + '\n');
}
