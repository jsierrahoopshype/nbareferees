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
  // Exposed on window so content injected after page load (the /matchup/
  // page's client-fetched tables -- everything else on the site is
  // server-rendered before this runs at DOMContentLoaded) can wire up the
  // same sort behavior on demand instead of duplicating it.
  function cellVal(td){
    var s=td.getAttribute("data-sort");
    if(s!==null){var n=parseFloat(s);return isNaN(n)?s.toLowerCase():n;}
    return td.textContent.trim().toLowerCase();
  }
  function initSortableTables(root){
    [].slice.call((root||document).querySelectorAll(".sortable-table")).forEach(function(table){
      if(table.__sortInit)return;
      table.__sortInit=true;
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
  }
  window.initSortableTables=initSortableTables;
  initSortableTables(document);
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
  // --- Tonight's Officials -- the page's promoted anchor module. The server
  // already rendered a non-empty fallback (the most recent real game day on
  // record, from data/dashboard.json's latest_game_day). This only OVERWRITES
  // that fallback when a live data/tonights-crews.json (September pipeline;
  // doesn't exist yet, and only ever covers in-season days once it does) is
  // present AND dated today/yesterday (US Eastern, the NBA's scheduling
  // clock) -- so the module switches to live assignments automatically
  // whenever they show up, with no further work, and otherwise the fallback
  // just stands as rendered.
  // Expected data/tonights-crews.json schema once the pipeline ships:
  //   {date, games:[{away, home, tipoff_et, crew:[{name, slug}], crew_note}]}
  (function(){
    var body=document.getElementById("tonight-officials-body");
    if(!body)return;
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
      if(!valid||!Array.isArray(data.games)||!data.games.length)return;  // keep the fallback
      var heading=document.getElementById("tonight-heading");
      var sub=document.getElementById("tonight-sub");
      if(heading)heading.textContent="Tonight's officials";
      if(sub)sub.textContent=data.date;
      body.innerHTML=data.games.map(function(g){
        var crew=(g.crew||[]).map(function(c){
          return '<a href="referee/'+c.slug+'/index.html">'+escHtml(c.name)+'</a>';
        }).join(", ");
        var note=g.crew_note?' <span class="caption">'+escHtml(g.crew_note)+'</span>':"";
        return '<div class="crew-game"><span class="crew-matchup">'+escHtml(g.away)+' @ '+escHtml(g.home)+'</span>'+
          '<span class="crew-tip">'+escHtml(g.tipoff_et||"")+'</span>'+
          '<span class="crew-names">'+crew+'</span>'+note+'</div>';
      }).join("");
    }).catch(function(){/* absent, unparseable, or stale -- keep the fallback */});
  })();
  // --- Recent form spotlight (data/recent_form_dashboard.json) -- gated the
  // same way Tonight's Crews is: absent, empty, off-season (in_season:false),
  // or a build whose as_of has gone stale against the viewer's own clock all
  // render nothing at all, not a placeholder. The as_of freshness check is
  // belt-and-suspenders on top of build.py's own in_season flag -- it stops a
  // build made during the season from still showing that season's spotlight
  // to someone browsing months later, off-season, before the site is rebuilt.
  (function(){
    var slot=document.getElementById("recent-form-spotlight");
    if(!slot)return;
    var RF_STAT_LABELS={avg_total_points:"combined points",avg_total_fta:"combined FTAs",
      avg_total_pf:"combined fouls",avg_abs_margin:"avg. margin",home_win_pct:"home win rate",
      ot_rate:"OT rate"};
    var RF_ISPCT={home_win_pct:1,ot_rate:1};
    function fmtRf(key,v){return RF_ISPCT[key]?(v*100).toFixed(1)+"%":v.toFixed(1);}
    function fmtRfDiff(key,v){
      var sign=v>=0?"+":"";
      return RF_ISPCT[key]?sign+(v*100).toFixed(1)+"%":sign+v.toFixed(1);
    }
    fetch("data/recent_form_dashboard.json").then(function(r){
      if(!r.ok)throw new Error("absent");
      return r.json();
    }).then(function(data){
      if(!data||!data.in_season||!Array.isArray(data.spotlight)||!data.spotlight.length)return;
      var asOf=new Date(data.as_of+"T00:00:00Z");
      var ageDays=(Date.now()-asOf.getTime())/86400000;
      if(!(ageDays>=-1&&ageDays<=5))return;   // stale build -- render nothing
      var body=slot.querySelector("#recent-form-spotlight-body");
      body.innerHTML='<div class="record-strip">'+data.spotlight.map(function(s){
        return '<a class="record-item" href="referee/'+s.slug+'/index.html">'+
          '<span class="record-label">'+escHtml(s.name)+'</span>'+
          '<span class="record-val">'+fmtRf(s.stat,s.value)+
          ' <span class="wm-diff">'+fmtRfDiff(s.stat,s.diff)+'</span></span>'+
          '<span class="record-ref">'+escHtml(s.window_label)+' &middot; '+
          escHtml(RF_STAT_LABELS[s.stat]||s.stat)+'</span></a>';
      }).join("")+'</div>';
      slot.hidden=false;
    }).catch(function(){/* absent, unparseable, or stale -- render nothing, by design */});
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
  // --- /matchup/ team x referee lookup (docs/MATCHUP_SPEC.md) -- reads
  // data/matchups/{official_id}.json (career + per-season, RS/PO) and
  // data/referee_games/{official_id}.json (the auditable log, filtered
  // client-side to the selected team) keyed off ?team=&ref=. ---
  (function(){
    var resultEl=document.getElementById("matchup-result");
    if(!resultEl)return;
    var MU_SUPPRESS=3, MU_FLAG=10;
    var promptEl=document.getElementById("matchup-prompt");
    var params=new URLSearchParams(window.location.search);
    var teamSlug=(params.get("team")||"").toLowerCase();
    var refSlug=params.get("ref");
    // Prefilled from a referee or team page: the picker itself must show
    // which one is already chosen, not just silently use it -- a reader
    // arriving via a "Team matchups" link should see their referee's name
    // sitting in the box, not two blank pickers.
    var teamInput=document.querySelector('.refsearch-wrap[data-compare="team"] .refsearch');
    var refInput=document.querySelector('.refsearch-wrap[data-compare="ref"] .refsearch');

    function fmtPct(v){return (v*100).toFixed(1)+"%";}
    function fmtDec(v){return v.toFixed(1);}
    function fmtSigned(v){return (v>=0?"+":"")+v.toFixed(1);}

    // One stat cell in the whistle-profile card shell (.wm/.wm-val/.wm-label/
    // .wm-n, same markup referee pages already use). games is what gates
    // suppression for every stat; n is the sample actually backing THIS
    // number (n_box for FTA/PF, since box scores can be missing even when
    // games themselves are on record) -- the two can differ, and the
    // suppressed-state message says which one is short.
    function wmCell(label,val,fmt,games,n){
      var body;
      if(val==null){
        var reason=games<MU_SUPPRESS
          ?("too few games (n="+games+", min "+MU_SUPPRESS+")")
          :("box score unavailable for these games (n="+n+")");
        body='<div class="wm-val wm-suppressed">'+escHtml(reason)+'</div>';
      }else{
        var flag=n<MU_FLAG?' <span class="mu-flag" title="Fewer than '+MU_FLAG+
          ' games back this number (n='+n+') -- shown, but a small sample.">small sample</span>':"";
        body='<div class="wm-val">'+fmt(val)+flag+'</div>';
      }
      return '<div class="wm">'+body+'<div class="wm-label">'+escHtml(label)+'</div>'+
        '<div class="wm-n">n = '+n+'</div></div>';
    }
    function matchupBlock(kindLabel,b){
      if(!b||!b.games)return "";
      var winTxt=b.win_pct!=null?fmtPct(b.win_pct):
        (b.games<MU_SUPPRESS?("too few games (n="+b.games+")"):"—");
      var header=b.wins+"-"+b.losses+" ("+winTxt+")"+
        " &middot; Home "+b.home_games+" ("+b.home_wins+"-"+(b.home_games-b.home_wins)+")"+
        " &middot; Road "+b.away_games+" ("+b.away_wins+"-"+(b.away_games-b.away_wins)+")";
      var cells=[
        ["Avg. margin",b.avg_margin,fmtSigned,b.games,b.games],
        ["Points for",b.avg_pts_for,fmtDec,b.games,b.games],
        ["Points against",b.avg_pts_against,fmtDec,b.games,b.games],
        ["Team FTA",b.avg_team_fta,fmtDec,b.games,b.n_box],
        ["Opponent FTA",b.avg_opp_fta,fmtDec,b.games,b.n_box],
        ["Team fouls",b.avg_team_pf,fmtDec,b.games,b.n_box],
        ["Opponent fouls",b.avg_opp_pf,fmtDec,b.games,b.n_box]
      ].map(function(c){return wmCell(c[0],c[1],c[2],c[3],c[4]);}).join("");
      return '<div class="whistle-col"><h3 class="whistle-kind">'+escHtml(kindLabel)+
        ' <span class="whistle-n">'+b.games+' games</span></h3>'+
        '<p class="mu-record-line">'+header+'</p>'+
        '<div class="whistle-grid">'+cells+'</div></div>';
    }
    // Table-cell twin of wmCell for the season-by-season table -- same
    // suppress/flag policy, td markup instead of a card.
    function muTd(label,val,fmt,games,n){
      if(val==null){
        var reason=games<MU_SUPPRESS
          ?("too few games (n="+games+", min "+MU_SUPPRESS+")")
          :("box score unavailable (n="+n+")");
        return '<td data-label="'+escHtml(label)+'" data-sort="-999999">'+
          '<span class="mu-td-suppressed" title="'+escHtml(reason)+'">'+escHtml(reason)+'</span></td>';
      }
      var flag=n<MU_FLAG?' <span class="mu-flag" title="Fewer than '+MU_FLAG+
        ' games back this number (n='+n+')">small</span>':"";
      return '<td data-label="'+escHtml(label)+'" data-sort="'+val+'">'+fmt(val)+flag+'</td>';
    }
    function seasonRow(seasonLabel,sortKey,kindLabel,b){
      if(!b||!b.games)return "";
      return "<tr>"+
        '<td data-label="Season" data-sort="'+escHtml(sortKey)+'">'+escHtml(seasonLabel)+'</td>'+
        '<td data-label="Type">'+kindLabel+'</td>'+
        '<td data-label="Games" data-sort="'+b.games+'">'+b.games+'</td>'+
        '<td data-label="W-L">'+b.wins+'-'+b.losses+'</td>'+
        muTd("Win%",b.win_pct,fmtPct,b.games,b.games)+
        muTd("Margin",b.avg_margin,fmtSigned,b.games,b.games)+
        muTd("Team FTA",b.avg_team_fta,fmtDec,b.games,b.n_box)+
        muTd("Opp FTA",b.avg_opp_fta,fmtDec,b.games,b.n_box)+
        muTd("Team PF",b.avg_team_pf,fmtDec,b.games,b.n_box)+
        muTd("Opp PF",b.avg_opp_pf,fmtDec,b.games,b.n_box)+
        "</tr>";
    }
    function seasonTable(seasons,career){
      var heads=["Season","Type","Games","W-L","Win%","Margin","Team FTA","Opp FTA","Team PF","Opp PF"];
      var ths=heads.map(function(h,idx){
        return '<th class="sortable '+(idx<2?"col-text":"col-num")+'" data-type="'+
          (idx<2?"text":"num")+'" scope="col">'+h+'</th>';
      }).join("");
      var rows="";
      seasons.forEach(function(s){
        rows+=seasonRow(s.season,s.season,"RS",s.rs);
        rows+=seasonRow(s.season,s.season,"PO",s.po);
      });
      rows+=seasonRow("Career","9999","RS",career.rs);
      rows+=seasonRow("Career","9999","PO",career.po);
      return '<table class="data-table sortable-table"><thead><tr>'+ths+
        '</tr></thead><tbody>'+rows+'</tbody></table>';
    }
    function filterGameLog(doc,tricode){
      var out=[];
      (doc.by_season||[]).forEach(function(season){
        (season.games||[]).forEach(function(g){
          if(g.kind==="PI")return;
          if(g.home_canon===tricode||g.away_canon===tricode)out.push(g);
        });
      });
      out.sort(function(a,b){return a.date<b.date?1:(a.date>b.date?-1:0);});
      return out;
    }
    function gameLogTable(games,tricode){
      if(!games.length)return '<p class="empty-note">No games on record for this pairing.</p>';
      var heads=["Date","Matchup","Score","Result","Round","Crew"];
      var ths=heads.map(function(h){
        return '<th class="sortable col-text" data-type="text" scope="col">'+h+'</th>';
      }).join("");
      var rows=games.map(function(g){
        var isHome=g.home_canon===tricode;
        var teamPts=isHome?g.home_pts:g.away_pts, oppPts=isHome?g.away_pts:g.home_pts;
        var result="—", resultClass="";
        if(teamPts!=null&&oppPts!=null){
          result=(teamPts>oppPts?"W ":"L ")+teamPts+"-"+oppPts;
          resultClass=teamPts>oppPts?"mu-win":"mu-loss";
        }
        var crew=(g.co_officials||[]).map(function(c){
          return '<a href="../referee/'+c.slug+'/index.html">'+escHtml(c.name)+'</a>';
        }).join(" &middot; ")||"—";
        // Away-home order, matching the Matchup column's "away @ home" reading --
        // a separate home-away order here would read backwards against it.
        var score=(g.home_pts!=null&&g.away_pts!=null)?(g.away_pts+"-"+g.home_pts):"—";
        return "<tr>"+
          '<td data-label="Date" data-sort="'+esc0(g.date)+'">'+escHtml(g.date)+'</td>'+
          '<td data-label="Matchup">'+escHtml(g.away_team_abbr)+' <span class="vs">@</span> '+
            escHtml(g.home_team_abbr)+'</td>'+
          '<td data-label="Score">'+score+'</td>'+
          '<td data-label="Result"><span class="'+resultClass+'">'+result+'</span></td>'+
          '<td data-label="Round">'+escHtml(g.round_label||"—")+'</td>'+
          '<td data-label="Crew" class="crew">'+crew+'</td>'+
        "</tr>";
      }).join("");
      return '<table class="data-table sortable-table"><thead><tr>'+ths+
        '</tr></thead><tbody>'+rows+'</tbody></table>';
    }
    function esc0(s){return String(s).replace(/"/g,"&quot;");}

    function renderMatchup(teamDoc,refDoc,matchupDoc,gameLogDoc){
      var tricode=teamDoc.summary.tricode, teamName=teamDoc.summary.name;
      var refName=refDoc.summary.name, rSlug=refDoc.summary.slug;
      var heading='<h3 class="matchup-heading">'+escHtml(teamName)+' &times; '+
        '<a href="../referee/'+rSlug+'/index.html">'+escHtml(refName)+'</a></h3>';
      var teamData=(matchupDoc.teams||{})[tricode];
      if(!teamData||(!teamData.career.rs&&!teamData.career.po)){
        resultEl.innerHTML=heading+
          '<p class="empty-note">No games on record for '+escHtml(refName)+
          ' officiating '+escHtml(teamName)+'.</p>';
        return;
      }
      var career=teamData.career;
      var summaryHtml=matchupBlock("Regular season",career.rs)+matchupBlock("Playoffs",career.po);
      var filtered=filterGameLog(gameLogDoc,tricode);
      resultEl.innerHTML=heading+
        '<div class="whistle-cols">'+summaryHtml+'</div>'+
        '<h3 class="lb-subhead" style="margin-top:1.6rem">Season by season</h3>'+
        '<div class="table-wrap">'+seasonTable(teamData.seasons,career)+'</div>'+
        '<h3 class="lb-subhead" style="margin-top:1.6rem">Game log ('+filtered.length+' games)</h3>'+
        '<p class="caption mu-section-caption">Every game on record for this pairing -- the '+
        'numbers above are computed from exactly these rows.</p>'+
        '<div class="table-wrap">'+gameLogTable(filtered,tricode)+'</div>';
      if(window.initSortableTables)window.initSortableTables(resultEl);
    }

    var teamPromise=teamSlug
      ?fetch("../data/teams/"+teamSlug+".json").then(function(r){
        if(!r.ok)throw new Error("team not found");return r.json();
      }).catch(function(){return null;})
      :Promise.resolve(null);
    var refPromise=refSlug
      ?fetch("../data/referees/"+refSlug+".json").then(function(r){
        if(!r.ok)throw new Error("ref not found");return r.json();
      }).catch(function(){return null;})
      :Promise.resolve(null);

    Promise.all([teamPromise,refPromise]).then(function(docs){
      var teamDoc=docs[0], refDoc=docs[1];
      if(teamDoc&&teamInput)teamInput.value=teamDoc.summary.name;
      if(refDoc&&refInput)refInput.value=refDoc.summary.name;
      if(promptEl)promptEl.hidden=!!(teamDoc&&refDoc);
      if(!teamDoc||!refDoc){
        resultEl.innerHTML=(teamSlug&&!teamDoc)||(refSlug&&!refDoc)
          ?'<p class="empty-note">Could not find that team or official -- check the URL.</p>':"";
        return;
      }
      var officialId=refDoc.summary.official_id;
      resultEl.innerHTML='<p class="empty-note">Loading…</p>';
      Promise.all([
        fetch("../data/matchups/"+officialId+".json").then(function(r){
          if(!r.ok)throw new Error("matchup data not found");
          return r.json();
        }),
        fetch("../data/referee_games/"+officialId+".json").then(function(r){
          if(!r.ok)throw new Error("game log not found");
          return r.json();
        })
      ]).then(function(results){
        renderMatchup(teamDoc,refDoc,results[0],results[1]);
      }).catch(function(){
        resultEl.innerHTML='<p class="empty-note">Could not load data for this pairing.</p>';
      });
    });
  })();
})();
