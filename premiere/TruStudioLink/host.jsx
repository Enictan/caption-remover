// Premiere Pro (ExtendScript, ES3) side of Tru Studio Link.
var CR_TICKS = 254016000000;

function crEsc(s){ return String(s).replace(/\\/g,'\\\\').replace(/"/g,'\\"').replace(/\n/g,'\\n').replace(/\r/g,'').replace(/\t/g,' '); }
function crJ(o){
  if(o === null || o === undefined) return 'null';
  var t = typeof o;
  if(t === 'number') return isFinite(o) ? String(o) : 'null';
  if(t === 'boolean') return String(o);
  if(t === 'string') return '"' + crEsc(o) + '"';
  if(o instanceof Array){ var a = []; for(var i = 0; i < o.length; i++) a.push(crJ(o[i])); return '[' + a.join(',') + ']'; }
  var p = []; for(var k in o){ if(o.hasOwnProperty(k)) p.push('"' + crEsc(k) + '":' + crJ(o[k])); }
  return '{' + p.join(',') + '}';
}

function crLinkInfo(){
  var places = ['~/.caption-remover-link.json'];
  try{ var up = $.getenv('USERPROFILE'); if(up) places.push(up + '/.caption-remover-link.json'); }catch(e){}
  for(var i = 0; i < places.length; i++){
    var f = new File(places[i]);
    if(f.exists){ f.encoding = 'UTF-8'; f.open('r'); var s = f.read(); f.close(); return s; }
  }
  return '';
}

function crSecs(v){ // Time object, ticks string or seconds
  if(v === null || v === undefined) return 0;
  if(typeof v === 'object' && v.seconds !== undefined) return Number(v.seconds);
  var n = Number(v); return n > 1e7 ? n / CR_TICKS : n;
}

function crSeqInfo(){
  try{
    var s = app.project.activeSequence;
    if(!s) return crJ({ok:true, seq:null, project: app.project.name});
    var inP = 0, outP = 0;
    try{ inP = crSecs(s.getInPointAsTime()); outP = crSecs(s.getOutPointAsTime()); }catch(e){ try{ inP = Number(s.getInPoint()); outP = Number(s.getOutPoint()); }catch(e2){} }
    return crJ({ok:true, project: app.project.name,
      seq:{name: s.name, id: s.sequenceID, end: crSecs(s.end), playhead: crSecs(s.getPlayerPosition()), inPoint: inP, outPoint: outP}});
  }catch(e){ return crJ({ok:false, error:String(e)}); }
}

function crBin(){
  var r = app.project.rootItem;
  for(var i = 0; i < r.children.numItems; i++){ var c = r.children[i]; if(c.type === ProjectItemType.BIN && c.name === 'Tru Studio') return c; }
  return r.createBin('Tru Studio');
}
function crFind(root, test){
  for(var i = 0; i < root.children.numItems; i++){
    var c = root.children[i];
    if(c.type === ProjectItemType.BIN){ var x = crFind(c, test); if(x) return x; }
    else if(test(c)) return c;
  }
  return null;
}
function crSamePath(a, b){ return String(a).replace(/\\/g,'/').toLowerCase() === String(b).replace(/\\/g,'/').toLowerCase(); }

function crFindClip(seq, item){
  for(var t = 0; t < seq.audioTracks.numTracks; t++){
    var tr = seq.audioTracks[t];
    for(var c = 0; c < tr.clips.numItems; c++){ var cl = tr.clips[c]; if(cl.projectItem && cl.projectItem.nodeId === item.nodeId) return cl; }
  }
  return null;
}
function crFreeTrack(seq, start, dur){
  for(var t = 0; t < seq.audioTracks.numTracks; t++){
    var tr = seq.audioTracks[t], free = true;
    for(var c = 0; c < tr.clips.numItems; c++){
      var cl = tr.clips[c], a = crSecs(cl.start), b = crSecs(cl.end);
      if(a < start + dur && b > start){ free = false; break; }
    }
    if(free) return tr;
  }
  return null;
}
function crClearMarkers(seq, tag){
  var kill = [], m = seq.markers.getFirstMarker();
  while(m){ if(String(m.comments) === tag) kill.push(m); m = seq.markers.getNextMarker(m); }
  for(var i = 0; i < kill.length; i++) seq.markers.deleteMarker(kill[i]);
}

// a = {id, label, path, dur, insert, markers:[{t, name, color}]}
function crApply(a){
  try{
    var p = app.project, fresh = false;
    if(!p) return crJ({ok:false, error:'No project is open'});
    var item = crFind(p.rootItem, function(c){ return c.name === a.label; });
    if(item){
      if(!item.canChangeMediaPath()) return crJ({ok:false, error:'Premiere won\'t let this item be replaced'});
      item.changeMediaPath(a.path, true);
    } else {
      var bin = crBin();
      p.importFiles([a.path], true, bin, false);
      item = crFind(bin, function(c){ try{ return crSamePath(c.getMediaPath(), a.path); }catch(e){ return false; } });
      if(!item) return crJ({ok:false, error:'Premiere couldn\'t import ' + a.path});
      item.name = a.label; fresh = true;
    }
    var seq = p.activeSequence, clip = null, n = 0;
    if(seq){
      clip = crFindClip(seq, item);
      if(!clip && fresh && a.insert){
        var at = crSecs(seq.getPlayerPosition()), tr = crFreeTrack(seq, at, a.dur || 1);
        if(tr){
          try{ var tm = new Time(); tm.seconds = at; tr.overwriteClip(item, tm); }catch(e){ tr.overwriteClip(item, String(at)); }
          clip = crFindClip(seq, item);
        }
      }
      if(clip && a.markers){
        var tag = 'tru:' + a.label;
        crClearMarkers(seq, tag);
        var off = crSecs(clip.start) - crSecs(clip.inPoint), lo = crSecs(clip.inPoint), hi = crSecs(clip.outPoint);
        for(var i = 0; i < a.markers.length; i++){
          var mk = a.markers[i]; if(mk.t < lo - 0.001 || mk.t > hi + 0.001) continue;
          var m = seq.markers.createMarker(off + mk.t);
          m.name = mk.name || ''; m.comments = tag;
          try{ m.setColorByIndex(mk.color || 0); }catch(e){}
          n++;
        }
      }
    }
    return crJ({ok:true, fresh:fresh, placed: !!clip, markers: n, sequence: seq ? seq.name : null});
  }catch(e){ return crJ({ok:false, error:String(e) + (e.line ? ' (line ' + e.line + ')' : '')}); }
}

// ---- whole mix: one audio track per stem, kept in line with the studio
function crClipOn(seq, item){
  for(var t = 0; t < seq.audioTracks.numTracks; t++){
    var tr = seq.audioTracks[t];
    for(var c = 0; c < tr.clips.numItems; c++){ var cl = tr.clips[c]; if(cl.projectItem && cl.projectItem.nodeId === item.nodeId) return {clip: cl, t: t}; }
  }
  return null;
}
function crTrackFree(tr, start, dur, ignoreId){
  for(var c = 0; c < tr.clips.numItems; c++){
    var cl = tr.clips[c];
    if(ignoreId && cl.projectItem && cl.projectItem.nodeId === ignoreId) continue;
    var a = crSecs(cl.start), b = crSecs(cl.end);
    if(a < start + dur - 0.001 && b > start + 0.001) return false;
  }
  return true;
}
function crAddAudioTrack(seq){
  try{
    app.enableQE();
    var qs = qe.project.getActiveSequence(), n = seq.audioTracks.numTracks;
    qs.addTracks(0, 0, 1, 1, n, 0, 0);   // no video, 1 stereo audio track after the last one
    return seq.audioTracks.numTracks > n;
  }catch(e){ return false; }
}
function crPlace(seq, item, start, dur, avoid){
  for(var pass = 0; pass < 2; pass++){
    for(var t = 0; t < seq.audioTracks.numTracks; t++){
      if(avoid[t]) continue;
      var tr = seq.audioTracks[t];
      if(crTrackFree(tr, start, dur, null)){
        try{ var tm = new Time(); tm.seconds = start; tr.overwriteClip(item, tm); }catch(e){ tr.overwriteClip(item, String(start)); }
        return t;
      }
    }
    if(pass === 0 && !crAddAudioTrack(seq)) break;
  }
  return -1;
}
function crRemoveClips(seq, item){
  var n = 0;
  for(var t = 0; t < seq.audioTracks.numTracks; t++){
    var tr = seq.audioTracks[t];
    for(var c = tr.clips.numItems - 1; c >= 0; c--){
      var cl = tr.clips[c];
      if(cl.projectItem && cl.projectItem.nodeId === item.nodeId){ cl.remove(false, false); n++; }
    }
  }
  return n;
}

// a = {id, lanes:[{label, path|null, media, dur, off, remove}], markers:[{t,name,color}], anchor}
function crApplyMix(a){
  try{
    var p = app.project; if(!p) return crJ({ok:false, error:'No project is open'});
    var seq = p.activeSequence; if(!seq) return crJ({ok:false, error:'Open a sequence in Premiere first'});
    var anchor = (a.anchor === null || a.anchor === undefined) ? crSecs(seq.getPlayerPosition()) : Number(a.anchor);
    var bin = null, done = [], errs = [], avoid = {}, i, L, item;
    // tracks already used by lanes we keep: new stems go on other tracks
    for(i = 0; i < a.lanes.length; i++){
      L = a.lanes[i]; if(L.remove) continue;
      item = crFind(p.rootItem, function(c){ return c.name === L.label; });
      if(item){ var h = crClipOn(seq, item); if(h) avoid[h.t] = true; }
    }
    for(i = 0; i < a.lanes.length; i++){
      L = a.lanes[i];
      item = crFind(p.rootItem, function(c){ return c.name === L.label; });
      if(L.remove){ if(item) crRemoveClips(seq, item); continue; }
      var path = L.path || L.media;
      if(!item){
        if(!bin) bin = crBin();
        p.importFiles([path], true, bin, false);
        item = crFind(bin, function(c){ try{ return crSamePath(c.getMediaPath(), path); }catch(e){ return false; } });
        if(!item){ errs.push(L.label + ': import failed'); continue; }
        item.name = L.label;
      } else if(!crSamePath(item.getMediaPath(), path)){
        if(item.canChangeMediaPath()) item.changeMediaPath(path, true);
        else { errs.push(L.label + ': can\'t be replaced'); continue; }
      }
      var want = anchor + Number(L.off), h2 = crClipOn(seq, item);
      if(h2){
        var cl = h2.clip, ok = Math.abs(crSecs(cl.start) - want) < 0.002 && Math.abs((crSecs(cl.end) - crSecs(cl.start)) - L.dur) < 0.02 && crSecs(cl.inPoint) < 0.002;
        if(ok){ done.push(L.label); continue; }
        var keepT = h2.t; crRemoveClips(seq, item);
        if(crTrackFree(seq.audioTracks[keepT], want, L.dur, null)){
          try{ var tm = new Time(); tm.seconds = want; seq.audioTracks[keepT].overwriteClip(item, tm); }catch(e){ seq.audioTracks[keepT].overwriteClip(item, String(want)); }
          done.push(L.label); continue;
        }
      }
      var t = crPlace(seq, item, want, L.dur, avoid);
      if(t < 0) errs.push(L.label + ': no free audio track — add one in Premiere');
      else { avoid[t] = true; done.push(L.label); }
    }
    var nm = 0;
    crClearMarkers(seq, 'tru:mix');
    for(i = 0; i < (a.markers || []).length; i++){
      var mk = a.markers[i], m = seq.markers.createMarker(anchor + mk.t);
      m.name = mk.name || ''; m.comments = 'tru:mix'; try{ m.setColorByIndex(mk.color || 0); }catch(e){} nm++;
    }
    return crJ({ok: errs.length === 0, error: errs.join('; '), placed: done.length, markers: nm, anchor: anchor, seq: seq.sequenceID});
  }catch(e){ return crJ({ok:false, error:String(e) + (e.line ? ' (line ' + e.line + ')' : '')}); }
}
