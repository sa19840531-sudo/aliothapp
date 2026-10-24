/* 同一绘图核心用于离线原生 Canvas 校验和文档阅读；不依赖网络、DOM 或业务运行时。 */
(function(root) {
  "use strict";
  const fonts='"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif';
  function rounded(ctx,x,y,w,h,r) {
    ctx.beginPath();ctx.moveTo(x+r,y);ctx.lineTo(x+w-r,y);ctx.quadraticCurveTo(x+w,y,x+w,y+r);ctx.lineTo(x+w,y+h-r);ctx.quadraticCurveTo(x+w,y+h,x+w-r,y+h);ctx.lineTo(x+r,y+h);ctx.quadraticCurveTo(x,y+h,x,y+h-r);ctx.lineTo(x,y+r);ctx.quadraticCurveTo(x,y,x+r,y);ctx.closePath();
  }
  function arrow(ctx,points,dashed,selected,dim) {
    ctx.save();ctx.globalAlpha=dim?0.28:1;ctx.strokeStyle=selected?"#12695f":dashed?"#866ca4":"#617f91";ctx.fillStyle=ctx.strokeStyle;ctx.lineWidth=selected?2.7:1.7;ctx.lineJoin="round";ctx.lineCap="round";ctx.setLineDash(dashed?[7,5]:[]);
    ctx.beginPath();ctx.moveTo(points[0][0],points[0][1]);for(const point of points.slice(1))ctx.lineTo(point[0],point[1]);ctx.stroke();ctx.setLineDash([]);
    const end=points[points.length-1],previous=points[points.length-2],angle=Math.atan2(end[1]-previous[1],end[0]-previous[0]);
    ctx.beginPath();ctx.moveTo(end[0],end[1]);ctx.lineTo(end[0]-9*Math.cos(angle-0.4),end[1]-9*Math.sin(angle-0.4));ctx.lineTo(end[0]-9*Math.cos(angle+0.4),end[1]-9*Math.sin(angle+0.4));ctx.closePath();ctx.fill();ctx.restore();
  }
  function centeredLines(ctx,lines,x,y,lineHeight,color,size,bold) {
    ctx.save();ctx.font=(bold?"600 ":"")+size+"px "+fonts;ctx.fillStyle=color;ctx.textAlign="center";ctx.textBaseline="middle";
    lines.forEach(function(line,index){ctx.fillText(line,x,y+index*lineHeight);});ctx.restore();
  }
  function flow(ctx,graph,selected) {
    for(const edge of graph.edges) arrow(ctx,edge.points,edge.dashed,edge.id===selected || edge.from===selected || edge.to===selected,Boolean(selected && edge.id!==selected && edge.from!==selected && edge.to!==selected));
    for(const edge of graph.edges) {
      if(!edge.label)continue;
      ctx.save();rounded(ctx,edge.labelX-edge.labelW/2,edge.labelY-edge.labelH/2,edge.labelW,edge.labelH,5);ctx.fillStyle="#fbfcfd";ctx.fill();
      centeredLines(ctx,edge.lines,edge.labelX,edge.labelY-(edge.lines.length-1)*9,18,edge.dashed?"#775a99":"#476474",12,false);ctx.restore();
    }
    for(const node of graph.nodes) {
      const isSelected=node.id===selected,decision=node.shape==="decision",limited=/(拒绝|隔离|禁止|失败|不足|人工|终结)/.test(node.label);
      const accent=decision?"#9c6423":limited?"#876746":"#266d83";
      ctx.save();
      if(decision) {ctx.beginPath();ctx.moveTo(node.x+node.w/2,node.y);ctx.lineTo(node.x+node.w,node.y+node.h/2);ctx.lineTo(node.x+node.w/2,node.y+node.h);ctx.lineTo(node.x,node.y+node.h/2);ctx.closePath();}
      else rounded(ctx,node.x,node.y,node.w,node.h,11);
      ctx.fillStyle=isSelected?"#e6f3ee":decision?"#fff5e5":limited?"#f8f3eb":"#eef5f9";ctx.fill();ctx.strokeStyle=isSelected?"#12695f":decision?"#d5b17c":limited?"#cfbca2":"#aac6d4";ctx.lineWidth=isSelected?2.7:1.15;ctx.stroke();
      if(!decision){ctx.fillStyle=accent;rounded(ctx,node.x+12,node.y+18,4,node.h-36,2);ctx.fill();}
      centeredLines(ctx,node.lines,node.x+node.w/2,node.y+node.h/2-(node.lines.length-1)*12.5,25,"#213a46",16,false);
      ctx.restore();
    }
  }
  function sequence(ctx,graph,selected) {
    for(const participant of graph.participants) {
      ctx.save();ctx.strokeStyle="#c6d2d9";ctx.lineWidth=1.2;ctx.setLineDash([4,5]);ctx.beginPath();ctx.moveTo(participant.x,participant.y+participant.h);ctx.lineTo(participant.x,graph.height-57);ctx.stroke();ctx.setLineDash([]);
      rounded(ctx,participant.x-participant.w/2,participant.y,participant.w,participant.h,9);ctx.fillStyle="#edf4fa";ctx.fill();ctx.strokeStyle="#aac1d4";ctx.stroke();
      centeredLines(ctx,participant.lines,participant.x,participant.y+participant.h/2-(participant.lines.length-1)*10.5,21,"#23465b",15,true);
      rounded(ctx,participant.x-participant.w/2,graph.height-58,participant.w,40,7);ctx.fillStyle="#f0f5f8";ctx.fill();ctx.strokeStyle="#c6d2d9";ctx.stroke();
      centeredLines(ctx,[participant.label],participant.x,graph.height-38,20,"#617984",12,false);ctx.restore();
    }
    for(const event of graph.events) {
      if(event.kind==="note") {
        ctx.save();rounded(ctx,event.x,event.y,event.w,event.h,7);ctx.fillStyle="#fff7e8";ctx.fill();ctx.strokeStyle="#dac394";ctx.stroke();
        centeredLines(ctx,event.lines,event.x+event.w/2,event.y+event.h/2-(event.lines.length-1)*10.5,21,"#7a613b",14,false);ctx.restore();continue;
      }
      const active=event.id===selected;
      if(active){ctx.fillStyle="#eaf4ef";ctx.fillRect(24,event.textY-15,graph.width-48,event.y-event.textY+(event.self?49:24));}
      arrow(ctx,event.points,event.dashed,active,Boolean(selected && !active));
      const lineWidth=Math.max.apply(null,event.lines.map(function(line){ctx.font="14px "+fonts;return ctx.measureText(line).width;}))+14;
      ctx.fillStyle=active?"#eaf4ef":"#fbfcfd";ctx.fillRect(event.textX-lineWidth/2,event.textY-11,lineWidth,event.lines.length*21);
      centeredLines(ctx,event.lines,event.textX,event.textY,21,event.dashed?"#795d99":"#2b5265",14,false);
      ctx.save();ctx.beginPath();ctx.arc(Math.min(event.x1,event.x2)-17,event.y,10,0,Math.PI*2);ctx.fillStyle=active?"#12695f":"#e4edf2";ctx.fill();centeredLines(ctx,[String(event.number)],Math.min(event.x1,event.x2)-17,event.y,16,active?"#fff":"#547381",10,true);ctx.restore();
    }
  }
  function draw(ctx,graph,selected) {
    ctx.save();ctx.clearRect(0,0,graph.width,graph.height);ctx.fillStyle="#fbfcfd";ctx.fillRect(0,0,graph.width,graph.height);
    ctx.fillStyle="#dfe8ed";for(let x=18;x<graph.width;x+=24)for(let y=18;y<graph.height;y+=24){ctx.beginPath();ctx.arc(x,y,0.6,0,Math.PI*2);ctx.fill();}
    if(graph.kind==="sequence")sequence(ctx,graph,selected);else flow(ctx,graph,selected);
    ctx.restore();
  }
  function items(graph) {
    if(graph.kind==="sequence")return graph.events.filter(function(event){return event.kind==="message";}).map(function(event){
      const byId=new Map(graph.participants.map(function(participant){return[participant.id,participant.label];}));
      return {id:event.id,label:String(event.number).padStart(2,"0")+" "+event.label,detail:byId.get(event.from)+" → "+byId.get(event.to)+"："+event.label+(event.dashed?"；虚线回执/消息，具体语义以正文为准。":"；交接条件及失败分支见图后正文。"),x:event.textX,y:event.textY};
    });
    return graph.nodes.map(function(node){
      const incoming=graph.edges.filter(function(edge){return edge.to===node.id;}).map(function(edge){return graph.nodes.find(function(item){return item.id===edge.from;}).label.replace(/\n/g," / ")+"："+(edge.label||"交接");});
      const outgoing=graph.edges.filter(function(edge){return edge.from===node.id;}).map(function(edge){return(edge.label||"交接")+" → "+graph.nodes.find(function(item){return item.id===edge.to;}).label.replace(/\n/g," / ");});
      const detail=graph.relationDiagram?"关联到本对象："+(incoming.join("；")||"本图归属入口")+"。本对象关联："+(outgoing.join("；")||"其他约束见正文")+"。箭头表达引用或包含，不是执行时序。":"进入："+(incoming.join("；")||"流程入口")+"。继续："+(outgoing.join("；")||"本图终点，终态约束见正文")+"。";
      return {id:node.id,label:node.label.replace(/\n/g," / "),detail:detail,x:node.x+node.w/2,y:node.y};
    });
  }
  function hit(graph,x,y) {
    if(graph.kind==="sequence")return graph.events.find(function(event){return event.kind==="message" && x>=event.hitLeft && x<=event.hitRight && y>=event.textY-15 && y<=event.y+(event.self?32:12);})?.id || null;
    return graph.nodes.find(function(node){
      if(node.shape==="decision")return Math.abs(x-node.x-node.w/2)/(node.w/2)+Math.abs(y-node.y-node.h/2)/(node.h/2)<=1;
      return x>=node.x && x<=node.x+node.w && y>=node.y && y<=node.y+node.h;
    })?.id || null;
  }
  root.AliothTechnicalCanvas=Object.freeze({draw:draw,items:items,hit:hit,fonts:fonts});
})(globalThis);
