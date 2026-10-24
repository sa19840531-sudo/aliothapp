/* 文档交互只控制阅读状态；不会请求网络、访问设备或改变业务数据。 */
(function() {
  "use strict";
  const core=globalThis.AliothTechnicalCanvas,data=JSON.parse(document.getElementById("technical-diagram-data").textContent),states=[];
  for(const figure of document.querySelectorAll("[data-technical-diagram]")) {
    const graph=data[figure.dataset.technicalDiagram],canvas=figure.querySelector("canvas"),viewport=figure.querySelector(".technical-viewport"),ctx=canvas.getContext("2d");
    if(!ctx)continue;
    const picker=figure.querySelector("select"),output=figure.querySelector("output"),detail=figure.querySelector(".technical-detail");
    const choices=core.items(graph),state={figure,canvas,ctx,viewport,graph,scale:1,selected:null,manual:false};states.push(state);
    const all=document.createElement("option");all.value="";all.textContent=graph.kind==="sequence"?"查看全部消息":"查看全部组件 / 节点";picker.append(all);
    for(const item of choices){const option=document.createElement("option");option.value=item.id;option.textContent=item.label;picker.append(option);}
    function paint() {
      // 高分屏大图限制单张位图面积，避免放大时无界申请内存；CSS 阅读比例不受影响。
      const ratio=Math.min(window.devicePixelRatio||1,2,Math.sqrt(16000000/(graph.width*graph.height*state.scale*state.scale)));
      canvas.width=Math.ceil(graph.width*state.scale*ratio);canvas.height=Math.ceil(graph.height*state.scale*ratio);
      canvas.style.width=graph.width*state.scale+"px";canvas.style.height=graph.height*state.scale+"px";
      ctx.setTransform(ratio*state.scale,0,0,ratio*state.scale,0,0);core.draw(ctx,graph,state.selected);
      output.textContent=Math.round(state.scale*100)+"%";
      figure.querySelector("[data-minus]").disabled=state.scale<=0.2;figure.querySelector("[data-plus]").disabled=state.scale>=1.6;
    }
    function fit(readable) {
      const target=Math.min(1,(Math.max(260,viewport.clientWidth)-1)/graph.width);
      state.scale=readable?Math.max(0.8,target):Math.max(0.2,target);viewport.scrollLeft=0;viewport.scrollTop=0;paint();
    }
    function select(id,jump) {
      const item=choices.find(function(value){return value.id===id;});state.selected=item?item.id:null;picker.value=item?item.id:"";
      detail.hidden=!item;detail.replaceChildren();
      if(item){const heading=document.createElement("strong"),paragraph=document.createElement("p");heading.textContent=item.label;paragraph.textContent=item.detail;detail.append(heading,paragraph);if(jump){viewport.scrollTop=Math.max(0,(item.y-36)*state.scale);viewport.scrollLeft=Math.max(0,item.x*state.scale-viewport.clientWidth/2);}}
      paint();
    }
    function zoom(delta) {
      state.manual=true;const previous=state.scale,cx=(viewport.scrollLeft+viewport.clientWidth/2)/previous,cy=(viewport.scrollTop+viewport.clientHeight/2)/previous;
      state.scale=Math.max(0.2,Math.min(1.6,Math.round((state.scale+delta)*100)/100));paint();viewport.scrollLeft=Math.max(0,cx*state.scale-viewport.clientWidth/2);viewport.scrollTop=Math.max(0,cy*state.scale-viewport.clientHeight/2);
    }
    canvas.addEventListener("click",function(event){const bounds=canvas.getBoundingClientRect();select(core.hit(graph,(event.clientX-bounds.left)*graph.width/bounds.width,(event.clientY-bounds.top)*graph.height/bounds.height),false);});
    picker.addEventListener("change",function(){select(picker.value,true);});
    figure.querySelector("[data-minus]").addEventListener("click",function(){zoom(-0.1);});
    figure.querySelector("[data-plus]").addEventListener("click",function(){zoom(0.1);});
    figure.querySelector("[data-fit]").addEventListener("click",function(){state.manual=true;fit(false);});
    figure.querySelector("[data-size]").addEventListener("click",function(){state.manual=true;state.scale=1;paint();});
    figure.querySelector(".technical-controls").hidden=false;figure.querySelector(".technical-source").open=false;
    state.paint=paint;state.fit=fit;fit(true);
  }
  let frame=0;
  window.addEventListener("resize",function(){cancelAnimationFrame(frame);frame=requestAnimationFrame(function(){for(const state of states)state.manual?state.paint():state.fit(true);});});
  if(document.fonts && document.fonts.ready)document.fonts.ready.then(function(){for(const state of states)state.paint();});
  window.addEventListener("beforeprint",function(){for(const state of states){state.previousScale=state.scale;state.scale=1;state.paint();}});
  window.addEventListener("afterprint",function(){for(const state of states){state.scale=state.previousScale||state.scale;state.paint();}});
  document.documentElement.dataset.technicalCanvasReady=String(states.length);
})();
