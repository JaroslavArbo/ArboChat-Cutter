import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './styles.css';
import { api, fmt, parseTime } from './api.js';

const EMPTY_META = { date:'', episode_number:'', topic:'', speaker:'' };
const TRACK_COLORS = {
  speaker_video: '#3b82f6',
  replacement_video: '#f59e0b',
  gallery_video: '#22c55e',
  imported_audio: '#22c55e',
  edit_master_video: '#3b82f6',
};

// Shared keyboard shortcuts for the trim editors: I/O mark in/out points at the
// playhead, arrow keys nudge the playhead, Space toggles the active preview
// video. Ignored while the user is typing into an input/textarea so it never
// hijacks normal text entry.
function useTrimShortcuts({ onMarkStart, onMarkEnd, onNudge, previewVideoId }) {
  useEffect(() => {
    const handler = (event) => {
      const tag = (event.target?.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea' || tag === 'select' || event.target?.isContentEditable) return;
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      const step = event.shiftKey ? 5 : 1;
      if (event.key === 'i' || event.key === 'I') {
        if (onMarkStart) { event.preventDefault(); onMarkStart(); }
      } else if (event.key === 'o' || event.key === 'O') {
        if (onMarkEnd) { event.preventDefault(); onMarkEnd(); }
      } else if (event.key === 'ArrowLeft') {
        if (onNudge) { event.preventDefault(); onNudge(-step); }
      } else if (event.key === 'ArrowRight') {
        if (onNudge) { event.preventDefault(); onNudge(step); }
      } else if (event.key === ' ') {
        const video = previewVideoId && document.getElementById(previewVideoId);
        if (video && !video.hidden) {
          event.preventDefault();
          if (video.paused) video.play().catch(() => {}); else video.pause();
        }
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [onMarkStart, onMarkEnd, onNudge, previewVideoId]);
}

function TimeField({ value, onCommit, title }) {
  const [text, setText] = useState(fmt(value));
  useEffect(() => { setText(fmt(value)); }, [value]);
  const commit = () => {
    const parsed = parseTime(text, value);
    setText(fmt(parsed));
    if (Math.abs(parsed - value) > 0.001) onCommit(parsed);
  };
  return <input
    className="timeField"
    type="text"
    inputMode="numeric"
    title={title}
    value={text}
    onChange={(e) => setText(e.target.value)}
    onBlur={commit}
    onKeyDown={(e) => { if (e.key === 'Enter') { e.currentTarget.blur(); } if (e.key === 'Escape') { setText(fmt(value)); e.currentTarget.blur(); } }}
  />;
}

function useLog() {
  const [lines, setLines] = useState([]);
  const log = useCallback((msg) => {
    setLines(prev => [`${new Date().toLocaleTimeString('cs-CZ')} · ${msg}`, ...prev].slice(0, 120));
  }, []);
  return [lines, log];
}


function waitForPaint(){
  return new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
}


function introCanvasItems(project){
  const meta = project?.meta || {};
  const layout = meta.intro_layout || {};
  const defs = [
    ['episode_number', `ArboChat #${meta.episode_number || ''}`, 130, 170, 46, 800],
    ['topic', meta.topic || '', 130, 250, 54, 800],
    ['speaker', meta.speaker ? `Řečník: ${meta.speaker}` : '', 130, 340, 34, 650],
    ['date', meta.date ? `Datum: ${meta.date}` : '', 130, 400, 30, 650],
  ];
  return defs.filter(([,text]) => String(text || '').trim()).map(([key,text,x,y,size,weight]) => {
    const custom = layout[key] || {};
    return {
      key,
      text,
      x: Number(custom.x ?? x),
      y: Number(custom.y ?? y),
      size: Number(custom.size ?? size),
      font: String(custom.font || 'Inter'),
      weight: Number(weight || 700)
    };
  });
}

function canvasFontFamily(font){
  const f = String(font || 'Inter');
  if (f === 'Inter') return 'Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
  return `${f}, Arial, sans-serif`;
}

async function uploadIntroOverlay(project){
  if(!project?.id) return;
  const items = introCanvasItems(project);
  if(!items.length) return;
  const canvas = document.createElement('canvas');
  canvas.width = 1920;
  canvas.height = 1080;
  const ctx = canvas.getContext('2d');
  if(!ctx) return;
  ctx.clearRect(0,0,canvas.width,canvas.height);
  ctx.textBaseline = 'alphabetic';
  const scale = 1.5;
  for(const it of items){
    const x = Math.round(Number(it.x || 0) * scale);
    const y = Math.round(Number(it.y || 0) * scale);
    const size = Math.max(8, Math.round(Number(it.size || 36) * scale));
    const weight = Math.max(100, Math.min(900, Number(it.weight || 700)));
    ctx.font = `${weight} ${size}px ${canvasFontFamily(it.font)}`;
    ctx.lineJoin = 'round';
    ctx.miterLimit = 2;
    ctx.strokeStyle = 'rgba(2,6,23,0.92)';
    ctx.lineWidth = Math.max(4, Math.round(size * 0.10));
    ctx.strokeText(String(it.text || ''), x, y);
    ctx.fillStyle = 'rgba(248,250,252,0.98)';
    ctx.fillText(String(it.text || ''), x, y);
  }
  const overlay = canvas.toDataURL('image/png');
  await api('/api/save_intro_overlay', {method:'POST', body:JSON.stringify({project:project.id, overlay})});
}

function durationOf(project) {
  const media = project?.analysis?.media || {};
  return Number(media.speaker_video?.duration || media.gallery_video?.duration || media.replacement_video?.duration || 5400);
}


function projectHasAnyVideo(project){
  return Boolean(project?.files?.speaker_video || project?.files?.gallery_video || project?.files?.replacement_video);
}

function replacementClipInfo(project){
  const cuts = project?.cuts || {};
  const start = parseTime(cuts.replacement_start, 0);
  const duration = Number(project?.analysis?.media?.replacement_video?.duration || 0);
  const end = duration > 0 ? start + duration : start;
  return { start, duration, end };
}

function playheadInsideReplacement(project, playheadTime){
  const clip = replacementClipInfo(project);
  return clip.duration > 0 && playheadTime >= clip.start && playheadTime <= clip.end;
}

function replacementLocalTime(project, playheadTime){
  const clip = replacementClipInfo(project);
  return Math.max(0, Math.min(clip.duration || 0, playheadTime - clip.start));
}

function playbackSourceForPlayhead(project, playheadTime){
  const cuts = project?.cuts || {};
  if(project?.files?.replacement_video && playheadInsideReplacement(project, playheadTime)){
    return { role: 'replacement_video', time: replacementLocalTime(project, playheadTime), label: 'Doprovodné video' };
  }
  const galleryStart = parseTime(cuts.discussion_start ?? cuts.gallery_start, null);
  if(project?.files?.gallery_video && galleryStart !== null && playheadTime >= galleryStart){
    return { role: 'gallery_video', time: Math.max(0, playheadTime), label: 'Galerie / diskuse' };
  }
  return { role: 'speaker_video', time: Math.max(0, playheadTime), label: 'Hlavní video' };
}

class AppErrorBoundary extends React.Component {
  constructor(props){ super(props); this.state = {error: null}; }
  static getDerivedStateFromError(error){ return {error}; }
  componentDidCatch(error, info){ console.error('ArboChat UI error', error, info); }
  render(){
    if(this.state.error){
      return <section className="card errorBox"><h2>Část rozhraní spadla</h2><p className="muted">{String(this.state.error?.message || this.state.error)}</p><button onClick={()=>this.setState({error:null})}>Zkusit znovu</button></section>;
    }
    return this.props.children;
  }
}

function mediaDuration(project, role, fallback = 0) {
  return Number(project?.analysis?.media?.[role]?.duration || fallback || 0);
}

function timelineFitZoom(duration, viewportWidth = 1200) {
  // ViteCut uses 80 px/s at zoom=1. Long workshop videos need zoom << 1,
  // otherwise the timeline shows only the first few seconds.
  const seconds = Math.max(60, Number(duration || 60));
  return Math.max(0.001, Math.min(0.35, viewportWidth / (seconds * 80)));
}

function clampRange(start, end, max) {
  const s = Math.max(0, Number(start || 0));
  const e = Math.max(s + 1, Math.min(max, Number(end || max || s + 1)));
  return [s, e];
}

function roleDuration(project, role, roleDurations = {}, fallback = 0) {
  return Number(roleDurations?.[role] || project?.analysis?.media?.[role]?.duration || fallback || 0);
}

function buildRows(project, waveforms = {}, roleDurations = {}) {
  const total = Math.max(60, roleDuration(project, 'speaker_video', roleDurations, durationOf(project)));
  const cuts = project?.cuts || {};
  const trimStart = Math.max(0, Math.min(total - 1, parseTime(cuts.real_start, 0)));
  const speakerDur = roleDuration(project, 'speaker_video', roleDurations, total) || total;
  const discussion = parseTime(cuts.discussion_start ?? cuts.gallery_start, Math.min(4792, total * 0.85));

  // Doprovodné video je překryvná stopa s pevnou délkou podle skutečného souboru.
  // Uživatel ho pouze posouvá po hlavní časové ose, nikdy neroztahuje.
  const realReplacementDuration = Number(roleDurations?.replacement_video || project?.analysis?.media?.replacement_video?.duration || 0);
  const replacementSourceDur = realReplacementDuration > 0 ? realReplacementDuration : 12;
  const guessedReplacementStart = parseTime(cuts.replacement_start, Math.min(473, total * 0.1));
  const replacementStart = Math.max(0, Math.min(total - 1, guessedReplacementStart));
  const replacementEnd = Math.max(replacementStart + 1, Math.min(total, replacementStart + replacementSourceDur));

  // Hlavní video a galerie zůstávají vizuálně ukotvené k nulové ose.
  // Začátek po trimu je jen marker; skutečné zkrácení se použije až ve workflow „Upravit video“.
  const speakerStart = 0;
  const speakerEnd = Math.max(1, Math.min(total, parseTime(cuts.main_end, speakerDur)));

  const trimMarker = trimStart > 0 ? [{
    id:'trim_start_marker',
    effectId:'suggestion',
    start:trimStart,
    end:Math.min(total, trimStart + Math.max(3, 10 / 1)),
    title:'Začátek po trimu',
    kind:'trim_start',
    movable:false,
    flexible:false,
    role:'speaker_video'
  }] : [];

  const galleryTrackActions = project?.files?.gallery_video ? [
    {
      id:'gallery_full',
      effectId:'video_clip',
      start:speakerStart,
      end:speakerEnd,
      kind:'gallery',
      title:'Galerie / diskuse',
      movable:false,
      flexible:false,
      role:'gallery_video',
      peaks: waveforms.gallery_video?.length ? waveforms.gallery_video : (waveforms.speaker_video || []),
    },
    ...trimMarker.map(m => ({...m, id:'gallery_trim_start_marker', role:'gallery_video'})),
    {
      id:'gallery_transition_marker',
      effectId:'suggestion',
      start:Math.max(speakerStart, Math.min(speakerEnd - 1, discussion)),
      end:Math.max(speakerStart + 1, Math.min(speakerEnd, discussion + 5)),
      title:'Přechod do galerie',
      kind:'discussion',
      movable:false,
      flexible:false,
      role:'gallery_video'
    }
  ] : [];

  const replacementActions = project?.files?.replacement_video ? [
    {
      id:'replacement_clip',
      effectId:'video_clip',
      start:replacementStart,
      end:replacementEnd,
      kind:'replacement',
      title:'Doprovodné video',
      movable:true,
      flexible:false,
      fixedDuration:true,
      role:'replacement_video',
      peaks: waveforms.replacement_video || [],
    }
  ] : [];

  return [
    {
      id: 'replacement_video',
      title: 'Doprovodné video',
      subtitle: 'překryvná stopa nahoře; pevná délka, pouze posun',
      role: 'replacement_video',
      actions: replacementActions
    },
    {
      id: 'gallery_video',
      title: 'Galerie / diskuse',
      subtitle: 'paralelní stopa; trim a přechod jsou jen vyznačené markery',
      role: 'gallery_video',
      actions: galleryTrackActions
    },
    {
      id: 'speaker_video',
      title: 'Hlavní video',
      subtitle: 'spodní hlavní obrazová osa / speaker view',
      role: 'speaker_video',
      actions: [
        ...trimMarker,
        ...((project?.analysis?.markers || [])
          .filter(m => m.kind === 'lecture_start_audio')
          .slice(0, 1)
          .map((m, i) => ({
            id:`lecture_start_audio_${i}`,
            effectId:'suggestion',
            start:Number(m.start || 0),
            end:Math.max(Number(m.end || 0), Number(m.start || 0) + 8),
            title:'Audio: zahájení přednášky',
            kind:'lecture_start_audio',
            movable:false,
            flexible:false,
            role:'speaker_video'
          }))),
        {
          id:'main_clip',
          effectId:'video_clip',
          start:speakerStart,
          end:speakerEnd,
          kind:'speaker',
          title:'Hlavní video',
          movable:false,
          flexible:false,
          role:'speaker_video',
          peaks: waveforms.speaker_video || [],
        }
      ]
    },
    {
      id: 'external_audio',
      title: 'Externí audio',
      subtitle: 'rezerva pro dodanou zvukovou stopu',
      role: 'external_audio',
      actions: []
    }
  ];
}

function rowsToCuts(rows, oldCuts) {
  const next = { ...(oldCuts || {}) };
  for (const row of rows) {
    for (const a of row.actions || []) {
      if (a.id === 'main_clip') {
        next.main_end = Number(a.end || 0);
      }
      if (a.id === 'replacement_clip') {
        next.replacement_start = Number(a.start || 0);
        next.replacement_end = Number(a.end || 0);
      }
      if (a.id === 'gallery_transition_marker') {
        next.discussion_start = Number(a.start || 0);
        next.gallery_start = Number(a.start || 0);
      }
    }
  }
  return next;
}

function StatusBar({status}) {
  return <div className="statusbar">
    <strong>ArboChat Cutter React</strong>
    <span className={status?.ffmpeg ? 'pill ok' : 'pill bad'}>{status?.ffmpeg ? 'FFmpeg OK' : 'FFmpeg nenalezen / neověřen'}</span>
    <span className="muted">Timeline: přesná časová osa podle hlavního videa</span>
  </div>;
}

function ProjectSidebar({projects, currentId, onCreate, onSelect, onRefresh}) {
  const [name, setName] = useState('Nový ArboChat');
  return <aside className="sidebar">
    <div className="brand">
      <div className="logo">AA</div>
      <div><h1>ArboChat Cutter</h1><p>React editor střihových bodů</p></div>
    </div>
    <div className="newProject">
      <input value={name} onChange={e=>setName(e.target.value)} placeholder="Název projektu" />
      <button onClick={() => onCreate(name)}>Vytvořit projekt</button>
    </div>
    <div className="sideTitle">Projekty</div>
    <div className="projectList">
      {projects.map(p => <button key={p.id} className={p.id===currentId ? 'selected' : ''} onClick={() => onSelect(p.id)}>
        <strong>{p.name || p.id}</strong>
        <small>{p.meta?.date || 'bez data'} · {p.meta?.topic || 'bez tématu'}</small>
      </button>)}
      {!projects.length && <p className="muted pad">Zatím žádný projekt.</p>}
    </div>
    <button className="ghost" onClick={onRefresh}>Obnovit seznam</button>
  </aside>;
}

function SetupPanel({project, onUpdate, log, status, setStatus}) {
  const chooseFolder = async () => {
    const data = await api(`/api/choose_directory?project=${encodeURIComponent(project.id)}`);
    onUpdate(data.project); log('Načtena lokální složka a automaticky rozpoznané soubory.');
  };
  const chooseCommon = async (role) => {
    const data = await api(`/api/choose_common_file?role=${encodeURIComponent(role)}`);
    if (data.settings && setStatus) setStatus({...(status || {}), settings:data.settings});
    log('Společný soubor uložen: ' + role);
  };
  const chooseProjectFile = async (role) => {
    const data = await api(`/api/choose_project_file?project=${encodeURIComponent(project.id)}&role=${encodeURIComponent(role)}`);
    onUpdate(data.project);
    log('Soubor přiřazen: ' + role);
  };
  const files = project.files || {};
  const settings = status?.settings || {};
  return <section className="card setup">
    <div className="cardHead"><h2>1 · Vstupy</h2><span className="muted">Jen lokální cesty, žádné velké uploady přes prohlížeč.</span></div>
    <div className="actions"><button onClick={chooseFolder}>Vybrat složku se Zoom soubory</button></div>
    <div className="fileGrid">
      <FileState label="Hlavní video" value={files.speaker_video} />
      <FileState label="Galerie / diskuse" value={files.gallery_video} />
      <FileState label="Přepis" value={files.transcript_file} />
      <FileState label="Doprovodné video" value={files.replacement_video} />
    </div>
    <div className="common">
      <button className={settings.intro_template ? 'templateLoaded' : ''} onClick={()=>chooseCommon('intro_template')}>{settings.intro_template ? '✓ Intro šablona' : 'Intro šablona'}</button>
      <button className={settings.outro_template ? 'templateLoaded' : ''} onClick={()=>chooseCommon('outro_template')}>{settings.outro_template ? '✓ Outro šablona' : 'Outro šablona'}</button>
      <details className="manualPick">
        <summary>Ruční výběr</summary>
        <div className="manualPickGrid">
          <button onClick={()=>chooseProjectFile('speaker_video')}>Ručně hlavní video</button>
          <button onClick={()=>chooseProjectFile('gallery_video')}>Ručně galerii</button>
          <button onClick={()=>chooseProjectFile('replacement_video')}>Ručně doprovodné video</button>
          <button onClick={()=>chooseProjectFile('transcript_file')}>Ručně přepis</button>
        </div>
      </details>
    </div>
  </section>;
}

function FileState({label, value}) {
  return <div className="fileState"><span>{label}</span><strong className={value?'':'missing'}>{value ? value.split('/').pop() : 'chybí'}</strong></div>;
}

function MetadataPanel({project, setProject, saveProject, analyzeProject, previewIntroScreen, busy}) {
  const meta = project.meta || EMPTY_META;
  const updateMeta = (key, value) => setProject({...project, meta:{...meta, [key]:value}});
  return <section className="card">
    <div className="cardHead"><h2>2 · Metadata a analýza</h2><span className="muted">Datum se při založení projektu doplní z názvu, pokud je v názvu rozpoznatelné.</span></div>
    <div className="metaGrid">
      <label>Datum<input value={meta.date || ''} onChange={e=>updateMeta('date', e.target.value)} /></label>
      <label>Číslo<input value={meta.episode_number || ''} onChange={e=>updateMeta('episode_number', e.target.value)} /></label>
      <label>Téma<input value={meta.topic || ''} onChange={e=>updateMeta('topic', e.target.value)} /></label>
      <label>Řečník<input value={meta.speaker || ''} onChange={e=>updateMeta('speaker', e.target.value)} /></label>
    </div>
    <div className="introScreenBox">
      <div className="introScreenHead">
        <h3>Vstupní obrazovka</h3>
        <button onClick={previewIntroScreen} disabled={busy}>Vygenerovat náhled</button>
      </div>
      <p className="muted">Podkladem je reálné video z Intro šablony. Texty můžeš posouvat, měnit jejich velikost i font. Ve výsledném videu texty postupně odezní pomocí fade out.</p>
      <IntroLayoutDesigner project={project} setProject={setProject} />
    </div>
    {project.meta?.metadata_source && <p className="muted">Zdroj metadat: {project.meta.metadata_source}</p>}
  </section>;
}

function IntroLayoutDesigner({project, setProject}) {
  const meta = project.meta || {};
  const layout = meta.intro_layout || {};
  const dragRef = useRef(null);
  const svgRef = useRef(null);
  const saveTimerRef = useRef(null);
  const defs = [
    ['episode_number', 'Číslo dílu', 130, 170, 46],
    ['topic', 'Téma', 130, 250, 54],
    ['speaker', 'Řečník', 130, 340, 34],
    ['date', 'Datum', 130, 400, 30],
  ];
  const fontOptions = [
    ['Inter', 'Inter / systémový'],
    ['Arial', 'Arial'],
    ['Helvetica', 'Helvetica'],
    ['Georgia', 'Georgia'],
    ['Times New Roman', 'Times New Roman'],
    ['Courier New', 'Courier New'],
    ['Verdana', 'Verdana'],
  ];
  const defaultFor = (key) => {
    const found = defs.find(d => d[0] === key);
    return found ? {x: found[2], y: found[3], size: found[4], font: 'Inter'} : {x: 120, y: 240, size: 40, font: 'Inter'};
  };
  const itemFor = (key) => ({...defaultFor(key), ...(layout[key] || {})});
  const valueFor = (key) => {
    if(key === 'episode_number') return meta.episode_number ? `ArboChat #${meta.episode_number}` : 'ArboChat #';
    if(key === 'topic') return meta.topic || 'Téma ArboChatu';
    if(key === 'speaker') return meta.speaker ? `Řečník: ${meta.speaker}` : 'Řečník:';
    if(key === 'date') return meta.date ? `Datum: ${meta.date}` : 'Datum:';
    return '';
  };
  const persistDefaults = (nextLayout) => {
    if(saveTimerRef.current) clearTimeout(saveTimerRef.current);
    saveTimerRef.current = setTimeout(() => {
      fetch('/api/save_intro_layout_defaults', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({layout: nextLayout})
      }).catch(()=>{});
    }, 250);
  };
  const applyLayout = (nextLayout) => {
    setProject({...project, meta:{...meta, intro_layout: nextLayout}});
    persistDefaults(nextLayout);
  };
  const update = (key, patch) => {
    const old = itemFor(key);
    const nextLayout = {...layout, [key]:{...old, ...patch}};
    applyLayout(nextLayout);
  };
  const updateAll = (dx, dy) => {
    const nextLayout = {...layout};
    defs.forEach(([key]) => {
      const old = itemFor(key);
      nextLayout[key] = {...old, x: Math.round(Number(old.x || 0) + dx), y: Math.round(Number(old.y || 0) + dy)};
    });
    applyLayout(nextLayout);
  };
  const svgPoint = (event) => {
    const svg = svgRef.current;
    if(!svg) return {x:0, y:0};
    const rect = svg.getBoundingClientRect();
    return {x: (event.clientX - rect.left) * 1280 / rect.width, y: (event.clientY - rect.top) * 720 / rect.height};
  };
  const beginDrag = (event, key='__all__') => {
    event.preventDefault();
    event.stopPropagation();
    const pt = svgPoint(event);
    const startItems = Object.fromEntries(defs.map(([k]) => [k, itemFor(k)]));
    dragRef.current = {key, x: pt.x, y: pt.y, startItems};
  };
  const moveDrag = (event) => {
    if(!dragRef.current) return;
    event.preventDefault();
    const pt = svgPoint(event);
    const drag = dragRef.current;
    const dx = pt.x - drag.x;
    const dy = pt.y - drag.y;
    if(drag.key === '__all__'){
      const nextLayout = {...layout};
      defs.forEach(([key]) => {
        const old = drag.startItems[key] || itemFor(key);
        nextLayout[key] = {...old, x: Math.round(Number(old.x || 0) + dx), y: Math.round(Number(old.y || 0) + dy)};
      });
      applyLayout(nextLayout);
    }else{
      const old = drag.startItems[drag.key] || itemFor(drag.key);
      const nextLayout = {...layout, [drag.key]:{...old, x: Math.round(Number(old.x || 0) + dx), y: Math.round(Number(old.y || 0) + dy)}};
      applyLayout(nextLayout);
    }
  };
  const endDrag = () => { dragRef.current = null; };

  return <div className="introDesigner">
    <div className="introLivePreview">
      <svg ref={svgRef} viewBox="0 0 1280 720" onMouseDown={e=>beginDrag(e, '__all__')} onMouseMove={moveDrag} onMouseUp={endDrag} onMouseLeave={endDrag}>
        <image href={`/api/intro_background_frame?project=${encodeURIComponent(project.id)}&_=${encodeURIComponent(project.analysis?.intro_screen_preview || project.id || '')}`} xlinkHref={`/api/intro_background_frame?project=${encodeURIComponent(project.id)}&_=${encodeURIComponent(project.analysis?.intro_screen_preview || project.id || '')}`} x="0" y="0" width="1280" height="720" preserveAspectRatio="xMidYMid slice" />
        <rect width="1280" height="720" fill="#000000" opacity="0.18" />
        {defs.map(([key, label]) => {
          const item = itemFor(key);
          return <g key={key} className="introTextDraggable" onMouseDown={e=>beginDrag(e, key)}>
            <text x={item.x} y={item.y} fontFamily={`${item.font || 'Inter'}, Helvetica, Arial, sans-serif`} fontSize={Number(item.size || 36)} fontWeight={key === 'speaker' || key === 'date' ? 650 : 800} fill="#f8fafc" stroke="#020617" strokeWidth="3" paintOrder="stroke fill">{valueFor(key)}</text>
            <title>{label}: táhni myší</title>
          </g>;
        })}
      </svg>
      <p className="muted">Táhni konkrétní titulek pro jeho posun. Táhni prázdné místo v náhledu pro posun všech titulků najednou. Změny se ukládají jako výchozí vzhled pro další nové projekty.</p>
    </div>
    {project.analysis?.intro_screen_preview && <div className="generatedIntroPreview"><strong>Vygenerovaný finální náhled</strong><img src={`/api/intro_screen_preview?project=${encodeURIComponent(project.id)}&_=${encodeURIComponent(project.analysis.intro_screen_preview)}`} alt="Vygenerovaný náhled vstupní obrazovky" /></div>}
    <div className="introLayoutGrid">
      {defs.map(([key, label, dx, dy, ds]) => {
        const item = itemFor(key);
        return <div className="introLayoutRow" key={key}>
          <strong>{label}</strong>
          <small>{valueFor(key) || 'zatím prázdné'}</small>
          <label>X<input type="number" value={item.x ?? dx} onChange={e=>update(key,{x:Number(e.target.value)})} /></label>
          <label>Y<input type="number" value={item.y ?? dy} onChange={e=>update(key,{y:Number(e.target.value)})} /></label>
          <label>Velikost<input type="number" min="12" max="140" value={item.size ?? ds} onChange={e=>update(key,{size:Number(e.target.value)})} /></label>
          <label>Font<select value={item.font || 'Inter'} onChange={e=>update(key,{font:e.target.value})}>
            {fontOptions.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select></label>
        </div>;
      })}
    </div>
  </div>;
}

function WaveSvg({peaks = [], color = '#3b82f6', renderWidth = 360}) {
  const ref = useRef(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const cssWidth = Math.max(320, Math.min(30000, Number(renderWidth) || 320));
    const cssHeight = 100;
    const dpr = window.devicePixelRatio || 1;
    const width = Math.max(320, Math.floor(cssWidth));
    const height = cssHeight;

    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
    canvas.style.width = '100%';
    canvas.style.height = '100%';

    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    if (!peaks.length) return;

    // Normalize visible waveform so quiet lecture speech is still visible.
    // Without this, Zoom audio often appears as a few fat bars only.
    let maxAbs = 0;
    for (const [mn, mx] of peaks) {
      maxAbs = Math.max(maxAbs, Math.abs(Number(mn) || 0), Math.abs(Number(mx) || 0));
    }
    const gain = maxAbs > 0 ? Math.min(18, 0.88 / Math.max(maxAbs, 0.025)) : 1;
    const mid = height / 2;

    ctx.lineWidth = 1;
    ctx.strokeStyle = color;
    ctx.globalAlpha = 0.92;
    ctx.beginPath();

    for (let x = 0; x < width; x++) {
      const a = Math.floor((x / width) * peaks.length);
      const b = Math.max(a + 1, Math.floor(((x + 1) / width) * peaks.length));
      let mn = 1;
      let mx = -1;
      for (let i = a; i < b && i < peaks.length; i++) {
        const p = peaks[i] || [0, 0];
        mn = Math.min(mn, Number(p[0]) || 0);
        mx = Math.max(mx, Number(p[1]) || 0);
      }
      const y1 = mid - Math.max(-1, Math.min(1, mx * gain)) * (height * 0.46);
      const y2 = mid - Math.max(-1, Math.min(1, mn * gain)) * (height * 0.46);
      ctx.moveTo(x + 0.5, y1);
      ctx.lineTo(x + 0.5, y2);
    }
    ctx.stroke();

    // Subtle zero line helps orientation at high zoom.
    ctx.globalAlpha = 0.28;
    ctx.strokeStyle = '#e5edf8';
    ctx.beginPath();
    ctx.moveTo(0, mid + 0.5);
    ctx.lineTo(width, mid + 0.5);
    ctx.stroke();
  }, [peaks, color, renderWidth]);

  return <canvas ref={ref} className="waveCanvas" aria-hidden="true" />;
}


function TrackClip({action}) {
  if (action.effectId === 'suggestion') {
    return <div className={`clipRender suggestion ${action.kind || ''}`}>
      <span>{action.title || action.id}</span>
    </div>;
  }
  const color = TRACK_COLORS[action.role] || '#64748b';
  const fixed = action.fixedDuration ? ' · pevná délka' : '';
  return <div className={`trackClip ${action.kind || ''}`}>
    <WaveSvg peaks={action.peaks || []} color={color} renderWidth={action.renderWidth || 360} />
    <div className="trackClipBody">
      <strong>{action.title}</strong>
      <small>{fmt(action.start)} → {fmt(action.end)}{fixed}</small>
    </div>
  </div>;
}


function chooseTickStep(pxPerSecond) {
  const candidates = [1,2,5,10,15,30,60,120,300,600,900,1800,3600];
  return candidates.find(sec => sec * pxPerSecond >= 90) || 3600;
}

function snapTime(value, points, pxPerSecond) {
  const threshold = Math.max(0.5, 10 / Math.max(pxPerSecond, 0.001));
  let best = value;
  let bestDist = threshold;
  points.forEach(point => {
    const dist = Math.abs(point - value);
    if (dist <= bestDist) {
      best = point;
      bestDist = dist;
    }
  });
  return best;
}

function TimelineClip({action, pxPerSecond, duration, snapPoints, onUpdateAction, onSelect, setTime}) {
  const [local, setLocal] = useState(null);
  const display = local || action;
  const left = display.start * pxPerSecond;
  const width = Math.max(3, (display.end - display.start) * pxPerSecond);
  const canMove = action.movable !== false;
  const canTrim = action.flexible !== false && action.fixedDuration !== true;

  const startDrag = (event, mode) => {
    if (mode === 'move' && !canMove) return;
    if ((mode === 'trim-left' || mode === 'trim-right') && !canTrim) return;
    event.preventDefault();
    event.stopPropagation();
    const startX = event.clientX;
    const origin = {...action};
    const pointerId = event.pointerId;
    event.currentTarget.setPointerCapture(pointerId);
    onSelect(action);

    const move = (ev) => {
      const delta = (ev.clientX - startX) / Math.max(pxPerSecond, 0.001);
      let next = {...origin};
      if (mode === 'move') {
        const len = origin.end - origin.start;
        let ns = Math.max(0, Math.min(duration - len, origin.start + delta));
        ns = snapTime(ns, snapPoints, pxPerSecond);
        next.start = ns;
        next.end = ns + len;
      }
      if (mode === 'trim-left') {
        let ns = Math.max(0, Math.min(origin.end - 1, origin.start + delta));
        ns = snapTime(ns, snapPoints, pxPerSecond);
        next.start = Math.min(ns, origin.end - 1);
      }
      if (mode === 'trim-right') {
        let ne = Math.max(origin.start + 1, Math.min(duration, origin.end + delta));
        ne = snapTime(ne, snapPoints, pxPerSecond);
        next.end = Math.max(origin.start + 1, ne);
      }
      setLocal(next);
    };
    const up = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      setLocal(current => {
        if (current) onUpdateAction(action.id, current);
        return null;
      });
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  };

  if (action.effectId === 'suggestion') {
    const markerLeft = action.start * pxPerSecond;
    const markerWidth = Math.max(4, (action.end - action.start) * pxPerSecond);
    return <div
      className={`timelineSuggestion ${action.kind || ''}`}
      style={{left: markerLeft, width: markerWidth}}
      title={`${action.title || action.id} · ${fmt(action.start)}`}
      onClick={(e)=>{ e.stopPropagation(); setTime(action.start); onSelect(action); }}
    >{markerWidth > 42 ? action.title : ''}</div>;
  }

  const displayForRender = {...display, renderWidth: width};

  return <div
    className={`customClip ${action.kind || ''} ${canMove ? 'editable' : 'locked'} ${canTrim ? 'trimEnabled' : 'fixedLength'}`}
    style={{left, width}}
    onPointerDown={(e)=>startDrag(e, 'move')}
    onClick={(e)=>{ e.stopPropagation(); onSelect(display); setTime(display.start); }}
    title={`${action.title}: ${fmt(display.start)} → ${fmt(display.end)}${action.fixedDuration ? ' · pevná délka' : ''}`}
  >
    {canTrim && <div className="trimHandle left" onPointerDown={(e)=>startDrag(e, 'trim-left')} />}
    <TrackClip action={displayForRender} />
    {canTrim && <div className="trimHandle right" onPointerDown={(e)=>startDrag(e, 'trim-right')} />}
  </div>;
}


function StandardTimeline({rows, duration, pxPerSecond, setPxPerSecond, time, setTime, onRowsChange, onSelect, log}) {
  const scrollRef = useRef(null);
  const labelWidth = 230;
  const rowHeight = 86;
  const rulerHeight = 34;
  const totalWidth = Math.max(900, duration * pxPerSecond);
  const totalHeight = rulerHeight + rows.length * rowHeight;
  const tickStep = chooseTickStep(pxPerSecond);
  const tickCount = Math.ceil(duration / tickStep) + 1;

  const snapPoints = useMemo(() => {
    const points = new Set([0, time]);
    rows.forEach(row => row.actions.forEach(a => { points.add(Number(a.start || 0)); points.add(Number(a.end || 0)); }));
    return [...points].filter(Number.isFinite);
  }, [rows, time]);

  const updateAction = (rowId, actionId, nextAction) => {
    const nextRows = rows.map(row => row.id !== rowId ? row : {
      ...row,
      actions: row.actions.map(a => a.id === actionId ? {...a, ...nextAction} : a)
    });
    onRowsChange(nextRows);
  };

  const scrollToPlayhead = (nextPps) => {
    const el = scrollRef.current;
    if (!el) return;
    requestAnimationFrame(() => {
      const target = Math.max(0, time * nextPps - el.clientWidth / 2);
      el.scrollLeft = target;
    });
  };

  const setZoomAroundPlayhead = (nextPps) => {
    const safe = Math.max(0.04, Math.min(80, Number(nextPps) || pxPerSecond));
    setPxPerSecond(safe);
    scrollToPlayhead(safe);
  };

  const fitTimeline = () => {
    const viewport = scrollRef.current?.clientWidth || 1000;
    const nextPps = Math.max(0.04, viewport / Math.max(60, duration));
    setPxPerSecond(nextPps);
    requestAnimationFrame(() => { if (scrollRef.current) scrollRef.current.scrollLeft = 0; });
  };

  const zoomBy = (factor) => {
    setZoomAroundPlayhead(pxPerSecond * factor);
  };

  const gestureStateRef = useRef({scale:1});

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;

    const handleWheel = (event) => {
      // macOS trackpad pinch in Chrome/Edge arrives as ctrl/meta + wheel.
      // Here we intentionally use Jarek's requested direction:
      // Prohozené mapování podle testu na Macu: pinch-in a pinch-out jsou obráceně proti minulé verzi.
      if (!(event.ctrlKey || event.metaKey)) return;
      event.preventDefault();
      event.stopPropagation();

      const direction = event.deltaY > 0 ? 1 : -1;
      const strength = Math.min(0.45, Math.max(0.08, Math.abs(event.deltaY) / 420));
      const factor = direction > 0 ? (1 + strength) : (1 / (1 + strength));
      setZoomAroundPlayhead(pxPerSecond * factor);
    };

    const handleGestureStart = (event) => {
      // Safari sends gesture events for trackpad pinch.
      event.preventDefault();
      gestureStateRef.current.scale = Number(event.scale || 1);
    };

    const handleGestureChange = (event) => {
      event.preventDefault();
      const previous = gestureStateRef.current.scale || 1;
      const current = Number(event.scale || 1);
      if (!Number.isFinite(current) || current <= 0) return;

      // Safari scale > 1 means fingers move apart. Prohozeno proti minulé verzi.
      const relative = current / Math.max(0.001, previous);
      const factor = relative < 1 ? (1 / Math.max(0.12, relative)) : (1 / Math.min(8, relative));
      setZoomAroundPlayhead(pxPerSecond * factor);
      gestureStateRef.current.scale = current;
    };

    el.addEventListener('wheel', handleWheel, {passive:false});
    el.addEventListener('gesturestart', handleGestureStart, {passive:false});
    el.addEventListener('gesturechange', handleGestureChange, {passive:false});
    return () => {
      el.removeEventListener('wheel', handleWheel);
      el.removeEventListener('gesturestart', handleGestureStart);
      el.removeEventListener('gesturechange', handleGestureChange);
    };
  }, [pxPerSecond, time, duration]);

  const clickTimeline = (event) => {
    if (!scrollRef.current) return;
    const rect = scrollRef.current.getBoundingClientRect();
    const x = event.clientX - rect.left + scrollRef.current.scrollLeft;
    setTime(Math.max(0, Math.min(duration, x / Math.max(pxPerSecond, 0.001))));
  };

  const ticks = [];
  for (let i = 0; i < tickCount; i++) {
    const t = i * tickStep;
    ticks.push(<div key={t} className="timeTick" style={{left: t * pxPerSecond}}><span>{fmt(t)}</span></div>);
  }

  return <div className="standardTimelineShell">
    <div className="standardTimelineControls">
      <button className="ghostLite" onClick={fitTimeline}>Celé video</button>
      <button className="ghostLite" onClick={()=>zoomBy(1.8)}>Detail +</button>
      <button className="ghostLite" onClick={()=>zoomBy(1/1.8)}>Detail −</button>
      <label>Zoom <input type="range" min="0.04" max="12" step="0.01" value={pxPerSecond} onChange={e=>setZoomAroundPlayhead(Number(e.target.value))} /></label>
      <strong>{fmt(time)}</strong>
    </div>
    <div className="standardTimelineGrid" style={{gridTemplateColumns:`${labelWidth}px 1fr`}}>
      <div className="trackLabels" style={{paddingTop:rulerHeight}}>
        {rows.map(row => <div key={row.id} className="trackLabel" style={{height:rowHeight}}>
          <strong>{row.title}</strong><small>{row.subtitle}</small>
        </div>)}
      </div>
      <div className="timelineScroll" ref={scrollRef} onClick={clickTimeline}>
        <div className="timelineCanvas" style={{width:totalWidth, height:totalHeight}}>
          <div className="ruler" style={{height:rulerHeight}}>{ticks}</div>
          {ticks.map(tick => React.cloneElement(tick, {className:'gridTick'}))}
          <div className="playheadLine" style={{left: time * pxPerSecond}} />
          {rows.map((row, rowIndex) => <div key={row.id} className={`customTrack ${row.role || ''}`} style={{top:rulerHeight + rowIndex * rowHeight, height:rowHeight}}>
            {(row.actions || []).map(action => <TimelineClip
              key={action.id}
              action={action}
              pxPerSecond={pxPerSecond}
              duration={duration}
              snapPoints={snapPoints}
              setTime={setTime}
              onSelect={onSelect}
              onUpdateAction={(actionId, nextAction)=>updateAction(row.id, actionId, nextAction)}
            />)}
          </div>)}
        </div>
      </div>
    </div>
  </div>;
}

function TimelineEditor({project, setProject, log}) {
  const [rows, setRows] = useState(() => buildRows(project));
  const [time, setTime] = useState(0);
  const [pxPerSecond, setPxPerSecond] = useState(0.2);
  const [selected, setSelected] = useState(null);
  const [previewStatus, setPreviewStatus] = useState('Náhled čeká na tlačítko Zobrazit video v pozici.');
  const [waveforms, setWaveforms] = useState({});
  const [roleDurations, setRoleDurations] = useState({});
  const duration = useMemo(() => Math.max(60, roleDuration(project, 'speaker_video', roleDurations, durationOf(project))), [project, roleDurations]);

  useEffect(() => { setPxPerSecond(0.2); }, [project?.id]);

  useEffect(() => {
    if (!project?.id) return;
    const roles = ['speaker_video', 'replacement_video', 'gallery_video'];
    let alive = true;
    (async () => {
      const entries = await Promise.all(roles.map(async role => {
        if (!project?.files?.[role]) return [role, {peaks: [], duration: 0}];
        try {
          const detailWidth = role === 'replacement_video' ? 12000 : 100000;
          const data = await api(`/api/waveform_peaks?project=${encodeURIComponent(project.id)}&role=${encodeURIComponent(role)}&width=${detailWidth}`);
          return [role, {peaks: data.peaks || [], duration: Number(data.duration || 0)}];
        } catch {
          return [role, {peaks: [], duration: 0}];
        }
      }));
      if (!alive) return;
      setWaveforms(Object.fromEntries(entries.map(([role, value]) => [role, value.peaks || []])));
      setRoleDurations(Object.fromEntries(entries.map(([role, value]) => [role, value.duration || 0])));
    })();
    return () => { alive = false; };
  }, [project?.id, project?.analysis?.analyzed_at, project?.files?.speaker_video, project?.files?.gallery_video, project?.files?.replacement_video]);

  useEffect(() => { setRows(buildRows(project, waveforms, roleDurations)); }, [project?.id, project?.analysis?.analyzed_at, project?.cuts, waveforms, roleDurations]);

  const commitRows = (nextRows) => {
    setRows(nextRows);
    const cuts = rowsToCuts(nextRows, project.cuts);
    setProject({...project, cuts});
  };

  const setCut = (key, value) => {
    if (key === 'replacement_end') { log('Konec doprovodného videa se needituje; délka je pevně podle souboru.'); return; }
    const cuts = {...(project.cuts || {}), [key]: Number(value || 0)};
    if (key === 'discussion_start') cuts.gallery_start = Number(value || 0);
    setProject({...project, cuts});
    log(`Nastaven střihový bod ${key}: ${fmt(value)}`);
  };

  const applyPlayhead = (key) => {
    if (key === 'real_start') {
      const nextCuts = {...(project.cuts || {}), real_start: Number(time || 0)};
      const nextProject = {...project, cuts: nextCuts};
      setProject(nextProject);
      setRows(buildRows(nextProject, waveforms, roleDurations));
      log(`Vyznačen začátek po trimu hlavního videa i galerie: ${fmt(time)}.`);
      return;
    }
    setCut(key, time);
  };

  useTrimShortcuts({
    onMarkStart: () => applyPlayhead('real_start'),
    onMarkEnd: () => applyPlayhead('discussion_start'),
    onNudge: (delta) => setTime(t => Math.max(0, Math.min(duration, t + delta))),
    previewVideoId: 'previewVideo',
  });

  const showVideoAtPlayhead = async () => {
    const video = document.getElementById('previewVideo');
    if(!video){ alert('Video přehrávač nebyl nalezen.'); return; }
    const src = playbackSourceForPlayhead(project, time);
    const clip = replacementClipInfo(project);
    const url = `/api/source_video?project=${encodeURIComponent(project.id)}&role=${encodeURIComponent(src.role)}&_=${Date.now()}`;
    video.hidden = false;
    video.controls = true;
    video.muted = false;
    video.src = url;
    video.load();
    video.ontimeupdate = () => {
      const t = Number(video.currentTime || 0);
      const globalTime = src.role === 'replacement_video' ? clip.start + t : t;
      setTime(Math.max(0, Math.min(duration, globalTime)));
    };
    const jumpAndPlay = async () => {
      video.onloadedmetadata = null;
      video.oncanplay = null;
      try { video.currentTime = Math.max(0, src.time); } catch {}
      try { await video.play(); } catch {}
    };
    video.onloadedmetadata = jumpAndPlay;
    video.oncanplay = jumpAndPlay;
    setPreviewStatus(`${src.label}: přehrávám od pozice střihací hlavy ${fmt(time)} bez automatického ukončení.`);
    requestAnimationFrame(() => video.scrollIntoView({behavior:'smooth', block:'center'}));
    log(`Video v pozici: ${src.label}, přehrávám od ${fmt(time)}.`);
  };

  return <section className="card timelineCard">
    <div className="cardHead"><h2>3 · Střihačská osa</h2><span className="muted">Nejdřív nastav začátek po trimu, doprovodné video a přechod do galerie.</span></div>
    <div className="timelineToolbar">
      <button onClick={() => applyPlayhead('real_start')}>Playhead → vyznačit začátek po trimu (I)</button>
      <label className="cutTimeField">Začátek <TimeField value={Number(project.cuts?.real_start || 0)} onCommit={(v) => setCut('real_start', v)} title="Přesný čas začátku po trimu (HH:MM:SS)" /></label>
      <button onClick={() => applyPlayhead('discussion_start')}>Playhead → začátek přechodu do galerie (O)</button>
      <label className="cutTimeField">Galerie <TimeField value={Number(project.cuts?.discussion_start || 0)} onCommit={(v) => setCut('discussion_start', v)} title="Přesný čas přechodu do galerie (HH:MM:SS)" /></label>
    </div>
    <p className="muted shortcutHint">Klávesy: I = začátek po trimu, O = přechod do galerie, ←/→ = posun o 1 s (Shift = 5 s), mezerník = přehrát/pauza náhledu.</p>
    <div className="timelineLegend">
      <span><i className="dot replacement"></i>Doprovodné video</span>
      <span><i className="dot gallery"></i>Galerie</span>
      <span><i className="dot speaker"></i>Hlavní video</span>
      <span><i className="dot audio"></i>Externí audio</span>
    </div>
    <StandardTimeline rows={rows} duration={duration} pxPerSecond={pxPerSecond} setPxPerSecond={setPxPerSecond} time={time} setTime={setTime} onRowsChange={commitRows} onSelect={setSelected} log={log} />
    <div className="positionPreviewPanel underTimeline">
      <button className="primary" onClick={showVideoAtPlayhead}>Zobrazit video v pozici</button>
      <div className="previewStatus">{previewStatus}</div>
      <video id="previewVideo" className="previewVideo" controls hidden />
    </div>
  </section>;
}

function MarkerSuggestions({project, setCut, playBefore}) {
  const markers = project.analysis?.markers || [];
  if (!markers.length) return <p className="muted">Po analýze se tady objeví návrhy z transkriptu, tich a zvukových změn.</p>;
  return <div className="markers">
    <h3>Návrhy z analýzy</h3>
    {markers.slice(0,120).map((m,i) => {
      const sec = Number(m.start || 0);
      return <div className={`markerRow ${m.kind || ''}`} key={i}>
        <time>{fmt(sec)}</time>
        <div><strong>{m.label || m.kind}</strong><p>{m.text || ''}</p></div>
        <button onClick={()=>playBefore(sec)}>Video 5 s před</button>
        <button onClick={()=>setCut('real_start', sec)}>Začátek po trimu</button>
        <button onClick={()=>setCut('discussion_start', sec)}>Přechod do galerie</button>
      </div>;
    })}
  </div>;
}

function EditVideoStep({project, saveProject, prepareEditVideo, fullAuto, exportAudio, importAudio, optimizeVideo, deleteSilences, adjustAudio, setProject, busy, busyAction}) {
  const master = project?.files?.edit_master_video;
  const importedAudio = project?.files?.imported_audio;
  const silences = project?.analysis?.edit_silences || [];
  return <section className="card editStep">
    <div className="cardHead">
      <h2>4 · Upravit video</h2>
      <span className="muted">Spojí hlavní video, doprovodné video a galerii do jedné pracovní stopy podle jejich průniku v časové ose.</span>
    </div>
    <div className="editChecklist">
      <div><strong>1.</strong> Vytvořit jedno pracovní video z připravené střihačské osy.</div>
      <div><strong>2.</strong> Volitelně vyexportovat zvuk, upravit ho mimo aplikaci a importovat zpět.</div>
      <div><strong>3.</strong> Potom spustit optimalizaci: analýzu tich nad 3 sekundy a jejich vyznačení.</div>
    </div>
    <div className="actions">
      <button className="primary" onClick={prepareEditVideo} disabled={busy}>{busyAction === 'prepare' ? 'Zpracovávám…' : 'Upravit video'}</button>
      <button className="render" onClick={fullAuto} disabled={busy}>{busyAction === 'fullAuto' ? 'FULL AUTO běží…' : 'FULL AUTO'}</button>
    </div>
    {master && <SingleVideoEditor project={project} setProject={setProject} exportAudio={exportAudio} importAudio={importAudio} optimizeVideo={optimizeVideo} deleteSilences={deleteSilences} adjustAudio={adjustAudio} busy={busy} busyAction={busyAction} importedAudio={importedAudio} silences={silences} />}
  </section>;
}

function SingleVideoEditor({project, setProject, exportAudio, importAudio, optimizeVideo, deleteSilences, adjustAudio, busy, busyAction, importedAudio, silences}) {
  const duration = Number(project?.analysis?.edit_master?.duration || project?.analysis?.media?.edit_master_video?.duration || 0);
  const cuts = project?.cuts || {};
  const trimStart = Math.max(0, Number(cuts.edit_master_start || 0));
  const trimEnd = Math.max(trimStart + 1, Math.min(duration || trimStart + 1, Number(cuts.edit_master_end || duration || trimStart + 1)));
  const [editPeaks, setEditPeaks] = useState([]);
  const [importedPeaks, setImportedPeaks] = useState([]);
  const peaks = editPeaks;

  useEffect(() => {
    if (!project?.id || !project?.files?.edit_master_video) return;
    let alive = true;
    (async () => {
      try {
        const data = await api(`/api/waveform_peaks?project=${encodeURIComponent(project.id)}&role=edit_master_video&width=100000`);
        if (alive) setEditPeaks(data.peaks || []);
      } catch {
        if (alive) setEditPeaks([]);
      }
    })();
    return () => { alive = false; };
  }, [project?.id, project?.files?.edit_master_video]);

  useEffect(() => {
    if (!project?.id || !project?.files?.imported_audio) { setImportedPeaks([]); return; }
    let alive = true;
    (async () => {
      try {
        const data = await api(`/api/waveform_peaks?project=${encodeURIComponent(project.id)}&role=imported_audio&width=100000`);
        if (alive) setImportedPeaks(data.peaks || []);
      } catch {
        if (alive) setImportedPeaks([]);
      }
    })();
    return () => { alive = false; };
  }, [project?.id, project?.files?.imported_audio]);

  const [singleTime, setSingleTime] = useState(trimStart);
  const [singleZoom, setSingleZoom] = useState(0.25);
  const [singlePreviewStatus, setSinglePreviewStatus] = useState('Náhled čeká na tlačítko Zobrazit video v pozici.');
  const manualCuts = project?.analysis?.edit_manual_cuts || [];
  const [manualStart, setManualStart] = useState(null);
  const rows = useMemo(() => {
    const baseRows = [{
      id: 'edit_master',
      title: 'Spojené pracovní video',
      subtitle: 'jedna stopa; doladění trimu tažením levého/pravého okraje',
      actions: [{
        id: 'edit_master_clip',
        effectId: 'video_clip',
        kind: 'speaker',
        role: 'edit_master_video',
        title: 'Pracovní video',
        start: trimStart,
        end: trimEnd,
        movable: false,
        flexible: true,
        peaks
      }, ...(silences || []).map((m, i) => ({
        id: `edit_silence_${i}`,
        effectId: 'suggestion',
        kind: 'silence',
        title: 'Ticho k odstranění',
        start: Number(m.start || 0),
        end: Number(m.end || Number(m.start || 0) + 1),
        movable: false,
        flexible: false
      })), ...(manualCuts || []).map((m, i) => ({
        id: `manual_delete_${i}`,
        effectId: 'suggestion',
        kind: 'manual_delete',
        title: 'Ruční úsek k odstranění',
        start: Number(m.start || 0),
        end: Number(m.end || Number(m.start || 0) + 1),
        movable: false,
        flexible: false
      }))]
    }];
    if(importedAudio){
      baseRows.push({
        id: 'imported_audio_track',
        title: 'Importovaná zvuková stopa',
        subtitle: 'aktivní zelená audio stopa; bude stříhána společně s videem',
        role: 'external_audio',
        actions: [{
          id: 'imported_audio_clip',
          effectId: 'video_clip',
          kind: 'gallery',
          role: 'imported_audio',
          title: 'Importovaný zvuk',
          start: trimStart,
          end: trimEnd,
          movable: false,
          flexible: false,
          peaks: importedPeaks
        }]
      });
    }
    return baseRows;
  }, [trimStart, trimEnd, peaks, importedPeaks, importedAudio, silences, manualCuts]);

  const commitRows = (nextRows) => {
    const clip = nextRows?.[0]?.actions?.find(a => a.id === 'edit_master_clip');
    if (!clip) return;
    const nextCuts = {
      ...(project.cuts || {}),
      edit_master_start: Number(clip.start || 0),
      edit_master_end: Number(clip.end || duration || 0)
    };
    setProject({...project, cuts: nextCuts});
  };

  const showSingleVideoAtPlayhead = async () => {
    const video = document.getElementById('editPreviewVideo');
    if(!video){ alert('Video přehrávač nebyl nalezen.'); return; }
    video.hidden = false;
    video.controls = true;
    video.muted = false;
    video.src = `/api/source_video?project=${encodeURIComponent(project.id)}&role=edit_master_video&_=${Date.now()}`;
    video.load();
    video.ontimeupdate = () => {
      setSingleTime(Math.max(0, Math.min(duration || 0, Number(video.currentTime || 0))));
    };
    const jumpAndPlay = async () => {
      video.onloadedmetadata = null;
      video.oncanplay = null;
      try { video.currentTime = Math.max(0, singleTime); } catch {}
      try { await video.play(); } catch {}
    };
    video.onloadedmetadata = jumpAndPlay;
    video.oncanplay = jumpAndPlay;
    setSinglePreviewStatus(`Pracovní video: přehrávám od pozice střihací hlavy ${fmt(singleTime)} bez automatického ukončení.`);
    requestAnimationFrame(() => video.scrollIntoView({behavior:'smooth', block:'center'}));
  };

  const markManualStart = () => setManualStart(Number(singleTime || 0));
  const markManualEnd = () => {
    if(manualStart === null){ alert('Nejdřív označ začátek úseku.'); return; }
    const a = Math.max(0, Math.min(Number(manualStart), Number(singleTime || 0)));
    const b = Math.min(duration || 0, Math.max(Number(manualStart), Number(singleTime || 0)));
    if(b <= a + 0.25){ alert('Vybraný úsek je příliš krátký.'); return; }
    const nextManual = [...manualCuts, {kind:'manual_delete', label:'Ruční úsek k odstranění', start:a, end:b, text:`Ruční úsek: ${fmt(a)}–${fmt(b)}`}];
    setProject({...project, analysis:{...(project.analysis || {}), edit_manual_cuts: nextManual}});
    setManualStart(null);
  };

  const clearManualCuts = () => {
    setProject({...project, analysis:{...(project.analysis || {}), edit_manual_cuts: []}});
    setManualStart(null);
  };

  const setTrimBound = (key, value) => {
    const nextCuts = {...(project.cuts || {}), [key]: Number(value || 0)};
    setProject({...project, cuts: nextCuts});
  };

  useTrimShortcuts({
    onMarkStart: markManualStart,
    onMarkEnd: markManualEnd,
    onNudge: (delta) => setSingleTime(t => Math.max(0, Math.min(duration || 0, t + delta))),
    previewVideoId: 'editPreviewVideo',
  });

  return <div className="singleEditor">
    <h3>Nová střihačská osa · jediné pracovní video</h3>
    <p className="muted">Tažením levého nebo pravého okraje klipu doladíš trim celého spojeného videa. Tyto hodnoty se použijí v sekci 6 při vytvoření finálního videa.</p>
    <StandardTimeline rows={rows} duration={duration || 60} pxPerSecond={singleZoom} setPxPerSecond={setSingleZoom} time={singleTime} setTime={setSingleTime} onRowsChange={commitRows} onSelect={()=>{}} log={()=>{}} />
    <div className="positionPreviewPanel underTimeline">
      <button className="primary" onClick={showSingleVideoAtPlayhead}>Zobrazit video v pozici</button>
      <div className="previewStatus">{singlePreviewStatus}</div>
      <video id="editPreviewVideo" className="previewVideo" controls hidden />
    </div>
    <p className="muted shortcutHint">Klávesy: I = začátek úseku, O = konec úseku, ←/→ = posun o 1 s (Shift = 5 s), mezerník = přehrát/pauza náhledu.</p>
    <div className="manualCutPanel">
      <strong>Ruční úseky k odstranění</strong>
      <button onClick={markManualStart}>Začátek úseku zde (I)</button>
      <button onClick={markManualEnd}>Konec úseku zde (O)</button>
      <button onClick={clearManualCuts} disabled={!manualCuts.length}>Vymazat ruční značky</button>
      <span className="muted">{manualStart !== null ? `Začátek označen: ${fmt(manualStart)}. Posuň hlavu na konec a stiskni Konec úseku zde.` : `${manualCuts.length} ručních úseků bude smazáno spolu s tichy.`}</span>
    </div>
    <div className="singleTrimInfo">
      Trim pracovní stopy:
      <TimeField value={trimStart} onCommit={(v) => setTrimBound('edit_master_start', Math.max(0, Math.min(v, trimEnd - 1)))} title="Přesný začátek trimu (HH:MM:SS)" />
      →
      <TimeField value={trimEnd} onCommit={(v) => setTrimBound('edit_master_end', Math.max(trimStart + 1, Math.min(v, duration || v)))} title="Přesný konec trimu (HH:MM:SS)" />
    </div>
    <div className="actions audioActions">
      <button className="primary" onClick={optimizeVideo} disabled={busy}>{busyAction === 'optimize' ? 'Optimalizuji…' : 'Optimalizuj video'}</button>
      <button className="danger" onClick={deleteSilences} disabled={busy || !(silences.length || manualCuts.length)}>{busyAction === 'deleteSilences' ? 'Mažu značky…' : 'Vymazat označená ticha'}</button>
      <button onClick={adjustAudio} disabled={busy}>{busyAction === 'adjustAudio' ? 'Upravuji zvuk…' : '🎚️ Upravit zvukovou stopu'}</button>
      <button onClick={exportAudio} disabled={busy}>Export zvukové stopy</button>
      <button onClick={importAudio} disabled={busy}>{busyAction === 'importAudio' ? 'Importuji…' : 'Import zvukové stopy'}</button>
    </div>
    {importedAudio && <p className="muted">Importovaný zvuk je aktivní. Původní zvuk pracovní stopy je při náhledu/renderu nahrazen importovanou stopou.</p>}
  </div>;
}

function FinalRenderSection({project, renderProject, busy, busyAction}) {
  const hasMaster = Boolean(project?.files?.edit_master_video);
  const cuts = project?.cuts || {};
  return <section className="card finalRenderStep">
    <div className="cardHead">
      <h2>6 · Rendruj finální video</h2>
      <span className="muted">Finální render se spouští až po vytvoření a případném doladění jedné pracovní stopy. Výstup bude ve Full HD.</span>
    </div>
    {hasMaster ? <p className="muted">Použije se spojené pracovní video v rozsahu {fmt(Number(cuts.edit_master_start || 0))} → {fmt(Number(cuts.edit_master_end || project?.analysis?.edit_master?.duration || 0))}.</p> : <p className="muted">Nejdřív v sekci 4 stiskni „Upravit video“ a vytvoř jednu pracovní stopu.</p>}
    <div className="actions">
      <button className="render" onClick={renderProject} disabled={busy || !hasMaster}>{busyAction === 'render' ? 'Rendruji…' : 'Rendruj finální video'}</button>
    </div>
  </section>;
}

function SocialClipSection({project, generateSocialClip, busy, busyAction}) {
  const canGenerate = Boolean(project?.files?.speaker_video);
  const hasGallery = Boolean(project?.files?.gallery_video);
  const info = project?.analysis?.social_clip;
  const clipFile = project?.files?.social_clip_video;
  const clipUrl = clipFile ? `/api/source_video?project=${encodeURIComponent(project.id)}&role=social_clip_video&_=${encodeURIComponent(info?.created_at || clipFile)}` : null;
  return <section className="card socialClipStep">
    <div className="cardHead">
      <h2>7 · Vzorek pro sociální sítě</h2>
      <span className="muted">Svislý (9:16) sestřih cca na minutu pro Facebook/LinkedIn: titulní obrazovka s dílem, jménem řečníka a číslem ArboChatu, záběr na řečníka, ukázky prezentace a záběry galerie s odbornými přechody.</span>
    </div>
    {canGenerate
      ? <p className="muted">Použije hlavní video{hasGallery ? ' a galerii' : ''} podle aktuálních střihových bodů (začátek po trimu{hasGallery ? ', přechod do galerie' : ''}).</p>
      : <p className="muted">Nejdřív v sekci 1 načti hlavní video.</p>}
    <div className="actions">
      <button className="render" onClick={generateSocialClip} disabled={busy || !canGenerate}>{busyAction === 'socialClip' ? 'Generuji vzorek…' : 'Vygenerovat vzorek pro sociální sítě'}</button>
    </div>
    {clipUrl && <div className="socialClipPreview">
      <video className="socialClipVideo" src={clipUrl} controls />
      {info && <p className="muted">Délka {fmt(info.duration)} · {info.clips} záběrů{info.has_gallery ? ' (včetně galerie)' : ''}</p>}
    </div>}
  </section>;
}

function SilenceAnalysisSection({project}) {
  const silences = project?.analysis?.edit_silences || [];
  const oldMarkers = (project?.analysis?.markers || []).filter(m => ['silence','final_silence','edit_silence'].includes(m.kind));
  const list = silences.length ? silences : oldMarkers;
  return <section className="card markers silenceAnalysis">
    <h2>5 · Analýza tichých úseků</h2>
    {!list.length && <p className="muted">Analýza tich se spustí až tlačítkem „Optimalizuj video“ pod jedním pracovním videem.</p>}
    {list.slice(0,160).map((m,i) => <div className={`markerRow ${m.kind || 'silence'}`} key={i}>
      <time>{fmt(Number(m.start || 0))}</time>
      <div><strong>{m.label || 'Ticho > 3 s'}</strong><p>{m.text || `${fmt(m.start)}–${fmt(m.end)}`}</p></div>
    </div>)}
  </section>;
}

function LogPanel({lines}) {
  return <section className="card log"><h2>Log</h2>{lines.map((l,i)=><div key={i}>{l}</div>)}</section>;
}

function ProcessOverlay({label}) {
  if(!label) return null;
  return <div className="processOverlay"><div className="processBox"><div className="spinner"></div><strong>{label}</strong><p>Probíhá dlouhá operace. Nezavírej toto okno.</p></div></div>;
}

function App() {
  const [status, setStatus] = useState(null);
  const [projects, setProjects] = useState([]);
  const [project, _setProject] = useState(null);
  const projectRef = useRef(null);
  const setProject = useCallback((value) => {
    const next = (typeof value === 'function') ? value(projectRef.current) : value;
    projectRef.current = next;
    _setProject(next);
  }, []);
  useEffect(() => { projectRef.current = project; }, [project]);
  const [busy, setBusy] = useState(false);
  const [busyLabel, setBusyLabel] = useState('');
  const [busyAction, setBusyAction] = useState('');
  const [lines, log] = useLog();

  const loadProjects = async () => {
    const data = await api('/api/projects');
    setProjects(data.projects || []);
  };
  const loadProject = async (id) => {
    const data = await api(`/api/project?id=${encodeURIComponent(id)}`);
    setProject(data);
  };
  useEffect(() => {
    api('/api/status').then(setStatus).catch(e=>log('Status selhal: '+e.message));
    loadProjects().catch(e=>log('Načtení projektů selhalo: '+e.message));
  }, []);

  const createProject = async (name) => {
    try {
      const data = await api('/api/create_project', {method:'POST', body:JSON.stringify({name})});
      const created = data.project || data;
      if(!created || !created.id) throw new Error('Backend nevrátil platný projekt.');
      setProject(created); await loadProjects(); log('Projekt vytvořen.');
    } catch(e) {
      alert('Nepodařilo se vytvořit projekt:\n' + e.message);
      log('Vytvoření projektu selhalo: ' + e.message);
    }
  };
  const saveProject = async (snapshot = null) => {
    const current = snapshot || projectRef.current || project;
    if(!current?.id) throw new Error('Není vybraný projekt k uložení.');
    const data = await api('/api/save_project', {method:'POST', body:JSON.stringify(current)});
    setProject(data.project);
    await loadProjects();
    log('Projekt uložen.');
    return data.project;
  };
  const analyzeProject = async () => {
    setBusy(true); setBusyAction('analyze'); setBusyLabel('Analyzuji video…');
    await waitForPaint();
    try {
      await saveProject();
      const data = await api('/api/analyze', {method:'POST', body:JSON.stringify({project:project.id})});
      setProject(data.project); await loadProjects(); log('Analýza dokončena.');
    } catch(e) { alert(e.message); log('Analýza selhala: '+e.message); }
    finally { setBusy(false); setBusyAction(''); setBusyLabel(''); }
  };
  const previewIntroScreen = async () => {
    setBusy(true); setBusyAction('intro'); setBusyLabel('Generuji vstupní obrazovku…');
    await waitForPaint();
    try {
      const saved = await saveProject();
      await uploadIntroOverlay(saved);
      const data = await api(`/api/generate_intro_screen?project=${encodeURIComponent(saved.id)}`);
      setProject(data.project); await loadProjects();
      log('Vstupní obrazovka vygenerována z metadat.');
    } catch(e) { alert(e.message); log('Vstupní obrazovka selhala: '+e.message); }
    finally { setBusy(false); setBusyAction(''); setBusyLabel(''); }
  };

  const prepareEditVideo = async () => {
    setBusy(true); setBusyAction('prepare'); setBusyLabel('Upravuji video a vytvářím pracovní stopu…');
    await waitForPaint();
    try {
      const saved = await saveProject();
      await uploadIntroOverlay(saved);
      const data = await api('/api/prepare_edit_video', {method:'POST', body:JSON.stringify({project:saved.id})});
      setProject(data.project); await loadProjects();
      log('Pracovní video vytvořeno: jedna stopa pro další střih a zvuk.');
    } catch(e) { alert(e.message); log('Upravit video selhalo: '+e.message); }
    finally { setBusy(false); setBusyAction(''); setBusyLabel(''); }
  };

  const exportAudio = () => {
    if(!project?.id) return;
    window.location.href = `/api/export_edit_audio?project=${encodeURIComponent(project.id)}&_=${Date.now()}`;
  };

  const importAudio = async () => {
    setBusy(true); setBusyAction('importAudio'); setBusyLabel('Importuji zvukovou stopu…');
    await waitForPaint();
    try {
      const data = await api(`/api/choose_edit_audio?project=${encodeURIComponent(project.id)}`);
      setProject(data.project); await loadProjects();
      log('Importovaný zvuk je aktivní.');
    } catch(e) { alert(e.message); log('Import zvuku selhal: '+e.message); }
    finally { setBusy(false); setBusyAction(''); setBusyLabel(''); }
  };

  const adjustAudio = async () => {
    setBusy(true); setBusyAction('adjustAudio'); setBusyLabel('Komprimuji a vyrovnávám zvukovou stopu…');
    await waitForPaint();
    try {
      await saveProject();
      const data = await api('/api/adjust_edit_audio', {method:'POST', body:JSON.stringify({project:project.id})});
      setProject(data.project); await loadProjects();
      log('Zvuková stopa byla komprimována a vyrovnána.');
    } catch(e) { alert(e.message); log('Úprava zvuku selhala: '+e.message); }
    finally { setBusy(false); setBusyAction(''); setBusyLabel(''); }
  };

  const optimizeVideo = async () => {
    setBusy(true); setBusyAction('optimize'); setBusyLabel('Vyhledávám tiché úseky…');
    await waitForPaint();
    try {
      const data = await api('/api/optimize_edit_video', {method:'POST', body:JSON.stringify({project:project.id})});
      setProject(data.project); await loadProjects();
      log('Tichá místa nad 3 s byla vyznačena.');
    } catch(e) { alert(e.message); log('Optimalizace selhala: '+e.message); }
    finally { setBusy(false); setBusyAction(''); setBusyLabel(''); }
  };

  const deleteSilences = async () => {
    if(!confirm('Vymazat všechna označená ticha z pracovního videa?')) return;
    setBusy(true); setBusyAction('deleteSilences'); setBusyLabel('Odstraňuji označená ticha…');
    await waitForPaint();
    try {
      await saveProject();
      const data = await api('/api/delete_marked_silences', {method:'POST', body:JSON.stringify({project:project.id})});
      setProject(data.project); await loadProjects();
      log('Označená ticha byla odstraněna z pracovní stopy.');
    } catch(e) { alert(e.message); log('Mazání tich selhalo: '+e.message); }
    finally { setBusy(false); setBusyAction(''); setBusyLabel(''); }
  };

  const fullAuto = async () => {
    const current = projectRef.current || project;
    if(!current?.id) return;
    if(!confirm('Spustit FULL AUTO: Upravit video → Optimalizovat → Vymazat značky → Upravit zvuk → Rendrovat finální video?')) return;
    setBusy(true); setBusyAction('fullAuto'); setBusyLabel('FULL AUTO: ukládám aktuální střihové body…');
    await waitForPaint();
    try {
      const saved = await saveProject(current);
      const pid = saved.id;
      await uploadIntroOverlay(saved);
      setBusyLabel('FULL AUTO: vytvářím pracovní video…'); await waitForPaint();
      let data = await api('/api/prepare_edit_video', {method:'POST', body:JSON.stringify({project:pid, mode:'fullAuto'})});
      setProject(data.project); await loadProjects();
      const manifest = data.project?.analysis?.edit_master?.segment_manifest || [];
      const labels = manifest.map(x => x.label).join(', ');
      log('FULL AUTO segmenty po složení: ' + labels);
      setBusyLabel('FULL AUTO: vyhledávám tiché úseky…'); await waitForPaint();
      data = await api('/api/optimize_edit_video', {method:'POST', body:JSON.stringify({project:pid})});
      setProject(data.project); await loadProjects();
      setBusyLabel('FULL AUTO: odstraňuji označené úseky…'); await waitForPaint();
      data = await api('/api/delete_marked_silences', {method:'POST', body:JSON.stringify({project:pid})});
      setProject(data.project); await loadProjects();
      setBusyLabel('FULL AUTO: komprimuji a vyrovnávám zvuk…'); await waitForPaint();
      data = await api('/api/adjust_edit_audio', {method:'POST', body:JSON.stringify({project:pid})});
      setProject(data.project); await loadProjects();
      setBusyLabel('FULL AUTO: rendruji finální video…'); await waitForPaint();
      const render = await api('/api/render', {method:'POST', body:JSON.stringify({project:pid})});
      log('FULL AUTO hotovo: '+render.output);
      alert('FULL AUTO hotovo:\n' + render.output);
    } catch(e) { alert(e.message); log('FULL AUTO selhalo: '+e.message); }
    finally { setBusy(false); setBusyAction(''); setBusyLabel(''); }
  };

  const renderProject = async () => {
    setBusy(true); setBusyAction('render'); setBusyLabel('Rendruji finální video ve Full HD…');
    await waitForPaint();
    try {
      await saveProject();
      const data = await api('/api/render', {method:'POST', body:JSON.stringify({project:project.id})});
      log('Render hotový: '+data.output);
      alert('Render hotový:\n' + data.output);
    } catch(e) { alert(e.message); log('Render selhal: '+e.message); }
    finally { setBusy(false); setBusyAction(''); setBusyLabel(''); }
  };

  const generateSocialClip = async () => {
    setBusy(true); setBusyAction('socialClip'); setBusyLabel('Generuji vzorek pro sociální sítě…');
    await waitForPaint();
    try {
      await saveProject();
      const data = await api('/api/generate_social_clip', {method:'POST', body:JSON.stringify({project:project.id})});
      setProject(data.project);
      log('Vzorek pro sociální sítě hotový.');
    } catch(e) { alert(e.message); log('Vzorek pro sociální sítě selhal: '+e.message); }
    finally { setBusy(false); setBusyAction(''); setBusyLabel(''); }
  };

  return <div className="app">
    <ProjectSidebar projects={projects} currentId={project?.id} onCreate={createProject} onSelect={loadProject} onRefresh={loadProjects} />
    <main>
      <StatusBar status={status} />
      {!project && <section className="empty"><h2>Vyber nebo vytvoř projekt</h2><p>Pak načti složku se Zoom soubory a spusť analýzu.</p></section>}
      {project && <>
        <SetupPanel project={project} onUpdate={setProject} log={log} status={status} setStatus={setStatus} />
        <MetadataPanel project={project} setProject={setProject} saveProject={saveProject} analyzeProject={analyzeProject} previewIntroScreen={previewIntroScreen} busy={busy} />
        {projectHasAnyVideo(project) ? (
          <AppErrorBoundary>
            <TimelineEditor project={project} setProject={setProject} log={log} />
          </AppErrorBoundary>
        ) : (
          <section className="card emptyTimeline"><h2>3 · Střihačská osa</h2><p className="muted">Nejdřív vyber složku se Zoom soubory. Časová osa se zobrazí až po načtení videí.</p></section>
        )}
        <EditVideoStep project={project} saveProject={saveProject} prepareEditVideo={prepareEditVideo} fullAuto={fullAuto} exportAudio={exportAudio} importAudio={importAudio} optimizeVideo={optimizeVideo} deleteSilences={deleteSilences} adjustAudio={adjustAudio} setProject={setProject} busy={busy} busyAction={busyAction} />
        <SilenceAnalysisSection project={project} />
        <FinalRenderSection project={project} renderProject={renderProject} busy={busy} busyAction={busyAction} />
        <SocialClipSection project={project} generateSocialClip={generateSocialClip} busy={busy} busyAction={busyAction} />
        <LogPanel lines={lines} />
      </>}
    </main>
    <ProcessOverlay label={busyLabel} />
  </div>;
}

createRoot(document.getElementById('root')).render(<App />);
