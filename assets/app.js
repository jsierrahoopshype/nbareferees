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
