// 将文档中约定的 Mermaid 子集转为离线布局；遇到未知语法立即失败，避免静默丢失关系。
export function compileDiagram(source, { dagre, measure }) {
  const rows=source.trim().split(/\r?\n/).map((line)=>line.trim()).filter((line)=>line && !line.startsWith("%%"));
  const heading=rows.shift();
  const clean=(value)=>value.replace(/<br\s*\/?>/gi,"\n").replace(/^"|"$/g,"");
  const wrap=(value,width,size=15)=>{
    const lines=[];
    for(const paragraph of clean(value).split("\n")) {
      let line="";
      for(const token of paragraph.match(/[A-Za-z0-9_./-]+|[ \t]+|[^\s]/gu)||[]) {
        if(measure(token,size)>width) {
          for(const character of token) {
            if(line && measure(line+character,size)>width){lines.push(line.trimEnd());line=character;}else line+=character;
          }
        } else if(line && measure(line+token,size)>width){lines.push(line.trimEnd());line=token.trimStart();}
        else line+=token;
      }
      lines.push(line.trimEnd());
    }
    return lines;
  };
  if(heading==="sequenceDiagram") {
    const participants=[],events=[];
    for(const line of rows) {
      let match;
      if(line==="autonumber") continue;
      if((match=line.match(/^participant\s+(\w+)\s+as\s+(.+)$/))) participants.push({id:match[1],label:clean(match[2])});
      else if((match=line.match(/^(\w+)\s*(-->>|->>)\s*(\w+)\s*:\s*(.+)$/))) events.push({kind:"message",from:match[1],to:match[3],dashed:match[2]==="-->>",label:clean(match[4])});
      else if((match=line.match(/^Note\s+over\s+(\w+)(?:\s*,\s*(\w+))?\s*:\s*(.+)$/i))) events.push({kind:"note",from:match[1],to:match[2]||match[1],label:clean(match[3])});
      else throw new Error("不支持的时序图语法："+line);
    }
    if(participants.length<2 || new Set(participants.map((item)=>item.id)).size!==participants.length) throw new Error("时序图参与者缺失或重复");
    const spacing=210,margin=108,width=Math.max(640,margin*2+(participants.length-1)*spacing);
    const index=new Map(participants.map((item,i)=>[item.id,i]));
    participants.forEach((item,i)=>Object.assign(item,{x:margin+i*spacing,y:38,w:180,h:64,lines:wrap(item.label,158,15)}));
    let top=138,number=0;
    for(const event of events) {
      if(!index.has(event.from)||!index.has(event.to)) throw new Error("时序图引用未知参与者："+event.label);
      const from=participants[index.get(event.from)],to=participants[index.get(event.to)];
      const length=Math.abs(from.x-to.x);
      const maxWidth=event.kind==="note"?Math.max(320,length+110):Math.max(170,length-32);
      event.lines=wrap(event.label,Math.min(maxWidth,width-90),14);
      event.number=event.kind==="message"?++number:null;
      event.id="event-"+events.indexOf(event);
      if(event.kind==="note") {
        event.w=Math.min(width-60,Math.max(340,length+110));event.h=event.lines.length*21+22;
        event.x=Math.max(30,Math.min(width-event.w-30,(from.x+to.x)/2-event.w/2));event.y=top;
        top+=event.h+30;
      } else {
        event.x1=from.x;event.x2=to.x;event.self=event.from===event.to;
        event.y=top+event.lines.length*21;
        const direction=index.get(event.from)===participants.length-1?-1:1;
        event.textX=event.self?from.x+direction*92:(from.x+to.x)/2;
        event.textY=top;
        event.points=event.self?[[from.x,event.y],[from.x+direction*78,event.y],[from.x+direction*78,event.y+24],[from.x,event.y+24]]:[[from.x,event.y],[to.x,event.y]];
        event.hitLeft=Math.min(from.x,to.x,event.textX-maxWidth/2)-12;
        event.hitRight=Math.max(from.x,to.x,event.textX+maxWidth/2)+12;
        top=event.y+(event.self?66:46);
      }
    }
    return {kind:"sequence",width,height:top+90,participants,events,source};
  }
  if(!/^flowchart\s+TB$/.test(heading)) throw new Error("只允许 flowchart TB 或 sequenceDiagram");
  const nodes=[],edges=[];
  for(const line of rows) {
    let match;
    if((match=line.match(/^(\w+)\[("(?:[^"]*)"|[^\]]+)\]$/))) nodes.push({id:match[1],label:clean(match[2]),shape:"box"});
    else if((match=line.match(/^(\w+)\{("(?:[^"]*)"|[^}]+)\}$/))) nodes.push({id:match[1],label:clean(match[2]),shape:"decision"});
    else if((match=line.match(/^(\w+)\s*(-->|-\.->)\s*(?:\|([^|]+)\|\s*)?(\w+)$/))) edges.push({id:"edge-"+edges.length,from:match[1],to:match[4],dashed:match[2]==="-.->",label:clean(match[3]||"")});
    else throw new Error("不支持的流程图语法："+line);
  }
  const byId=new Map(nodes.map((node)=>[node.id,node]));
  if(nodes.length!==byId.size || nodes.length<2) throw new Error("流程图节点重复或不足");
  const graph=new dagre.graphlib.Graph({multigraph:true});
  // 按声明顺序处理回边，保留入口在上、正常步骤向下；贪心断环可能把入口排到末尾。
  graph.setGraph({rankdir:"TB",nodesep:42,edgesep:28,ranksep:65,marginx:44,marginy:48});
  graph.setDefaultEdgeLabel(()=>({}));
  for(const node of nodes) {
    node.w=node.shape==="decision"?300:292;
    node.lines=wrap(node.label,node.shape==="decision"?194:248,16);
    node.h=node.shape==="decision"?Math.max(128,node.lines.length*28+76):Math.max(88,node.lines.length*25+34);
    if(node.shape==="decision") {
      // 菱形上下收窄，按每行真实字宽与行高留白，不能只检查矩形包围盒。
      node.lines.forEach((line,index)=>{
        const halfTextWidth=measure(line,16)/2+10;
        const halfTextHeight=Math.abs(index-(node.lines.length-1)/2)*25+11;
        node.h=Math.max(node.h,Math.ceil(2*halfTextHeight/(1-halfTextWidth/(node.w/2)))+12);
      });
    }
    graph.setNode(node.id,{width:node.w,height:node.h});
  }
  for(const edge of edges) {
    if(!byId.has(edge.from)||!byId.has(edge.to)) throw new Error("流程图引用未知节点："+edge.from+" → "+edge.to);
    edge.lines=wrap(edge.label,155,12);
    const labelWidth=edge.label?Math.max(...edge.lines.map((line)=>measure(line,12)))+16:0;
    graph.setEdge(edge.from,edge.to,{width:labelWidth,height:edge.label?edge.lines.length*18+8:0,labelpos:"c",weight:edge.dashed?1:2},edge.id);
  }
  dagre.layout(graph);
  for(const node of nodes) {
    const position=graph.node(node.id);node.x=position.x-node.w/2;node.y=position.y-node.h/2;
  }
  function adjustDiamond(point,adjacent,node) {
    if(node.shape!=="decision") return point;
    const cx=node.x+node.w/2,cy=node.y+node.h/2,dx=adjacent.x-cx,dy=adjacent.y-cy;
    const divisor=Math.abs(dx)/(node.w/2)+Math.abs(dy)/(node.h/2);
    return {x:cx+dx/divisor,y:cy+dy/divisor};
  }
  for(const edge of edges) {
    const layout=graph.edge({v:edge.from,w:edge.to,name:edge.id});
    const points=layout.points.map((point)=>({x:point.x,y:point.y}));
    points[0]=adjustDiamond(points[0],points[1],byId.get(edge.from));
    points[points.length-1]=adjustDiamond(points.at(-1),points.at(-2),byId.get(edge.to));
    edge.points=points.map((point)=>[point.x,point.y]);
    edge.labelX=layout.x;edge.labelY=layout.y;edge.labelW=layout.width;edge.labelH=layout.height;
  }
  return {kind:"flow",width:Math.ceil(graph.graph().width),height:Math.ceil(graph.graph().height+36),nodes,edges,source};
}
