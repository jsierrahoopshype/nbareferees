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
    var typeFilter=wrap.getAttribute("data-type-filter");   // e.g. "ref" -- comparator boxes
    var compareSlot=wrap.getAttribute("data-compare");      // "a" or "b" -- comparator boxes
    var idx=null, active=-1;
    function href(e){
      if(compareSlot){
        var params=new URLSearchParams(window.location.search);
        params.set(compareSlot,e.s);
        return "?"+params.toString();
      }
      return root+TYPE_DIR[e.t]+"/"+e.s+"/index.html";
    }
    function close(){out.hidden=true;out.innerHTML="";active=-1;}
    function render(q){
      if(!q){close();return;}
      var pool=typeFilter?(idx||[]).filter(function(e){return e.t===typeFilter;}):(idx||[]);
      var hits=pool.filter(function(e){return e.n.toLowerCase().indexOf(q)!==-1;});
      hits.sort(function(a,b){
        var ap=a.n.toLowerCase().indexOf(q)===0?0:1, bp=b.n.toLowerCase().indexOf(q)===0?0:1;
        if(ap!==bp)return ap-bp;
        return a.n.length-b.n.length;
      });
      hits=hits.slice(0,12);
      if(!hits.length){
        out.innerHTML='<div class="rs-empty">No '+(typeFilter?"referee":"referee, team or player")+' matches.</div>';
        out.hidden=false;active=-1;return;
      }
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
  // Scoped PER .lb-tabs container (its .lb-panels sibling), not globally --
  // a page can carry more than one independent tab group (e.g. the index's
  // Career-leaders tabs AND its separate Era-leaders tabs), and a single
  // shared tabs/panels array would cross-wire them: clicking a tab in one
  // group would deactivate every tab in the OTHER group too, with no
  // matching panel id to reactivate, leaving it blank.
  [].slice.call(document.querySelectorAll(".lb-tabs")).forEach(function(tabsEl){
    var tabs=[].slice.call(tabsEl.querySelectorAll(".lb-tab"));
    var panelsEl=tabsEl.nextElementSibling;
    var panels=panelsEl?[].slice.call(panelsEl.querySelectorAll(".lb-panel")):[];
    tabs.forEach(function(tab){
      tab.addEventListener("click",function(){
        var id=tab.getAttribute("data-tab");
        tabs.forEach(function(t){var on=t===tab;t.classList.toggle("is-active",on);
          t.setAttribute("aria-selected",on?"true":"false");});
        panels.forEach(function(p){p.classList.toggle("is-active",p.getAttribute("data-panel")===id);});
      });
    });
  });
  // --- dashboard: spotlight of the day + on this date (index only) ---
  // Rotation is deterministic client-side: day-of-year modulo the spotlight
  // array (ordered by slug at build time for a stable rotation). The data
  // itself ships inline in the page (real content in the HTML); only the
  // day-dependent SELECTION runs in JS, since a statically-built site can't
  // otherwise know the viewer's "today".
  var WHISTLE_LABELS={avg_total_points:"Combined points",avg_total_fta:"Combined free-throw attempts",
    avg_total_pf:"Combined personal fouls",home_win_pct:"Home team win rate",ot_rate:"Games to overtime"};
  var WHISTLE_ISPCT={home_win_pct:1,ot_rate:1};
  function fmtWhistle(key,v){return WHISTLE_ISPCT[key]?(v*100).toFixed(1)+"%":v.toFixed(1);}
  var dashData=document.getElementById("dashboard-rotation-data");
  if(dashData){
    try{
      var dash=JSON.parse(dashData.textContent);
      var spotlight=dash.spotlight||[];
      var dateIndex=dash.date_index||{};
      var now=new Date();
      var startOfYear=new Date(now.getFullYear(),0,0);
      var doy=Math.floor((now-startOfYear)/86400000);

      var spotCard=document.getElementById("spotlight-card");
      if(spotCard&&spotlight.length){
        var pick=spotlight[doy%spotlight.length];
        var sigHtml;
        if(pick.signature){
          var sig=pick.signature, dir=sig.pctile>=50?"higher":"lower",
            pctShow=(sig.pctile>=50?sig.pctile:(100-sig.pctile)).toFixed(0),
            label=WHISTLE_LABELS[sig.key]||sig.key, val=fmtWhistle(sig.key,sig.value);
          sigHtml='<p class="spotlight-sig">'+escHtml(label)+": "+escHtml(val)+" — "+dir+
            " than "+pctShow+"% of qualifying officials (n="+sig.n+").</p>";
        }else{
          sigHtml='<p class="spotlight-sig">'+pick.seasons_active+" seasons officiating, "+
            pick.games_total+" career games.</p>";
        }
        var badge=pick.active?' <span class="badge badge-active">Active</span>':"";
        spotCard.innerHTML='<a class="spotlight-name" href="referee/'+pick.slug+'/index.html">'+
          escHtml(pick.name)+'</a>'+badge+
          '<p class="spotlight-meta">'+pick.games_total+' games &middot; '+pick.first_season+'–'+pick.last_season+'</p>'+sigHtml;
      }

      var mm=("0"+(now.getMonth()+1)).slice(-2), dd=("0"+now.getDate()).slice(-2);
      var entry=dateIndex[mm+"-"+dd];
      var onDate=document.getElementById("ondate-card");
      if(onDate&&entry){
        var playerBit=entry.player_slug
          ?'<a href="player/'+entry.player_slug+'/index.html">'+escHtml(entry.player_name)+'</a>'
          :escHtml(entry.player_name);
        var fallbackNote=entry.month_day===(mm+"-"+dd)?"":
          ' <span class="caption">(nearest date with games on record; from '+entry.date+')</span>';
        onDate.innerHTML='<span class="history-pts">'+entry.pts+'</span> '+playerBit+' '+
          escHtml(entry.team_abbr)+' <span class="vs">vs</span> '+escHtml(entry.opp_abbr)+
          ' — '+escHtml(entry.date)+fallbackNote;
      }
    }catch(e){/* dashboard rotation is decorative -- fail silently */}
  }
  // --- Tonight's Crews (September pipeline; absent all season until then) ---
  // Expected data/tonights-crews.json schema once the pipeline ships:
  //   {date, games:[{away, home, tipoff_et, crew:[{name, slug}], crew_note}]}
  // Absent, unparseable, or dated anything other than today/yesterday (US
  // Eastern time, since that's the NBA's scheduling clock) -- render NOTHING,
  // not even a placeholder.
  (function(){
    var slot=document.getElementById("tonights-crews");
    if(!slot)return;
    function usEasternISO(offsetDays){
      var d=new Date(Date.now()+offsetDays*86400000);
      var parts=new Intl.DateTimeFormat("en-CA",{timeZone:"America/New_York",
        year:"numeric",month:"2-digit",day:"2-digit"}).formatToParts(d);
      var o={};parts.forEach(function(p){o[p.type]=p.value;});
      return o.year+"-"+o.month+"-"+o.day;
    }
    fetch("data/tonights-crews.json").then(function(r){
      if(!r.ok)throw new Error("absent");
      return r.json();
    }).then(function(data){
      var valid=data&&data.date&&(data.date===usEasternISO(0)||data.date===usEasternISO(-1));
      if(!valid||!Array.isArray(data.games)||!data.games.length)return;
      var body=slot.querySelector("#tonights-crews-body");
      body.innerHTML=data.games.map(function(g){
        var crew=(g.crew||[]).map(function(c){
          return '<a href="referee/'+c.slug+'/index.html">'+escHtml(c.name)+'</a>';
        }).join(", ");
        var note=g.crew_note?' <span class="caption">'+escHtml(g.crew_note)+'</span>':"";
        return '<div class="crew-game"><span class="crew-matchup">'+escHtml(g.away)+' @ '+escHtml(g.home)+'</span>'+
          '<span class="crew-tip">'+escHtml(g.tipoff_et||"")+'</span>'+
          '<span class="crew-names">'+crew+'</span>'+note+'</div>';
      }).join("");
      slot.hidden=false;
    }).catch(function(){/* absent or unparseable -- render nothing, by design */});
  })();
  // --- comparator (/compare/) -- reads existing data/referees/{slug}.json
  // client-side, keyed off the ?a=/?b= query string so any pair is shareable
  // without pre-rendering the ~159*158/2 possible combinations. ---
  (function(){
    var colA=document.getElementById("compare-col-a"), colB=document.getElementById("compare-col-b");
    if(!colA||!colB)return;
    var WHISTLE_ALL_LABELS={avg_total_points:"Combined points",avg_total_fta:"Combined free-throw attempts",
      avg_total_pf:"Combined personal fouls",avg_abs_margin:"Avg. margin of victory",
      home_win_pct:"Home team win rate",ot_rate:"Games to overtime"};
    var WHISTLE_ALL_ISPCT={home_win_pct:1,ot_rate:1};
    function fmtWhistleAll(key,v){return WHISTLE_ALL_ISPCT[key]?(v*100).toFixed(1)+"%":v.toFixed(1);}
    function fmtDiffAll(key,v){
      if(v==null)return "—";
      var sign=v>=0?"+":"";
      return WHISTLE_ALL_ISPCT[key]?sign+(v*100).toFixed(1)+"%":sign+v.toFixed(1);
    }
    function ordinalAll(n){
      if(n==null)return "—";
      n=Math.trunc(n);
      var m10=n%10,m100=n%100;
      var suf=(m10===1&&m100!==11)?"st":(m10===2&&m100!==12)?"nd":(m10===3&&m100!==13)?"rd":"th";
      return n+suf;
    }
    function diffRankLabelAll(rank,total,diff){
      if(!total)return "ranking not available";
      if(rank==null)return "not enough games to rank";
      if(total<=1)return "only qualifying official";
      if(diff!=null&&diff<0)return ordinalAll(total-rank+1)+" lowest of "+total;
      return ordinalAll(rank)+" highest of "+total;
    }
    function intensityClass(pctile){
      if(pctile==null)return"";
      var d=Math.abs(pctile-50),lvl=d>=40?4:d>=30?3:d>=20?2:d>=10?1:0;
      return "wm-i"+lvl;
    }
    function whistleColHtml(kindLabel,w){
      if(!w||!w.n)return "";
      var keys=["avg_total_points","avg_total_fta","avg_total_pf","avg_abs_margin","home_win_pct","ot_rate"];
      var nMap={avg_total_points:w.n,avg_total_fta:w.n_boxscore,avg_total_pf:w.n_boxscore,
        avg_abs_margin:w.n,home_win_pct:w.n,ot_rate:w.n_boxscore};
      var exp=w.expected||{}, dif=w.differential||{};
      var cells=keys.map(function(k){
        var v=w[k],cls=intensityClass(w[k+"_pctile"]),vs=(v==null)?"—":fmtWhistleAll(k,v);
        var lg=exp[k],lgs=(lg==null)?"—":fmtWhistleAll(k,lg);
        var d=(dif[k]==null)?null:dif[k],ds=fmtDiffAll(k,d);
        var rankTxt=diffRankLabelAll(w[k+"_rank"],w[k+"_qualifying"],d);
        return '<div class="wm '+cls+'"><div class="wm-val">'+vs+' <span class="wm-lg">lg '+lgs+
          '</span> <span class="wm-diff">'+ds+'</span></div>'+
          '<div class="wm-label">'+WHISTLE_ALL_LABELS[k]+'</div>'+
          '<div class="wm-rank">'+rankTxt+'</div>'+
          '<div class="wm-n">n = '+(nMap[k]||0)+'</div></div>';
      }).join("");
      return '<div class="whistle-col"><h3 class="whistle-kind">'+kindLabel+
        ' <span class="whistle-n">'+(w.n||0)+' games</span></h3><div class="whistle-grid">'+cells+'</div></div>';
    }
    function teamExtremes(records){
      if(!records||!records.length)return '<p class="empty-note">No team records on file.</p>';
      var byGames=records.slice().sort(function(a,b){return b.games-a.games;})[0];
      var qualifying=records.filter(function(r){return r.games>=10&&r.win_pct!=null;});
      var byWin=qualifying.length?qualifying.slice().sort(function(a,b){return b.win_pct-a.win_pct;})[0]:null;
      var out='<p class="compare-line">Most games: '+escHtml(byGames.team_abbr)+' ('+byGames.games+' games)</p>';
      if(byWin)out+='<p class="compare-line">Best record: '+escHtml(byWin.team_abbr)+' ('+
        (byWin.win_pct*100).toFixed(1)+'%, '+byWin.games+' games)</p>';
      return out;
    }
    function renderCompareCol(container,doc){
      var s=doc.summary;
      var badge=s.active?' <span class="badge badge-active">Active</span>':"";
      container.innerHTML=
        '<a class="spotlight-name" href="../referee/'+s.slug+'/index.html">'+escHtml(s.name)+'</a>'+badge+
        '<p class="spotlight-meta">'+s.games_total+' games &middot; '+s.first_season+'–'+s.last_season+
        ' &middot; RS '+s.games_rs+' &middot; PO '+s.games_po+' &middot; Finals '+s.finals_games+
        ' &middot; G7s '+s.game7s+'</p>'+
        '<div class="whistle-cols">'+whistleColHtml("Regular season",doc.whistle_profile.rs)+
        whistleColHtml("Playoffs",doc.whistle_profile.po)+'</div>'+
        '<h3 class="lb-subhead">Team records</h3>'+teamExtremes(doc.team_records);
    }
    var params=new URLSearchParams(window.location.search);
    var aSlug=params.get("a"), bSlug=params.get("b");
    var promptEl=document.getElementById("compare-prompt");
    if(promptEl)promptEl.hidden=!!(aSlug||bSlug);
    function loadCol(container,slug){
      if(!slug){container.innerHTML='<p class="empty-note">Select a referee above.</p>';return;}
      fetch("../data/referees/"+slug+".json").then(function(r){
        if(!r.ok)throw new Error("not found");
        return r.json();
      }).then(function(doc){renderCompareCol(container,doc);})
        .catch(function(){container.innerHTML='<p class="empty-note">Referee not found.</p>';});
    }
    loadCol(colA,aSlug);
    loadCol(colB,bSlug);
  })();
})();
