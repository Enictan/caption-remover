// Tru Studio Link: polls the Caption Remover studio running on this computer and applies what it sends
// (new or updated mixes, markers) to the open Premiere Pro project, and tells the studio about the active sequence.
(function(){
  var cep = window.__adobe_cep__;
  function jsx(code){ return new Promise(function(res){ if(!cep) return res(''); cep.evalScript(code, res); }); }
  var link = null, busy = false, fails = 0;
  var $ = function(id){ return document.getElementById(id); };
  function status(txt, cls){ $('st').textContent = txt; $('dot').className = 'dot ' + (cls||''); }
  function log(txt, cls){ var d=document.createElement('div'); var n=new Date(), hh=n.getHours(), mm=n.getMinutes(); d.textContent=(hh<10?'0':'')+hh+':'+(mm<10?'0':'')+mm+'  '+txt; if(cls) d.className=cls; d.title=txt;
    var L=$('log'); L.insertBefore(d, L.firstChild); while(L.children.length>12) L.removeChild(L.lastChild); }
  function fmt(s){ s=Math.max(0,s||0); var m=Math.floor(s/60), r=Math.floor(s%60); return m+':'+(r<10?'0':'')+r; }
  function post(path, body){
    return fetch('http://127.0.0.1:'+link.port+path+'?k='+encodeURIComponent(link.token), {method:'POST', headers:{'Content-Type':'text/plain'}, body: body||''})
      .then(function(r){ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); });
  }
  function tick(){
    if(busy) return; busy = true;
    var p = link ? Promise.resolve() : jsx('crLinkInfo()').then(function(s){ try{ link = s ? JSON.parse(s) : null; }catch(e){ link = null; } });
    p.then(function(){
      if(!link){ status('Open Caption Remover on this computer', 'wait'); return; }
      return jsx('crSeqInfo()').then(function(seqJson){
        var seq = {}; try{ seq = JSON.parse(seqJson||'{}'); }catch(e){}
        $('seq').textContent = seq.seq ? ('Sequence: '+seq.seq.name+' · '+fmt(seq.seq.end)+' · playhead '+fmt(seq.seq.playhead)) : 'No sequence open';
        return post('/api/link/poll', JSON.stringify({seq: seq.seq||null, project: seq.project||null, insert: $('ins').checked}));
      }).then(function(d){
        fails = 0; status('Connected to Caption Remover', 'on');
        var chain = Promise.resolve();
        (d.actions||[]).forEach(function(a){
          chain = chain.then(function(){
            log(a.kind==='mix'?'Updating the mix…':'Applying '+a.label+'…');
            a.insert = a.insert && $('ins').checked;
            return jsx((a.kind==='mix'?'crApplyMix(':'crApply(')+JSON.stringify(a)+')').then(function(res){
              var r = {}; try{ r = JSON.parse(res); }catch(e){ r = {ok:false, error: res || 'no reply from Premiere'}; }
              if(a.kind==='mix'){ if(r.ok) log('Mix in sync · '+r.placed+' stem track'+(r.placed===1?'':'s')+(r.markers?' · '+r.markers+' markers':''), 'ok'); else log('Mix: '+r.error, 'err'); }
              else if(r.ok) log((r.fresh?'Added ':'Updated ')+a.label+(r.placed?'':' (in the Tru Studio bin)')+(r.markers?' · '+r.markers+' markers':''), 'ok');
              else if(a.kind!=='mix') log(a.label+': '+r.error, 'err');
              return post('/api/link/ack', JSON.stringify({id:a.id, result:r}));
            });
          });
        });
        return chain;
      });
    }).catch(function(e){
      fails++; if(fails>2){ link = null; status('Caption Remover isn\'t running', 'wait'); }
    }).then(function(){ busy = false; });
  }
  setInterval(tick, 1000); tick();
})();
