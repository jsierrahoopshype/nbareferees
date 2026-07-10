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
  var _refCache={};
  function loadRefs(url){
    if(!_refCache[url]){
      _refCache[url]=fetch(url).then(function(r){return r.json();}).catch(function(){return [];});
    }
    return _refCache[url];
  }
  function calYears(r){
    var a=String(r.first_season).slice(0,4), b=parseInt(String(r.last_season).slice(0,4),10)+1;
    return a+"-"+b;
  }
  [].slice.call(document.querySelectorAll(".refsearch-wrap")).forEach(function(wrap){
    var input=wrap.querySelector(".refsearch");
    var out=wrap.querySelector(".refsearch-results");
    var base=wrap.getAttribute("data-refbase");
    var url=wrap.getAttribute("data-json");
    var refs=null, active=-1;
    function href(r){return base+r.slug+"/index.html";}
    function close(){out.hidden=true;out.innerHTML="";active=-1;}
    function render(q){
      if(!q){close();return;}
      var hits=(refs||[]).filter(function(r){return r.name.toLowerCase().indexOf(q)!==-1;}).slice(0,12);
      if(!hits.length){out.innerHTML='<div class="rs-empty">No referee matches that name.</div>';out.hidden=false;active=-1;return;}
      out.innerHTML=hits.map(function(r){
        return '<a class="rs-item" href="'+href(r)+'"><span class="rs-name">'+
          r.name.replace(/[&<>]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;'}[c];})+
          '</span><span class="rs-meta">'+r.games_total.toLocaleString()+' g · '+calYears(r)+'</span></a>';
      }).join("");
      out.hidden=false;active=-1;
    }
    function items(){return [].slice.call(out.querySelectorAll(".rs-item"));}
    function setActive(i){var el=items();el.forEach(function(x){x.classList.remove("active");});
      if(i>=0&&i<el.length){active=i;el[i].classList.add("active");el[i].scrollIntoView({block:"nearest"});}}
    input.addEventListener("input",function(){
      var q=input.value.trim().toLowerCase();
      loadRefs(url).then(function(data){refs=data;if(input.value.trim().toLowerCase()===q)render(q);});
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
