import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";
import { compileDiagram } from "./diagram-build.mjs";

// 构建依赖只用于生成文档；最终 HTML 不读取这些包，也不依赖 CDN。
const toolRoot=path.dirname(fileURLToPath(import.meta.url));
const searchRoots=(process.env.ALIOTH_DOC_MODULE_PATHS || "").split(path.delimiter).filter(Boolean);
function resolveDependency(name) {
  const candidates=[...searchRoots.map((root)=>createRequire(path.join(path.resolve(root),"__alioth_docs.cjs"))),createRequire(import.meta.url)];
  for(const resolver of candidates){try{return resolver.resolve(name);}catch{}}
  throw new Error("缺少文档构建依赖 "+name+"；请设置 ALIOTH_DOC_MODULE_PATHS，勿给业务项目添加依赖");
}
const { marked }=await import(pathToFileURL(resolveDependency("marked")));
const dagreModule=await import(pathToFileURL(resolveDependency("@dagrejs/dagre")));
const dagre=dagreModule.default || dagreModule;
const canvasModule=await import(pathToFileURL(resolveDependency("@napi-rs/canvas")));
const measurement=canvasModule.createCanvas(8,8).getContext("2d");
const diagramCore=fs.readFileSync(path.join(toolRoot,"diagram-canvas.js"),"utf8");
const diagramInterface=fs.readFileSync(path.join(toolRoot,"diagram-interface.js"),"utf8");
const measure=(text,size)=>{measurement.font=size+'px "PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif';return measurement.measureText(text).width;};

