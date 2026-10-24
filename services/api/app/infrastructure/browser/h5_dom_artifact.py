"""在隔离世界缩减H5资源载荷，禁止把完整内联资源先传回事件循环。"""

import json

_SCRIPT = r"""(async limit => {
  const started=performance.now(), url=location.href, origin=performance.timeOrigin;
  const inert=document.implementation.createHTMLDocument('');
  const root=inert.importNode(document.documentElement,true);
  const encoder=new TextEncoder(), cache=new Map();
  let resourceBytes=0, redactions=0, missingDigests=0;
  const redact=async value => {
    const bytes=encoder.encode(value); resourceBytes+=bytes.length;redactions++;
    if(cache.has(value)) return cache.get(value);
    let hash=null;
    if(globalThis.crypto?.subtle?.digest) {
      hash=[...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))]
        .map(x=>x.toString(16).padStart(2,'0')).join('');
    } else {missingDigests++;}
    const result={bytes:bytes.length,hash,
      marker:'alioth:inline-resource-redacted;bytes='+bytes.length
        +';sha256='+(hash||'unavailable')};
    cache.set(value,result);return result;
  };
  for(const node of root.querySelectorAll('script,style,noscript,template')) {
    if(!root.contains(node)) continue;
    const fact=await redact(node.outerHTML);
    if(node instanceof HTMLTemplateElement) node.content.replaceChildren();
    else node.replaceChildren();
    node.setAttribute('data-alioth-redacted','true');
    node.setAttribute('data-alioth-original-bytes',fact.bytes);
    node.setAttribute('data-alioth-sha256',fact.hash||'unavailable');
  }
  for(const node of [root,...root.querySelectorAll('*')]) {
    for(const name of ['src','srcset','href','poster','style']) {
      const value=node.getAttribute(name);
      if(!value || !value.toLowerCase().includes('data:')) continue;
      if(name==='style') {
        for(const property of [...node.style]) {
          const css=node.style.getPropertyValue(property);
          if(!css.toLowerCase().includes('data:')) continue;
          const fact=await redact(css);
          node.style.setProperty(property,'url("'+fact.marker+'")',
                                 node.style.getPropertyPriority(property));
        }
      } else {node.setAttribute(name,(await redact(value)).marker);}
    }
  }
  const doctype=document.doctype?new XMLSerializer().serializeToString(document.doctype):'';
  let dom=doctype+root.outerHTML;
  let bytes=encoder.encode(dom), truncated=false;
  if(bytes.length>limit) {
    const original=bytes.length;
    let hash=null;
    if(globalThis.crypto?.subtle?.digest) {
      hash=[...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))]
        .map(x=>x.toString(16).padStart(2,'0')).join('');
    }
    const marker='<!-- alioth:dom-truncated original_bytes='+original
      +' sha256='+(hash||'unavailable')+' -->';
    const size=Math.floor((limit-encoder.encode(marker).length-8)/2), decode=new TextDecoder();
    dom=decode.decode(bytes.slice(0,size))+marker+decode.decode(bytes.slice(-size));
    truncated=true;
  }
  if(location.href!==url || performance.timeOrigin!==origin) throw Error('h5_dom_document_changed');
  return {dom,metrics:{redacted_resource_bytes:resourceBytes,redaction_count:redactions,
    digest_unavailable_count:missingDigests,truncated,wire_bytes:encoder.encode(dom).length,
    browser_duration_ms:performance.now()-started}};
})"""


async def read_h5_dom_artifact(page, *, max_bytes, timeout_ms):
    """只读克隆真实DOM；失败保留采集缺口，不退回无界原文传输。"""
    session = await page.context.new_cdp_session(page)
    try:
        tree = await session.send("Page.getFrameTree")
        world = await session.send(
            "Page.createIsolatedWorld",
            {
                "frameId": tree["frameTree"]["frame"]["id"],
                "worldName": "alioth.h5.dom-artifact.v1",
            },
        )
        result = await session.send(
            "Runtime.evaluate",
            {
                "expression": f"{_SCRIPT}({json.dumps(max_bytes)})",
                "contextId": world["executionContextId"],
                "returnByValue": True,
                "awaitPromise": True,
                "timeout": timeout_ms,
            },
        )
        value = result.get("result", {}).get("value")
        if result.get("exceptionDetails") or not isinstance(value, dict):
            raise RuntimeError("H5隔离DOM采集失败，禁止退回无界原文")
        if not isinstance(value.get("dom"), str) or len(value["dom"].encode()) > max_bytes:
            raise RuntimeError("H5隔离DOM产物超过协议边界")
        return value
    finally:
        await session.detach()
