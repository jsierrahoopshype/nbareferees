(function(){
  "use strict";
  // --- referee search (filters directory rows by name) ---
  var search=document.getElementById("ref-search");
  if(search){
    var table=document.getElementById("ref-directory");
    var empty=document.getElementById("search-empty");
    var rows=[].slice.call(table.querySelectorAll("tbody .ref-row"));
    search.addEventListener("input",function(){
      var q=search.value.trim().toLowerCase();
      var shown=0;
      rows.forEach(function(r){
        var hit=!q||r.getAttribute("data-name").indexOf(q)!==-1;
        r.style.display=hit?"":"none";
        if(hit)shown++;
      });
      if(empty)empty.hidden=shown!==0;
    });
  }
  // --- navigate-search (top/bottom of ref pages, bottom of index) ---
  var _idxCache={};
  function loadIndex(url){
    if(!_idxCache[url]){
      _idxCache[url]=fetch(url).then(function(r){return r.json();}).catch(function(){return [];});
    }
    return _idxCache[url];
  }
  var TYPE_DIR={ref:"referee",team:"team",player:"player"};
  var TYPE_LABEL={ref:"Ref",team:"Team",player:"Player"};
  function escHtml(s){return String(s).replace(/[&<>]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;'}[c];});}
  [].slice.call(document.querySelectorAll(".refsearch-wrap")).forEach(function(wrap){
    var input=wrap.querySelector(".refsearch");
    var out=wrap.querySelector(".refsearch-results");
    var root=wrap.getAttribute("data-root")||"";
    var url=wrap.getAttribute("data-json");
    var idx=null, active=-1;
    function href(e){return root+TYPE_DIR[e.t]+"/"+e.s+"/index.html";}
    function close(){out.hidden=true;out.innerHTML="";active=-1;}
    function render(q){
      if(!q){close();return;}
      var hits=(idx||[]).filter(function(e){return e.n.toLowerCase().indexOf(q)!==-1;});
      hits.sort(function(a,b){
        var ap=a.n.toLowerCase().indexOf(q)===0?0:1, bp=b.n.toLowerCase().indexOf(q)===0?0:1;
        if(ap!==bp)return ap-bp;
        return a.n.length-b.n.length;
      });
      hits=hits.slice(0,12);
      if(!hits.length){out.innerHTML='<div class="rs-empty">No referee, team or player matches.</div>';out.hidden=false;active=-1;return;}
      out.innerHTML=hits.map(function(e){
        return '<a class="rs-item" href="'+href(e)+'">'+
          '<span class="rs-badge rs-'+e.t+'">'+TYPE_LABEL[e.t]+'</span>'+
          '<span class="rs-name">'+escHtml(e.n)+'</span>'+
          '<span class="rs-meta">'+escHtml(e.u||"")+'</span></a>';
      }).join("");
      out.hidden=false;active=-1;
    }
    function items(){return [].slice.call(out.querySelectorAll(".rs-item"));}
    function setActive(i){var el=items();el.forEach(function(x){x.classList.remove("active");});
      if(i>=0&&i<el.length){active=i;el[i].classList.add("active");el[i].scrollIntoView({block:"nearest"});}}
    input.addEventListener("input",function(){
      var q=input.value.trim().toLowerCase();
      loadIndex(url).then(function(data){idx=data;if(input.value.trim().toLowerCase()===q)render(q);});
    });
    input.addEventListener("keydown",function(e){
      var el=items();
      if(e.key==="ArrowDown"){e.preventDefault();setActive(Math.min(active+1,el.length-1));}
      else if(e.key==="ArrowUp"){e.preventDefault();setActive(Math.max(active-1,0));}
      else if(e.key==="Enter"){var t=active>=0?el[active]:el[0];if(t){e.preventDefault();window.location.href=t.getAttribute("href");}}
      else if(e.key==="Escape"){close();}
    });
    document.addEventListener("click",function(e){if(!wrap.contains(e.target))close();});
  });
  // --- sortable tables ---
  function cellVal(td){
    var s=td.getAttribute("data-sort");
    if(s!==null){var n=parseFloat(s);return isNaN(n)?s.toLowerCase():n;}
    return td.textContent.trim().toLowerCase();
  }
  [].slice.call(document.querySelectorAll(".sortable-table")).forEach(function(table){
    var ths=[].slice.call(table.querySelectorAll("th.sortable"));
    ths.forEach(function(th,col){
      th.addEventListener("click",function(){
        var tbody=table.tBodies[0];
        var rows=[].slice.call(tbody.querySelectorAll("tr"));
        var asc=!th.classList.contains("sort-asc");
        ths.forEach(function(o){o.classList.remove("sort-asc","sort-desc");});
        th.classList.add(asc?"sort-asc":"sort-desc");
        rows.sort(function(a,b){
          var x=cellVal(a.cells[col]),y=cellVal(b.cells[col]);
          if(x<y)return asc?-1:1;
          if(x>y)return asc?1:-1;
          return 0;
        });
        rows.forEach(function(r){tbody.appendChild(r);});
      });
    });
  });
  // --- leaderboard tabs ---
  var tabs=[].slice.call(document.querySelectorAll(".lb-tab"));
  if(tabs.length){
    var panels=[].slice.call(document.querySelectorAll(".lb-panel"));
    tabs.forEach(function(tab){
      tab.addEventListener("click",function(){
        var id=tab.getAttribute("data-tab");
        tabs.forEach(function(t){var on=t===tab;t.classList.toggle("is-active",on);
          t.setAttribute("aria-selected",on?"true":"false");});
        panels.forEach(function(p){p.classList.toggle("is-active",p.getAttribute("data-panel")===id);});
      });
    });
  }
})();