(function generateDocument() {
  const sourceRoot = path.resolve(process.argv[2] || '');
  const file = process.argv[3] || '';
  const entries = [
    {file:'README.md', out:'index.html', no:'00', name:'总览与闭环', color:'#176257', detail:'七项设计 · 共同边界 · 实施依赖'},
    {file:'01-controlled-exploration.md', out:'01-controlled-exploration.html', no:'01', name:'受控探索', color:'#0b6e65', detail:'有限自主探索 · CLI / ADB · Skills'},
    {file:'02-page-state-graph.md', out:'02-page-state-graph.html', no:'02', name:'页面状态图', color:'#285caa', detail:'稳定身份 · 状态版本 · 树形投影', diagramTitles:['概念数据结构与实体关系','组件职责与依赖','调用时序与交接','判定与异常流转']},
    {file:'03-targeted-recapture.md', out:'03-targeted-recapture.html', no:'03', name:'精准补采', color:'#88501c', detail:'验证式恢复 · 状态等价 · 缺口闭环'},
    {file:'04-page-recognition.md', out:'04-page-recognition.html', no:'04', name:'页面识别', color:'#7252a3', detail:'产品能力 · 页面类型 · 规则范围'},
    {file:'05-capture-readiness.md', out:'05-capture-readiness.html', no:'05', name:'截图时机', color:'#a33b50', detail:'就绪判定 · 网络信号 · 时间证据'},
    {file:'06-interaction-precision.md', out:'06-interaction-precision.html', no:'06', name:'精准交互', color:'#b45a1b', detail:'复合动作 · 弹窗关闭 · 外跳恢复'},
    {file:'07-trusted-evidence.md', out:'07-trusted-evidence.html', no:'07', name:'可信证据', color:'#116781', detail:'可见层 · 原始与派生 · 规则输入'}
  ];
  const entry = entries.find(item => item.file === file);
  if (!entry || path.basename(sourceRoot) !== '2026-09-03-exploration-optimization') {
    throw new Error('仅允许渲染已确定的专项文档目录与文件');
  }
  const source = fs.readFileSync(path.join(sourceRoot, file), 'utf8');
  const esc = value => String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  const plain = value => value.replace(/<[^>]+>/g, '').replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"');
  const title = source.match(/^# (.+)$/m)?.[1];
  if (!title) throw new Error('文档缺少一级标题');
  const sourceHash = crypto.createHash('sha256').update(source).digest('hex');
  let chapter = 0;
  let subchapter = 0;
  const toc = [];
  let body = marked.parse(source, {gfm:true, breaks:false, async:false});
  body = body.replace(/<h([1-6])>([\s\S]*?)<\/h\1>/g, (all, level, label) => {
    let id = 'document-top';
    if (level === '2') {
      chapter += 1;
      subchapter = 0;
      id = 'section-' + chapter;
      toc.push({id, label:plain(label)});
    } else if (level !== '1') {
      subchapter += 1;
      id = 'section-' + chapter + '-' + subchapter;
    }
    return '<h' + level + ' id="' + id + '">' + label + '</h' + level + '>';
  });
  body = body.replace(/<a href="([^"]+)"([^>]*)>([\s\S]*?)<\/a>/g, (all, href, attrs, label) => {
    if (/^https?:\/\//.test(href)) return '<a href="' + href + '"' + attrs + ' target="_blank" rel="noopener noreferrer">' + label + '</a>';
    const parts = href.split('#');
    const match = entries.find(item => item.file === parts[0].replace(/^\.\//, ''));
    if (match && !/^(Markdown|MD)$/i.test(plain(label).trim())) {
      href = match.out + (parts[1] ? '#' + parts[1] : '');
    }
    return '<a href="' + href + '"' + attrs + '>' + label + '</a>';
  });
  body = body.replace(/<table>/g, '<div class="table-scroll" role="region" aria-label="设计对照表，可横向滚动" tabindex="0"><table>').replace(/<\/table>/g, '</table></div>');

  const diagramSources=[...source.matchAll(/^```mermaid[ \t]*\r?\n([\s\S]*?)^```[ \t]*$/gm)].map((match)=>match[1].trim());
  const diagramTitles=entry.diagramTitles || ["组件职责与依赖","调用时序与交接","判定与异常流转"];
  const diagrams=[];
  body=body.replace(/<pre><code class="language-mermaid">[\s\S]*?<\/code><\/pre>/g,(original)=>{
    const graph=compileDiagram(diagramSources[diagrams.length],{dagre,measure});
    const number=diagrams.length+1,title=diagramTitles[number-1] || "技术关系";
    if(entry.no==="02" && number===1)graph.relationDiagram=true;
    graph.title="技术图 "+String(number).padStart(2,"0")+" · "+title;
    diagrams.push(graph);
    const relations=graph.kind==="flow"?graph.edges.map((edge)=>{
      const from=graph.nodes.find((node)=>node.id===edge.from).label.replace(/\n/g," / "),to=graph.nodes.find((node)=>node.id===edge.to).label.replace(/\n/g," / ");
      return from+" — "+(edge.label || "交接")+" → "+to;
    }):graph.events.map((event)=>{
      const from=graph.participants.find((participant)=>participant.id===event.from).label,to=graph.participants.find((participant)=>participant.id===event.to).label;
      return (event.kind==="note"?"注释：":event.number+". ")+from+" → "+to+"："+event.label;
    });
    return '<figure class="technical-diagram" data-technical-diagram="'+(number-1)+'" aria-labelledby="technical-title-'+number+'">'+
      '<figcaption id="technical-title-'+number+'">'+esc(graph.title)+'</figcaption>'+
      '<p class="technical-legend">'+(graph.relationDiagram?'概念关系图 · 实线为包含或事实引用，虚线为可选关联或派生；数字为数量约束，箭头不是执行顺序。不是数据库表设计。':'纵向阅读 · 菱形表示条件 · 虚线表示回流、事件或回执，以边上文字和正文为准。图均为设计目标。')+'</p>'+
      '<div class="technical-controls" hidden><label>定位 <select aria-label="选择'+esc(title)+'中的节点或消息"></select></label><div><button type="button" data-minus aria-label="缩小技术图">−</button><output>100%</output><button type="button" data-plus aria-label="放大技术图">＋</button><button type="button" data-fit>全图概览</button><button type="button" data-size>清晰原寸</button></div></div>'+
      '<div class="technical-viewport" role="region" tabindex="0" aria-label="'+esc(title)+'，可上下和横向滚动"><canvas width="'+graph.width+'" height="'+graph.height+'" role="img" aria-label="'+esc(graph.title)+'；完整关系见下方文字替代">技术图文字关系见下方。</canvas></div>'+
      '<p class="technical-hint">点击节点或消息查看交接关系；可缩放并在图内滚动。下方提供同源文字与 Mermaid 图源。</p>'+
      '<aside class="technical-detail" aria-live="polite" hidden></aside>'+
      '<details class="technical-source" open><summary>文字关系与 Mermaid 图源</summary><ol>'+relations.map((relation)=>'<li>'+esc(relation)+'</li>').join("")+'</ol>'+original+'</details></figure>';
  });
  if(diagrams.length!==diagramSources.length)throw new Error("Mermaid 图源与 HTML 图数不一致");
  if(entry.no!=="00" && diagrams.length!==diagramTitles.length)throw new Error("专项图数必须与配置标题一致");
  const technicalLinks=diagrams.length?'<aside class="technical-entry"><strong>'+(entry.no==="02"?'v1.2 技术深化 · 四张设计图 + 概念数据结构':'v1.1 技术深化 · 三张设计图 + 五章实现机制')+'</strong><div>'+(entry.no==="02"?'<a href="#section-5-10">数据结构与关系</a>':'')+'<a href="#section-15">组件与接入</a><a href="#section-16">技术时序</a><a href="#section-17">算法与判断</a><a href="#section-18">并发与恢复</a><a href="#section-19">故障推演</a></div><p>保留原有方案和反馈；新增实现评审细节，不展开表设计或具体字段。</p></aside>':"";
  const diagramScripts=diagrams.length?'<script type="application/json" id="technical-diagram-data">'+JSON.stringify(diagrams).replace(/</g,"\\u003c")+'</script>\n<script id="technical-canvas-core">'+diagramCore+'</script>\n<script id="technical-canvas-interface">'+diagramInterface+'</script>':"";

  const seriesNav = entries.map(item => '<a class="series-link' + (item.file === file ? ' active' : '') + '" href="' + item.out + '"' + (item.file === file ? ' aria-current="page"' : '') + '><span class="series-no">' + item.no + '</span><span>' + esc(item.name) + '</span></a>').join('\n');
  const chapterNav = toc.map(item => '<a href="#' + item.id + '">' + esc(item.label) + '</a>').join('\n');
  const idx = entries.indexOf(entry);
  const previous = entries[idx - 1];
  const next = entries[idx + 1];
  const footerNav = [
    previous ? '<a href="' + previous.out + '"><span>上一篇</span>' + esc(previous.name) + '</a>' : '<a href="../README.md"><span>返回</span>设计文档总索引</a>',
    '<a class="back-top" href="#document-top"><span>本篇</span>返回顶部 ↑</a>',
    next ? '<a class="next" href="' + next.out + '"><span>下一篇</span>' + esc(next.name) + '</a>' : '<a class="next" href="index.html"><span>全系列</span>返回总览</a>'
  ].join('\n');
  const css = `
:root { --accent: ${entry.color}; --ink: #192b34; --muted: #526772; --line: #dfe6e8; --paper: #fff; --wash: #f3f6f7; color-scheme: light; }
* { box-sizing: border-box; }
html { scroll-behavior: smooth; scroll-padding-top: 24px; }
body { margin: 0; color: var(--ink); background: var(--wash); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif; -webkit-font-smoothing: antialiased; }
a { color: var(--accent); text-decoration-thickness: 1px; text-underline-offset: 3px; }
a:hover { text-decoration-thickness: 2px; }
a:focus-visible, [tabindex]:focus-visible { outline: 3px solid var(--accent); outline-offset: 4px; }
.skip { position: fixed; left: 16px; top: -100px; padding: 12px 18px; background: var(--paper); border: 2px solid var(--accent); z-index: 10; }
.skip:focus { top: 12px; }
.topbar { background: var(--paper); border-top: 5px solid var(--accent); border-bottom: 1px solid var(--line); }
.topbar-inner { max-width: 1460px; margin: auto; padding: 22px 32px; display: flex; align-items: center; justify-content: space-between; gap: 18px; }
.brand { text-decoration: none; color: var(--ink); display: flex; align-items: center; gap: 13px; }
.brand-mark { width: 39px; height: 39px; background: var(--accent); color: #fff; display: grid; place-items: center; font-weight: 750; border-radius: 11px; }
.brand strong { display: block; font-size: 16px; letter-spacing: .11em; }
.brand small { display: block; color: var(--muted); font-size: 12px; margin-top: 4px; letter-spacing: .02em; }
.top-meta { text-align: right; font-size: 12px; color: var(--muted); line-height: 1.8; }
.layout { max-width: 1460px; margin: 30px auto 50px; padding: 0 32px; display: grid; grid-template-columns: 242px minmax(0, 1fr); gap: 30px; align-items: start; }
.rail { position: sticky; top: 24px; max-height: calc(100vh - 48px); overflow-y: auto; padding: 4px 10px 12px 0; scrollbar-width: thin; }
.rail-label { margin: 0 0 12px; font-size: 11px; font-weight: 750; letter-spacing: .13em; color: var(--muted); }
.series { display: grid; gap: 4px; }
.series-link { display: flex; gap: 12px; align-items: center; padding: 10px 12px; border: 1px solid transparent; border-radius: 9px; text-decoration: none; color: var(--muted); font-size: 13px; line-height: 1.35; }
.series-link:hover { background: #e8eef0; text-decoration: none; }
.series-link.active { background: var(--paper); border-color: var(--line); color: var(--accent); font-weight: 700; box-shadow: 0 3px 9px #20303a06; }
.series-no { font-size: 11px; font-variant-numeric: tabular-nums; letter-spacing: .08em; opacity: .8; }
.toc { margin-top: 26px; padding-top: 20px; border-top: 1px solid var(--line); display: grid; }
.toc a { display: block; font-size: 12px; line-height: 1.55; color: var(--muted); padding: 6px 8px; text-decoration: none; border-left: 2px solid transparent; }
.toc a:hover { border-left-color: var(--accent); color: var(--accent); background: #fff8; }
.rail-note { margin: 20px 8px 0; color: var(--muted); font-size: 11px; line-height: 1.8; }
.main-column { min-width: 0; }
.doc-toolbar { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; margin-bottom: 15px; padding: 0 3px; font-size: 12px; line-height: 1.5; color: var(--muted); }
.breadcrumb { display: flex; flex-wrap: wrap; gap: 9px; align-items: center; }
.breadcrumb a { text-decoration: none; }
.toolbar-links { display: flex; gap: 17px; }
.doc-paper { background: var(--paper); border: 1px solid var(--line); border-radius: 18px; overflow: hidden; box-shadow: 0 12px 40px #152b3810; }
.document-banner { border-bottom: 1px solid var(--line); padding: 21px 46px; display: flex; align-items: center; gap: 14px; background: linear-gradient(105deg, #f8fbfb, #fff); }
.document-number { color: var(--accent); font-size: 30px; font-weight: 730; line-height: 1; letter-spacing: -.04em; font-variant-numeric: tabular-nums; }
.document-kicker { font-size: 11px; letter-spacing: .13em; font-weight: 700; color: var(--accent); line-height: 1.7; }
.document-subtitle { color: var(--muted); font-size: 12px; line-height: 1.7; margin-top: 2px; }
.document-body { padding: 35px 46px 48px; font-size: 15px; line-height: 1.95; overflow-wrap: anywhere; }
.document-body h1 { margin: 0 0 23px; font-size: clamp(26px, 2.5vw, 37px); line-height: 1.43; letter-spacing: -.025em; font-weight: 740; max-width: 950px; }
.document-body h2 { font-size: 23px; line-height: 1.55; margin: 50px 0 18px; padding-top: 24px; border-top: 1px solid var(--line); color: var(--ink); letter-spacing: -.02em; }
.document-body h3 { margin: 30px 0 12px; font-size: 18px; line-height: 1.65; color: var(--accent); }
.document-body h4 { margin: 24px 0 10px; font-size: 16px; }
.document-body h1, .document-body h2, .document-body h3 { scroll-margin-top: 24px; }
.document-body p { margin: 0 0 16px; }
.document-body blockquote { margin: 22px 0; padding: 17px 20px; border-left: 3px solid var(--accent); background: #f3f7f8; border-radius: 0 10px 10px 0; color: #3c5561; font-size: 14px; line-height: 1.85; }
.document-body blockquote p { margin: 0 0 8px; }
.document-body blockquote p:last-child { margin-bottom: 0; }
.document-body h1 + blockquote { margin-bottom: 30px; }
.document-body strong { font-weight: 680; color: #223e4b; }
.document-body ul, .document-body ol { margin: 12px 0 22px; padding-left: 24px; }
.document-body li { padding-left: 3px; margin: 7px 0; }
.document-body li > p { margin-bottom: 7px; }
.document-body code { padding: .1em .33em; border-radius: 4px; background: #edf2f4; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: .86em; overflow-wrap: anywhere; }
.document-body a code { background: none; padding: 0; }
.document-body pre { padding: 20px; border: 1px solid var(--line); border-radius: 10px; overflow: auto; background: #f5f8f9; line-height: 1.7; white-space: pre-wrap; }
.document-body pre code { padding: 0; background: none; }
.document-body hr { border: 0; border-top: 1px solid var(--line); margin: 30px 0; }
.table-scroll { overflow-x: auto; margin: 20px 0 25px; border: 1px solid var(--line); border-radius: 10px; background: #fff; scrollbar-width: thin; }
table { width: 100%; min-width: 590px; border-collapse: collapse; text-align: left; font-size: 13px; line-height: 1.8; }
thead { background: #ecf2f4; }
th { padding: 13px 14px; font-weight: 680; color: #274350; vertical-align: top; border-bottom: 1px solid var(--line); }
td { padding: 13px 14px; border-bottom: 1px solid var(--line); vertical-align: top; }
tr:last-child td { border-bottom: 0; }
tbody tr:nth-child(even) { background: #f9fbfc; }
tbody tr:hover { background: #f0f6f7; }
td:first-child { font-weight: 530; }
td p { margin: 0; }
.doc-footer { margin-top: 22px; display: grid; grid-template-columns: 1fr auto 1fr; gap: 14px; padding: 20px 4px; border-top: 1px solid var(--line); }
.doc-footer a { font-size: 13px; line-height: 1.65; text-decoration: none; }
.doc-footer span { display: block; font-size: 11px; color: var(--muted); margin-bottom: 3px; }
.doc-footer .next { text-align: right; }
.doc-footer .back-top { text-align: center; }
.page-note { text-align: center; color: var(--muted); font-size: 11px; line-height: 1.9; margin: 12px 0 20px; }
@media (min-width: 1600px) { .document-body { font-size: 16px; } }
@media (max-width: 1100px) { .layout { gap: 21px; grid-template-columns: 205px minmax(0, 1fr); padding: 0 22px; } .document-body { padding: 30px 30px 40px; } .document-banner { padding: 19px 30px; } }
@media (max-width: 800px) { .topbar-inner { padding: 18px 20px; } .top-meta { font-size: 11px; } .layout { display: block; padding: 0 16px; margin-top: 19px; } .rail { position: static; max-height: none; overflow: visible; padding: 0; margin-bottom: 23px; } .series { grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 5px; } .series-link { padding: 10px 8px; gap: 6px; font-size: 12px; border-color: var(--line); } .toc { margin-top: 15px; padding-top: 12px; grid-template-columns: repeat(2, minmax(0, 1fr)); max-height: 188px; overflow-y: auto; } .toc .rail-label { grid-column: 1/-1; margin-bottom: 5px; } .toc a { padding: 5px 7px; font-size: 11px; } .rail-note { display: none; } .doc-paper { border-radius: 12px; } .document-body { padding: 25px 24px 34px; } .document-banner { padding: 18px 24px; } .document-body h2 { font-size: 21px; margin-top: 38px; } }
@media (max-width: 480px) { .top-meta { display: none; } .series { grid-template-columns: repeat(2, minmax(0, 1fr)); } .document-body { padding: 22px 18px 30px; font-size: 14px; } .document-banner { padding: 17px 18px; } .document-body h1 { font-size: 27px; } .document-body h2 { font-size: 20px; } .document-body h3 { font-size: 17px; } .doc-footer { gap: 10px; } .doc-footer a { font-size: 12px; } .doc-toolbar { font-size: 11px; } }
@media (prefers-reduced-motion: reduce) { html { scroll-behavior: auto; } }
@media print { @page { size: A4; margin: 17mm 14mm; } body { background: #fff; font-size: 10pt; } .skip, .topbar, .rail, .doc-toolbar, .doc-footer, .page-note { display: none !important; } .layout { display: block; margin: 0; padding: 0; max-width: none; } .doc-paper { box-shadow: none; border: 0; border-radius: 0; overflow: visible; } .document-banner { padding: 0 0 11pt; margin-bottom: 14pt; background: #fff; } .document-body { padding: 0; font-size: 10pt; line-height: 1.65; } .document-body h1 { font-size: 23pt; } .document-body h2 { font-size: 16pt; margin-top: 24pt; padding-top: 13pt; break-after: avoid; } .document-body h3 { font-size: 12pt; break-after: avoid; } .document-body p, .document-body li { orphans: 3; widows: 3; } .table-scroll { overflow: visible; border-radius: 0; break-inside: auto; margin: 12pt 0; } table { min-width: 0; table-layout: fixed; font-size: 8.3pt; line-height: 1.55; } th, td { padding: 7pt 6pt; overflow-wrap: anywhere; } thead { display: table-header-group; } tr { break-inside: avoid; } .document-body blockquote { border: 1px solid #c4d0d5; border-left: 3px solid var(--accent); break-inside: avoid; font-size: 9pt; padding: 9pt 12pt; } a { color: inherit; text-decoration: underline; } }
`;
  const diagramCss = "\n.technical-entry { margin:0 0 26px; padding:17px 20px; background:#edf6f3; border:1px solid #bbd9d0; border-radius:10px; font-size:13px; line-height:1.8; }\n.technical-entry strong { display:block; margin-bottom:9px; color:#1e6459; }\n.technical-entry div { display:flex; flex-wrap:wrap; gap:7px 16px; }\n.technical-entry p { margin:9px 0 0; color:#526c73; font-size:12px; }\n.technical-diagram { margin:25px -18px 30px; border:1px solid #d4e0e5; border-radius:11px; overflow:hidden; background:#fff; }\n.technical-diagram figcaption { padding:17px 18px 6px; font-weight:680; font-size:16px; color:#244453; }\n.technical-diagram .technical-legend { padding:0 18px 12px; margin:0; color:#58717e; font-size:11px; line-height:1.8; }\n.technical-controls { display:flex; gap:9px 15px; align-items:center; flex-wrap:wrap; border-block:1px solid #dbe5e9; padding:10px 12px; background:#f4f8fa; }\n.technical-controls[hidden],.technical-detail[hidden] { display:none; }\n.technical-controls label { display:flex; align-items:center; gap:7px; font-size:12px; flex:1; min-width:190px; }\n.technical-controls label select { min-width:0; flex:1; max-width:340px; }\n.technical-controls > div { display:flex; align-items:center; gap:5px; }\n.technical-controls button,.technical-controls select { font-family:inherit; font-size:12px; line-height:1.5; color:#254552; background:#fff; border:1px solid #bfd0d8; border-radius:6px; min-height:36px; padding:6px 9px; }\n.technical-controls button { cursor:pointer; }\n.technical-controls button:disabled { opacity:0.4; cursor:default; }\n.technical-controls output { min-width:40px; text-align:center; color:#5a707d; font-size:11px; font-variant-numeric:tabular-nums; }\n.technical-controls :focus-visible { outline:3px solid #34958a; outline-offset:3px; }\n.technical-viewport { overflow:auto; max-height:720px; scrollbar-width:thin; overscroll-behavior:contain; background:#fbfcfd; }\n.technical-viewport canvas { display:block; max-width:none; cursor:pointer; touch-action:pan-x pan-y; }\n.technical-diagram .technical-hint { margin:0; padding:9px 16px; border-top:1px solid #dbe5e9; color:#657b86; font-size:11px; }\n.technical-detail { border-top:1px solid #dbe5e9; background:#edf5f6; padding:13px 17px; font-size:13px; }\n.technical-detail strong { display:block; margin-bottom:6px; }\n.technical-detail p { margin:0; }\n.technical-source { border-top:1px solid #dbe5e9; padding:0 17px 10px; font-size:12px; }\n.technical-source summary { padding:11px 0; color:var(--accent); cursor:pointer; font-weight:600; }\n.technical-source pre { font-size:11px; margin:10px 0; }\n.technical-source ol { margin-top:4px; }\n@media (max-width:640px) {\n  .technical-diagram { margin-inline:-9px; }\n  .technical-controls label { flex-basis:100%; }\n  .technical-controls > div { flex-wrap:wrap; }\n  .technical-controls button,.technical-controls select { min-height:44px; }\n  .technical-viewport { max-height:570px; }\n}\n@media print {\n  .technical-entry,.technical-controls,.technical-hint,.technical-detail { display:none !important; }\n  .technical-diagram { margin:15pt 0; break-inside:avoid; }\n  .technical-viewport { max-height:none; overflow:visible; }\n  .technical-viewport canvas { max-width:100%; max-height:232mm; width:auto !important; height:auto !important; margin:auto; }\n  .technical-source { font-size:8pt; }\n}\n";
  const html = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="${esc(entry.detail)}；Alioth 七项探索优化专项设计。包含组件图、时序图、判断流程、算法、并发恢复与技术验收，不展开数据库表或具体数据结构。">
  <meta name="color-scheme" content="light">
  <title>${esc(title)} · Alioth</title>
  <style>${css}\n${diagramCss}</style>\n<noscript><style>.technical-viewport,.technical-hint{display:none}</style></noscript>
</head>
<body>
  <a class="skip" href="#document-top">跳到正文</a>
  <header class="topbar">
    <div class="topbar-inner">
      <a class="brand" href="index.html"><span class="brand-mark" aria-hidden="true">衡</span><span><strong>ALIOTH · 玉衡</strong><small>有限自主探索 / 七项专项设计</small></span></a>
      <div class="top-meta">TECHNICAL DESIGN · 2026.09.04<br>设计评审稿 · 不代表已实现或验收</div>
    </div>
  </header>
  <div class="layout">
    <aside class="rail" aria-label="文档导航">
      <p class="rail-label">全系列 / 07 TOPICS</p>
      <nav class="series" aria-label="七项专项导航">${seriesNav}</nav>
      <nav class="toc" aria-label="本篇章节目录"><p class="rail-label">本篇目录 / ${toc.length} SECTIONS</p>${chapterNav}</nav>
      <p class="rail-note">HTML 与 Markdown 正文一致。<br>离线 Canvas 技术图。无外部运行依赖。<br>浏览器查找可检索全文，打印可导出 PDF。</p>
    </aside>
    <main class="main-column">
      <div class="doc-toolbar">
        <div class="breadcrumb"><a href="index.html">设计文档集</a><span aria-hidden="true">/</span><span>${esc(entry.name)}</span></div>
        <div class="toolbar-links"><a href="${entry.file}">Markdown 原稿</a><a href="../2026-09-02-bounded-exploration-optimization-solution.html">完整方案 v1.1</a></div>
      </div>
      <div class="doc-paper">
        <div class="document-banner"><span class="document-number">${entry.no}</span><div><div class="document-kicker">${idx === 0 ? 'SERIES OVERVIEW' : 'SPECIALIZED DESIGN'} / 方案与设计</div><div class="document-subtitle">${esc(entry.detail)}</div></div></div>
        <article id="document-content" class="document-body" data-markdown-sha256="${sourceHash}">
${technicalLinks}\n${body}
        </article>
      </div>
      <nav class="doc-footer" aria-label="前后篇导航">${footerNav}</nav>
      <p class="page-note">正文按 Markdown 原稿生成 · 保留既有控制面 · 收益需基线与真实 H5 / App 验证<br>本篇不含表设计、字段定义或具体数据结构。</p>
    </main>
  </div>\n${diagramScripts}
</body>
</html>
`;
  const outPath = path.join(sourceRoot, entry.out);
  if (process.argv[4] === '--check') {
    const actual = fs.readFileSync(outPath, 'utf8');
    const valid = actual === html;
    console.log(JSON.stringify({file:entry.out, source:file, valid, chapters:toc.length, diagrams:diagrams.length, sha256:sourceHash, bytes:Buffer.byteLength(html)}));
    if (!valid) process.exitCode = 1;
    return;
  }
  if (fs.existsSync(outPath)) {
    if (process.argv[4] !== '--update') throw new Error('目标 HTML 已存在，请先检查差异，禁止无审查覆盖');
    const oldLines = fs.readFileSync(outPath, 'utf8').trimEnd().split('\n');
    const newLines = html.trimEnd().split('\n');
    let first = 0;
    while (first < oldLines.length && first < newLines.length && oldLines[first] === newLines[first]) first += 1;
    let oldEnd = oldLines.length;
    let newEnd = newLines.length;
    while (oldEnd > first && newEnd > first && oldLines[oldEnd - 1] === newLines[newEnd - 1]) { oldEnd -= 1; newEnd -= 1; }
    if (first === oldLines.length && first === newLines.length) throw new Error('内容无变化，无需更新');
    const diff = oldLines.slice(first, oldEnd).map(line => '-' + line).concat(newLines.slice(first, newEnd).map(line => '+' + line)).join('\n');
    process.stdout.write('*** Begin Patch\n*** Update File: ' + outPath + '\n@@\n' + diff + '\n*** End Patch\n');
    return;
  }
  process.stdout.write('*** Begin Patch\n*** Add File: ' + outPath + '\n' + html.trimEnd().split('\n').map(line => '+' + line).join('\n') + '\n*** End Patch\n');
})();
